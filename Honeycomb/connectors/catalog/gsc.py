"""Google Search Console connector (Search Console API v3 + URL Inspection v1, google_oauth).

Ported faithfully from RAVEN's gsc_mcp deployed tool set. Exposes:

  Sites & sitemaps:
    - list_sites:       Search Console properties the account can access
    - get_site:         detail for a single property (permission level)
    - list_sitemaps:    submitted sitemaps for a site with status/errors
    - get_sitemap:      detail for a single sitemap
    - submit_sitemap:   submit a sitemap URL (write)
    - delete_sitemap:   remove a sitemap submission (write)

  Search analytics (searchAnalytics.query):
    - search_analytics:            generic query — dimensions/filters/date range
    - top_queries:                 top search queries by clicks
    - top_pages:                   top landing pages by clicks
    - query_by_country:            performance by country
    - query_by_device:             desktop / mobile / tablet split
    - query_by_date:               daily time series
    - query_by_search_appearance:  performance by Search Appearance
    - branded_vs_nonbranded:       branded vs non-branded buckets via brand regex
    - page_queries:                top queries surfacing a specific page
    - query_page_matrix:           query x page cross-tab

  URL inspection (urlInspection.index.inspect):
    - inspect_url:      indexing status, mobile usability, last crawl

All credentials come from conn.creds() (a Google refresh token), exchanged for a
short-lived access token before every call. Read-only tools are cached; sitemap
writes are not.
"""
import re
from datetime import date, timedelta
from urllib.parse import quote

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
    request as http_request,
)

SLUG = "gsc"
WEBMASTERS = "https://www.googleapis.com/webmasters/v3"
URL_INSPECT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _site_path(site_url: str) -> str:
    """URL-encode a property URL for use in a path segment.
    Works for both 'sc-domain:example.com' and 'https://example.com/'."""
    return quote(site_url or "", safe="")


def _su(args: dict) -> str:
    return (args or {}).get("site_url") or (args or {}).get("siteUrl") or ""


def _require_site(args: dict) -> str:
    site_url = _su(args)
    if not site_url:
        raise ConnectorError(
            "site_url is required (e.g. 'sc-domain:example.com' or "
            "'https://example.com/'). Call list_sites to find yours."
        )
    return site_url


def _days(args: dict, default: int = 28) -> int:
    try:
        return max(1, min(int((args or {}).get("days", default)), 3650))
    except (TypeError, ValueError):
        return default


def _limit(args: dict, default: int = 25, max_value: int = 1000) -> int:
    try:
        return max(1, min(int((args or {}).get("limit", default)), max_value))
    except (TypeError, ValueError):
        return default


def _date_window(days: int = 28, start_date=None, end_date=None) -> tuple[str, str]:
    if start_date and end_date:
        return start_date, end_date
    end = date.today()
    start = end - timedelta(days=max(1, int(days or 28)))
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Core Search Analytics call (ported from services.run_search_analytics)
# ---------------------------------------------------------------------------
async def _run_search_analytics(
    conn: Connection,
    db,
    *,
    site_url: str,
    dimensions: list,
    days: int = 28,
    limit: int = 25,
    search_type: str = "web",
    filters: list | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """searchAnalytics.query. Most GSC tools wrap this with different dimensions/filters."""
    if not site_url:
        raise ConnectorError(
            "site_url is required (e.g. 'sc-domain:example.com' or "
            "'https://example.com/'). Call list_sites to find yours."
        )
    sd, ed = _date_window(days, start_date, end_date)
    dims = list(dimensions or [])
    body = {
        "startDate": sd,
        "endDate": ed,
        "dimensions": dims,
        "rowLimit": min(max(1, int(limit or 25)), 25000),
        "type": (search_type or "web"),
    }
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]

    async def _load() -> dict:
        token = await _access_token(conn, db)
        url = f"{WEBMASTERS}/sites/{_site_path(site_url)}/searchAnalytics/query"
        try:
            async with limit_for(url):
                res = await http_post(url, headers=_bearer(token), json=body)
        except UpstreamUnavailable as e:
            raise ConnectorError(f"searchAnalytics unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(f"searchAnalytics failed {res.status_code}: {res.text[:400]}")
        out = []
        for r in (res.json() or {}).get("rows", []) or []:
            keys = r.get("keys") or []
            item = {
                "clicks": r.get("clicks", 0),
                "impressions": r.get("impressions", 0),
                "ctr": round((r.get("ctr", 0) or 0) * 100, 2),
                "position": round(r.get("position", 0) or 0, 1),
            }
            for i, dim in enumerate(dims):
                item[dim] = keys[i] if i < len(keys) else None
            out.append(item)
        return {
            "site_url": site_url,
            "start_date": sd,
            "end_date": ed,
            "dimensions": dims,
            "search_type": body["type"],
            "row_count": len(out),
            "rows": out,
        }

    cache_args = {
        "s": site_url, "d": dims, "sd": sd, "ed": ed,
        "lim": body["rowLimit"], "t": body["type"], "f": filters,
    }
    return await cached(SLUG, conn.id, "search_analytics", TTL_SHORT, _load, args=cache_args)


# ---------------------------------------------------------------------------
# Search analytics tools
# ---------------------------------------------------------------------------
async def search_analytics(conn: Connection, db, args: dict) -> dict:
    dims = (args or {}).get("dimensions") or ["query"]
    if isinstance(dims, str):
        dims = [d.strip() for d in dims.split(",") if d.strip()]
    return await _run_search_analytics(
        conn, db,
        site_url=_require_site(args), dimensions=dims,
        days=_days(args), limit=_limit(args, 25, 25000),
        search_type=(args or {}).get("search_type", "web"),
        filters=(args or {}).get("filters"),
        start_date=(args or {}).get("start_date"),
        end_date=(args or {}).get("end_date"),
    )


async def top_queries(conn: Connection, db, args: dict) -> dict:
    return await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["query"],
        days=_days(args), limit=_limit(args),
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )


async def top_pages(conn: Connection, db, args: dict) -> dict:
    return await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["page"],
        days=_days(args), limit=_limit(args),
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )


async def query_by_country(conn: Connection, db, args: dict) -> dict:
    return await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["country"],
        days=_days(args), limit=_limit(args, 250, 250),
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )


async def query_by_device(conn: Connection, db, args: dict) -> dict:
    return await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["device"],
        days=_days(args), limit=10,
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )


async def query_by_date(conn: Connection, db, args: dict) -> dict:
    return await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["date"],
        days=_days(args), limit=_limit(args, 480, 480),
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )


async def query_by_search_appearance(conn: Connection, db, args: dict) -> dict:
    return await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["searchAppearance"],
        days=_days(args), limit=_limit(args, 50, 250),
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )


async def query_page_matrix(conn: Connection, db, args: dict) -> dict:
    return await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["query", "page"],
        days=_days(args), limit=_limit(args, 100, 5000),
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )


async def page_queries(conn: Connection, db, args: dict) -> dict:
    page = (args or {}).get("page_url") or (args or {}).get("page")
    if not page:
        raise ConnectorError("page_url is required (the page whose queries you want).")
    filters = [{"dimension": "page", "operator": "equals", "expression": page}]
    return await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["query"],
        days=_days(args), limit=_limit(args), filters=filters,
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )


async def branded_vs_nonbranded(conn: Connection, db, args: dict) -> dict:
    brand = (args or {}).get("brand_regex") or (args or {}).get("brand")
    if not brand:
        raise ConnectorError("brand_regex is required (e.g. 'techshu|tech shu').")
    try:
        rx = re.compile(brand, re.IGNORECASE)
    except re.error as e:
        raise ConnectorError(f"Invalid brand_regex: {e}")
    data = await _run_search_analytics(
        conn, db, site_url=_require_site(args), dimensions=["query"],
        days=_days(args), limit=5000,
        start_date=(args or {}).get("start_date"), end_date=(args or {}).get("end_date"),
    )
    buckets = {
        "branded": {"clicks": 0, "impressions": 0, "queries": 0},
        "non_branded": {"clicks": 0, "impressions": 0, "queries": 0},
    }
    for r in data["rows"]:
        b = buckets["branded"] if rx.search(r.get("query") or "") else buckets["non_branded"]
        b["clicks"] += r["clicks"]
        b["impressions"] += r["impressions"]
        b["queries"] += 1
    for b in buckets.values():
        b["ctr"] = round(b["clicks"] / b["impressions"] * 100, 2) if b["impressions"] else 0.0
    return {
        "site_url": data["site_url"], "start_date": data["start_date"],
        "end_date": data["end_date"], "brand_regex": brand, **buckets,
    }


# ---------------------------------------------------------------------------
# Sites & sitemaps
# ---------------------------------------------------------------------------
async def list_sites(conn: Connection, db, args: dict) -> dict:
    async def _loader():
        token = await _access_token(conn, db)
        url = f"{WEBMASTERS}/sites"
        try:
            async with limit_for(url):
                res = await http_get(url, headers=_bearer(token))
        except UpstreamUnavailable as e:
            raise ConnectorError(f"list sites unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(f"list sites failed {res.status_code}: {res.text[:300]}")
        out = []
        for entry in (res.json() or {}).get("siteEntry", []) or []:
            out.append({
                "site_url": entry.get("siteUrl"),
                "permission_level": entry.get("permissionLevel"),
            })
        return {"count": len(out), "sites": out}

    return await cached(SLUG, conn.id, "list_sites", TTL_LONG, _loader, args={})


async def get_site(conn: Connection, db, args: dict) -> dict:
    site_url = _require_site(args)

    async def _loader():
        token = await _access_token(conn, db)
        url = f"{WEBMASTERS}/sites/{_site_path(site_url)}"
        try:
            async with limit_for(url):
                res = await http_get(url, headers=_bearer(token))
        except UpstreamUnavailable as e:
            raise ConnectorError(f"get_site unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(f"get_site failed {res.status_code}: {res.text[:300]}")
        d = res.json() or {}
        return {
            "site_url": d.get("siteUrl", site_url),
            "permission_level": d.get("permissionLevel", ""),
        }

    return await cached(SLUG, conn.id, "get_site", TTL_LONG, _loader, args={"s": site_url})


def _serialize_sitemap(s: dict) -> dict:
    return {
        "path": s.get("path"),
        "type": s.get("type"),
        "last_submitted": s.get("lastSubmitted"),
        "last_downloaded": s.get("lastDownloaded"),
        "is_pending": s.get("isPending"),
        "is_sitemaps_index": s.get("isSitemapsIndex"),
        "warnings": s.get("warnings"),
        "errors": s.get("errors"),
        "contents": s.get("contents"),
    }


async def list_sitemaps(conn: Connection, db, args: dict) -> dict:
    site_url = _require_site(args)

    async def _loader():
        token = await _access_token(conn, db)
        url = f"{WEBMASTERS}/sites/{_site_path(site_url)}/sitemaps"
        try:
            async with limit_for(url):
                res = await http_get(url, headers=_bearer(token))
        except UpstreamUnavailable as e:
            raise ConnectorError(f"list_sitemaps unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(f"list_sitemaps failed {res.status_code}: {res.text[:300]}")
        items = [_serialize_sitemap(s) for s in (res.json() or {}).get("sitemap", []) or []]
        return {"site_url": site_url, "count": len(items), "sitemaps": items}

    return await cached(SLUG, conn.id, "list_sitemaps", TTL_MEDIUM, _loader, args={"s": site_url})


async def get_sitemap(conn: Connection, db, args: dict) -> dict:
    site_url = _require_site(args)
    feedpath = (args or {}).get("feedpath") or (args or {}).get("sitemap_url")
    if not feedpath:
        raise ConnectorError("feedpath (sitemap URL) is required.")

    async def _loader():
        token = await _access_token(conn, db)
        url = f"{WEBMASTERS}/sites/{_site_path(site_url)}/sitemaps/{_site_path(feedpath)}"
        try:
            async with limit_for(url):
                res = await http_get(url, headers=_bearer(token))
        except UpstreamUnavailable as e:
            raise ConnectorError(f"get_sitemap unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(f"get_sitemap failed {res.status_code}: {res.text[:300]}")
        return _serialize_sitemap(res.json() or {})

    return await cached(
        SLUG, conn.id, "get_sitemap", TTL_MEDIUM, _loader,
        args={"s": site_url, "f": feedpath},
    )


async def submit_sitemap(conn: Connection, db, args: dict) -> dict:
    site_url = _require_site(args)
    feedpath = (args or {}).get("feedpath") or (args or {}).get("sitemap_url")
    if not feedpath:
        raise ConnectorError("feedpath (sitemap URL) is required.")
    token = await _access_token(conn, db)
    url = f"{WEBMASTERS}/sites/{_site_path(site_url)}/sitemaps/{_site_path(feedpath)}"
    try:
        async with limit_for(url):
            res = await http_request("PUT", url, headers=_bearer(token))
    except UpstreamUnavailable as e:
        raise ConnectorError(f"submit_sitemap unavailable: {e}")
    if res.status_code >= 400:
        raise ConnectorError(f"submit_sitemap failed {res.status_code}: {res.text[:300]}")
    return {"submitted": feedpath, "site_url": site_url, "status": "ok"}


async def delete_sitemap(conn: Connection, db, args: dict) -> dict:
    site_url = _require_site(args)
    feedpath = (args or {}).get("feedpath") or (args or {}).get("sitemap_url")
    if not feedpath:
        raise ConnectorError("feedpath (sitemap URL) is required.")
    token = await _access_token(conn, db)
    url = f"{WEBMASTERS}/sites/{_site_path(site_url)}/sitemaps/{_site_path(feedpath)}"
    try:
        async with limit_for(url):
            res = await http_request("DELETE", url, headers=_bearer(token))
    except UpstreamUnavailable as e:
        raise ConnectorError(f"delete_sitemap unavailable: {e}")
    if res.status_code >= 400:
        raise ConnectorError(f"delete_sitemap failed {res.status_code}: {res.text[:300]}")
    return {"deleted": feedpath, "site_url": site_url, "status": "ok"}


# ---------------------------------------------------------------------------
# URL inspection
# ---------------------------------------------------------------------------
async def inspect_url(conn: Connection, db, args: dict) -> dict:
    site_url = _require_site(args)
    inspection_url = (
        (args or {}).get("inspection_url")
        or (args or {}).get("page_url")
        or (args or {}).get("url")
    )
    if not inspection_url:
        raise ConnectorError("inspection_url (the page to inspect) is required.")

    async def _loader():
        token = await _access_token(conn, db)
        body = {"inspectionUrl": inspection_url, "siteUrl": site_url}
        try:
            async with limit_for(URL_INSPECT):
                res = await http_post(URL_INSPECT, headers=_bearer(token), json=body)
        except UpstreamUnavailable as e:
            raise ConnectorError(f"inspect_url unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(f"inspect_url failed {res.status_code}: {res.text[:400]}")
        result = (res.json() or {}).get("inspectionResult", {}) or {}
        idx = result.get("indexStatusResult", {}) or {}
        mobile = result.get("mobileUsabilityResult", {}) or {}
        rich = result.get("richResultsResult", {}) or {}
        return {
            "inspection_url": inspection_url,
            "verdict": idx.get("verdict"),
            "coverage_state": idx.get("coverageState"),
            "robots_txt_state": idx.get("robotsTxtState"),
            "indexing_state": idx.get("indexingState"),
            "last_crawl_time": idx.get("lastCrawlTime"),
            "page_fetch_state": idx.get("pageFetchState"),
            "google_canonical": idx.get("googleCanonical"),
            "user_canonical": idx.get("userCanonical"),
            "sitemaps": idx.get("sitemap"),
            "referring_urls": idx.get("referringUrls"),
            "mobile_usability_verdict": mobile.get("verdict"),
            "rich_results_verdict": rich.get("verdict"),
            "inspection_link": result.get("inspectionResultLink"),
        }

    return await cached(
        SLUG, conn.id, "inspect_url", TTL_SHORT, _loader,
        args={"s": site_url, "u": inspection_url},
    )


# ---------------------------------------------------------------------------
# Catalog / registration
# ---------------------------------------------------------------------------
_SITE_PROP = {
    "type": "string",
    "description": "Property URL, e.g. 'https://example.com/' or 'sc-domain:example.com'. Call list_sites to find yours.",
}
_DAYS_PROP = {"type": "integer", "description": "Lookback window in days (1-3650). Default 28. Ignored if start_date & end_date given."}
_START_PROP = {"type": "string", "description": "YYYY-MM-DD. Overrides days when paired with end_date."}
_END_PROP = {"type": "string", "description": "YYYY-MM-DD. Overrides days when paired with start_date."}
_LIMIT_PROP = {"type": "integer", "description": "Max rows to return."}

CATALOG = {
    "list_sites": {
        "description": "List Search Console properties the connected account can access.",
        "input": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_site": {
        "description": "Detailed info for a single Search Console property (permission level, type).",
        "input": {
            "type": "object",
            "properties": {"site_url": _SITE_PROP},
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "list_sitemaps": {
        "description": "All submitted sitemaps for a site with status, warnings, and errors.",
        "input": {
            "type": "object",
            "properties": {"site_url": _SITE_PROP},
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "get_sitemap": {
        "description": "Details for a single sitemap (last submitted/downloaded, warnings, errors, contents).",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "feedpath": {"type": "string", "description": "Full sitemap URL, e.g. 'https://example.com/sitemap.xml'."},
            },
            "required": ["site_url", "feedpath"],
            "additionalProperties": False,
        },
    },
    "submit_sitemap": {
        "description": "Submit a sitemap URL to Search Console.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "feedpath": {"type": "string", "description": "Full sitemap URL to submit, e.g. 'https://example.com/sitemap.xml'."},
            },
            "required": ["site_url", "feedpath"],
            "additionalProperties": False,
        },
        "write": True,
    },
    "delete_sitemap": {
        "description": "Remove a sitemap submission from Search Console.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "feedpath": {"type": "string", "description": "Full sitemap URL to remove."},
            },
            "required": ["site_url", "feedpath"],
            "additionalProperties": False,
        },
        "write": True,
    },
    "search_analytics": {
        "description": "Generic Search Analytics query (clicks, impressions, ctr, position) — pass dimensions, filters, date range.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Dimensions to group by, e.g. ['query','page','country','device','date','searchAppearance']. Default ['query'].",
                },
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": {"type": "integer", "description": "Max rows (1-25000). Default 25."},
                "search_type": {"type": "string", "description": "Search type: 'web', 'image', 'video', 'news', 'discover', 'googleNews'. Default 'web'."},
                "filters": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Dimension filters, e.g. [{'dimension':'page','operator':'equals','expression':'https://...'}].",
                },
            },
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "top_queries": {
        "description": "Top search queries with clicks, impressions, CTR, position.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": {**_LIMIT_PROP, "description": "Number of queries to return. Default 25."},
            },
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "top_pages": {
        "description": "Top landing pages by clicks over a date range.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": {**_LIMIT_PROP, "description": "Number of pages to return. Default 25."},
            },
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "query_by_country": {
        "description": "Search performance broken down by country.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": {**_LIMIT_PROP, "description": "Max countries (1-250). Default 25."},
            },
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "query_by_device": {
        "description": "Search performance split across desktop, mobile, and tablet.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
            },
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "query_by_date": {
        "description": "Daily time series of clicks, impressions, CTR, and position.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": {**_LIMIT_PROP, "description": "Max days (1-480). Default 480."},
            },
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "query_by_search_appearance": {
        "description": "Performance by Search Appearance (AMP, rich result types, etc.).",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": {**_LIMIT_PROP, "description": "Max rows (1-250). Default 50."},
            },
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "branded_vs_nonbranded": {
        "description": "Split traffic into branded vs non-branded buckets given a brand regex.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "brand_regex": {"type": "string", "description": "Case-insensitive regex matching brand terms, e.g. 'techshu|tech shu'."},
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
            },
            "required": ["site_url", "brand_regex"],
            "additionalProperties": False,
        },
    },
    "page_queries": {
        "description": "Top queries that surface a specific page.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "page_url": {"type": "string", "description": "The exact page URL whose queries you want."},
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": {**_LIMIT_PROP, "description": "Number of queries to return. Default 25."},
            },
            "required": ["site_url", "page_url"],
            "additionalProperties": False,
        },
    },
    "query_page_matrix": {
        "description": "Cross-tab of queries x pages with clicks, impressions, CTR, position.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": {**_LIMIT_PROP, "description": "Max rows (1-5000). Default 100."},
            },
            "required": ["site_url"],
            "additionalProperties": False,
        },
    },
    "inspect_url": {
        "description": "Inspect a single URL: indexing status, mobile usability, rich results, last crawl.",
        "input": {
            "type": "object",
            "properties": {
                "site_url": _SITE_PROP,
                "inspection_url": {"type": "string", "description": "The full page URL to inspect (must belong to the property)."},
            },
            "required": ["site_url", "inspection_url"],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "list_sites": list_sites,
    "get_site": get_site,
    "list_sitemaps": list_sitemaps,
    "get_sitemap": get_sitemap,
    "submit_sitemap": submit_sitemap,
    "delete_sitemap": delete_sitemap,
    "search_analytics": search_analytics,
    "top_queries": top_queries,
    "top_pages": top_pages,
    "query_by_country": query_by_country,
    "query_by_device": query_by_device,
    "query_by_date": query_by_date,
    "query_by_search_appearance": query_by_search_appearance,
    "branded_vs_nonbranded": branded_vs_nonbranded,
    "page_queries": page_queries,
    "query_page_matrix": query_page_matrix,
    "inspect_url": inspect_url,
}

registry.register(
    Connector(
        slug=SLUG,
        label="Google Search Console",
        auth="google_oauth",
        description="Reads Search Console properties, sitemaps, search analytics and URL inspection results, and can submit or remove sitemap submissions.",
        category="Search",
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
