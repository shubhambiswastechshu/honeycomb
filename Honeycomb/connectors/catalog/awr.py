"""Advanced Web Ranking (AWR) connector — keyword rank tracking via the AWR Developer API v2.

AWR exposes a single CGI endpoint (https://api.awrcloud.com/v2/get.php) whose behaviour is
selected with an ?action= query param. Authentication is a per-account API token passed as
?token=. Response shapes vary by action: projects / details / get_dates return a JSON v2
envelope ({"response_code":0,"details":{...}}); export_ranking returns JSON pointing at a
download URL whose body is a ZIP of per-keyword ranking JSON files. Helpers below are
deliberately tolerant of envelope differences and mirror the RAVEN sync originals.

Tools (all reads):
  - list_projects        action=projects        -> all AWR projects on this token
  - get_project          action=details         -> project detail (domain, engines, languages)
  - get_project_dates    action=get_dates       -> available snapshot dates
  - get_project_keywords action=details         -> tracked keywords (+ groups/priority)
  - get_rankings         action=export_ranking  -> per-keyword rank positions (latest snapshot)
  - rank_history         action=export_ranking  -> position over time per keyword
  - competitor_rankings  action=export_ranking  -> per-website ranking summary
  - serp_features        action=export_ranking  -> distribution of SERP result types
  - share_of_voice       action=export_ranking  -> CTR-weighted visibility share per website
  - top_movers           action=export_ranking  -> keywords with biggest position change
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from collections import Counter
from urllib.parse import urlparse

from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get
from connections.models import Connection

BASE = "https://api.awrcloud.com/v2/get.php"

# response_code values inside AWR's v2 JSON envelope that are NOT hard errors:
#   0  = OK
#   9  = no update dates in interval (benign empty state for get_dates / a fresh project)
#   10 = export already scheduled (handled by the export flow)
#   25 = export still generating (handled by the export download poller)
_OK_RESPONSE_CODES = {0, 9, 10, 25}
_EXPORT_IN_PROGRESS = 25

# Rough organic CTR-by-position curve for visibility / share-of-voice.
_CTR = {1: .32, 2: .17, 3: .11, 4: .08, 5: .06, 6: .05, 7: .04, 8: .03, 9: .025, 10: .02}

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _token(conn: Connection) -> str:
    token = conn.creds().get("api_token")
    if not token:
        raise ConnectorError("Not connected: missing api_token.")
    return token


def _host(url: str) -> str:
    return urlparse(url).hostname or "unknown"


# --------------------------------------------------------------------------- raw transport

async def _raw_get(conn: Connection, action: str, **params):
    """Issue a GET against the AWR endpoint, raising ConnectorError on hard failure.

    Returns the raw httpx.Response. AWR signals some errors as a plain-string body
    even on HTTP 200, so callers (and this helper) inspect the text too."""
    q = {"action": action, "token": _token(conn)}
    for k, v in params.items():
        if v is not None:
            q[k] = v
    try:
        async with limit_for(BASE):
            res = await http_get(BASE, params=q)
    except UpstreamUnavailable as e:
        raise ConnectorError(f"AWR {action} unavailable: {e}")
    if res.status_code >= 400:
        raise ConnectorError(f"AWR {action} failed {res.status_code}: {res.text[:300]}")
    txt = (res.text or "").strip()
    if txt[:6].upper() == "ERROR" or txt.startswith('{"error'):
        raise ConnectorError(f"AWR {action}: {txt[:300]}")
    return res


async def _json(conn: Connection, action: str, **params):
    """Fetch and parse the AWR v2 JSON envelope, surfacing nonzero response codes."""
    res = await _raw_get(conn, action, **params)
    try:
        data = res.json()
    except Exception:
        txt = (res.text or "").strip()
        try:
            data = json.loads(txt)
        except (ValueError, TypeError):
            return {"_raw": txt[:5000]}
    # AWR signals failures via a nonzero response_code on an HTTP 200 body
    # (e.g. 11/2 = invalid token, 14/15 = bad/missing project).
    if isinstance(data, dict):
        rc = data.get("response_code")
        if rc is not None and rc not in _OK_RESPONSE_CODES:
            msg = data.get("message") or f"response_code {rc}"
            raise ConnectorError(f"AWR {action}: {msg} (code {rc})")
    return data


# --------------------------------------------------------------------------- projects

def _normalize_project(p) -> dict:
    if isinstance(p, str):
        return {"name": p}
    if isinstance(p, dict):
        return {
            "name": p.get("name"),
            "id": p.get("id"),
            "frequency": p.get("frequency"),
            "depth": p.get("depth"),
            "keyword_count": p.get("kwcount") or p.get("keyword_count"),
            "main_website": p.get("main_website") or p.get("mainWebsite"),
            "last_updated": p.get("last_updated") or p.get("timestamp"),
        }
    return {"value": p}


async def _list_projects(conn: Connection) -> dict:
    async def _loader():
        data = await _json(conn, "projects")
        projects = data
        if isinstance(data, dict):
            # API v2 JSON envelope: {"response_code":0,"details":{"projects":[...]}}.
            # The plain/older form puts the array at the top level under "projects".
            details = data.get("details")
            if isinstance(details, dict):
                projects = details.get("projects")
            elif isinstance(details, list):
                projects = details
            else:
                projects = data.get("projects") or data.get("_raw")
        if isinstance(projects, str):  # _raw fallthrough
            return {"count": 0, "projects": [], "_raw": projects[:2000]}
        if isinstance(projects, dict):  # guard: never iterate a dict's keys
            projects = projects.get("projects") or []
        out = [_normalize_project(p) for p in (projects or [])]
        return {"count": len(out), "projects": out}

    return await cached("awr", conn.id, "list_projects", TTL_MEDIUM, _loader)


async def _resolve_project(conn: Connection, project=None, project_id=None) -> dict | None:
    projs = (await _list_projects(conn)).get("projects", [])
    if project_id is not None:
        for p in projs:
            if str(p.get("id")) == str(project_id):
                return p
    if project:
        for p in projs:
            if (p.get("name") or "").strip().lower() == str(project).strip().lower():
                return p
    return None


async def _details(conn: Connection, project: str) -> dict:
    if not project:
        raise ConnectorError("project (name) is required. Call list_projects first.")

    async def _loader():
        data = await _json(conn, "details", project=project)
        return data.get("details") if isinstance(data, dict) and "details" in data else data

    return await cached("awr", conn.id, "details", TTL_MEDIUM, _loader, args={"p": project})


# --------------------------------------------------------------------------- export download

def _date_from_name(name: str):
    m = _DATE_RE.search(name or "")
    return m.group(1) if m else None


def _rows_from_export_obj(j, date) -> list:
    """Flatten one AWR export JSON payload into per-result ranking rows.

    Documented v2 shape is keyword-level with a nested `rankdata` array:
        {"searchengine","depth","location","keyword",
         "rankdata":[{"position","url","typedescription","page"}, ...]}
    The keyword / search engine live on the parent and rows carry no date, so we
    lift those down and take the snapshot date from the export filename. Lists of
    such objects and the legacy flat-row shape are both still handled."""
    out = []
    if isinstance(j, list):
        for item in j:
            out.extend(_rows_from_export_obj(item, date))
        return out
    if not isinstance(j, dict):
        return out
    rankdata = j.get("rankdata")
    if isinstance(rankdata, list):
        kw = j.get("keyword")
        se = j.get("searchengine") or j.get("se")
        for rd in rankdata:
            if not isinstance(rd, dict):
                continue
            url = rd.get("url")
            out.append({
                "date": rd.get("date") or date,
                "se": se,
                "keyword": kw,
                "website": _host(url) if url else None,
                "url": url,
                "position": rd.get("position"),
                "page": rd.get("page"),
                "type": rd.get("typedescription") or rd.get("type"),
                "searches": rd.get("searches"),
                "cpc": rd.get("cpc"),
            })
        return out
    # Legacy/flat fallback: the dict is itself a row, or wraps row lists.
    if any(k in j for k in ("position", "se", "url")):
        row = dict(j)
        row.setdefault("date", date)
        out.append(row)
        return out
    for v in j.values():
        if isinstance(v, list):
            out.extend(_rows_from_export_obj(v, date))
    return out


def _extract_export_zip(content: bytes) -> dict:
    """Open the export zip and concatenate ranking rows from every JSON file inside
    (AWR writes one keyword-snapshot file per entry)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return {"rows": [], "_error": "could not open export zip"}
    rows = []
    for nm in zf.namelist():
        try:
            j = json.loads(zf.read(nm).decode("utf-8", "replace"))
        except Exception:
            continue
        rows.extend(_rows_from_export_obj(j, _date_from_name(nm)))
    return {"rows": rows}


async def _download_export(url: str, attempts: int = 6, delay: float = 2.5):
    """Poll an AWR export URL. While the async export builds it returns a JSON body
    with response_code 25; once ready it returns a ZIP of JSON files. We read raw
    bytes via res.content so the zip can be unpacked."""
    for i in range(attempts):
        try:
            async with limit_for(url):
                r = await http_get(url)
        except UpstreamUnavailable as e:
            raise ConnectorError(f"AWR export download failed: {e}")
        content = r.content or b""
        if content[:2] == b"PK":
            return _extract_export_zip(content)
        try:
            data = json.loads(content.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            return {"_raw": content[:2000].decode("utf-8", "replace")}
        if isinstance(data, dict) and data.get("response_code") == _EXPORT_IN_PROGRESS:
            if i < attempts - 1:
                await asyncio.sleep(delay)
                continue
            return {"_in_progress": True, "note": "Export still generating — call get_rankings again in ~30s."}
        return data
    return {"_in_progress": True, "note": "Export still generating — call get_rankings again in ~30s."}


# --------------------------------------------------------------------------- ranking rows

def _to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _norm_ranking(r: dict) -> dict:
    return {
        "date": r.get("date"),
        "search_engine": r.get("se"),
        "keyword": r.get("keyword"),
        "website": r.get("website"),
        "url": r.get("url"),
        "position": _to_int(r.get("position")),
        "page": _to_int(r.get("page")),
        "type": r.get("type"),
        "searches": _to_int(r.get("searches")),
        "cpc": r.get("cpc"),
    }


async def _get_project_dates(conn: Connection, project: str) -> dict:
    if not project:
        raise ConnectorError("project (name) is required. Call list_projects first.")
    data = await _json(conn, "get_dates", project=project)
    dates = data
    if isinstance(data, dict):
        if isinstance(data.get("details"), dict):
            dates = (data.get("details") or {}).get("dates")
        else:
            dates = data.get("dates", data)
    return {"project": project, "dates": dates}


async def _resolve_dates(conn: Connection, project: str, full: bool = False):
    raw = (await _get_project_dates(conn, project)).get("dates") or []
    ds = sorted([d.get("date") for d in raw if isinstance(d, dict) and d.get("date")])
    if not ds:
        return None, None
    stop = ds[-1]
    start = ds[0] if full else ds[-1]
    return start, stop


async def _fetch_ranking_rows(conn: Connection, project: str, start_date, stop_date,
                              search_engine_id=None) -> dict:
    """Run export_ranking, download+unzip, and return normalized rows. Returns
    {'in_progress': True} if AWR is still generating the export."""
    resp = await _json(conn, "export_ranking", project=project, startDate=start_date,
                       stopDate=stop_date, format="json", searchEngineId=search_engine_id)
    url = resp.get("details") if isinstance(resp, dict) else None
    if not (isinstance(url, str) and url.startswith("http")):
        return {"rows": [], "in_progress": False, "note": "Unexpected export response", "export_response": resp}
    data = await _download_export(url)
    if isinstance(data, dict) and data.get("_in_progress"):
        return {"rows": [], "in_progress": True}
    raw = data.get("rows", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    return {"rows": [_norm_ranking(r) for r in raw if isinstance(r, dict)], "in_progress": False}


async def _rows_or_progress(conn, project, start_date, stop_date, search_engine_id, full=False):
    if not project:
        raise ConnectorError("project (name) is required. Call list_projects first.")
    if not (start_date and stop_date):
        ds_start, ds_stop = await _resolve_dates(conn, project, full=full)
        start_date = start_date or ds_start
        stop_date = stop_date or ds_stop
    res = await _fetch_ranking_rows(conn, project, start_date, stop_date, search_engine_id)
    return res, start_date, stop_date


# --------------------------------------------------------------------------- tools

async def list_projects(conn: Connection, db, args: dict) -> dict:
    return await _list_projects(conn)


async def get_project(conn: Connection, db, args: dict) -> dict:
    project = (args or {}).get("project")
    return {"project": project, "details": await _details(conn, project)}


async def get_project_dates(conn: Connection, db, args: dict) -> dict:
    project = (args or {}).get("project")

    async def _loader():
        return await _get_project_dates(conn, project)

    return await cached("awr", conn.id, "get_project_dates", TTL_MEDIUM, _loader,
                        args={"p": project})


async def get_project_keywords(conn: Connection, db, args: dict) -> dict:
    """Keywords come from the `details` action (which returns a full keywords array) —
    reliable, unlike the get_keywords CSV export which returns empty for plain mode."""
    args = args or {}
    name = args.get("project")
    project_id = args.get("project_id")
    if not name and project_id is not None:
        match = await _resolve_project(conn, project_id=project_id)
        if match:
            name = match.get("name")
    if not name:
        raise ConnectorError("project (name) is required. Call list_projects first.")

    async def _loader():
        det = await _details(conn, name) or {}
        out = []
        for k in (det.get("keywords") or []):
            if isinstance(k, dict):
                out.append({
                    "keyword": k.get("name"),
                    "priority": k.get("priority"),
                    "added_on": k.get("added_on"),
                    "groups": k.get("kw_groups") or [],
                })
        return {"project": name, "keyword_count": len(out), "keywords": out}

    return await cached("awr", conn.id, "get_project_keywords", TTL_MEDIUM, _loader,
                        args={"p": name})


async def get_rankings(conn: Connection, db, args: dict) -> dict:
    """Per-keyword rank positions (latest snapshot by default), best position first."""
    args = args or {}
    project = args.get("project")
    start_date = args.get("start_date")
    stop_date = args.get("stop_date")
    search_engine_id = args.get("search_engine_id")
    limit = args.get("limit", 100)
    keyword = args.get("keyword")
    website = args.get("website")

    async def _loader():
        res, sd, ed = await _rows_or_progress(conn, project, start_date, stop_date, search_engine_id)
        if res.get("in_progress"):
            return {"project": project, "start_date": sd, "stop_date": ed, "in_progress": True,
                    "note": "Export still generating — call get_rankings again in ~30s."}
        rows = res["rows"]
        if keyword:
            rows = [r for r in rows if keyword.lower() in (r.get("keyword") or "").lower()]
        if website:
            rows = [r for r in rows if website.lower() in (r.get("website") or "").lower()]
        rows_sorted = sorted(rows, key=lambda r: (r["position"] is None, r["position"] or 9999))
        lim = max(1, min(int(limit or 100), 2000))
        return {"project": project, "start_date": sd, "stop_date": ed,
                "total_rows": len(rows), "returned": min(len(rows), lim),
                "rankings": rows_sorted[:lim]}

    cache_args = {"p": project, "sd": start_date, "ed": stop_date, "se": search_engine_id,
                  "limit": limit, "kw": keyword, "web": website}
    return await cached("awr", conn.id, "get_rankings", TTL_MEDIUM, _loader, args=cache_args)


async def rank_history(conn: Connection, db, args: dict) -> dict:
    """Position over time per keyword across the snapshots in range."""
    args = args or {}
    project = args.get("project")
    keyword = args.get("keyword")
    start_date = args.get("start_date")
    stop_date = args.get("stop_date")
    search_engine_id = args.get("search_engine_id")
    limit = args.get("limit", 50)

    async def _loader():
        res, sd, ed = await _rows_or_progress(conn, project, start_date, stop_date,
                                              search_engine_id, full=True)
        if res.get("in_progress"):
            return {"project": project, "in_progress": True, "note": "Export still generating — retry in ~30s."}
        hist: dict = {}
        for r in res["rows"]:
            kw = r.get("keyword")
            if keyword and keyword.lower() not in (kw or "").lower():
                continue
            hist.setdefault(kw, {})[r.get("date")] = r["position"]
        out = [{"keyword": k, "history": [{"date": d, "position": v[d]} for d in sorted(x for x in v if x)]}
               for k, v in hist.items()]
        return {"project": project, "start_date": sd, "stop_date": ed,
                "keyword_count": len(out), "keywords": out[:max(1, min(int(limit or 50), 1000))]}

    cache_args = {"p": project, "kw": keyword, "sd": start_date, "ed": stop_date, "se": search_engine_id}
    return await cached("awr", conn.id, "rank_history", TTL_MEDIUM, _loader, args=cache_args)


async def competitor_rankings(conn: Connection, db, args: dict) -> dict:
    """Per-website ranking summary (the project's site + any tracked competitors)."""
    args = args or {}
    project = args.get("project")
    start_date = args.get("start_date")
    stop_date = args.get("stop_date")
    search_engine_id = args.get("search_engine_id")

    async def _loader():
        res, sd, ed = await _rows_or_progress(conn, project, start_date, stop_date, search_engine_id)
        if res.get("in_progress"):
            return {"project": project, "in_progress": True, "note": "Export still generating — retry in ~30s."}
        by_site: dict = {}
        for r in res["rows"]:
            w = r.get("website") or "?"
            d = by_site.setdefault(w, {"website": w, "keywords": 0, "top3": 0, "top10": 0, "_pos": []})
            d["keywords"] += 1
            p = r["position"]
            if p:
                d["_pos"].append(p)
                if p <= 3:
                    d["top3"] += 1
                if p <= 10:
                    d["top10"] += 1
        out = []
        for d in by_site.values():
            ps = d.pop("_pos")
            d["avg_position"] = round(sum(ps) / len(ps), 1) if ps else None
            out.append(d)
        out.sort(key=lambda x: (x["avg_position"] is None, x["avg_position"] or 9999))
        return {"project": project, "start_date": sd, "stop_date": ed, "websites": out}

    cache_args = {"p": project, "sd": start_date, "ed": stop_date, "se": search_engine_id}
    return await cached("awr", conn.id, "competitor_rankings", TTL_MEDIUM, _loader, args=cache_args)


async def serp_features(conn: Connection, db, args: dict) -> dict:
    """Distribution of SERP result types (Organic, features) across tracked rankings."""
    args = args or {}
    project = args.get("project")
    start_date = args.get("start_date")
    stop_date = args.get("stop_date")
    search_engine_id = args.get("search_engine_id")

    async def _loader():
        res, sd, ed = await _rows_or_progress(conn, project, start_date, stop_date, search_engine_id)
        if res.get("in_progress"):
            return {"project": project, "in_progress": True, "note": "Export still generating — retry in ~30s."}
        c = Counter((r.get("type") or "Unknown") for r in res["rows"])
        page1 = sum(1 for r in res["rows"] if r.get("page") == 1)
        return {"project": project, "start_date": sd, "stop_date": ed,
                "total_rows": len(res["rows"]), "page1_results": page1,
                "by_type": dict(c.most_common())}

    cache_args = {"p": project, "sd": start_date, "ed": stop_date, "se": search_engine_id}
    return await cached("awr", conn.id, "serp_features", TTL_MEDIUM, _loader, args=cache_args)


async def share_of_voice(conn: Connection, db, args: dict) -> dict:
    """CTR-weighted visibility share per website, derived from tracked positions."""
    args = args or {}
    project = args.get("project")
    start_date = args.get("start_date")
    stop_date = args.get("stop_date")
    search_engine_id = args.get("search_engine_id")

    async def _loader():
        res, sd, ed = await _rows_or_progress(conn, project, start_date, stop_date, search_engine_id)
        if res.get("in_progress"):
            return {"project": project, "in_progress": True, "note": "Export still generating — retry in ~30s."}
        by_site: dict = {}
        for r in res["rows"]:
            p = r["position"]
            w = r.get("website") or "?"
            by_site[w] = by_site.get(w, 0.0) + (_CTR.get(p, 0.0) if p else 0.0)
        total = sum(by_site.values()) or 1.0
        out = [{"website": w, "visibility_score": round(v, 3), "share_pct": round(v / total * 100, 1)}
               for w, v in sorted(by_site.items(), key=lambda kv: -kv[1])]
        return {"project": project, "start_date": sd, "stop_date": ed,
                "method": "CTR-weighted visibility from tracked positions", "share_of_voice": out}

    cache_args = {"p": project, "sd": start_date, "ed": stop_date, "se": search_engine_id}
    return await cached("awr", conn.id, "share_of_voice", TTL_MEDIUM, _loader, args=cache_args)


async def top_movers(conn: Connection, db, args: dict) -> dict:
    """Keywords with the biggest position change between the first and last snapshot in range."""
    args = args or {}
    project = args.get("project")
    start_date = args.get("start_date")
    stop_date = args.get("stop_date")
    search_engine_id = args.get("search_engine_id")
    limit = args.get("limit", 25)

    async def _loader():
        res, sd, ed = await _rows_or_progress(conn, project, start_date, stop_date,
                                              search_engine_id, full=True)
        if res.get("in_progress"):
            return {"project": project, "in_progress": True, "note": "Export still generating — retry in ~30s."}
        by_kw: dict = {}
        for r in res["rows"]:
            if r["position"] is not None:
                by_kw.setdefault(r.get("keyword"), {})[r.get("date")] = r["position"]
        movers = []
        for kw, dd in by_kw.items():
            dates = sorted(d for d in dd if d)
            if len(dates) < 2:
                continue
            first, last = dd[dates[0]], dd[dates[-1]]
            movers.append({"keyword": kw, "from_position": first, "to_position": last,
                           "change": first - last})  # +ve = moved up (improved)
        movers.sort(key=lambda m: abs(m["change"]), reverse=True)
        note = None if movers else "Needs >=2 snapshot dates in range to compute movement."
        return {"project": project, "start_date": sd, "stop_date": ed,
                "movers": movers[:max(1, min(int(limit or 25), 500))], "note": note}

    cache_args = {"p": project, "sd": start_date, "ed": stop_date, "se": search_engine_id, "limit": limit}
    return await cached("awr", conn.id, "top_movers", TTL_MEDIUM, _loader, args=cache_args)


# --------------------------------------------------------------------------- catalog

_PROJECT_PROP = {"type": "string", "description": "AWR project name (from list_projects)."}
_DATE_PROPS = {
    "start_date": {"type": "string", "description": "Range start, YYYY-MM-DD. Defaults to the latest snapshot."},
    "stop_date": {"type": "string", "description": "Range end, YYYY-MM-DD. Defaults to the latest snapshot."},
    "search_engine_id": {"type": "string", "description": "Optional AWR search-engine id to filter to one engine."},
}

CATALOG = {
    "list_projects": {
        "description": "List all AWR projects accessible with this token (name, id, keyword count, main website).",
        "input": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_project": {
        "description": "Detail for a project: domain, search engines, languages, and configuration.",
        "input": {
            "type": "object",
            "properties": {"project": _PROJECT_PROP},
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    "get_project_dates": {
        "description": "Available data snapshot dates for a project.",
        "input": {
            "type": "object",
            "properties": {"project": _PROJECT_PROP},
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    "get_project_keywords": {
        "description": "All keywords tracked in a project, with their priority, added date, and groups.",
        "input": {
            "type": "object",
            "properties": {
                "project": _PROJECT_PROP,
                "project_id": {"type": "string", "description": "AWR project id (alternative to project name)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_rankings": {
        "description": "Per-keyword rank positions across configured search engines (latest snapshot by default).",
        "input": {
            "type": "object",
            "properties": {
                "project": _PROJECT_PROP,
                **_DATE_PROPS,
                "keyword": {"type": "string", "description": "Filter to keywords containing this substring."},
                "website": {"type": "string", "description": "Filter to results for websites containing this substring."},
                "limit": {"type": "integer", "description": "Max ranking rows to return (default 100, max 2000)."},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    "rank_history": {
        "description": "Historical position movement per keyword across the snapshots in range.",
        "input": {
            "type": "object",
            "properties": {
                "project": _PROJECT_PROP,
                "keyword": {"type": "string", "description": "Filter to keywords containing this substring."},
                **_DATE_PROPS,
                "limit": {"type": "integer", "description": "Max keywords to return (default 50, max 1000)."},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    "competitor_rankings": {
        "description": "Per-website ranking summary (avg position, top3/top10 counts) for the site and tracked competitors.",
        "input": {
            "type": "object",
            "properties": {"project": _PROJECT_PROP, **_DATE_PROPS},
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    "serp_features": {
        "description": "Distribution of SERP result types (organic, features) and page-1 counts across tracked rankings.",
        "input": {
            "type": "object",
            "properties": {"project": _PROJECT_PROP, **_DATE_PROPS},
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    "share_of_voice": {
        "description": "CTR-weighted visibility share per website, derived from tracked positions.",
        "input": {
            "type": "object",
            "properties": {"project": _PROJECT_PROP, **_DATE_PROPS},
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    "top_movers": {
        "description": "Keywords with the biggest position gains or losses between the first and last snapshot in range.",
        "input": {
            "type": "object",
            "properties": {
                "project": _PROJECT_PROP,
                **_DATE_PROPS,
                "limit": {"type": "integer", "description": "Max movers to return (default 25, max 500)."},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "list_projects": list_projects,
    "get_project": get_project,
    "get_project_dates": get_project_dates,
    "get_project_keywords": get_project_keywords,
    "get_rankings": get_rankings,
    "rank_history": rank_history,
    "competitor_rankings": competitor_rankings,
    "serp_features": serp_features,
    "share_of_voice": share_of_voice,
    "top_movers": top_movers,
}

registry.register(
    Connector(
        slug="awr",
        label="Advanced Web Ranking",
        auth="api_key",
        cred_fields=["api_token"],
        catalog=CATALOG,
        handlers=HANDLERS,
        description="Reads Advanced Web Ranking projects, tracked keywords, rank snapshots, SERP features and share of voice.",
        category="Search",
    )
)
