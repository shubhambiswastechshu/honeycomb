"""Google BigQuery connector (BigQuery REST API v2 + INFORMATION_SCHEMA, google_oauth).

Thirty-two read-only tools over the project -> dataset -> table hierarchy,
BigQuery ML models and routines, job history, schema discovery, storage and
cost accounting, and column-level data profiling.

  Hierarchy (REST, free):
    list_projects, list_datasets, get_dataset, list_tables, get_table,
    preview_table, list_models, get_model, list_routines, get_routine

  Jobs (REST, free):
    list_jobs, get_job, get_query_results

  Query:
    dry_run_query, run_query

  Schema discovery (INFORMATION_SCHEMA):
    search_tables, search_columns, table_columns, list_views, get_view_sql,
    table_constraints, table_options, list_partitions

  Storage and cost (INFORMATION_SCHEMA):
    dataset_storage, query_history, cost_by_day, top_costly_queries,
    cost_by_user

  Profiling (billed, capped):
    count_rows, column_stats, top_values, sample_rows, time_series,
    distinct_count

Three things shape this module, and all three are about not doing damage.

**BigQuery bills by bytes scanned.** A careless `SELECT *` against a wide
partitioned table is a real invoice, and the caller here is an AI client that
cannot be assumed to know that. Every query carries a ``maximumBytesBilled``
ceiling; BigQuery refuses the job outright rather than running it when the
estimate exceeds the cap. `dry_run_query` gets the estimate for free first, and
`preview_table` reads off storage so "let me look at this table" never becomes
a billed query at all.

**The scope is broader than the tools.** Google's ``bigquery.readonly`` scope
cannot create a query job, so anything that runs SQL needs the full
``bigquery`` scope. Nothing here is registered with ``write: True`` and the
registry derives an empty ``write_tools`` -- but the token itself can write, so
`run_query` refuses any statement that is not a SELECT or a WITH rather than
relying on the scope to stop it. See _require_readonly_sql.

**Most tools build SQL from names the caller supplies**, and a write-capable
token makes injection a data-loss bug rather than a disclosure one. Two rules,
applied without exception:

  * An IDENTIFIER (project, dataset, table, column, region) goes through
    _ident / _table_ref, which validate against a strict character class and
    then backtick-quote. SQL has no way to parameterise an identifier, so
    validation is the only defence and it is deliberately stricter than
    BigQuery itself.
  * A VALUE (a search pattern, a limit, a date) is NEVER interpolated. It
    travels as a named query parameter, which cannot change the shape of the
    statement whatever it contains.
"""
import re

from django.conf import settings

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import (
    UpstreamUnavailable,
    get as http_get,
    post as http_post,
)

SLUG = "bigquery"
BQ = "https://bigquery.googleapis.com/bigquery/v2"

# 1 GiB scanned is roughly a US cent at list price -- large enough for real
# analytical questions over a tidy table, small enough that a mistake is not an
# invoice. The ceiling is what a caller may raise it to when they know better.
MAX_BYTES_DEFAULT = 1024 ** 3
MAX_BYTES_CEILING = 100 * 1024 ** 3

# INFORMATION_SCHEMA views are metadata, not table data: they scan almost
# nothing, so they get their own small cap rather than the caller's.
META_BYTES_CAP = 512 * 1024 ** 2

# BigQuery's own wait, not ours: the job keeps running server-side past this and
# `jobComplete: false` comes back, which _sql surfaces rather than hiding.
QUERY_TIMEOUT_MS = 30000


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _oauth_conf() -> tuple[str, str, str]:
    """Google OAuth client config, read defensively.

    Honeycomb may be deployed without the Google client configured; a missing
    setting must surface as a ConnectorError the user can act on, not as an
    AttributeError deep inside a tool call.
    """
    token_uri = getattr(settings, 'GOOGLE_OAUTH_TOKEN_URI', 'https://oauth2.googleapis.com/token')
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    client_secret = getattr(settings, 'GOOGLE_CLIENT_SECRET', '')
    if not client_id or not client_secret:
        raise ConnectorError('Google OAuth is not configured on this server.')
    return token_uri, client_id, client_secret


async def _access_token(conn: Connection, db) -> str:
    creds = conn.creds()
    rt = creds.get("refresh_token")
    if not rt:
        raise ConnectorError("Not connected: missing refresh token.")
    token_uri, client_id, client_secret = _oauth_conf()
    try:
        res = await http_post(
            token_uri,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": rt,
                "grant_type": "refresh_token",
            },
        )
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code != 200:
        raise ConnectorError(f"token refresh failed {res.status_code}: {res.text[:300]}")
    return res.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _fail(res) -> None:
    """Turn a non-2xx into a ConnectorError carrying BigQuery's own reason.

    BigQuery puts the useful part in error.message ("Query exceeded limit for
    bytes billed", "Not found: Dataset x:y"); the raw body is JSON noise around
    it. Truncated because it is echoed to the AI client and stored in the
    activity log.
    """
    try:
        payload = res.json().get("error") or {}
    except ValueError:
        payload = {}
    message = payload.get("message") or res.text[:300]
    raise ConnectorError(f"BigQuery {res.status_code}: {message[:300]}")


async def _get(conn: Connection, db, path: str, params: dict | None = None) -> dict:
    token = await _access_token(conn, db)
    async with limit_for(BQ):
        try:
            res = await http_get(BQ + path, headers=_bearer(token), params=params or {})
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        _fail(res)
    return res.json()


async def _post(conn: Connection, db, path: str, body: dict) -> dict:
    token = await _access_token(conn, db)
    async with limit_for(BQ):
        try:
            res = await http_post(BQ + path, headers=_bearer(token), json=body)
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        _fail(res)
    return res.json()


# --------------------------------------------------------------------------- #
# Identifier safety
#
# SQL cannot parameterise an identifier, so every name that reaches a generated
# statement is validated against a strict character class and then backticked.
# Stricter than BigQuery allows on purpose: a table whose name needs characters
# outside this set is reachable through run_query, and widening the class here
# to accommodate it would widen it for everything.
# --------------------------------------------------------------------------- #
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
# Project ids allow hyphens and may be "org:project" for older domain-scoped ones.
_PROJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{2,63}$")
# Multi-regions ('us', 'eu') and regions ('us-central1', 'europe-west4',
# 'northamerica-northeast1', 'me-west1'). The leading part is NOT two letters:
# that was the first cut and it rejected every European and Asian region.
_REGION_RE = re.compile(r"^[a-z]{2,20}(-[a-z]+[0-9]{1,2})?$")


def _ident(value: str, what: str) -> str:
    name = str(value or "").strip()
    if not _IDENT_RE.match(name):
        raise ConnectorError(
            f"{what} must be a plain BigQuery identifier -- letters, digits and "
            f"underscores, not starting with a digit. Got {name[:60]!r}."
        )
    return f"`{name}`"


def _project_ident(value: str) -> str:
    name = str(value or "").strip()
    if not _PROJECT_RE.match(name):
        raise ConnectorError(f"project_id is not a valid GCP project id: {name[:60]!r}.")
    return f"`{name}`"


def _region_suffix(value: str) -> str:
    """`region-us` etc. The region-qualified INFORMATION_SCHEMA views need it."""
    name = str(value or "us").strip().lower()
    if not _REGION_RE.match(name):
        raise ConnectorError(
            f"region must look like 'us', 'eu' or 'europe-west4'. Got {name[:40]!r}."
        )
    return f"`region-{name}`"


def _table_ref(project: str, dataset: str, table: str) -> str:
    """A fully-qualified, quoted table reference: `proj`.`ds`.`tbl`."""
    return f"{_project_ident(project)}.{_ident(dataset, 'dataset_id')}.{_ident(table, 'table_id')}"


def _schema_ref(project: str, dataset: str, view: str) -> str:
    """A dataset-scoped INFORMATION_SCHEMA view reference."""
    return (
        f"{_project_ident(project)}.{_ident(dataset, 'dataset_id')}"
        f".INFORMATION_SCHEMA.{view}"
    )


def _region_schema_ref(project: str, region: str, view: str) -> str:
    """A region-scoped INFORMATION_SCHEMA view reference (JOBS, TABLE_STORAGE)."""
    return f"{_project_ident(project)}.{_region_suffix(region)}.INFORMATION_SCHEMA.{view}"


# --------------------------------------------------------------------------- #
# Argument helpers
# --------------------------------------------------------------------------- #
def _project(conn: Connection, args: dict) -> str:
    """Caller's project, else the one saved at setup. Never guessed.

    Unlike GA4's property, a project is not safely inferable: an account often
    has many, and picking the first would silently bill the wrong one.
    """
    pid = str((args or {}).get("project_id") or "").strip()
    if pid:
        return pid
    saved = str(conn.creds().get("project_id") or "").strip()
    if saved:
        return saved
    raise ConnectorError(
        "project_id is required. Call list_projects to find one, or save a "
        "default project in this connection's settings."
    )


def _require(args: dict, key: str, example: str) -> str:
    value = str((args or {}).get(key) or "").strip()
    if not value:
        raise ConnectorError(f"{key} is required (e.g. '{example}').")
    return value


def _limit(args: dict, default: int = 50, max_value: int = 1000) -> int:
    try:
        return max(1, min(int((args or {}).get("limit", default)), max_value))
    except (TypeError, ValueError):
        return default


def _days(args: dict, default: int = 7, max_value: int = 180) -> int:
    try:
        return max(1, min(int((args or {}).get("days", default)), max_value))
    except (TypeError, ValueError):
        return default


def _max_bytes(args: dict) -> int:
    try:
        asked = int((args or {}).get("max_bytes_billed", MAX_BYTES_DEFAULT))
    except (TypeError, ValueError):
        return MAX_BYTES_DEFAULT
    return max(1, min(asked, MAX_BYTES_CEILING))


def _region(args: dict) -> str:
    return str((args or {}).get("region") or "us").strip().lower()


# Comments are stripped before the check so that `-- SELECT` or a /* */ block
# cannot be used to push the real verb out of view.
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _require_readonly_sql(sql: str) -> str:
    """Allow a single SELECT (or WITH ... SELECT) and nothing else.

    This is the real boundary, not the OAuth scope: the token can write, so a
    DML or DDL statement would otherwise succeed. Deliberately strict --
    multiple statements are refused outright rather than parsed, because
    telling "SELECT 1; DROP TABLE t" from a semicolon inside a string literal
    needs a real SQL parser, and a half-parser here would be a false sense of
    safety.
    """
    text = (sql or "").strip()
    if not text:
        raise ConnectorError("sql is required.")
    bare = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", text)).strip()
    if bare.endswith(";"):
        bare = bare[:-1].strip()
    if ";" in bare:
        raise ConnectorError(
            "Only one statement per call. Remove the ';' and send a single SELECT."
        )
    head = bare.split(None, 1)[0].upper() if bare else ""
    if head not in ("SELECT", "WITH"):
        raise ConnectorError(
            f"Only read-only SELECT or WITH queries are allowed here; got '{head or '?'}'. "
            "This connector never writes to BigQuery."
        )
    return text


# --------------------------------------------------------------------------- #
# Shaping
# --------------------------------------------------------------------------- #
def _cell(value):
    """Unwrap one tabledata/query cell.

    BigQuery wraps every scalar as {"v": ...}; a REPEATED field is a list of
    those, and a RECORD is {"f": [...]}. Unwrapped recursively so the caller
    gets ordinary JSON instead of BigQuery's envelope.
    """
    if isinstance(value, dict):
        if "f" in value:
            return [_cell(item) for item in value["f"]]
        if "v" in value:
            return _cell(value["v"])
        return value
    if isinstance(value, list):
        return [_cell(item) for item in value]
    return value


def _rows_to_dicts(schema: dict, rows: list) -> list[dict]:
    """Zip BigQuery's positional rows against the schema into named dicts.

    The wire format is positional -- {"f": [{"v": "3"}, {"v": "x"}]} -- which is
    unreadable on its own and useless to an AI client that has to guess which
    column is which. Names come from the same response, so this cannot drift.
    """
    names = [f.get("name", "") for f in (schema or {}).get("fields", []) or []]
    out = []
    for row in rows or []:
        cells = [_cell(c) for c in (row or {}).get("f", []) or []]
        out.append({
            names[i] if i < len(names) else f"col_{i}": cells[i]
            for i in range(len(cells))
        })
    return out


def _int(value) -> int | None:
    """BigQuery returns counts and byte sizes as strings; callers want numbers."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _schema_fields(fields: list) -> list[dict]:
    """Flatten a schema to name/type/mode, recursing into RECORD fields."""
    out = []
    for f in fields or []:
        entry = {
            "name": f.get("name"),
            "type": f.get("type"),
            "mode": f.get("mode", "NULLABLE"),
        }
        if f.get("description"):
            entry["description"] = f["description"]
        if f.get("fields"):
            entry["fields"] = _schema_fields(f["fields"])
        out.append(entry)
    return out


def _params(values: dict | None) -> list[dict]:
    """Named query parameters, typed by the Python value.

    Every caller-supplied VALUE in this module goes through here rather than
    into the SQL string. A parameter cannot change the shape of a statement,
    so a search pattern of "%'; DROP TABLE x --" is just a pattern that matches
    nothing.
    """
    out = []
    for name, value in (values or {}).items():
        if isinstance(value, bool):
            kind, text = "BOOL", "true" if value else "false"
        elif isinstance(value, int):
            kind, text = "INT64", str(value)
        elif isinstance(value, float):
            kind, text = "FLOAT64", repr(value)
        else:
            kind, text = "STRING", str(value)
        out.append({
            "name": name,
            "parameterType": {"type": kind},
            "parameterValue": {"value": text},
        })
    return out


# --------------------------------------------------------------------------- #
# The one query path
# --------------------------------------------------------------------------- #
async def _sql(
    conn: Connection,
    db,
    project: str,
    sql: str,
    *,
    params: dict | None = None,
    limit: int = 100,
    cap: int = META_BYTES_CAP,
    dry_run: bool = False,
) -> dict:
    """Run one statement through jobs.query and return it already shaped.

    Every tool in this module goes through here, so the bytes-billed ceiling,
    the parameter binding and the row unwrapping are applied in exactly one
    place and cannot be forgotten by a tool added later.
    """
    body: dict = {
        "query": sql,
        "useLegacySql": False,
        "timeoutMs": QUERY_TIMEOUT_MS,
    }
    if dry_run:
        # No ceiling on a dry run. Nothing is billed, and the whole point is to
        # learn the estimate -- capping it would make BigQuery refuse exactly
        # the expensive query the caller is trying to find out about.
        body["dryRun"] = True
    else:
        body["maxResults"] = limit
        # A string, not an int: the field is an int64 and BigQuery rejects a
        # JSON number that large.
        body["maximumBytesBilled"] = str(cap)
    if params:
        body["parameterMode"] = "NAMED"
        body["queryParameters"] = _params(params)

    data = await _post(conn, db, f"/projects/{project}/queries", body)
    schema = data.get("schema") or {}
    if dry_run:
        return {
            "bytes_processed": _int(data.get("totalBytesProcessed")),
            "schema": _schema_fields(schema.get("fields")),
        }
    return {
        # False means the job is still running server-side and `rows` is empty
        # -- surfaced rather than hidden, so the caller knows to narrow the
        # query instead of concluding the table is empty.
        "job_complete": bool(data.get("jobComplete", True)),
        "cache_hit": bool(data.get("cacheHit", False)),
        "total_rows": _int(data.get("totalRows")),
        "bytes_processed": _int(data.get("totalBytesProcessed")),
        "schema": _schema_fields(schema.get("fields")),
        "rows": _rows_to_dicts(schema, data.get("rows", []) or []),
    }


# =========================================================================== #
# A. Hierarchy (REST, free)
# =========================================================================== #
async def list_projects(conn: Connection, db, args: dict) -> dict:
    limit = _limit(args, 50, 200)

    async def _load():
        data = await _get(conn, db, "/projects", {"maxResults": limit})
        rows = [
            {
                "project_id": p.get("id"),
                "numeric_id": p.get("numericId"),
                "name": p.get("friendlyName") or p.get("id"),
            }
            for p in data.get("projects", []) or []
        ]
        return {"row_count": len(rows), "projects": rows}

    return await cached(SLUG, conn.id, "list_projects", TTL_LONG, _load, args={"lim": limit})


async def list_datasets(conn: Connection, db, args: dict) -> dict:
    project = _project(conn, args)
    limit = _limit(args, 100, 1000)
    hidden = bool((args or {}).get("include_hidden"))

    async def _load():
        params = {"maxResults": limit}
        if hidden:
            params["all"] = "true"
        data = await _get(conn, db, f"/projects/{project}/datasets", params)
        rows = []
        for d in data.get("datasets", []) or []:
            ref = d.get("datasetReference", {}) or {}
            rows.append({
                "dataset_id": ref.get("datasetId"),
                "project_id": ref.get("projectId", project),
                "location": d.get("location"),
                "friendly_name": d.get("friendlyName"),
                "labels": d.get("labels") or {},
            })
        return {"project_id": project, "row_count": len(rows), "datasets": rows}

    return await cached(
        SLUG, conn.id, "list_datasets", TTL_MEDIUM, _load,
        args={"p": project, "lim": limit, "all": hidden},
    )


async def get_dataset(conn: Connection, db, args: dict) -> dict:
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")

    async def _load():
        d = await _get(conn, db, f"/projects/{project}/datasets/{dataset}")
        return {
            "project_id": project,
            "dataset_id": dataset,
            "friendly_name": d.get("friendlyName"),
            "description": d.get("description"),
            "location": d.get("location"),
            "labels": d.get("labels") or {},
            "created_ms": _int(d.get("creationTime")),
            "last_modified_ms": _int(d.get("lastModifiedTime")),
            "default_table_expiration_ms": _int(d.get("defaultTableExpirationMs")),
            "access_entries": len(d.get("access", []) or []),
        }

    return await cached(
        SLUG, conn.id, "get_dataset", TTL_MEDIUM, _load, args={"p": project, "d": dataset},
    )


async def list_tables(conn: Connection, db, args: dict) -> dict:
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    limit = _limit(args, 100, 1000)

    async def _load():
        data = await _get(
            conn, db, f"/projects/{project}/datasets/{dataset}/tables",
            {"maxResults": limit},
        )
        rows = []
        for t in data.get("tables", []) or []:
            ref = t.get("tableReference", {}) or {}
            rows.append({
                "table_id": ref.get("tableId"),
                "dataset_id": ref.get("datasetId", dataset),
                "project_id": ref.get("projectId", project),
                # TABLE, VIEW, MATERIALIZED_VIEW or EXTERNAL -- worth carrying,
                # because a VIEW cannot be previewed off storage.
                "type": t.get("type"),
                "friendly_name": t.get("friendlyName"),
                "created_ms": _int(t.get("creationTime")),
            })
        return {
            "project_id": project, "dataset_id": dataset,
            "row_count": len(rows), "tables": rows,
        }

    return await cached(
        SLUG, conn.id, "list_tables", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "lim": limit},
    )


async def get_table(conn: Connection, db, args: dict) -> dict:
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events_20240101")

    async def _load():
        t = await _get(conn, db, f"/projects/{project}/datasets/{dataset}/tables/{table}")
        return {
            "project_id": project,
            "dataset_id": dataset,
            "table_id": table,
            "type": t.get("type"),
            "description": t.get("description"),
            "num_rows": _int(t.get("numRows")),
            "num_bytes": _int(t.get("numBytes")),
            "created_ms": _int(t.get("creationTime")),
            "last_modified_ms": _int(t.get("lastModifiedTime")),
            "expiration_ms": _int(t.get("expirationTime")),
            "partitioning": t.get("timePartitioning") or t.get("rangePartitioning"),
            "clustering": (t.get("clustering") or {}).get("fields"),
            "labels": t.get("labels") or {},
            "schema": _schema_fields((t.get("schema") or {}).get("fields")),
        }

    return await cached(
        SLUG, conn.id, "get_table", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "t": table},
    )


async def preview_table(conn: Connection, db, args: dict) -> dict:
    """Rows straight off storage. tabledata.list scans nothing and bills nothing."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events_20240101")
    limit = _limit(args, 20, 500)

    async def _load():
        path = f"/projects/{project}/datasets/{dataset}/tables/{table}/data"
        data = await _get(conn, db, path, {"maxResults": limit})
        # tabledata.list does not return the schema, so it is fetched alongside
        # to name the columns -- a positional row is not an answer.
        meta = await _get(conn, db, f"/projects/{project}/datasets/{dataset}/tables/{table}")
        return {
            "project_id": project,
            "dataset_id": dataset,
            "table_id": table,
            "total_rows": _int(data.get("totalRows")),
            "row_count": len(data.get("rows", []) or []),
            "bytes_billed": 0,
            "rows": _rows_to_dicts(meta.get("schema") or {}, data.get("rows", []) or []),
        }

    return await cached(
        SLUG, conn.id, "preview_table", TTL_SHORT, _load,
        args={"p": project, "d": dataset, "t": table, "lim": limit},
    )


async def list_models(conn: Connection, db, args: dict) -> dict:
    """BigQuery ML models in a dataset."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    limit = _limit(args, 50, 500)

    async def _load():
        data = await _get(
            conn, db, f"/projects/{project}/datasets/{dataset}/models",
            {"maxResults": limit},
        )
        rows = []
        for m in data.get("models", []) or []:
            ref = m.get("modelReference", {}) or {}
            rows.append({
                "model_id": ref.get("modelId"),
                "dataset_id": ref.get("datasetId", dataset),
                "model_type": m.get("modelType"),
                "created_ms": _int(m.get("creationTime")),
                "last_modified_ms": _int(m.get("lastModifiedTime")),
            })
        return {
            "project_id": project, "dataset_id": dataset,
            "row_count": len(rows), "models": rows,
        }

    return await cached(
        SLUG, conn.id, "list_models", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "lim": limit},
    )


async def get_model(conn: Connection, db, args: dict) -> dict:
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    model = _require(args, "model_id", "churn_model")

    async def _load():
        m = await _get(conn, db, f"/projects/{project}/datasets/{dataset}/models/{model}")
        runs = m.get("trainingRuns", []) or []
        return {
            "project_id": project,
            "dataset_id": dataset,
            "model_id": model,
            "model_type": m.get("modelType"),
            "description": m.get("description"),
            "created_ms": _int(m.get("creationTime")),
            "last_modified_ms": _int(m.get("lastModifiedTime")),
            "feature_columns": m.get("featureColumns") or [],
            "label_columns": m.get("labelColumns") or [],
            "training_run_count": len(runs),
            # Only the newest run's summary: the full history is megabytes of
            # per-iteration loss that no caller asked for.
            "latest_training": (runs[-1] or {}).get("evaluationMetrics") if runs else None,
        }

    return await cached(
        SLUG, conn.id, "get_model", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "m": model},
    )


async def list_routines(conn: Connection, db, args: dict) -> dict:
    """Stored procedures, UDFs and table functions in a dataset."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    limit = _limit(args, 50, 500)

    async def _load():
        data = await _get(
            conn, db, f"/projects/{project}/datasets/{dataset}/routines",
            {"maxResults": limit},
        )
        rows = []
        for r in data.get("routines", []) or []:
            ref = r.get("routineReference", {}) or {}
            rows.append({
                "routine_id": ref.get("routineId"),
                "dataset_id": ref.get("datasetId", dataset),
                "routine_type": r.get("routineType"),
                "language": r.get("language"),
                "created_ms": _int(r.get("creationTime")),
            })
        return {
            "project_id": project, "dataset_id": dataset,
            "row_count": len(rows), "routines": rows,
        }

    return await cached(
        SLUG, conn.id, "list_routines", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "lim": limit},
    )


async def get_routine(conn: Connection, db, args: dict) -> dict:
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    routine = _require(args, "routine_id", "normalise_url")

    async def _load():
        r = await _get(conn, db, f"/projects/{project}/datasets/{dataset}/routines/{routine}")
        return {
            "project_id": project,
            "dataset_id": dataset,
            "routine_id": routine,
            "routine_type": r.get("routineType"),
            "language": r.get("language"),
            "description": r.get("description"),
            "arguments": r.get("arguments") or [],
            "return_type": r.get("returnType"),
            "definition_body": (r.get("definitionBody") or "")[:8000],
            "created_ms": _int(r.get("creationTime")),
        }

    return await cached(
        SLUG, conn.id, "get_routine", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "r": routine},
    )


# =========================================================================== #
# B. Jobs (REST, free)
# =========================================================================== #
async def list_jobs(conn: Connection, db, args: dict) -> dict:
    project = _project(conn, args)
    limit = _limit(args, 25, 200)
    all_users = bool((args or {}).get("all_users"))

    async def _load():
        data = await _get(
            conn, db, f"/projects/{project}/jobs",
            {
                "maxResults": limit,
                "projection": "full",
                "allUsers": "true" if all_users else "false",
            },
        )
        rows = []
        for j in data.get("jobs", []) or []:
            stats = j.get("statistics", {}) or {}
            q = stats.get("query", {}) or {}
            status = j.get("status", {}) or {}
            error = (status.get("errorResult") or {}).get("message")
            rows.append({
                "job_id": (j.get("jobReference") or {}).get("jobId"),
                "state": status.get("state"),
                "error": error[:300] if error else "",
                "user_email": j.get("user_email"),
                "created_ms": _int(stats.get("creationTime")),
                "started_ms": _int(stats.get("startTime")),
                "ended_ms": _int(stats.get("endTime")),
                "bytes_processed": _int(q.get("totalBytesProcessed")),
                "bytes_billed": _int(q.get("totalBytesBilled")),
                "cache_hit": q.get("cacheHit"),
            })
        return {"project_id": project, "row_count": len(rows), "jobs": rows}

    return await cached(
        SLUG, conn.id, "list_jobs", TTL_SHORT, _load,
        args={"p": project, "lim": limit, "all": all_users},
    )


async def get_job(conn: Connection, db, args: dict) -> dict:
    """One job in full: its SQL, its outcome and its per-stage query plan."""
    project = _project(conn, args)
    job_id = _require(args, "job_id", "bquxjob_1a2b3c4d_1")
    location = str((args or {}).get("location") or "").strip()

    async def _load():
        params = {"location": location} if location else None
        j = await _get(conn, db, f"/projects/{project}/jobs/{job_id}", params)
        stats = j.get("statistics", {}) or {}
        q = stats.get("query", {}) or {}
        status = j.get("status", {}) or {}
        error = (status.get("errorResult") or {}).get("message")
        stages = [
            {
                "name": s.get("name"),
                "status": s.get("status"),
                "records_read": _int(s.get("recordsRead")),
                "records_written": _int(s.get("recordsWritten")),
                "shuffle_output_bytes": _int(s.get("shuffleOutputBytes")),
            }
            for s in (q.get("queryPlan") or [])[:40]
        ]
        return {
            "project_id": project,
            "job_id": job_id,
            "state": status.get("state"),
            "error": error[:300] if error else "",
            "user_email": j.get("user_email"),
            "sql": ((j.get("configuration") or {}).get("query") or {}).get("query", "")[:8000],
            "created_ms": _int(stats.get("creationTime")),
            "started_ms": _int(stats.get("startTime")),
            "ended_ms": _int(stats.get("endTime")),
            "bytes_processed": _int(q.get("totalBytesProcessed")),
            "bytes_billed": _int(q.get("totalBytesBilled")),
            "slot_ms": _int(q.get("totalSlotMs")),
            "cache_hit": q.get("cacheHit"),
            "referenced_tables": [
                f"{t.get('projectId')}.{t.get('datasetId')}.{t.get('tableId')}"
                for t in (q.get("referencedTables") or [])[:50]
            ],
            "stage_count": len(q.get("queryPlan") or []),
            "stages": stages,
        }

    return await cached(
        SLUG, conn.id, "get_job", TTL_SHORT, _load,
        args={"p": project, "j": job_id, "loc": location},
    )


async def get_query_results(conn: Connection, db, args: dict) -> dict:
    """Rows from an already-completed job. Re-reads a result, bills nothing."""
    project = _project(conn, args)
    job_id = _require(args, "job_id", "bquxjob_1a2b3c4d_1")
    limit = _limit(args, 100, 1000)
    location = str((args or {}).get("location") or "").strip()

    async def _load():
        params = {"maxResults": limit}
        if location:
            params["location"] = location
        data = await _get(conn, db, f"/projects/{project}/queries/{job_id}", params)
        schema = data.get("schema") or {}
        rows = _rows_to_dicts(schema, data.get("rows", []) or [])
        return {
            "project_id": project,
            "job_id": job_id,
            "job_complete": bool(data.get("jobComplete", True)),
            "total_rows": _int(data.get("totalRows")),
            "row_count": len(rows),
            "schema": _schema_fields(schema.get("fields")),
            "rows": rows,
        }

    return await cached(
        SLUG, conn.id, "get_query_results", TTL_SHORT, _load,
        args={"p": project, "j": job_id, "lim": limit, "loc": location},
    )


# =========================================================================== #
# C. Query
# =========================================================================== #
async def dry_run_query(conn: Connection, db, args: dict) -> dict:
    """What the query would scan, without running it. Costs nothing."""
    project = _project(conn, args)
    sql = _require_readonly_sql((args or {}).get("sql", ""))

    async def _load():
        out = await _sql(conn, db, project, sql, cap=MAX_BYTES_CEILING, dry_run=True)
        scanned = out.get("bytes_processed") or 0
        return {
            "project_id": project,
            "would_scan_bytes": scanned,
            "would_scan_mib": round(scanned / (1024 ** 2), 3),
            "default_cap_bytes": MAX_BYTES_DEFAULT,
            "within_default_cap": scanned <= MAX_BYTES_DEFAULT,
            "schema": out.get("schema"),
        }

    return await cached(
        SLUG, conn.id, "dry_run_query", TTL_SHORT, _load, args={"p": project, "q": sql},
    )


async def run_query(conn: Connection, db, args: dict) -> dict:
    """Run read-only SQL under a hard bytes-billed ceiling."""
    project = _project(conn, args)
    sql = _require_readonly_sql((args or {}).get("sql", ""))
    limit = _limit(args, 100, 1000)
    cap = _max_bytes(args)

    async def _load():
        out = await _sql(conn, db, project, sql, limit=limit, cap=cap)
        out["project_id"] = project
        out["bytes_billed_cap"] = cap
        out["row_count"] = len(out.get("rows") or [])
        return out

    return await cached(
        SLUG, conn.id, "run_query", TTL_SHORT, _load,
        args={"p": project, "q": sql, "lim": limit, "cap": cap},
    )


# =========================================================================== #
# D. Schema discovery (INFORMATION_SCHEMA)
# =========================================================================== #
async def search_tables(conn: Connection, db, args: dict) -> dict:
    """Find tables whose name contains a substring, within one dataset."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    pattern = _require(args, "pattern", "events")
    limit = _limit(args, 50, 500)
    ref = _schema_ref(project, dataset, "TABLES")

    async def _load():
        sql = (
            f"SELECT table_name, table_type, creation_time "
            f"FROM {ref} "
            f"WHERE LOWER(table_name) LIKE LOWER(CONCAT('%', @pattern, '%')) "
            f"ORDER BY table_name LIMIT @lim"
        )
        out = await _sql(
            conn, db, project, sql,
            params={"pattern": pattern, "lim": limit}, limit=limit,
        )
        return {
            "project_id": project, "dataset_id": dataset, "pattern": pattern,
            "row_count": len(out.get("rows") or []), "tables": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "search_tables", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "pat": pattern, "lim": limit},
    )


async def search_columns(conn: Connection, db, args: dict) -> dict:
    """Find columns whose name contains a substring, across a dataset's tables."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    pattern = _require(args, "pattern", "user_id")
    limit = _limit(args, 100, 1000)
    ref = _schema_ref(project, dataset, "COLUMNS")

    async def _load():
        sql = (
            f"SELECT table_name, column_name, data_type, is_nullable "
            f"FROM {ref} "
            f"WHERE LOWER(column_name) LIKE LOWER(CONCAT('%', @pattern, '%')) "
            f"ORDER BY table_name, ordinal_position LIMIT @lim"
        )
        out = await _sql(
            conn, db, project, sql,
            params={"pattern": pattern, "lim": limit}, limit=limit,
        )
        return {
            "project_id": project, "dataset_id": dataset, "pattern": pattern,
            "row_count": len(out.get("rows") or []), "columns": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "search_columns", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "pat": pattern, "lim": limit},
    )


async def table_columns(conn: Connection, db, args: dict) -> dict:
    """Columns of one table in declaration order, with types and defaults."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events_20240101")
    ref = _schema_ref(project, dataset, "COLUMNS")

    async def _load():
        sql = (
            f"SELECT column_name, ordinal_position, data_type, is_nullable, "
            f"is_partitioning_column, clustering_ordinal_position "
            f"FROM {ref} WHERE table_name = @tbl ORDER BY ordinal_position LIMIT 2000"
        )
        out = await _sql(conn, db, project, sql, params={"tbl": table}, limit=2000)
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "row_count": len(out.get("rows") or []), "columns": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "table_columns", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "t": table},
    )


async def list_views(conn: Connection, db, args: dict) -> dict:
    """Views in a dataset, with the first part of each definition."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    limit = _limit(args, 50, 500)
    ref = _schema_ref(project, dataset, "VIEWS")

    async def _load():
        sql = (
            f"SELECT table_name, SUBSTR(view_definition, 1, 400) AS view_definition_head "
            f"FROM {ref} ORDER BY table_name LIMIT @lim"
        )
        out = await _sql(conn, db, project, sql, params={"lim": limit}, limit=limit)
        return {
            "project_id": project, "dataset_id": dataset,
            "row_count": len(out.get("rows") or []), "views": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "list_views", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "lim": limit},
    )


async def get_view_sql(conn: Connection, db, args: dict) -> dict:
    """The full SQL behind one view."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    view = _require(args, "view_id", "daily_sessions")
    ref = _schema_ref(project, dataset, "VIEWS")

    async def _load():
        sql = f"SELECT view_definition FROM {ref} WHERE table_name = @v LIMIT 1"
        out = await _sql(conn, db, project, sql, params={"v": view}, limit=1)
        rows = out.get("rows") or []
        if not rows:
            raise ConnectorError(f"No view named '{view}' in {dataset}.")
        return {
            "project_id": project, "dataset_id": dataset, "view_id": view,
            "view_definition": (rows[0].get("view_definition") or "")[:20000],
        }

    return await cached(
        SLUG, conn.id, "get_view_sql", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "v": view},
    )


async def table_constraints(conn: Connection, db, args: dict) -> dict:
    """Primary and foreign key constraints declared on a table."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "orders")
    ref = _schema_ref(project, dataset, "TABLE_CONSTRAINTS")

    async def _load():
        sql = (
            f"SELECT constraint_name, constraint_type, enforced "
            f"FROM {ref} WHERE table_name = @tbl ORDER BY constraint_name LIMIT 200"
        )
        out = await _sql(conn, db, project, sql, params={"tbl": table}, limit=200)
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "row_count": len(out.get("rows") or []), "constraints": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "table_constraints", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "t": table},
    )


async def table_options(conn: Connection, db, args: dict) -> dict:
    """Options set on a table: description, labels, expiry, partition filter."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events_20240101")
    ref = _schema_ref(project, dataset, "TABLE_OPTIONS")

    async def _load():
        sql = (
            f"SELECT option_name, option_type, option_value "
            f"FROM {ref} WHERE table_name = @tbl ORDER BY option_name LIMIT 200"
        )
        out = await _sql(conn, db, project, sql, params={"tbl": table}, limit=200)
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "row_count": len(out.get("rows") or []), "options": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "table_options", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "t": table},
    )


async def list_partitions(conn: Connection, db, args: dict) -> dict:
    """Partitions of a partitioned table, newest first, with row counts."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events")
    limit = _limit(args, 50, 1000)
    ref = _schema_ref(project, dataset, "PARTITIONS")

    async def _load():
        sql = (
            f"SELECT partition_id, total_rows, total_logical_bytes, last_modified_time "
            f"FROM {ref} WHERE table_name = @tbl "
            f"ORDER BY partition_id DESC LIMIT @lim"
        )
        out = await _sql(
            conn, db, project, sql, params={"tbl": table, "lim": limit}, limit=limit,
        )
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "row_count": len(out.get("rows") or []), "partitions": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "list_partitions", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "t": table, "lim": limit},
    )


# =========================================================================== #
# E. Storage and cost (INFORMATION_SCHEMA)
# =========================================================================== #
async def dataset_storage(conn: Connection, db, args: dict) -> dict:
    """Every table in a dataset by size, biggest first."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    limit = _limit(args, 50, 1000)
    ref = _schema_ref(project, dataset, "TABLE_STORAGE")

    async def _load():
        sql = (
            f"SELECT table_name, total_rows, total_logical_bytes, "
            f"total_physical_bytes, storage_last_modified_time "
            f"FROM {ref} ORDER BY total_logical_bytes DESC LIMIT @lim"
        )
        out = await _sql(conn, db, project, sql, params={"lim": limit}, limit=limit)
        rows = out.get("rows") or []
        total = sum(_int(r.get("total_logical_bytes")) or 0 for r in rows)
        return {
            "project_id": project, "dataset_id": dataset,
            "row_count": len(rows),
            "total_logical_bytes": total,
            "total_logical_gib": round(total / (1024 ** 3), 3),
            "tables": rows,
        }

    return await cached(
        SLUG, conn.id, "dataset_storage", TTL_MEDIUM, _load,
        args={"p": project, "d": dataset, "lim": limit},
    )


async def query_history(conn: Connection, db, args: dict) -> dict:
    """Recent query jobs across the project, with what each one cost."""
    project = _project(conn, args)
    region = _region(args)
    days = _days(args, 7, 180)
    limit = _limit(args, 50, 500)
    ref = _region_schema_ref(project, region, "JOBS_BY_PROJECT")

    async def _load():
        sql = (
            f"SELECT job_id, user_email, job_type, statement_type, state, "
            f"creation_time, total_bytes_billed, total_slot_ms, cache_hit, "
            f"error_result.message AS error_message, "
            f"SUBSTR(query, 1, 300) AS query_head "
            f"FROM {ref} "
            f"WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @d DAY) "
            f"ORDER BY creation_time DESC LIMIT @lim"
        )
        out = await _sql(
            conn, db, project, sql, params={"d": days, "lim": limit}, limit=limit,
        )
        return {
            "project_id": project, "region": region, "days": days,
            "row_count": len(out.get("rows") or []), "jobs": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "query_history", TTL_SHORT, _load,
        args={"p": project, "r": region, "d": days, "lim": limit},
    )


async def cost_by_day(conn: Connection, db, args: dict) -> dict:
    """Bytes billed per day, so a spike has a date on it."""
    project = _project(conn, args)
    region = _region(args)
    days = _days(args, 30, 180)
    ref = _region_schema_ref(project, region, "JOBS_BY_PROJECT")

    async def _load():
        sql = (
            f"SELECT DATE(creation_time) AS day, COUNT(*) AS jobs, "
            f"SUM(total_bytes_billed) AS bytes_billed, "
            f"ROUND(SUM(total_bytes_billed) / POW(1024, 4), 4) AS tib_billed, "
            f"SUM(total_slot_ms) AS slot_ms "
            f"FROM {ref} "
            f"WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @d DAY) "
            f"AND job_type = 'QUERY' "
            f"GROUP BY day ORDER BY day DESC LIMIT 400"
        )
        out = await _sql(conn, db, project, sql, params={"d": days}, limit=400)
        return {
            "project_id": project, "region": region, "days": days,
            "row_count": len(out.get("rows") or []), "by_day": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "cost_by_day", TTL_MEDIUM, _load,
        args={"p": project, "r": region, "d": days},
    )


async def top_costly_queries(conn: Connection, db, args: dict) -> dict:
    """The queries that billed the most bytes in the window."""
    project = _project(conn, args)
    region = _region(args)
    days = _days(args, 7, 180)
    limit = _limit(args, 20, 200)
    ref = _region_schema_ref(project, region, "JOBS_BY_PROJECT")

    async def _load():
        sql = (
            f"SELECT job_id, user_email, creation_time, total_bytes_billed, "
            f"ROUND(total_bytes_billed / POW(1024, 3), 3) AS gib_billed, "
            f"total_slot_ms, SUBSTR(query, 1, 400) AS query_head "
            f"FROM {ref} "
            f"WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @d DAY) "
            f"AND job_type = 'QUERY' AND total_bytes_billed > 0 "
            f"ORDER BY total_bytes_billed DESC LIMIT @lim"
        )
        out = await _sql(
            conn, db, project, sql, params={"d": days, "lim": limit}, limit=limit,
        )
        return {
            "project_id": project, "region": region, "days": days,
            "row_count": len(out.get("rows") or []), "queries": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "top_costly_queries", TTL_MEDIUM, _load,
        args={"p": project, "r": region, "d": days, "lim": limit},
    )


async def cost_by_user(conn: Connection, db, args: dict) -> dict:
    """Bytes billed per user in the window, biggest spender first."""
    project = _project(conn, args)
    region = _region(args)
    days = _days(args, 30, 180)
    limit = _limit(args, 50, 500)
    ref = _region_schema_ref(project, region, "JOBS_BY_PROJECT")

    async def _load():
        sql = (
            f"SELECT user_email, COUNT(*) AS jobs, "
            f"SUM(total_bytes_billed) AS bytes_billed, "
            f"ROUND(SUM(total_bytes_billed) / POW(1024, 4), 4) AS tib_billed, "
            f"SUM(total_slot_ms) AS slot_ms "
            f"FROM {ref} "
            f"WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @d DAY) "
            f"AND job_type = 'QUERY' "
            f"GROUP BY user_email ORDER BY bytes_billed DESC LIMIT @lim"
        )
        out = await _sql(
            conn, db, project, sql, params={"d": days, "lim": limit}, limit=limit,
        )
        return {
            "project_id": project, "region": region, "days": days,
            "row_count": len(out.get("rows") or []), "by_user": out.get("rows"),
        }

    return await cached(
        SLUG, conn.id, "cost_by_user", TTL_MEDIUM, _load,
        args={"p": project, "r": region, "d": days, "lim": limit},
    )


# =========================================================================== #
# F. Profiling (billed, capped)
# =========================================================================== #
async def count_rows(conn: Connection, db, args: dict) -> dict:
    """Exact row count, optionally under a WHERE clause of your own."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events")
    ref = _table_ref(project, dataset, table)
    cap = _max_bytes(args)

    async def _load():
        out = await _sql(
            conn, db, project, f"SELECT COUNT(*) AS row_count FROM {ref}",
            limit=1, cap=cap,
        )
        rows = out.get("rows") or [{}]
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "row_count": _int(rows[0].get("row_count")),
            "bytes_processed": out.get("bytes_processed"),
        }

    return await cached(
        SLUG, conn.id, "count_rows", TTL_SHORT, _load,
        args={"p": project, "d": dataset, "t": table, "cap": cap},
    )


async def distinct_count(conn: Connection, db, args: dict) -> dict:
    """Approximate distinct values in a column. APPROX_COUNT_DISTINCT, not exact."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events")
    column = _require(args, "column", "user_id")
    ref = _table_ref(project, dataset, table)
    col = _ident(column, "column")
    cap = _max_bytes(args)

    async def _load():
        sql = (
            f"SELECT APPROX_COUNT_DISTINCT({col}) AS approx_distinct, "
            f"COUNT(*) AS total_rows, COUNTIF({col} IS NULL) AS null_rows FROM {ref}"
        )
        out = await _sql(conn, db, project, sql, limit=1, cap=cap)
        rows = out.get("rows") or [{}]
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "column": column,
            "approx_distinct": _int(rows[0].get("approx_distinct")),
            "total_rows": _int(rows[0].get("total_rows")),
            "null_rows": _int(rows[0].get("null_rows")),
            "bytes_processed": out.get("bytes_processed"),
        }

    return await cached(
        SLUG, conn.id, "distinct_count", TTL_SHORT, _load,
        args={"p": project, "d": dataset, "t": table, "c": column, "cap": cap},
    )


async def column_stats(conn: Connection, db, args: dict) -> dict:
    """Null share, distinct estimate and min/max for one column."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events")
    column = _require(args, "column", "event_timestamp")
    ref = _table_ref(project, dataset, table)
    col = _ident(column, "column")
    cap = _max_bytes(args)

    async def _load():
        # MIN/MAX are cast to STRING so one shape comes back whatever the
        # column's type is -- a caller charting this does not want to branch on
        # whether today's column happened to be a DATE or an INT64.
        sql = (
            f"SELECT COUNT(*) AS total_rows, "
            f"COUNTIF({col} IS NULL) AS null_rows, "
            f"APPROX_COUNT_DISTINCT({col}) AS approx_distinct, "
            f"CAST(MIN({col}) AS STRING) AS min_value, "
            f"CAST(MAX({col}) AS STRING) AS max_value "
            f"FROM {ref}"
        )
        out = await _sql(conn, db, project, sql, limit=1, cap=cap)
        row = (out.get("rows") or [{}])[0]
        total = _int(row.get("total_rows")) or 0
        nulls = _int(row.get("null_rows")) or 0
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "column": column,
            "total_rows": total,
            "null_rows": nulls,
            "null_share": round(nulls / total, 6) if total else None,
            "approx_distinct": _int(row.get("approx_distinct")),
            "min_value": row.get("min_value"),
            "max_value": row.get("max_value"),
            "bytes_processed": out.get("bytes_processed"),
        }

    return await cached(
        SLUG, conn.id, "column_stats", TTL_SHORT, _load,
        args={"p": project, "d": dataset, "t": table, "c": column, "cap": cap},
    )


async def top_values(conn: Connection, db, args: dict) -> dict:
    """The most frequent values in a column, with their share of the table."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events")
    column = _require(args, "column", "event_name")
    limit = _limit(args, 20, 500)
    ref = _table_ref(project, dataset, table)
    col = _ident(column, "column")
    cap = _max_bytes(args)

    async def _load():
        sql = (
            f"SELECT CAST({col} AS STRING) AS value, COUNT(*) AS rows_count, "
            f"ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 6) AS share "
            f"FROM {ref} GROUP BY value ORDER BY rows_count DESC LIMIT @lim"
        )
        out = await _sql(
            conn, db, project, sql, params={"lim": limit}, limit=limit, cap=cap,
        )
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "column": column,
            "row_count": len(out.get("rows") or []),
            "values": out.get("rows"),
            "bytes_processed": out.get("bytes_processed"),
        }

    return await cached(
        SLUG, conn.id, "top_values", TTL_SHORT, _load,
        args={"p": project, "d": dataset, "t": table, "c": column, "lim": limit, "cap": cap},
    )


async def sample_rows(conn: Connection, db, args: dict) -> dict:
    """A random sample via TABLESAMPLE. Unlike preview_table this scans, so it costs."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events")
    limit = _limit(args, 20, 500)
    ref = _table_ref(project, dataset, table)
    cap = _max_bytes(args)
    try:
        percent = float((args or {}).get("percent", 1))
    except (TypeError, ValueError):
        percent = 1.0
    percent = max(0.01, min(percent, 100.0))

    async def _load():
        # The percent is formatted into the SQL because TABLESAMPLE does not
        # accept a query parameter. It is a float clamped to (0, 100] two lines
        # above, so it cannot carry anything but a number.
        sql = (
            f"SELECT * FROM {ref} TABLESAMPLE SYSTEM ({percent:.4f} PERCENT) "
            f"LIMIT @lim"
        )
        out = await _sql(
            conn, db, project, sql, params={"lim": limit}, limit=limit, cap=cap,
        )
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "percent": percent,
            "row_count": len(out.get("rows") or []),
            "rows": out.get("rows"),
            "bytes_processed": out.get("bytes_processed"),
        }

    return await cached(
        SLUG, conn.id, "sample_rows", TTL_SHORT, _load,
        args={"p": project, "d": dataset, "t": table, "pct": percent, "lim": limit, "cap": cap},
    )


async def time_series(conn: Connection, db, args: dict) -> dict:
    """Rows per day over a date/timestamp column."""
    project = _project(conn, args)
    dataset = _require(args, "dataset_id", "analytics_123456")
    table = _require(args, "table_id", "events")
    column = _require(args, "column", "event_timestamp")
    days = _days(args, 30, 3650)
    ref = _table_ref(project, dataset, table)
    col = _ident(column, "column")
    cap = _max_bytes(args)

    async def _load():
        # CAST, not TIMESTAMP(): the column may be DATE, DATETIME or TIMESTAMP,
        # and TIMESTAMP() has no signature that accepts a TIMESTAMP, so the
        # obvious spelling fails on exactly the commonest column type. CAST is
        # a no-op on TIMESTAMP and widens the other two.
        sql = (
            f"SELECT DATE({col}) AS day, COUNT(*) AS rows_count FROM {ref} "
            f"WHERE CAST({col} AS TIMESTAMP) >= "
            f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @d DAY) "
            f"GROUP BY day ORDER BY day DESC LIMIT 4000"
        )
        out = await _sql(conn, db, project, sql, params={"d": days}, limit=4000, cap=cap)
        return {
            "project_id": project, "dataset_id": dataset, "table_id": table,
            "column": column, "days": days,
            "row_count": len(out.get("rows") or []),
            "series": out.get("rows"),
            "bytes_processed": out.get("bytes_processed"),
        }

    return await cached(
        SLUG, conn.id, "time_series", TTL_SHORT, _load,
        args={"p": project, "d": dataset, "t": table, "c": column, "dd": days, "cap": cap},
    )


# =========================================================================== #
# Catalog
# =========================================================================== #
_PROJECT_PROP = {
    "type": "string",
    "description": "GCP project id. Optional when a default project is saved on the connection.",
}
_DATASET_PROP = {
    "type": "string",
    "description": "Dataset id, without the project prefix, e.g. 'analytics_123456'.",
}
_TABLE_PROP = {
    "type": "string",
    "description": "Table id, without the project or dataset prefix, e.g. 'events_20240101'.",
}
_COLUMN_PROP = {
    "type": "string",
    "description": "Column name. Letters, digits and underscores only.",
}
_REGION_PROP = {
    "type": "string",
    "description": "BigQuery region for INFORMATION_SCHEMA, e.g. 'us', 'eu', 'europe-west4'. Default 'us'.",
}
_CAP_PROP = {
    "type": "integer",
    "description": (
        "Hard ceiling on bytes scanned. Defaults to 1 GiB and cannot exceed "
        "100 GiB. The job is refused rather than truncated."
    ),
}
_SQL_PROP = {
    "type": "string",
    "description": (
        "Standard SQL. Must be a single read-only SELECT or WITH statement. "
        "Fully qualify tables as `project.dataset.table`."
    ),
}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        **({"required": required} if required else {}),
        "additionalProperties": False,
    }


_DT = {"project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP, "table_id": _TABLE_PROP}
_DTC = {**_DT, "column": _COLUMN_PROP}

CATALOG = {
    # --- A. Hierarchy ---
    "list_projects": {
        "description": "GCP projects this account can run BigQuery jobs in.",
        "input": _obj({"limit": {"type": "integer", "description": "Max projects (1-200). Default 50."}}),
    },
    "list_datasets": {
        "description": "Datasets in a project.",
        "input": _obj({
            "project_id": _PROJECT_PROP,
            "include_hidden": {"type": "boolean", "description": "Include datasets starting with an underscore. Default false."},
            "limit": {"type": "integer", "description": "Max datasets (1-1000). Default 100."},
        }),
    },
    "get_dataset": {
        "description": "One dataset's metadata: location, labels, descriptions and expiry.",
        "input": _obj({"project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP}, ["dataset_id"]),
    },
    "list_tables": {
        "description": "Tables and views in a dataset, with their type.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "limit": {"type": "integer", "description": "Max tables (1-1000). Default 100."},
        }, ["dataset_id"]),
    },
    "get_table": {
        "description": "Table schema, row count, byte size, partitioning, clustering and labels.",
        "input": _obj(_DT, ["dataset_id", "table_id"]),
    },
    "preview_table": {
        "description": (
            "Sample rows straight off storage. Scans no bytes and costs nothing, so "
            "prefer this over run_query when you only want to see what is in a table."
        ),
        "input": _obj({**_DT, "limit": {"type": "integer", "description": "Rows (1-500). Default 20."}},
                      ["dataset_id", "table_id"]),
    },
    "list_models": {
        "description": "BigQuery ML models in a dataset.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "limit": {"type": "integer", "description": "Max models (1-500). Default 50."},
        }, ["dataset_id"]),
    },
    "get_model": {
        "description": "One BigQuery ML model: type, feature and label columns, latest evaluation metrics.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "model_id": {"type": "string", "description": "Model id, e.g. 'churn_model'."},
        }, ["dataset_id", "model_id"]),
    },
    "list_routines": {
        "description": "Stored procedures, UDFs and table functions in a dataset.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "limit": {"type": "integer", "description": "Max routines (1-500). Default 50."},
        }, ["dataset_id"]),
    },
    "get_routine": {
        "description": "One routine's signature, return type and body.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "routine_id": {"type": "string", "description": "Routine id, e.g. 'normalise_url'."},
        }, ["dataset_id", "routine_id"]),
    },

    # --- B. Jobs ---
    "list_jobs": {
        "description": "Recent jobs for a project, with state, errors and bytes billed.",
        "input": _obj({
            "project_id": _PROJECT_PROP,
            "all_users": {"type": "boolean", "description": "Include other users' jobs. Default false."},
            "limit": {"type": "integer", "description": "Max jobs (1-200). Default 25."},
        }),
    },
    "get_job": {
        "description": "One job in full: its SQL, outcome, referenced tables and per-stage query plan.",
        "input": _obj({
            "project_id": _PROJECT_PROP,
            "job_id": {"type": "string", "description": "Job id, e.g. 'bquxjob_1a2b3c4d_1'."},
            "location": {"type": "string", "description": "Job location, if it is not the project default."},
        }, ["job_id"]),
    },
    "get_query_results": {
        "description": "Rows from an already-completed job. Re-reads a result and bills nothing.",
        "input": _obj({
            "project_id": _PROJECT_PROP,
            "job_id": {"type": "string", "description": "Job id of a finished query."},
            "location": {"type": "string", "description": "Job location, if it is not the project default."},
            "limit": {"type": "integer", "description": "Rows (1-1000). Default 100."},
        }, ["job_id"]),
    },

    # --- C. Query ---
    "dry_run_query": {
        "description": (
            "Estimate how many bytes a query would scan, without running it and without "
            "being billed. Use this before run_query on an unfamiliar table."
        ),
        "input": _obj({"project_id": _PROJECT_PROP, "sql": _SQL_PROP}, ["sql"]),
    },
    "run_query": {
        "description": (
            "Run a read-only SELECT or WITH query. Rejects anything that writes. "
            "Refused by BigQuery if it would scan more than max_bytes_billed."
        ),
        "input": _obj({
            "project_id": _PROJECT_PROP, "sql": _SQL_PROP,
            "limit": {"type": "integer", "description": "Rows (1-1000). Default 100."},
            "max_bytes_billed": _CAP_PROP,
        }, ["sql"]),
    },

    # --- D. Schema discovery ---
    "search_tables": {
        "description": "Find tables in a dataset whose name contains a substring.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "pattern": {"type": "string", "description": "Substring to match, case-insensitive."},
            "limit": {"type": "integer", "description": "Max rows (1-500). Default 50."},
        }, ["dataset_id", "pattern"]),
    },
    "search_columns": {
        "description": "Find columns across a dataset's tables whose name contains a substring.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "pattern": {"type": "string", "description": "Substring to match, case-insensitive."},
            "limit": {"type": "integer", "description": "Max rows (1-1000). Default 100."},
        }, ["dataset_id", "pattern"]),
    },
    "table_columns": {
        "description": "Columns of one table in order, with type, nullability and partition/cluster role.",
        "input": _obj(_DT, ["dataset_id", "table_id"]),
    },
    "list_views": {
        "description": "Views in a dataset with the first 400 characters of each definition.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "limit": {"type": "integer", "description": "Max views (1-500). Default 50."},
        }, ["dataset_id"]),
    },
    "get_view_sql": {
        "description": "The full SQL behind one view.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "view_id": {"type": "string", "description": "View name, e.g. 'daily_sessions'."},
        }, ["dataset_id", "view_id"]),
    },
    "table_constraints": {
        "description": "Primary and foreign key constraints declared on a table.",
        "input": _obj(_DT, ["dataset_id", "table_id"]),
    },
    "table_options": {
        "description": "Options set on a table: description, labels, expiry, require_partition_filter.",
        "input": _obj(_DT, ["dataset_id", "table_id"]),
    },
    "list_partitions": {
        "description": "Partitions of a partitioned table, newest first, with row counts and sizes.",
        "input": _obj({**_DT, "limit": {"type": "integer", "description": "Max partitions (1-1000). Default 50."}},
                      ["dataset_id", "table_id"]),
    },

    # --- E. Storage and cost ---
    "dataset_storage": {
        "description": "Every table in a dataset by size, biggest first, with the dataset total.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "dataset_id": _DATASET_PROP,
            "limit": {"type": "integer", "description": "Max tables (1-1000). Default 50."},
        }, ["dataset_id"]),
    },
    "query_history": {
        "description": "Recent query jobs across the project with cost, errors and the query text.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "region": _REGION_PROP,
            "days": {"type": "integer", "description": "Window in days (1-180). Default 7."},
            "limit": {"type": "integer", "description": "Max jobs (1-500). Default 50."},
        }),
    },
    "cost_by_day": {
        "description": "Bytes billed and slot-ms per day, so a spend spike has a date on it.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "region": _REGION_PROP,
            "days": {"type": "integer", "description": "Window in days (1-180). Default 30."},
        }),
    },
    "top_costly_queries": {
        "description": "The queries that billed the most bytes in the window.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "region": _REGION_PROP,
            "days": {"type": "integer", "description": "Window in days (1-180). Default 7."},
            "limit": {"type": "integer", "description": "Max rows (1-200). Default 20."},
        }),
    },
    "cost_by_user": {
        "description": "Bytes billed per user in the window, biggest spender first.",
        "input": _obj({
            "project_id": _PROJECT_PROP, "region": _REGION_PROP,
            "days": {"type": "integer", "description": "Window in days (1-180). Default 30."},
            "limit": {"type": "integer", "description": "Max users (1-500). Default 50."},
        }),
    },

    # --- F. Profiling ---
    "count_rows": {
        "description": "Exact row count for a table. Scans the table, so it is billed.",
        "input": _obj({**_DT, "max_bytes_billed": _CAP_PROP}, ["dataset_id", "table_id"]),
    },
    "distinct_count": {
        "description": "Approximate distinct values in a column, plus total and null rows.",
        "input": _obj({**_DTC, "max_bytes_billed": _CAP_PROP}, ["dataset_id", "table_id", "column"]),
    },
    "column_stats": {
        "description": "Null share, approximate distinct count and min/max for one column.",
        "input": _obj({**_DTC, "max_bytes_billed": _CAP_PROP}, ["dataset_id", "table_id", "column"]),
    },
    "top_values": {
        "description": "The most frequent values in a column, with each one's share of the table.",
        "input": _obj({
            **_DTC,
            "limit": {"type": "integer", "description": "Max values (1-500). Default 20."},
            "max_bytes_billed": _CAP_PROP,
        }, ["dataset_id", "table_id", "column"]),
    },
    "sample_rows": {
        "description": (
            "A random sample via TABLESAMPLE. Unlike preview_table this scans the table "
            "and is billed -- use preview_table unless you specifically need randomness."
        ),
        "input": _obj({
            **_DT,
            "percent": {"type": "number", "description": "Percent of blocks to sample (0.01-100). Default 1."},
            "limit": {"type": "integer", "description": "Rows (1-500). Default 20."},
            "max_bytes_billed": _CAP_PROP,
        }, ["dataset_id", "table_id"]),
    },
    "time_series": {
        "description": "Rows per day over a DATE or TIMESTAMP column.",
        "input": _obj({
            **_DTC,
            "days": {"type": "integer", "description": "Window in days (1-3650). Default 30."},
            "max_bytes_billed": _CAP_PROP,
        }, ["dataset_id", "table_id", "column"]),
    },
}

HANDLERS = {
    "list_projects": list_projects,
    "list_datasets": list_datasets,
    "get_dataset": get_dataset,
    "list_tables": list_tables,
    "get_table": get_table,
    "preview_table": preview_table,
    "list_models": list_models,
    "get_model": get_model,
    "list_routines": list_routines,
    "get_routine": get_routine,
    "list_jobs": list_jobs,
    "get_job": get_job,
    "get_query_results": get_query_results,
    "dry_run_query": dry_run_query,
    "run_query": run_query,
    "search_tables": search_tables,
    "search_columns": search_columns,
    "table_columns": table_columns,
    "list_views": list_views,
    "get_view_sql": get_view_sql,
    "table_constraints": table_constraints,
    "table_options": table_options,
    "list_partitions": list_partitions,
    "dataset_storage": dataset_storage,
    "query_history": query_history,
    "cost_by_day": cost_by_day,
    "top_costly_queries": top_costly_queries,
    "cost_by_user": cost_by_user,
    "count_rows": count_rows,
    "distinct_count": distinct_count,
    "column_stats": column_stats,
    "top_values": top_values,
    "sample_rows": sample_rows,
    "time_series": time_series,
}

registry.register(
    Connector(
        slug=SLUG,
        label="Google BigQuery",
        auth="google_oauth",
        description=(
            "Reads BigQuery projects, datasets, tables, schemas, ML models and routines; "
            "previews rows off storage for free; profiles columns; reports storage and "
            "query cost; and runs read-only SQL under a bytes-billed cap."
        ),
        category="Analytics",
        # The full bigquery scope, not bigquery.readonly: the read-only scope
        # cannot create the query job that run_query needs. Every tool here is
        # read-only regardless -- see _require_readonly_sql, and the identifier
        # and parameter rules in the module docstring, for why the guards
        # rather than the scope are the boundary.
        scopes=["https://www.googleapis.com/auth/bigquery"],
        setup_fields=["project_id"],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
