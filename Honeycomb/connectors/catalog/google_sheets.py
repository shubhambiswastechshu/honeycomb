"""Google Sheets connector — read and write spreadsheets through the Sheets API.

Reading is the point of most of these tools: a sheet is where a marketing team
already keeps the numbers nobody has put in a database yet, and being able to
ask an AI client about it without exporting a CSV is most of the value.

Writing is here too, because the other half of that job is putting an answer
back — appending a row of results, updating a cell, adding a tab. Those tools
carry ``write`` so they are labelled "[WRITE]" in every client's tool list and
refused by the dashboard's live-data panel, which runs read tools only.

Two Google APIs are used and the split matters:

* Sheets (``sheets.googleapis.com``) can do everything to a spreadsheet it has
  been given the id of, and nothing else. It cannot answer "which spreadsheets
  exist".
* Drive (``www.googleapis.com/drive/v3``) is the only way to list or search
  files, so ``list_spreadsheets`` and ``find_spreadsheet`` go there. It is asked
  for ``drive.metadata.readonly`` -- names and ids, never file contents -- so
  granting this connector does not hand it every document in the account.

Scopes are therefore: spreadsheets (read+write, because the write tools need it)
plus drive.metadata.readonly (discovery only).
"""
from django.conf import settings

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_SHORT, cached
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"

#: Ceiling on rows returned in one call. A sheet can hold millions of cells and
#: a tool result is read by a model with a context window; truncating loudly
#: beats returning something that gets silently cut in half downstream.
MAX_ROWS = 1000
DEFAULT_ROWS = 200

#: Ceiling on files listed from Drive.
MAX_FILES = 200


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _oauth_conf() -> tuple[str, str, str]:
    token_uri = getattr(settings, 'GOOGLE_OAUTH_TOKEN_URI', 'https://oauth2.googleapis.com/token')
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    client_secret = getattr(settings, 'GOOGLE_CLIENT_SECRET', '')
    if not client_id or not client_secret:
        raise ConnectorError('Google OAuth is not configured on this server.')
    return token_uri, client_id, client_secret


async def _access_token(conn: Connection, db) -> str:
    creds = conn.creds()
    refresh = creds.get("refresh_token")
    if not refresh:
        raise ConnectorError("Not connected: missing refresh token. Reconnect this source.")
    token_uri, client_id, client_secret = _oauth_conf()
    try:
        res = await http_post(token_uri, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        })
    except UpstreamUnavailable as exc:
        raise ConnectorError(str(exc))
    if res.status_code != 200:
        raise ConnectorError("token refresh failed {0}: {1}".format(
            res.status_code, res.text[:300]))
    return res.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": "Bearer {0}".format(token)}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _spreadsheet_id(args: dict, conn: Connection) -> str:
    """The sheet to act on: the caller's, else the one saved on the connection.

    Accepts a full Google Sheets URL as well as a bare id, because that is what
    a person actually has in their clipboard.
    """
    args = args or {}
    raw = str(
        args.get("spreadsheet_id")
        or args.get("spreadsheet")
        or args.get("id")
        or (conn.creds() or {}).get("spreadsheet_id")
        or ""
    ).strip()
    if not raw:
        raise ConnectorError(
            "No spreadsheet given. Pass spreadsheet_id (the long id in the sheet's "
            "URL, or the whole URL), or use list_spreadsheets to find one."
        )
    if "docs.google.com" in raw and "/d/" in raw:
        raw = raw.split("/d/", 1)[1].split("/", 1)[0]
    return raw


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int((args or {}).get(key, default))))
    except (TypeError, ValueError):
        return default


async def _call(conn: Connection, db, method: str, url: str, **kwargs):
    """One request, with the upstream's own error text surfaced readably.

    Google answers a bad range or a missing sheet with a 400 and a JSON error
    whose `message` is genuinely useful ("Unable to parse range: Sheet5!A1"), so
    it is worth passing through rather than replacing with something generic.
    """
    token = await _access_token(conn, db)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update(_bearer(token))
    try:
        if method == "GET":
            res = await http_get(url, headers=headers, **kwargs)
        else:
            res = await http_post(url, headers=headers, **kwargs)
    except UpstreamUnavailable as exc:
        raise ConnectorError(str(exc))

    if res.status_code == 403:
        raise ConnectorError(
            "Google refused that (403). Either this account cannot open the "
            "spreadsheet, or the connection was granted without the Sheets "
            "scope -- reconnect it to re-consent."
        )
    if res.status_code == 404:
        raise ConnectorError(
            "No spreadsheet with that id, or this account cannot see it."
        )
    if res.status_code >= 400:
        detail = ""
        try:
            detail = str(((res.json() or {}).get("error") or {}).get("message") or "")
        except ValueError:
            detail = res.text[:300]
        raise ConnectorError("Sheets API error {0}: {1}".format(res.status_code, detail))
    return res.json()


def _grid(properties: dict) -> dict:
    grid = (properties or {}).get("gridProperties") or {}
    return {"rows": grid.get("rowCount"), "columns": grid.get("columnCount")}


def _tab_summary(sheet: dict) -> dict:
    props = (sheet or {}).get("properties") or {}
    return {
        "title": props.get("title"),
        "sheet_id": props.get("sheetId"),
        "index": props.get("index"),
        "type": props.get("sheetType"),
        "grid": _grid(props),
    }


# --------------------------------------------------------------------------- #
# Discovery (Drive)
# --------------------------------------------------------------------------- #
async def _drive_list(conn: Connection, db, query: str, limit: int) -> dict:
    """List spreadsheet files, cached briefly.

    `cached` is a function, not a decorator: it takes the loader as an argument
    so the cache key can include the connector, the connection and the query.
    Two connections must never share an entry.
    """
    params = {
        "q": query,
        "pageSize": limit,
        "fields": "files(id,name,modifiedTime,owners(displayName,emailAddress),webViewLink)",
        "orderBy": "modifiedTime desc",
        # Shared drives are where team spreadsheets actually live; without these
        # two the tool quietly reports only what is in My Drive.
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }

    async def _load():
        return await _call(conn, db, "GET", DRIVE_API, params=params)

    return await cached(
        "google_sheets", conn.id, "drive_list", TTL_SHORT, _load,
        args={"q": query, "n": limit},
    )


async def list_spreadsheets(conn: Connection, db, args: dict) -> dict:
    """Spreadsheets this account can open, newest first."""
    limit = _int(args, "limit", 50, 1, MAX_FILES)
    data = await _drive_list(
        conn, db,
        "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
        limit,
    )
    files = data.get("files") or []
    return {
        "count": len(files),
        "spreadsheets": [{
            "id": f.get("id"),
            "name": f.get("name"),
            "modified": f.get("modifiedTime"),
            "owner": ((f.get("owners") or [{}])[0]).get("displayName"),
            "url": f.get("webViewLink"),
        } for f in files],
    }


async def find_spreadsheet(conn: Connection, db, args: dict) -> dict:
    """Search spreadsheets by name."""
    args = args or {}
    term = str(args.get("name") or args.get("query") or "").strip()
    if not term:
        raise ConnectorError("name is required.")
    # Drive's query language uses ' as its string delimiter, so a name with an
    # apostrophe has to escape it or the query is a syntax error.
    safe = term.replace("\\", "\\\\").replace("'", "\\'")
    limit = _int(args, "limit", 25, 1, MAX_FILES)
    data = await _drive_list(
        conn, db,
        "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false "
        "and name contains '{0}'".format(safe),
        limit,
    )
    files = data.get("files") or []
    return {
        "query": term,
        "count": len(files),
        "spreadsheets": [{
            "id": f.get("id"),
            "name": f.get("name"),
            "modified": f.get("modifiedTime"),
            "url": f.get("webViewLink"),
        } for f in files],
    }


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
async def get_spreadsheet(conn: Connection, db, args: dict) -> dict:
    """Title, locale, and every tab with its size."""
    sid = _spreadsheet_id(args, conn)
    data = await _call(conn, db, "GET", "{0}/{1}".format(SHEETS_API, sid),
                       params={"fields": "properties,sheets.properties,spreadsheetUrl"})
    props = data.get("properties") or {}
    sheets = data.get("sheets") or []
    return {
        "id": sid,
        "title": props.get("title"),
        "locale": props.get("locale"),
        "time_zone": props.get("timeZone"),
        "url": data.get("spreadsheetUrl"),
        "tab_count": len(sheets),
        "tabs": [_tab_summary(s) for s in sheets],
    }


async def list_tabs(conn: Connection, db, args: dict) -> dict:
    """Just the tabs, for picking one to read."""
    sid = _spreadsheet_id(args, conn)
    data = await _call(conn, db, "GET", "{0}/{1}".format(SHEETS_API, sid),
                       params={"fields": "sheets.properties"})
    sheets = data.get("sheets") or []
    return {"spreadsheet_id": sid, "count": len(sheets),
            "tabs": [_tab_summary(s) for s in sheets]}


async def read_range(conn: Connection, db, args: dict) -> dict:
    """Cell values from an A1 range, exactly as the API returns them."""
    args = args or {}
    sid = _spreadsheet_id(args, conn)
    rng = str(args.get("range") or args.get("a1") or "").strip()
    if not rng:
        raise ConnectorError(
            "range is required, in A1 notation -- 'Sheet1!A1:D100', or just "
            "'Sheet1' for the whole tab."
        )
    limit = _int(args, "limit", DEFAULT_ROWS, 1, MAX_ROWS)
    data = await _call(
        conn, db, "GET",
        "{0}/{1}/values/{2}".format(SHEETS_API, sid, rng),
        params={
            "majorDimension": "ROWS",
            # UNFORMATTED means a number arrives as a number rather than the
            # string "1,234" -- which is what makes the result arithmetic-safe.
            "valueRenderOption": str(args.get("render") or "UNFORMATTED_VALUE"),
            "dateTimeRenderOption": "FORMATTED_STRING",
        },
    )
    values = data.get("values") or []
    return {
        "spreadsheet_id": sid,
        "range": data.get("range"),
        "row_count": len(values),
        "truncated": len(values) > limit,
        "rows": values[:limit],
    }


async def read_table(conn: Connection, db, args: dict) -> dict:
    """A range read as records, taking the first row as the header.

    Most sheets people ask about are tables with a header row, and a list of
    {column: value} is far easier for a model to reason over than a bare grid of
    positional cells.
    """
    args = dict(args or {})
    raw = await read_range(conn, db, args)
    rows = raw.get("rows") or []
    if not rows:
        return {"spreadsheet_id": raw.get("spreadsheet_id"), "range": raw.get("range"),
                "count": 0, "columns": [], "records": []}

    header = [str(cell).strip() for cell in rows[0]]
    # A blank header cell still needs a name or its column vanishes from every
    # record; positional names keep the data addressable.
    columns = [name or "column_{0}".format(i + 1) for i, name in enumerate(header)]
    records = []
    for row in rows[1:]:
        record = {}
        for index, column in enumerate(columns):
            record[column] = row[index] if index < len(row) else None
        records.append(record)
    return {
        "spreadsheet_id": raw.get("spreadsheet_id"),
        "range": raw.get("range"),
        "count": len(records),
        "truncated": raw.get("truncated"),
        "columns": columns,
        "records": records,
    }


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _values_arg(args: dict) -> list:
    values = (args or {}).get("values")
    if not isinstance(values, list) or not values:
        raise ConnectorError(
            "values must be a non-empty list of rows, each row a list of cells -- "
            "e.g. [[\"Jan\", 120], [\"Feb\", 138]]."
        )
    rows = []
    for row in values:
        rows.append(row if isinstance(row, list) else [row])
    return rows


async def append_rows(conn: Connection, db, args: dict) -> dict:
    """Add rows to the end of a tab (live)."""
    args = args or {}
    sid = _spreadsheet_id(args, conn)
    rng = str(args.get("range") or args.get("tab") or "").strip()
    if not rng:
        raise ConnectorError("range is required -- the tab to append to, e.g. 'Sheet1'.")
    rows = _values_arg(args)
    data = await _call(
        conn, db, "POST",
        "{0}/{1}/values/{2}:append".format(SHEETS_API, sid, rng),
        params={
            "valueInputOption": str(args.get("input_option") or "USER_ENTERED"),
            # INSERT_ROWS, not OVERWRITE: appending must never land on top of
            # something already below the table.
            "insertDataOption": "INSERT_ROWS",
            "includeValuesInResponse": "false",
        },
        json={"values": rows},
    )
    updates = data.get("updates") or {}
    return {
        "spreadsheet_id": sid,
        "updated_range": updates.get("updatedRange"),
        "rows_added": updates.get("updatedRows"),
        "cells_added": updates.get("updatedCells"),
    }


async def update_range(conn: Connection, db, args: dict) -> dict:
    """Overwrite the cells in an A1 range (live)."""
    args = args or {}
    sid = _spreadsheet_id(args, conn)
    rng = str(args.get("range") or "").strip()
    if not rng:
        raise ConnectorError("range is required, in A1 notation -- e.g. 'Sheet1!A2:C4'.")
    rows = _values_arg(args)
    token = await _access_token(conn, db)
    # A values update is a PUT; the shim exposes GET and POST, so this goes
    # through httpx directly rather than pretending it is a POST.
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.put(
                "{0}/{1}/values/{2}".format(SHEETS_API, sid, rng),
                headers=_bearer(token),
                params={"valueInputOption": str(args.get("input_option") or "USER_ENTERED")},
                json={"values": rows},
            )
    except httpx.HTTPError as exc:
        raise ConnectorError("Could not reach the Sheets API: {0}".format(exc))
    if res.status_code >= 400:
        detail = ""
        try:
            detail = str(((res.json() or {}).get("error") or {}).get("message") or "")
        except ValueError:
            detail = res.text[:300]
        raise ConnectorError("Sheets API error {0}: {1}".format(res.status_code, detail))
    data = res.json()
    return {
        "spreadsheet_id": sid,
        "updated_range": data.get("updatedRange"),
        "updated_rows": data.get("updatedRows"),
        "updated_cells": data.get("updatedCells"),
    }


async def add_tab(conn: Connection, db, args: dict) -> dict:
    """Add a new tab to the spreadsheet (live)."""
    args = args or {}
    sid = _spreadsheet_id(args, conn)
    title = str(args.get("title") or args.get("name") or "").strip()
    if not title:
        raise ConnectorError("title is required.")
    data = await _call(
        conn, db, "POST", "{0}/{1}:batchUpdate".format(SHEETS_API, sid),
        json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    )
    replies = data.get("replies") or [{}]
    props = ((replies[0] or {}).get("addSheet") or {}).get("properties") or {}
    return {"spreadsheet_id": sid, "title": props.get("title"),
            "sheet_id": props.get("sheetId")}


async def create_spreadsheet(conn: Connection, db, args: dict) -> dict:
    """Create a new spreadsheet owned by this account (live)."""
    args = args or {}
    title = str(args.get("title") or args.get("name") or "").strip()
    if not title:
        raise ConnectorError("title is required.")
    data = await _call(conn, db, "POST", SHEETS_API,
                       json={"properties": {"title": title}})
    return {
        "id": data.get("spreadsheetId"),
        "title": ((data.get("properties") or {}).get("title")),
        "url": data.get("spreadsheetUrl"),
    }


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
_SHEET_PROP = {
    "type": "string",
    "description": "Spreadsheet id, or the full Google Sheets URL. Defaults to the "
                   "one saved on this connection.",
}
_RANGE_PROP = {
    "type": "string",
    "description": "A1 notation, e.g. 'Sheet1!A1:D100'. A bare tab name reads the "
                   "whole tab.",
}


def _input(props: dict | None = None, required: list | None = None) -> dict:
    return {
        "type": "object",
        "properties": dict({"spreadsheet_id": _SHEET_PROP}, **(props or {})),
        "required": required or [],
        "additionalProperties": False,
    }


CATALOG = {
    "list_spreadsheets": {
        "description": "Spreadsheets this Google account can open, newest first.",
        "input": {
            "type": "object",
            "properties": {"limit": {"type": "integer",
                                     "description": "Max files to return (1-200)."}},
            "required": [], "additionalProperties": False,
        },
    },
    "find_spreadsheet": {
        "description": "Search spreadsheets by name.",
        "input": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Text to match in the file name."},
                "limit": {"type": "integer", "description": "Max files to return (1-200)."},
            },
            "required": ["name"], "additionalProperties": False,
        },
    },
    "get_spreadsheet": {
        "description": "Title, locale, time zone and every tab with its row/column size.",
        "input": _input(),
    },
    "list_tabs": {
        "description": "The tabs in a spreadsheet, with their sizes and sheet ids.",
        "input": _input(),
    },
    "read_range": {
        "description": "Cell values from an A1 range, as a grid of rows.",
        "input": _input({
            "range": _RANGE_PROP,
            "limit": {"type": "integer", "description": "Max rows to return (1-1000)."},
            "render": {"type": "string",
                       "description": "UNFORMATTED_VALUE (default, numbers stay numbers), "
                                      "FORMATTED_VALUE, or FORMULA."},
        }, required=["range"]),
    },
    "read_table": {
        "description": "A range read as records, using the first row as column names. "
                       "Use this for anything shaped like a table.",
        "input": _input({
            "range": _RANGE_PROP,
            "limit": {"type": "integer", "description": "Max rows to return (1-1000)."},
        }, required=["range"]),
    },
    "append_rows": {
        "description": "Add rows to the end of a tab.",
        "write": True,
        "input": _input({
            "range": {"type": "string", "description": "Tab to append to, e.g. 'Sheet1'."},
            "values": {"type": "array", "description": "Rows to add; each row a list of cells.",
                       "items": {"type": "array", "items": {}}},
            "input_option": {"type": "string",
                             "description": "USER_ENTERED (default, parses dates and "
                                            "formulas) or RAW."},
        }, required=["range", "values"]),
    },
    "update_range": {
        "description": "Overwrite the cells in an A1 range.",
        "write": True,
        "input": _input({
            "range": _RANGE_PROP,
            "values": {"type": "array", "description": "Rows to write; each row a list of cells.",
                       "items": {"type": "array", "items": {}}},
            "input_option": {"type": "string", "description": "USER_ENTERED (default) or RAW."},
        }, required=["range", "values"]),
    },
    "add_tab": {
        "description": "Add a new tab to a spreadsheet.",
        "write": True,
        "input": _input({"title": {"type": "string", "description": "Name for the new tab."}},
                        required=["title"]),
    },
    "create_spreadsheet": {
        "description": "Create a new spreadsheet owned by this Google account.",
        "write": True,
        "input": {
            "type": "object",
            "properties": {"title": {"type": "string", "description": "Name for the spreadsheet."}},
            "required": ["title"], "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "list_spreadsheets": list_spreadsheets,
    "find_spreadsheet": find_spreadsheet,
    "get_spreadsheet": get_spreadsheet,
    "list_tabs": list_tabs,
    "read_range": read_range,
    "read_table": read_table,
    "append_rows": append_rows,
    "update_range": update_range,
    "add_tab": add_tab,
    "create_spreadsheet": create_spreadsheet,
}


registry.register(
    Connector(
        slug="google_sheets",
        label="Google Sheets",
        auth="google_oauth",
        description=(
            "Read and write Google Sheets — list and search spreadsheets, read a "
            "range as rows or as records with the header as column names, append "
            "rows, update cells, and create tabs or whole spreadsheets."
        ),
        category="Content",
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            # Metadata only: names and ids, so listing works without granting
            # read access to every file in the account.
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        ],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
