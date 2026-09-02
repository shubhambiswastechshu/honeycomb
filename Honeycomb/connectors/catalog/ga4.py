"""Google Analytics 4 (GA4) connector — GA4 Data API + Admin API v1beta.

Reads accounts/properties hierarchy, property dimension/metric metadata,
data streams, conversion events, custom dimensions/metrics, and runs analytics
reports (generic, realtime, plus a suite of preset reports) for a connected
GA4 property.

Auth: google_oauth. The saved credentials hold a refresh token plus an optional
default ``property_id``; tools may override the property via args.
"""
from django.conf import settings

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post

ANALYTICS_DATA = "https://analyticsdata.googleapis.com/v1beta"
ANALYTICS_ADMIN = "https://analyticsadmin.googleapis.com/v1beta"


# --------------------------------------------------------------------------- #
# Auth / token refresh (preserved)
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
# Helpers
# --------------------------------------------------------------------------- #
def _pid_raw(value) -> str:
    pid = str(value or "").strip()
    if pid.startswith("properties/"):
        pid = pid.split("/")[-1]
    return pid


def _args_property(args: dict):
    args = args or {}
    return args.get("property_id") or args.get("property") or args.get("propertyId")


async def _resolve_property_id(conn: Connection, db, args: dict) -> str:
    """Return a usable property id: caller's if given, else saved default, else
    the first property from the account summaries (handy with a single property)."""
    pid = _pid_raw(_args_property(args) or conn.creds().get("property_id"))
    if pid:
        return pid
    try:
        summ = await _account_summaries(conn, db)
    except ConnectorError:
        summ = {}
    for acc in summ.get("accounts", []) or []:
        for p in acc.get("properties", []) or []:
            if p.get("id"):
                return str(p["id"])
    raise ConnectorError(
        "property_id is required (e.g. '270513553'). Call list_properties to find one."
    )


def _days(args: dict, default: int = 28) -> int:
    try:
        return max(1, min(int((args or {}).get("days", default)), 3650))
    except (TypeError, ValueError):
        return default


def _limit(args: dict, default: int = 25, max_value: int = 10000) -> int:
    try:
        return max(1, min(int((args or {}).get("limit", default)), max_value))
    except (TypeError, ValueError):
        return default


def _dates(args: dict) -> dict:
    return {"start_date": (args or {}).get("start_date"), "end_date": (args or {}).get("end_date")}


def _date_window(days=28, start_date=None, end_date=None) -> tuple[str, str]:
    if start_date and end_date:
        return start_date, end_date
    return f"{max(1, int(days or 28))}daysAgo", "today"


def _event_filter(event_name: str) -> dict:
    return {
        "filter": {
            "fieldName": "eventName",
            "stringFilter": {"matchType": "EXACT", "value": event_name},
        }
    }


def _as_list(v):
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return list(v or [])


def _coerce_metric(value):
    try:
        f = float(value)
        return int(f) if f.is_integer() else round(f, 2)
    except (TypeError, ValueError):
        return value


def _parse_report(data: dict) -> list[dict]:
    dim_headers = [h.get("name") for h in data.get("dimensionHeaders", []) or []]
    met_headers = [h.get("name") for h in data.get("metricHeaders", []) or []]
    rows = []
    for r in data.get("rows", []) or []:
        item: dict = {}
        for i, dv in enumerate(r.get("dimensionValues", []) or []):
            key = dim_headers[i] if i < len(dim_headers) else f"dim{i}"
            item[key] = dv.get("value")
        for i, mv in enumerate(r.get("metricValues", []) or []):
            key = met_headers[i] if i < len(met_headers) else f"met{i}"
            item[key] = _coerce_metric(mv.get("value"))
        rows.append(item)
    return rows


# --------------------------------------------------------------------------- #
# Account summaries (accounts + properties hierarchy)
# --------------------------------------------------------------------------- #
async def _fetch_account_summaries(conn: Connection, db) -> dict:
    token = await _access_token(conn, db)
    accounts = []
    page_token = None
    url = f"{ANALYTICS_ADMIN}/accountSummaries"
    while True:
        params = {"pageSize": 200}
        if page_token:
            params["pageToken"] = page_token
        try:
            async with limit_for(url):
                res = await http_get(url, headers=_bearer(token), params=params)
        except UpstreamUnavailable as e:
            raise ConnectorError(f"account summaries unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(
                f"account summaries failed {res.status_code}: {res.text[:300]}"
            )
        data = res.json() or {}
        for a in data.get("accountSummaries", []) or []:
            properties = []
            for p in a.get("propertySummaries", []) or []:
                properties.append(
                    {
                        "property": p.get("property"),
                        "id": (p.get("property") or "").split("/")[-1],
                        "display_name": p.get("displayName"),
                        "property_type": p.get("propertyType"),
                        "parent": p.get("parent"),
                    }
                )
            accounts.append(
                {
                    "account": a.get("account"),
                    "id": (a.get("account") or "").split("/")[-1],
                    "display_name": a.get("displayName"),
                    "properties": properties,
                }
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    total_properties = sum(len(a["properties"]) for a in accounts)
    return {
        "accounts": accounts,
        "total_accounts": len(accounts),
        "total_properties": total_properties,
    }


async def _account_summaries(conn: Connection, db) -> dict:
    return await cached(
        "ga4", conn.id, "account_summaries", TTL_LONG,
        lambda: _fetch_account_summaries(conn, db),
    )


# --------------------------------------------------------------------------- #
# Admin API: property metadata
# --------------------------------------------------------------------------- #
async def _admin_get(conn: Connection, db, path: str, label: str) -> dict:
    token = await _access_token(conn, db)
    url = f"{ANALYTICS_ADMIN}/{path}"
    try:
        async with limit_for(url):
            res = await http_get(url, headers=_bearer(token))
    except UpstreamUnavailable as e:
        raise ConnectorError(f"{label} unavailable: {e}")
    if res.status_code >= 400:
        raise ConnectorError(f"{label} failed {res.status_code}: {res.text[:300]}")
    return res.json() or {}


# --------------------------------------------------------------------------- #
# Data API: reports
# --------------------------------------------------------------------------- #
async def _post_data(conn: Connection, db, pid: str, verb: str, body: dict, label: str) -> dict:
    token = await _access_token(conn, db)
    url = f"{ANALYTICS_DATA}/properties/{pid}:{verb}"
    try:
        async with limit_for(url):
            res = await http_post(url, headers=_bearer(token), json=body)
    except UpstreamUnavailable as e:
        raise ConnectorError(f"{label} unavailable: {e}")
    if res.status_code >= 400:
        raise ConnectorError(f"{label} failed {res.status_code}: {res.text[:400]}")
    return res.json() or {}


async def _run_report(
    conn: Connection,
    db,
    *,
    pid: str,
    dimensions: list,
    metrics: list,
    days: int = 28,
    limit: int = 25,
    dimension_filter: dict | None = None,
    order_by_metric: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Core GA4 Data API runReport. Most GA4 report tools wrap this."""
    sd, ed = _date_window(days, start_date, end_date)
    mets = list(metrics or [])
    body = {
        "dateRanges": [{"startDate": sd, "endDate": ed}],
        "dimensions": [{"name": d} for d in (dimensions or [])],
        "metrics": [{"name": m} for m in mets],
        "limit": str(min(max(1, int(limit or 25)), 100000)),
        "keepEmptyRows": False,
    }
    if dimension_filter:
        body["dimensionFilter"] = dimension_filter
    ob = order_by_metric or (mets[0] if mets else None)
    if ob:
        body["orderBys"] = [{"metric": {"metricName": ob}, "desc": True}]

    async def _load():
        data = await _post_data(conn, db, pid, "runReport", body, "runReport")
        rows = _parse_report(data)
        return {
            "property_id": pid,
            "start_date": sd,
            "end_date": ed,
            "dimensions": [d.get("name") for d in body["dimensions"]],
            "metrics": mets,
            "row_count": len(rows),
            "rows": rows,
        }

    return await cached(
        "ga4", conn.id, "run_report", TTL_SHORT, _load,
        args={
            "p": pid, "d": body["dimensions"], "m": mets, "sd": sd, "ed": ed,
            "lim": limit, "f": dimension_filter, "o": ob,
        },
    )


# --------------------------------------------------------------------------- #
# Properties tools
# --------------------------------------------------------------------------- #
async def list_accounts(conn: Connection, db, args: dict) -> dict:
    summ = await _account_summaries(conn, db)
    accounts = [
        {
            "account": a.get("account"),
            "id": a.get("id"),
            "display_name": a.get("display_name"),
            "property_count": len(a.get("properties", []) or []),
        }
        for a in summ.get("accounts", []) or []
    ]
    return {"count": len(accounts), "total_accounts": summ.get("total_accounts"), "accounts": accounts}


async def list_properties(conn: Connection, db, args: dict) -> dict:
    args = args or {}
    summ = await _account_summaries(conn, db)
    account_filter = _pid_raw(args.get("account_id") or args.get("account"))
    if account_filter and "/" in str(account_filter):
        account_filter = str(account_filter).split("/")[-1]
    properties = []
    for a in summ.get("accounts", []) or []:
        if account_filter and a.get("id") != str(account_filter):
            continue
        for p in a.get("properties", []) or []:
            properties.append(
                {
                    "property": p.get("property"),
                    "id": p.get("id"),
                    "display_name": p.get("display_name"),
                    "property_type": p.get("property_type"),
                    "account": a.get("account"),
                    "account_display_name": a.get("display_name"),
                }
            )
    return {"count": len(properties), "properties": properties}


async def get_property(conn: Connection, db, args: dict) -> dict:
    pid = await _resolve_property_id(conn, db, args)

    async def _load():
        d = await _admin_get(conn, db, f"properties/{pid}", "get_property")
        return {
            "property_id": pid,
            "display_name": d.get("displayName"),
            "currency_code": d.get("currencyCode"),
            "time_zone": d.get("timeZone"),
            "industry_category": d.get("industryCategory"),
            "create_time": d.get("createTime"),
            "service_level": d.get("serviceLevel"),
            "account": d.get("account"),
        }

    return await cached("ga4", conn.id, "get_property", TTL_LONG, _load, args={"p": pid})


async def list_data_streams(conn: Connection, db, args: dict) -> dict:
    pid = await _resolve_property_id(conn, db, args)

    async def _load():
        d = await _admin_get(conn, db, f"properties/{pid}/dataStreams", "list_data_streams")
        streams = []
        for s in d.get("dataStreams", []) or []:
            web = s.get("webStreamData", {}) or {}
            streams.append(
                {
                    "name": s.get("displayName"),
                    "type": s.get("type"),
                    "measurement_id": web.get("measurementId"),
                    "default_uri": web.get("defaultUri"),
                    "stream_id": (s.get("name") or "").split("/")[-1],
                }
            )
        return {"property_id": pid, "count": len(streams), "data_streams": streams}

    return await cached("ga4", conn.id, "list_data_streams", TTL_LONG, _load, args={"p": pid})


async def list_custom_dimensions(conn: Connection, db, args: dict) -> dict:
    pid = await _resolve_property_id(conn, db, args)

    async def _load():
        d = await _admin_get(
            conn, db, f"properties/{pid}/customDimensions", "list_custom_dimensions"
        )
        items = [
            {
                "parameter_name": c.get("parameterName"),
                "display_name": c.get("displayName"),
                "scope": c.get("scope"),
            }
            for c in d.get("customDimensions", []) or []
        ]
        return {"property_id": pid, "count": len(items), "custom_dimensions": items}

    return await cached(
        "ga4", conn.id, "list_custom_dimensions", TTL_LONG, _load, args={"p": pid}
    )


async def list_custom_metrics(conn: Connection, db, args: dict) -> dict:
    pid = await _resolve_property_id(conn, db, args)

    async def _load():
        d = await _admin_get(conn, db, f"properties/{pid}/customMetrics", "list_custom_metrics")
        items = [
            {
                "parameter_name": c.get("parameterName"),
                "display_name": c.get("displayName"),
                "measurement_unit": c.get("measurementUnit"),
                "scope": c.get("scope"),
            }
            for c in d.get("customMetrics", []) or []
        ]
        return {"property_id": pid, "count": len(items), "custom_metrics": items}

    return await cached("ga4", conn.id, "list_custom_metrics", TTL_LONG, _load, args={"p": pid})


async def list_conversion_events(conn: Connection, db, args: dict) -> dict:
    pid = await _resolve_property_id(conn, db, args)

    async def _load():
        # Newer API calls these "key events"; fall back to legacy conversionEvents.
        try:
            d = await _admin_get(conn, db, f"properties/{pid}/keyEvents", "list_key_events")
            raw = d.get("keyEvents", []) or []
        except ConnectorError:
            d = await _admin_get(
                conn, db, f"properties/{pid}/conversionEvents", "list_conversion_events"
            )
            raw = d.get("conversionEvents", []) or []
        items = [
            {
                "event_name": e.get("eventName"),
                "counting_method": e.get("countingMethod"),
                "create_time": e.get("createTime"),
            }
            for e in raw
        ]
        return {"property_id": pid, "count": len(items), "conversion_events": items}

    return await cached(
        "ga4", conn.id, "list_conversion_events", TTL_LONG, _load, args={"p": pid}
    )


async def get_metadata(conn: Connection, db, args: dict) -> dict:
    pid = await _resolve_property_id(conn, db, args)

    async def _load():
        token = await _access_token(conn, db)
        url = f"{ANALYTICS_DATA}/properties/{pid}/metadata"
        try:
            async with limit_for(url):
                res = await http_get(url, headers=_bearer(token))
        except UpstreamUnavailable as e:
            raise ConnectorError(f"get_metadata unavailable: {e}")
        if res.status_code >= 400:
            raise ConnectorError(f"get_metadata failed {res.status_code}: {res.text[:300]}")
        data = res.json() or {}
        dims = [
            {"api_name": m.get("apiName"), "ui_name": m.get("uiName")}
            for m in data.get("dimensions", []) or []
        ]
        mets = [
            {"api_name": m.get("apiName"), "ui_name": m.get("uiName"), "type": m.get("type")}
            for m in data.get("metrics", []) or []
        ]
        return {
            "property_id": pid,
            "dimension_count": len(dims),
            "metric_count": len(mets),
            "dimensions": dims,
            "metrics": mets,
        }

    return await cached("ga4", conn.id, "get_metadata", TTL_LONG, _load, args={"p": pid})


# --------------------------------------------------------------------------- #
# Reports tools
# --------------------------------------------------------------------------- #
async def run_report(conn: Connection, db, args: dict) -> dict:
    args = args or {}
    pid = await _resolve_property_id(conn, db, args)
    dims = _as_list(args.get("dimensions"))
    mets = _as_list(args.get("metrics")) or ["activeUsers"]
    return await _run_report(
        conn,
        db,
        pid=pid,
        dimensions=dims,
        metrics=mets,
        days=_days(args),
        limit=_limit(args),
        dimension_filter=args.get("dimension_filter"),
        order_by_metric=args.get("order_by_metric"),
        **_dates(args),
    )


async def realtime_report(conn: Connection, db, args: dict) -> dict:
    args = args or {}
    pid = await _resolve_property_id(conn, db, args)
    dims = _as_list(args.get("dimensions")) or ["country"]
    mets = _as_list(args.get("metrics")) or ["activeUsers"]
    limit = _limit(args)
    body = {
        "dimensions": [{"name": d} for d in dims],
        "metrics": [{"name": m} for m in mets],
        "limit": str(min(max(1, int(limit or 25)), 10000)),
    }
    data = await _post_data(conn, db, pid, "runRealtimeReport", body, "runRealtimeReport")
    rows = _parse_report(data)
    return {"property_id": pid, "realtime": True, "row_count": len(rows), "rows": rows}


# tool -> (dimensions, metrics, event_filter_name, default_limit)
REPORTS = {
    "top_pages": (["pagePath"], ["screenPageViews", "totalUsers", "engagementRate"], None, 25),
    "traffic_sources": (["sessionSourceMedium"], ["sessions", "totalUsers", "conversions"], None, 25),
    "acquisition_overview": (["firstUserDefaultChannelGroup"], ["totalUsers", "newUsers", "sessions"], None, 25),
    "conversions": (["eventName"], ["conversions", "eventCount"], None, 50),
    "ecommerce_purchases": (["date"], ["transactions", "purchaseRevenue"], None, 90),
    "demographics_report": (["userAgeBracket", "userGender"], ["totalUsers", "sessions"], None, 50),
    "geo_report": (["country", "city"], ["sessions", "totalUsers"], None, 50),
    "device_breakdown": (["deviceCategory"], ["sessions", "totalUsers", "screenPageViews"], None, 10),
    "tech_report": (["browser", "operatingSystem"], ["sessions", "totalUsers"], None, 50),
    "device_model_breakdown": (["mobileDeviceModel", "operatingSystemWithVersion"], ["sessions", "totalUsers", "screenPageViews"], None, 50),
    "new_vs_returning": (["newVsReturning"], ["activeUsers", "sessions", "engagementRate", "conversions"], None, 10),
    "audience_overview": ([], ["activeUsers", "newUsers", "sessions", "engagementRate", "averageSessionDuration"], None, 1),
    "events": (["eventName"], ["eventCount", "totalUsers"], None, 50),
    "search_terms": (["searchTerm"], ["eventCount", "totalUsers"], None, 50),
    "file_downloads": (["fileName"], ["eventCount"], "file_download", 50),
    "outbound_clicks": (["linkUrl"], ["eventCount"], "click", 50),
    "landing_pages": (["landingPage"], ["sessions", "totalUsers", "screenPageViews"], None, 50),
    "campaign_performance": (["sessionCampaignName", "sessionSourceMedium"], ["sessions", "totalUsers", "conversions"], None, 50),
    "google_ads_campaigns": (["sessionGoogleAdsCampaignName"], ["advertiserAdClicks", "advertiserAdCost", "sessions", "conversions"], None, 50),
}


def _make_report(dims, mets, event_name, default_limit):
    async def handler(conn: Connection, db, args: dict) -> dict:
        args = args or {}
        pid = await _resolve_property_id(conn, db, args)
        return await _run_report(
            conn,
            db,
            pid=pid,
            dimensions=dims,
            metrics=mets,
            days=_days(args),
            limit=_limit(args, default_limit),
            dimension_filter=_event_filter(event_name) if event_name else None,
            **_dates(args),
        )

    return handler


# --------------------------------------------------------------------------- #
# Catalog + registration
# --------------------------------------------------------------------------- #
_PROPERTY_PROP = {"type": "string", "description": "GA4 property id (overrides saved default)."}
_DAYS_PROP = {"type": "integer", "description": "Lookback window in days (default 28)."}
_START_PROP = {"type": "string", "description": "Exact start date YYYY-MM-DD (overrides days)."}
_END_PROP = {"type": "string", "description": "Exact end date YYYY-MM-DD (overrides days)."}
_LIMIT_PROP = {"type": "integer", "description": "Max rows."}


def _report_input(default_limit: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "property_id": _PROPERTY_PROP,
            "days": _DAYS_PROP,
            "start_date": _START_PROP,
            "end_date": _END_PROP,
            "limit": {"type": "integer", "description": f"Max rows (default {default_limit})."},
        },
        "required": [],
        "additionalProperties": False,
    }


def _property_only_input() -> dict:
    return {
        "type": "object",
        "properties": {"property_id": _PROPERTY_PROP},
        "required": [],
        "additionalProperties": False,
    }


_REPORT_DESCRIPTIONS = {
    "top_pages": "Pageviews / users / engagement by page path.",
    "traffic_sources": "Sessions and users by source/medium.",
    "acquisition_overview": "First-touch acquisition broken down by channel group.",
    "conversions": "Conversion counts and event counts per event over time.",
    "ecommerce_purchases": "Purchase events with revenue and transactions by date.",
    "demographics_report": "Age and gender breakdown for users.",
    "geo_report": "Sessions and users by country and city.",
    "device_breakdown": "Performance split across desktop, mobile, tablet.",
    "tech_report": "Browser and operating-system breakdown.",
    "device_model_breakdown": "Granular device model + OS version (beyond device category).",
    "new_vs_returning": "Active users, sessions, engagement split by new vs returning users.",
    "audience_overview": "Active users, new users, engagement rate, session duration.",
    "events": "Top events with event count and users.",
    "search_terms": "Top on-site search terms (view_search_results event).",
    "file_downloads": "file_download event counts by file name.",
    "outbound_clicks": "Outbound link clicks by destination URL.",
    "landing_pages": "First-page sessions by landing page URL.",
    "campaign_performance": "Campaign attribution by source/medium with sessions and conversions.",
    "google_ads_campaigns": "GA4-reported metrics for linked Google Ads campaigns.",
}


CATALOG = {
    # Accounts & properties
    "list_accounts": {
        "description": "All GA4 accounts the user can access.",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "list_properties": {
        "description": "GA4 properties under an account or across all accounts.",
        "input": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Filter to one account id (optional)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_property": {
        "description": "Detailed info for a single property (currency, timezone, industry).",
        "input": _property_only_input(),
    },
    "list_data_streams": {
        "description": "Web/iOS/Android data streams configured on a property.",
        "input": _property_only_input(),
    },
    "list_conversion_events": {
        "description": "Configured conversion (key) events with counting method.",
        "input": _property_only_input(),
    },
    "list_custom_dimensions": {
        "description": "User-defined custom dimensions for a property.",
        "input": _property_only_input(),
    },
    "list_custom_metrics": {
        "description": "User-defined custom metrics for a property.",
        "input": _property_only_input(),
    },
    "get_metadata": {
        "description": "Metric and dimension metadata available for queries.",
        "input": _property_only_input(),
    },
    # Reports
    "run_report": {
        "description": "Generic GA4 Data API report: dimensions, metrics, filters, date range.",
        "input": {
            "type": "object",
            "properties": {
                "property_id": _PROPERTY_PROP,
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Dimension API names.",
                },
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Metric API names (default ['activeUsers']).",
                },
                "days": _DAYS_PROP,
                "start_date": _START_PROP,
                "end_date": _END_PROP,
                "limit": _LIMIT_PROP,
                "dimension_filter": {"type": "object", "description": "GA4 FilterExpression object."},
                "order_by_metric": {"type": "string", "description": "Metric name to sort by (desc)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "realtime_report": {
        "description": "Realtime users by country, device, page, or event.",
        "input": {
            "type": "object",
            "properties": {
                "property_id": _PROPERTY_PROP,
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Realtime dimension API names (default ['country']).",
                },
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Realtime metric API names (default ['activeUsers']).",
                },
                "limit": _LIMIT_PROP,
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

# Preset report tools share the same input shape.
for _name, (_d, _m, _e, _l) in REPORTS.items():
    CATALOG[_name] = {
        "description": _REPORT_DESCRIPTIONS.get(_name, f"GA4 {_name} report."),
        "input": _report_input(_l),
    }


HANDLERS = {
    "list_accounts": list_accounts,
    "list_properties": list_properties,
    "get_property": get_property,
    "list_data_streams": list_data_streams,
    "list_conversion_events": list_conversion_events,
    "list_custom_dimensions": list_custom_dimensions,
    "list_custom_metrics": list_custom_metrics,
    "get_metadata": get_metadata,
    "run_report": run_report,
    "realtime_report": realtime_report,
}
for _name, (_d, _m, _e, _l) in REPORTS.items():
    HANDLERS[_name] = _make_report(_d, _m, _e, _l)


registry.register(
    Connector(
        slug="ga4",
        label="Google Analytics 4",
        auth="google_oauth",
        description="Reads GA4 accounts, properties and metadata and runs analytics reports - generic, realtime and a suite of presets - through the GA4 Data and Admin APIs.",
        category="Analytics",
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
