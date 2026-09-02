"""Meta Ad Library connector (Graph API `ads_archive`).

Lets users query Meta's public Ad Library for ads by keyword, advertiser Page ID,
or political/issue status, plus competitive rollups. auth=api_key — the user
supplies a Graph API `access_token` (a Meta app/user token with ads_archive
access), passed as a query param.

API reality checks (ported from RAVEN):
- `ads_archive` does NOT expose creative media (image/video) CDN URLs, nor page
  metadata (verified / category / likes). Those live only on the Ad Library
  *website*. So `media_urls` is always [] and resolve_page's page metadata is
  null — surfaced honestly via `warnings`, not faked.
- `spend` / `impressions` / demographics → political ads only.
- `eu_total_reach` → EU-delivered ads only (DSA transparency).

Docs: https://developers.facebook.com/docs/graph-api/reference/ads_archive/
"""
import json
import re
from urllib.parse import urlparse

from connectors import registry
from connectors.registry import Connector
from connections.models import Connection
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get
from connectors.shims.cache import TTL_MEDIUM, cached
from connectors.shims.concurrency import limit_for

GRAPH_VERSION = "v23.0"
BASE = f"https://graph.facebook.com/{GRAPH_VERSION}/ads_archive"

# Fields available for ALL ad types (commercial competitors included).
_COMMON_FIELDS = (
    "id,page_id,page_name,ad_creation_time,ad_delivery_start_time,"
    "ad_delivery_stop_time,ad_creative_bodies,ad_creative_link_titles,"
    "ad_creative_link_captions,ad_creative_link_descriptions,ad_snapshot_url,"
    "publisher_platforms,languages,eu_total_reach"
)
# Extra fields that only populate for political / issue / social ads.
# NB: `funding_entity` was deprecated in Graph API v13.0+ — `bylines` (the
# "Paid for by" disclaimer) is its replacement.
_POLITICAL_FIELDS = _COMMON_FIELDS + (
    ",spend,impressions,currency,demographic_distribution,"
    "delivery_by_region,bylines,estimated_audience_size"
)

# EU / EEA member states — ads delivered here return eu_total_reach + richer
# commercial data thanks to the Digital Services Act transparency rules.
_EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------
# Auth + helpers
# ------------------------------------------------------------------
def _token(conn: Connection) -> str:
    creds = conn.creds()
    token = creds.get("access_token")
    if not token:
        raise ConnectorError("Not connected: missing access_token.")
    return token


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.replace(",", " ").split() if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def _json_list(items) -> str:
    """ads_archive wants array params as a JSON-style string, e.g. ['US','GB']."""
    return json.dumps(list(items))


def _countries(conn: Connection, args: dict) -> list[str]:
    raw = (args or {}).get("ad_reached_countries") or (args or {}).get("countries")
    if not raw:
        raw = conn.creds().get("default_countries")
    codes = _as_list(raw) or ["US"]
    return [c.upper()[:2] for c in codes]


def _limit(args: dict, default: int = 25, max_value: int = 100) -> int:
    try:
        n = int((args or {}).get("limit", default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, max_value))


def _dedupe(seq) -> list:
    """Unique values, order preserved."""
    seen = set()
    out = []
    for x in seq or []:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _date_arg(args: dict, key: str):
    v = str((args or {}).get(key, "")).strip()
    if not v:
        return None
    if not _DATE_RE.match(v):
        raise ConnectorError(f"{key} must be YYYY-MM-DD (got '{v}').")
    return v


def _available_metrics(countries: list[str], political: bool) -> tuple[list, list]:
    """What metric fields the caller can expect to be populated, and why some
    are missing — so empty fields aren't a mystery."""
    metrics: list[str] = []
    warnings: list[str] = []
    if any(c in _EU_COUNTRIES for c in countries):
        metrics.append("eu_total_reach")
    else:
        warnings.append("Reach (eu_total_reach) is only returned for ads delivered in the EU/EEA.")
    if political:
        metrics += ["spend", "impressions", "demographic_distribution", "delivery_by_region"]
    else:
        warnings.append("Spend, impressions and demographics are only available for political/issue ads — use search_political_ads.")
    warnings.append("Creative media (image/video) URLs are not exposed by Meta's Ad Library API — open snapshot_url or library_url to see the visual.")
    return metrics, warnings


def _raise_graph_error(res) -> None:
    """Surface a Graph API error.message when the upstream returns >= 400."""
    msg = ""
    try:
        body = res.json()
        err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
        msg = err.get("message") or res.text[:300]
        code = err.get("code", "?")
        msg = f"{msg} (code={code})"
    except (ValueError, AttributeError):
        msg = res.text[:300] if res.text else f"HTTP {res.status_code}"
    raise ConnectorError(f"Meta Ad Library error {res.status_code}: {msg}")


async def _request(token: str, params: dict) -> dict:
    """Single GET to /ads_archive. Returns the parsed JSON envelope."""
    p = dict(params)
    p["access_token"] = token
    try:
        async with limit_for(BASE):
            res = await http_get(BASE, params=p)
    except UpstreamUnavailable as e:
        raise ConnectorError(f"Meta Ad Library API unavailable: {e}")
    if res.status_code >= 400:
        _raise_graph_error(res)
    return res.json() or {}


async def _follow(token: str, url: str) -> dict:
    """Follow a paging.next URL (already carries access_token + cursor)."""
    try:
        async with limit_for(url):
            res = await http_get(url)
    except UpstreamUnavailable as e:
        raise ConnectorError(f"Meta Ad Library API unavailable: {e}")
    if res.status_code >= 400:
        raise ConnectorError(f"Meta Ad Library API {res.status_code} while paginating.")
    return res.json() or {}


async def _search_page(token: str, params: dict, after: str | None = None) -> tuple[list, str | None]:
    """One page of results + the next cursor (None when no more pages)."""
    p = dict(params)
    if after:
        p["after"] = after
    data = await _request(token, p)
    rows = list(data.get("data", []) or [])
    paging = data.get("paging") or {}
    next_cursor = (paging.get("cursors") or {}).get("after")
    has_next = bool(paging.get("next"))
    return rows, (next_cursor if has_next else None)


async def _collect(token: str, params: dict, want: int, max_pages: int = 6) -> list[dict]:
    """Fetch up to `want` rows, following pagination (capped at max_pages)."""
    data = await _request(token, params)
    rows = list(data.get("data", []) or [])
    pages = 1
    while len(rows) < want and pages < max_pages:
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        data = await _follow(token, nxt)
        rows.extend(data.get("data", []) or [])
        pages += 1
    return rows[:want]


def _parse_ad(r: dict, political: bool = False, media_type: str | None = None) -> dict:
    bodies = r.get("ad_creative_bodies", []) or []
    titles = r.get("ad_creative_link_titles", []) or []
    descs = r.get("ad_creative_link_descriptions", []) or []
    # number of source creative cards collapsed (carousel / DCO variants)
    variant_count = max(len(bodies), len(titles), len(descs), 1)
    ad_id = r.get("id", "")
    out = {
        "id": ad_id,
        "page_id": str(r.get("page_id", "")),
        "page_name": r.get("page_name", ""),
        "created": r.get("ad_creation_time", ""),
        "started": r.get("ad_delivery_start_time", ""),
        "stopped": r.get("ad_delivery_stop_time", "") or "(still active)",
        "body": _dedupe(bodies),
        "link_titles": _dedupe(titles),
        "link_descriptions": _dedupe(descs),
        "ad_variant_count": variant_count,
        "platforms": r.get("publisher_platforms", []) or [],
        "languages": r.get("languages", []) or [],
        "media_type": media_type or "unknown",   # API doesn't return media type
        "media_urls": [],                          # API doesn't expose creative CDN URLs
        "snapshot_url": r.get("ad_snapshot_url", ""),
        # token-free, non-expiring permalink — safe to keep in saved reports
        "library_url": f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else "",
    }
    if r.get("eu_total_reach") is not None:
        out["eu_total_reach"] = r.get("eu_total_reach")
    if political:
        out["spend"] = r.get("spend")
        out["impressions"] = r.get("impressions")
        out["currency"] = r.get("currency", "")
        out["bylines"] = r.get("bylines", "")  # "Paid for by" — replaces funding_entity
        out["estimated_audience_size"] = r.get("estimated_audience_size")
        if r.get("demographic_distribution") is not None:
            out["demographic_distribution"] = r.get("demographic_distribution")
        if r.get("delivery_by_region") is not None:
            out["delivery_by_region"] = r.get("delivery_by_region")
    return out


def _sort_and_filter(ads: list[dict], args: dict) -> list[dict]:
    """Apply min_reach filter + order_by sort (client-side, on the returned set)."""
    mr = (args or {}).get("min_reach")
    if mr is not None:
        try:
            threshold = int(mr)
            ads = [a for a in ads if int(a.get("eu_total_reach") or 0) >= threshold]
        except (TypeError, ValueError):
            pass
    order = str((args or {}).get("order_by", "date_desc")).lower().strip()
    if order == "date_asc":
        ads = sorted(ads, key=lambda a: a.get("started", ""))
    elif order == "reach_desc":
        ads = sorted(ads, key=lambda a: int(a.get("eu_total_reach") or 0), reverse=True)
    else:  # date_desc (default)
        ads = sorted(ads, key=lambda a: a.get("started", ""), reverse=True)
    return ads


def _date_params(args: dict) -> dict:
    """Native ads_archive delivery-date filters."""
    out = {}
    df = _date_arg(args, "date_from")
    dt = _date_arg(args, "date_to")
    if df:
        out["ad_delivery_date_min"] = df
    if dt:
        out["ad_delivery_date_max"] = dt
    return out


def _media_hint(args: dict):
    m = str((args or {}).get("media_type", "")).lower().strip()
    return m if m in {"image", "video", "meme"} else None


def _cache_args(extra: dict) -> dict:
    """Stable, JSON-serializable cache key fragment."""
    return {k: v for k, v in extra.items() if v is not None}


# ============================================================
# TOOL HANDLERS  —  async def fn(conn, db, args) -> dict
# ============================================================
async def search_ads(conn: Connection, db, args: dict) -> dict:
    terms = str((args or {}).get("search_terms", "")).strip()
    if not terms:
        raise ConnectorError("search_terms is required (a keyword, brand, or phrase).")
    countries = _countries(conn, args)
    limit = _limit(args)
    ad_type = str((args or {}).get("ad_type", "ALL")).upper().strip() or "ALL"
    status = str((args or {}).get("ad_active_status", "ACTIVE")).upper().strip() or "ACTIVE"
    token = _token(conn)

    async def _loader():
        params = {
            "search_terms": terms,
            "ad_reached_countries": _json_list(countries),
            "ad_type": ad_type,
            "ad_active_status": status,
            "fields": _COMMON_FIELDS,
            "limit": limit,
            **_date_params(args),
        }
        media = str((args or {}).get("media_type", "")).upper().strip()
        if media:
            params["media_type"] = media
        rows, next_token = await _search_page(token, params, after=(args or {}).get("page_token"))
        ads = _sort_and_filter([_parse_ad(r, media_type=_media_hint(args)) for r in rows], args)
        metrics, warnings = _available_metrics(countries, political=False)
        return {
            "query": terms, "countries": countries, "ad_type": ad_type,
            "ad_active_status": status, "available_metrics": metrics, "warnings": warnings,
            "count": len(ads), "ads": ads, "next_page_token": next_token,
        }

    cache_args = _cache_args({
        "search_terms": terms, "countries": countries, "ad_type": ad_type,
        "ad_active_status": status, "limit": limit,
        "order_by": (args or {}).get("order_by"), "min_reach": (args or {}).get("min_reach"),
        "media_type": (args or {}).get("media_type"), "page_token": (args or {}).get("page_token"),
        "date_from": (args or {}).get("date_from"), "date_to": (args or {}).get("date_to"),
    })
    return await cached("meta_ad_library", conn.id, "search_ads", TTL_MEDIUM, _loader, args=cache_args)


async def search_page_ads(conn: Connection, db, args: dict) -> dict:
    page_ids = _as_list((args or {}).get("page_ids") or (args or {}).get("page_id"))
    if not page_ids:
        raise ConnectorError(
            "page_ids is required — the numeric Facebook Page ID(s) of the advertiser. "
            "Use resolve_page to turn a brand name into a Page ID."
        )
    countries = _countries(conn, args)
    limit = _limit(args, default=50)
    status = str((args or {}).get("ad_active_status", "ACTIVE")).upper().strip() or "ACTIVE"
    token = _token(conn)

    async def _loader():
        params = {
            "search_page_ids": _json_list(page_ids),
            "ad_reached_countries": _json_list(countries),
            "ad_type": "ALL",
            "ad_active_status": status,
            "fields": _COMMON_FIELDS,
            "limit": limit,
            **_date_params(args),
        }
        rows, next_token = await _search_page(token, params, after=(args or {}).get("page_token"))
        ads = _sort_and_filter([_parse_ad(r, media_type=_media_hint(args)) for r in rows], args)
        metrics, warnings = _available_metrics(countries, political=False)
        return {
            "page_ids": page_ids, "countries": countries, "ad_active_status": status,
            "available_metrics": metrics, "warnings": warnings,
            "count": len(ads), "ads": ads, "next_page_token": next_token,
        }

    cache_args = _cache_args({
        "page_ids": page_ids, "countries": countries, "ad_active_status": status, "limit": limit,
        "order_by": (args or {}).get("order_by"), "min_reach": (args or {}).get("min_reach"),
        "media_type": (args or {}).get("media_type"), "page_token": (args or {}).get("page_token"),
        "date_from": (args or {}).get("date_from"), "date_to": (args or {}).get("date_to"),
    })
    return await cached("meta_ad_library", conn.id, "search_page_ads", TTL_MEDIUM, _loader, args=cache_args)


async def search_political_ads(conn: Connection, db, args: dict) -> dict:
    terms = str((args or {}).get("search_terms", "")).strip()
    page_ids = _as_list((args or {}).get("page_ids") or (args or {}).get("page_id"))
    if not terms and not page_ids:
        raise ConnectorError("Provide search_terms and/or page_ids.")
    countries = _countries(conn, args)
    limit = _limit(args)
    status = str((args or {}).get("ad_active_status", "ALL")).upper().strip() or "ALL"
    token = _token(conn)

    async def _loader():
        params = {
            "ad_reached_countries": _json_list(countries),
            "ad_type": "POLITICAL_AND_ISSUE_ADS",
            "ad_active_status": status,
            "fields": _POLITICAL_FIELDS,
            "limit": limit,
            **_date_params(args),
        }
        if terms:
            params["search_terms"] = terms
        if page_ids:
            params["search_page_ids"] = _json_list(page_ids)
        rows, next_token = await _search_page(token, params, after=(args or {}).get("page_token"))
        ads = _sort_and_filter([_parse_ad(r, political=True) for r in rows], args)
        metrics, warnings = _available_metrics(countries, political=True)
        return {
            "query": terms, "page_ids": page_ids, "countries": countries,
            "available_metrics": metrics, "warnings": warnings,
            "count": len(ads), "ads": ads, "next_page_token": next_token,
        }

    cache_args = _cache_args({
        "search_terms": terms, "page_ids": page_ids, "countries": countries,
        "ad_active_status": status, "limit": limit,
        "order_by": (args or {}).get("order_by"), "min_reach": (args or {}).get("min_reach"),
        "page_token": (args or {}).get("page_token"),
        "date_from": (args or {}).get("date_from"), "date_to": (args or {}).get("date_to"),
    })
    return await cached("meta_ad_library", conn.id, "search_political_ads", TTL_MEDIUM, _loader, args=cache_args)


async def resolve_page(conn: Connection, db, args: dict) -> dict:
    """Resolve a brand/advertiser name to its Facebook Page ID(s).

    The Ad Library API has no page-search endpoint, so this aggregates the
    advertiser pages that appear in ad results for the name and ranks them.
    Page verification / category / like counts are NOT exposed by the API
    (null), so confidence is a name-match heuristic, not an identity check.
    """
    name = str((args or {}).get("name", "")).strip()
    if not name:
        raise ConnectorError("name is required (the brand / advertiser to resolve).")
    countries = _countries(conn, args)
    limit = _limit(args, default=10, max_value=25)
    # Widest net for page DISCOVERY: ALL statuses (a brand that paused its ads
    # still has the Page we want), unless the caller narrows it.
    status = str((args or {}).get("ad_active_status", "ALL")).upper().strip() or "ALL"
    token = _token(conn)

    async def _loader():
        params = {
            "search_terms": name,
            "ad_reached_countries": _json_list(countries),
            "ad_type": "ALL",
            "ad_active_status": status,
            "fields": "id,page_id,page_name",
            "limit": 100,
        }
        rows = await _collect(token, params, 400, max_pages=5)

        agg: dict[str, dict] = {}
        for r in rows:
            pid = str(r.get("page_id", ""))
            if not pid:
                continue
            entry = agg.setdefault(pid, {"page_id": pid, "page_name": r.get("page_name", ""), "active_ads": 0})
            entry["active_ads"] += 1
            if not entry["page_name"]:
                entry["page_name"] = r.get("page_name", "")

        nl = name.lower()
        pages = []
        for e in agg.values():
            pn = (e["page_name"] or "").lower()
            if pn == nl:
                conf = "high"
            elif nl in pn or pn in nl:
                conf = "medium"
            else:
                conf = "low"
            pages.append({
                "page_id": e["page_id"],
                "page_name": e["page_name"],
                "active_ads": e["active_ads"],     # ads seen in this search (approximate)
                "confidence": conf,
                # Not exposed by the Ad Library API — included for shape only:
                "verified": None, "category": None, "likes": None,
            })

        rank = {"high": 0, "medium": 1, "low": 2}
        pages.sort(key=lambda p: (rank[p["confidence"]], -p["active_ads"]))
        pages = pages[:limit]
        return {
            "query": name, "countries": countries, "count": len(pages), "pages": pages,
            "note": "Page IDs are aggregated from ad results (the only API-available method). "
                    "active_ads counts ads seen in this search, not the page's true total. "
                    "verified/category/likes aren't exposed by the Ad Library API — confidence "
                    "is a name-match heuristic.",
        }

    cache_args = _cache_args({
        "name": name, "countries": countries, "ad_active_status": status, "limit": limit,
    })
    return await cached("meta_ad_library", conn.id, "resolve_page", TTL_MEDIUM, _loader, args=cache_args)


async def _advertiser_rollup(token: str, page_id: str, countries: list[str], status: str, pull: int) -> dict:
    params = {
        "search_page_ids": _json_list([page_id]),
        "ad_reached_countries": _json_list(countries),
        "ad_type": "ALL",
        "ad_active_status": status,
        "fields": _COMMON_FIELDS,
        "limit": 100,
    }
    rows = await _collect(token, params, pull, max_pages=4)
    parsed = [_parse_ad(r) for r in rows]
    by_platform: dict[str, int] = {}
    by_language: dict[str, int] = {}
    page_name = ""
    total_reach = 0
    has_reach = False
    for a in parsed:
        page_name = page_name or a["page_name"]
        for p in a["platforms"]:
            by_platform[p] = by_platform.get(p, 0) + 1
        for lng in a["languages"]:
            by_language[lng] = by_language.get(lng, 0) + 1
        if a.get("eu_total_reach") is not None:
            has_reach = True
            total_reach += int(a.get("eu_total_reach") or 0)
    top = sorted(
        [a for a in parsed if a.get("eu_total_reach") is not None],
        key=lambda a: int(a.get("eu_total_reach") or 0), reverse=True,
    )[:5]
    return {
        "page_id": page_id,
        "page_name": page_name,
        "total_ads": len(parsed),
        "by_platform": dict(sorted(by_platform.items(), key=lambda x: -x[1])),
        "by_language": dict(sorted(by_language.items(), key=lambda x: -x[1])),
        "total_eu_reach": total_reach if has_reach else None,
        "top_creatives": [
            {"id": a["id"], "link_title": (a["link_titles"][0] if a["link_titles"] else ""),
             "eu_total_reach": a.get("eu_total_reach")}
            for a in top
        ],
    }


async def advertiser_summary(conn: Connection, db, args: dict) -> dict:
    """Competitive snapshot for one advertiser Page."""
    page_ids = _as_list((args or {}).get("page_ids") or (args or {}).get("page_id"))
    if not page_ids:
        raise ConnectorError("page_ids is required (the advertiser's Facebook Page ID).")
    countries = _countries(conn, args)
    pull = _limit(args, default=100, max_value=300)
    status = str((args or {}).get("ad_active_status", "ACTIVE")).upper().strip() or "ACTIVE"
    token = _token(conn)

    async def _loader():
        roll = await _advertiser_rollup(token, page_ids[0], countries, status, pull)
        metrics, warnings = _available_metrics(countries, political=False)
        newest_params = {
            "search_page_ids": _json_list(page_ids),
            "ad_reached_countries": _json_list(countries),
            "ad_type": "ALL", "ad_active_status": status,
            "fields": _COMMON_FIELDS, "limit": 100,
        }
        newest_rows = await _collect(token, newest_params, pull, max_pages=4)
        newest = sorted([_parse_ad(r) for r in newest_rows], key=lambda a: a.get("started", ""), reverse=True)[:10]
        return {
            "page_ids": page_ids,
            "page_names": {roll["page_id"]: roll["page_name"]} if roll["page_name"] else {},
            "countries": countries,
            "ad_active_status": status,
            "available_metrics": metrics,
            "warnings": warnings,
            "total_ads_seen": roll["total_ads"],
            "total_eu_reach": roll["total_eu_reach"],
            "by_platform": roll["by_platform"],
            "by_language": roll["by_language"],
            "newest_ads": newest,
        }

    cache_args = _cache_args({
        "page_ids": page_ids, "countries": countries, "ad_active_status": status, "limit": pull,
    })
    return await cached("meta_ad_library", conn.id, "advertiser_summary", TTL_MEDIUM, _loader, args=cache_args)


async def compare_advertisers(conn: Connection, db, args: dict) -> dict:
    """Side-by-side rollup of multiple advertiser Pages for competitor reports."""
    page_ids = _as_list((args or {}).get("page_ids"))
    if len(page_ids) < 2:
        raise ConnectorError("Provide at least 2 page_ids to compare.")
    page_ids = page_ids[:10]
    countries = _countries(conn, args)
    pull = _limit(args, default=100, max_value=300)
    status = str((args or {}).get("ad_active_status", "ACTIVE")).upper().strip() or "ACTIVE"
    token = _token(conn)

    async def _loader():
        advertisers = [await _advertiser_rollup(token, pid, countries, status, pull) for pid in page_ids]
        metrics, warnings = _available_metrics(countries, political=False)
        return {
            "countries": countries,
            "ad_active_status": status,
            "available_metrics": metrics,
            "warnings": warnings,
            "advertisers": advertisers,
        }

    cache_args = _cache_args({
        "page_ids": page_ids, "countries": countries, "ad_active_status": status, "limit": pull,
    })
    return await cached("meta_ad_library", conn.id, "compare_advertisers", TTL_MEDIUM, _loader, args=cache_args)


# ============================================================
# CATALOG
# ============================================================
_COUNTRIES_PROP = {
    "type": "array",
    "items": {"type": "string"},
    "description": "ISO country codes the ad reached (e.g. ['US','GB']). Defaults to ['US'].",
}
_STATUS_PROP = {
    "type": "string",
    "enum": ["ALL", "ACTIVE", "INACTIVE"],
    "description": "Filter by ad delivery status.",
}
_ORDER_PROP = {
    "type": "string",
    "enum": ["date_desc", "date_asc", "reach_desc"],
    "description": "Client-side sort of the returned ads.",
    "default": "date_desc",
}

CATALOG = {
    "resolve_page": {
        "description": "Resolve a brand/advertiser name to its Facebook Page ID(s) — the missing first step before search_page_ads / advertiser_summary.",
        "input": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Brand / advertiser name to resolve to a Page ID."},
                "ad_reached_countries": _COUNTRIES_PROP,
                "ad_active_status": _STATUS_PROP,
                "limit": {"type": "integer", "description": "Max candidate pages to return (max 25).", "default": 10},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "search_ads": {
        "description": "Search the Ad Library by keyword / brand / free text. Returns matching ads with creative text, page, platforms, and a snapshot link.",
        "input": {
            "type": "object",
            "properties": {
                "search_terms": {"type": "string", "description": "Keyword(s), brand, or phrase to search ad text/page names for."},
                "ad_reached_countries": _COUNTRIES_PROP,
                "ad_type": {
                    "type": "string",
                    "enum": ["ALL", "POLITICAL_AND_ISSUE_ADS"],
                    "description": "Ad universe to search.",
                    "default": "ALL",
                },
                "ad_active_status": {**_STATUS_PROP, "default": "ACTIVE"},
                "media_type": {
                    "type": "string",
                    "enum": ["ALL", "IMAGE", "VIDEO", "MEME", "NONE"],
                    "description": "Filter by creative media type.",
                },
                "date_from": {"type": "string", "description": "Earliest ad delivery date (YYYY-MM-DD)."},
                "date_to": {"type": "string", "description": "Latest ad delivery date (YYYY-MM-DD)."},
                "min_reach": {"type": "integer", "description": "Drop ads with eu_total_reach below this (EU ads only)."},
                "order_by": _ORDER_PROP,
                "limit": {"type": "integer", "description": "Max ads to return (max 100).", "default": 25},
                "page_token": {"type": "string", "description": "Cursor from a prior response's next_page_token."},
            },
            "required": ["search_terms"],
            "additionalProperties": False,
        },
    },
    "search_page_ads": {
        "description": "All ads currently/recently run by a specific advertiser Page (by Page ID). The cleanest way to pull one competitor's whole ad set.",
        "input": {
            "type": "object",
            "properties": {
                "page_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Numeric Facebook Page ID(s) of the advertiser. Use resolve_page to find these.",
                },
                "ad_reached_countries": _COUNTRIES_PROP,
                "ad_active_status": {**_STATUS_PROP, "default": "ACTIVE"},
                "media_type": {
                    "type": "string",
                    "enum": ["image", "video", "meme"],
                    "description": "Label the returned creatives' media type (display hint only).",
                },
                "date_from": {"type": "string", "description": "Earliest ad delivery date (YYYY-MM-DD)."},
                "date_to": {"type": "string", "description": "Latest ad delivery date (YYYY-MM-DD)."},
                "min_reach": {"type": "integer", "description": "Drop ads with eu_total_reach below this (EU ads only)."},
                "order_by": _ORDER_PROP,
                "limit": {"type": "integer", "description": "Max ads to return (max 100).", "default": 50},
                "page_token": {"type": "string", "description": "Cursor from a prior response's next_page_token."},
            },
            "required": ["page_ids"],
            "additionalProperties": False,
        },
    },
    "search_political_ads": {
        "description": "Search political / issue / social ads — the richest data: spend ranges, impressions, demographic distribution, and funding entity (worldwide).",
        "input": {
            "type": "object",
            "properties": {
                "search_terms": {"type": "string", "description": "Keyword(s) to search. Provide this and/or page_ids."},
                "page_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Advertiser Page ID(s) to scope to. Provide this and/or search_terms.",
                },
                "ad_reached_countries": _COUNTRIES_PROP,
                "ad_active_status": {**_STATUS_PROP, "default": "ALL"},
                "date_from": {"type": "string", "description": "Earliest ad delivery date (YYYY-MM-DD)."},
                "date_to": {"type": "string", "description": "Latest ad delivery date (YYYY-MM-DD)."},
                "min_reach": {"type": "integer", "description": "Drop ads with eu_total_reach below this (EU ads only)."},
                "order_by": _ORDER_PROP,
                "limit": {"type": "integer", "description": "Max ads to return (max 100).", "default": 25},
                "page_token": {"type": "string", "description": "Cursor from a prior response's next_page_token."},
            },
            "additionalProperties": False,
        },
    },
    "advertiser_summary": {
        "description": "Roll up a competitor's active ads (by Page ID): how many, which platforms, languages, EU reach, and newest creatives — a quick competitive snapshot.",
        "input": {
            "type": "object",
            "properties": {
                "page_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Advertiser Page ID (first is summarized). Use resolve_page to find it.",
                },
                "ad_reached_countries": _COUNTRIES_PROP,
                "ad_active_status": {**_STATUS_PROP, "default": "ACTIVE"},
                "limit": {"type": "integer", "description": "Ads to pull for the rollup (max 300).", "default": 100},
            },
            "required": ["page_ids"],
            "additionalProperties": False,
        },
    },
    "compare_advertisers": {
        "description": "Side-by-side rollup of 2-10 advertiser Page IDs: ad volume, platforms, languages, EU reach, and top creatives — for competitor reports.",
        "input": {
            "type": "object",
            "properties": {
                "page_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-10 advertiser Page IDs to compare side by side.",
                },
                "ad_reached_countries": _COUNTRIES_PROP,
                "ad_active_status": {**_STATUS_PROP, "default": "ACTIVE"},
                "limit": {"type": "integer", "description": "Ads to pull per advertiser (max 300).", "default": 100},
            },
            "required": ["page_ids"],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "resolve_page": resolve_page,
    "search_ads": search_ads,
    "search_page_ads": search_page_ads,
    "search_political_ads": search_political_ads,
    "advertiser_summary": advertiser_summary,
    "compare_advertisers": compare_advertisers,
}

registry.register(
    Connector(
        slug="meta_ad_library",
        label="Meta Ad Library",
        auth="api_key",
        cred_fields=["access_token"],
        catalog=CATALOG,
        handlers=HANDLERS,
        description="Searches Meta's public Ad Library for ads by keyword, advertiser Page or political status, with competitive rollups.",
        category='Advertising',
    )
)
