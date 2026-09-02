"""Google Merchant Center connector — Merchant API v1.

Auth: google_oauth (scope https://www.googleapis.com/auth/content).

The Merchant API (merchantapi.googleapis.com) is Google's GA successor to the
Content API for Shopping; it is a family of independently-versioned sub-APIs
(accounts, products, reports, datasources, …). The legacy v1beta surface was
discontinued 2026-02-28, so this connector targets the GA v1 sub-APIs.

Credentials hold {"merchant_id": "...", "refresh_token": "..."}; every tool also
accepts a `merchant_id` arg to override the saved default.

Tools (reads unless noted):
  - register_developer (WRITE)  one-time GCP project registration (fixes GCP_NOT_REGISTERED)
  - list_accounts               accounts the connected Google user can access
  - get_account                 one account's profile
  - list_subaccounts            sub-accounts under an advanced (MCA) account
  - list_products               catalog products (paged)
  - get_product                 one product + embedded status
  - list_product_statuses       per-item approval status / item-level issues (paged)
  - get_product_status          status + issues for one product
  - account_issues              account-level policy / data-quality issues
  - list_data_sources           product data sources / feeds
  - report_search               raw Merchant API Query Language SELECT (reports.search)
  - product_performance         clicks/impressions/CTR/conversions per offer
  - best_sellers                best-selling product clusters (preview access)
  - price_competitiveness       price vs market benchmark (preview access)
  - price_insights              suggested prices + predicted impact (preview access)
"""
from datetime import date, timedelta
from urllib.parse import quote

from django.conf import settings

from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post
from connections.models import Connection

# Merchant API sub-API roots
ACCOUNTS_API = "https://merchantapi.googleapis.com/accounts/v1"
PRODUCTS_API = "https://merchantapi.googleapis.com/products/v1"
REPORTS_API = "https://merchantapi.googleapis.com/reports/v1"
DATASOURCES_API = "https://merchantapi.googleapis.com/datasources/v1"

# Legacy Content API for Shopping — used ONLY to bootstrap account discovery for
# register_developer (Merchant API v1 gates every call behind registration, but
# registration needs an account id; authinfo predates that gate).
CONTENT_AUTHINFO = "https://shoppingcontent.googleapis.com/content/v2.1/accounts/authinfo"

_MAX_ERR_BODY = 4000


# --------------------------------------------------------------------------- #
# OAuth + helpers
# --------------------------------------------------------------------------- #
async def _access_token(conn: Connection, db) -> str:
    creds = conn.creds()
    rt = creds.get("refresh_token")
    if not rt:
        raise ConnectorError("Not connected: missing refresh token.")
    try:
        res = await http_post_token(rt)
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code != 200:
        raise ConnectorError(f"token refresh failed {res.status_code}: {res.text[:300]}")
    return res.json()["access_token"]


async def http_post_token(refresh_token: str):
    # token endpoint takes a form body; kept separate so _access_token stays tidy
    # getattr keeps a missing setting a ConnectorError at call time, not an ImportError at boot
    return await http_post(
        getattr(settings, "GOOGLE_OAUTH_TOKEN_URI", "https://oauth2.googleapis.com/token"),
        data={
            "client_id": getattr(settings, "GOOGLE_CLIENT_ID", ""),
            "client_secret": getattr(settings, "GOOGLE_CLIENT_SECRET", ""),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )


def _acct(value) -> str:
    """Normalize a merchant/account id to the bare numeric id the path expects.
    Accepts '123', 'accounts/123', or a full resource name."""
    s = str(value or "").strip()
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    return s


def _merchant_id(conn: Connection, args: dict) -> str:
    raw = (
        args.get("merchant_id")
        or args.get("merchantId")
        or args.get("account_id")
        or conn.creds().get("merchant_id")
        or ""
    )
    mid = _acct(raw)
    if not mid:
        raise ConnectorError(
            "merchant_id is required (your Merchant Center account ID, digits only). "
            "Call list_accounts to find yours."
        )
    return mid


def _seg(value: str) -> str:
    """URL-encode a path segment. Product ids use '~' (unreserved, kept literal)."""
    return quote(str(value or ""), safe="~")


def _err_message(res) -> str:
    """Surface upstream error.message up front, keep the full payload."""
    body = res.text or ""
    try:
        data = res.json()
        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        if msg:
            return f"{msg} | {body[:_MAX_ERR_BODY]}"
    except Exception:
        pass
    return body[:_MAX_ERR_BODY]


async def _api_get(conn: Connection, db, url: str, params: dict | None = None, label: str = "merchant") -> dict:
    token = await _access_token(conn, db)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with limit_for(url):
            res = await http_get(url, headers=headers, params=params or {})
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code >= 400:
        raise ConnectorError(f"Google Merchant {label} error {res.status_code}: {_err_message(res)}")
    return res.json() or {}


async def _api_post(conn: Connection, db, url: str, body: dict | None = None, params: dict | None = None, label: str = "merchant") -> dict:
    token = await _access_token(conn, db)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with limit_for(url):
            res = await http_post(url, headers=headers, json=(body or {}), params=params or {})
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code >= 400:
        raise ConnectorError(f"Google Merchant {label} error {res.status_code}: {_err_message(res)}")
    return res.json() or {}


def _micros_to_str(money) -> str | None:
    """Render a Merchant API Money {amountMicros, currencyCode} as a flat string."""
    if not isinstance(money, dict):
        return None
    am = money.get("amountMicros")
    if am is None:
        return None
    try:
        return f"{int(am) / 1_000_000:.2f} {money.get('currencyCode', '')}".strip()
    except (TypeError, ValueError):
        return f"{am} {money.get('currencyCode', '')}".strip()


def _clamp(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _date_window(days=30, start_date=None, end_date=None) -> tuple[str, str]:
    if start_date and end_date:
        return str(start_date), str(end_date)
    end = date.today()
    start = end - timedelta(days=max(1, int(days or 30)))
    return start.isoformat(), end.isoformat()


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def _serialize_product(p: dict) -> dict:
    # Merchant API v1 renamed Product.attributes → productAttributes and
    # gtin (scalar) → gtins (array). Prefer v1 names, fall back to legacy.
    attrs = p.get("productAttributes") or p.get("attributes") or {}
    gtins = attrs.get("gtins")
    gtin = gtins[0] if isinstance(gtins, list) and gtins else attrs.get("gtin")
    return {
        "name": p.get("name"),
        "offer_id": p.get("offerId"),
        "title": attrs.get("title"),
        "brand": attrs.get("brand"),
        "link": attrs.get("link"),
        "image_link": attrs.get("imageLink"),
        "price": _micros_to_str(attrs.get("price")),
        "availability": attrs.get("availability"),
        "condition": attrs.get("condition"),
        "channel": p.get("channel"),
        "content_language": p.get("contentLanguage"),
        "feed_label": p.get("feedLabel"),
        "data_source": p.get("dataSource"),
        "gtin": gtin,
        "mpn": attrs.get("mpn"),
        "google_product_category": attrs.get("googleProductCategory"),
    }


def _serialize_status(status: dict) -> dict:
    issues = [{
        "code": i.get("code"),
        "severity": i.get("severity"),
        "resolution": i.get("resolution"),
        "attribute": i.get("attribute"),
        "reporting_context": i.get("reportingContext"),
        "description": i.get("description"),
        "detail": i.get("detail"),
        "documentation": i.get("documentation"),
        "applicable_countries": i.get("applicableCountries"),
    } for i in status.get("itemLevelIssues", []) or []]
    dest = [{
        "reporting_context": d.get("reportingContext"),
        "approved_countries": len(d.get("approvedCountries", []) or []),
        "disapproved_countries": len(d.get("disapprovedCountries", []) or []),
        "pending_countries": len(d.get("pendingCountries", []) or []),
    } for d in status.get("destinationStatuses", []) or []]
    return {"destination_statuses": dest, "item_level_issues": issues, "issue_count": len(issues)}


# --------------------------------------------------------------------------- #
# Reporting core (reports.search) — used directly and by the report shortcuts
# --------------------------------------------------------------------------- #
async def _report_search(conn: Connection, db, aid: str, query: str, page_token: str | None = None) -> dict:
    if not query or not str(query).strip():
        raise ConnectorError("query is required (a Merchant API Query Language SELECT statement).")
    body: dict = {"query": str(query)}
    if page_token:
        body["pageToken"] = page_token

    async def _loader():
        data = await _api_post(
            conn, db, f"{REPORTS_API}/accounts/{_seg(aid)}/reports:search",
            body=body, label="report_search",
        )
        return {
            "merchant_id": aid,
            "query": body["query"],
            "row_count": len(data.get("results", []) or []),
            "results": data.get("results", []) or [],
            "next_page_token": data.get("nextPageToken"),
        }

    return await cached(
        "google_merchant", conn.id, "report_search", TTL_SHORT, _loader,
        args={"a": aid, "q": body["query"], "pt": page_token or ""},
    )


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
async def list_accounts(conn: Connection, db, args: dict) -> dict:
    async def _loader():
        data = await _api_get(
            conn, db, f"{ACCOUNTS_API}/accounts",
            params={"pageSize": 250}, label="list_accounts",
        )
        rows = [{
            "account_id": _acct(a.get("name")),
            "account_name": a.get("accountName"),
            "adult_content": a.get("adultContent"),
            "test_account": a.get("testAccount"),
            "time_zone": (a.get("timeZone") or {}).get("id"),
        } for a in data.get("accounts", []) or []]
        return {"count": len(rows), "accounts": rows, "next_page_token": data.get("nextPageToken")}

    return await cached("google_merchant", conn.id, "list_accounts", TTL_LONG, _loader, args={})


async def get_account(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)

    async def _loader():
        d = await _api_get(conn, db, f"{ACCOUNTS_API}/accounts/{_seg(aid)}", label="get_account")
        return {
            "account_id": _acct(d.get("name")),
            "account_name": d.get("accountName"),
            "adult_content": d.get("adultContent"),
            "test_account": d.get("testAccount"),
            "language_code": d.get("languageCode"),
            "time_zone": (d.get("timeZone") or {}).get("id"),
        }

    return await cached("google_merchant", conn.id, "get_account", TTL_LONG, _loader, args={"a": aid})


async def list_subaccounts(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    limit = _clamp(args.get("limit", 50), 50, 1, 250)

    async def _loader():
        data = await _api_get(
            conn, db, f"{ACCOUNTS_API}/accounts/{_seg(aid)}:listSubaccounts",
            params={"pageSize": limit}, label="list_subaccounts",
        )
        rows = [{
            "account_id": _acct(a.get("name")),
            "account_name": a.get("accountName"),
        } for a in data.get("accounts", []) or []]
        return {
            "merchant_id": aid,
            "count": len(rows),
            "subaccounts": rows,
            "next_page_token": data.get("nextPageToken"),
        }

    return await cached(
        "google_merchant", conn.id, "list_subaccounts", TTL_LONG, _loader,
        args={"a": aid, "lim": limit},
    )


async def register_developer(conn: Connection, db, args: dict) -> dict:
    """One-time setup: register the app's GCP project as a Merchant API developer.
    Merchant API v1 refuses every call until this is done (GCP_NOT_REGISTERED)."""
    creds = conn.creds()
    email = (args.get("developer_email") or args.get("email") or creds.get("google_account_email") or "").strip()

    raw = args.get("merchant_id") or args.get("merchantId") or args.get("account_id")
    aid = _acct(raw) if raw else ""
    discovered: list[str] = []
    if not aid:
        # fall back to saved default, then to authinfo discovery
        aid = _acct(creds.get("merchant_id") or "")
    if not aid:
        try:
            data = await _api_get(conn, db, CONTENT_AUTHINFO, label="authinfo")
        except ConnectorError:
            data = {}
        for ident in data.get("accountIdentifiers", []) or []:
            for key in ("merchantId", "aggregatorId"):
                v = ident.get(key)
                if v and str(v) not in discovered:
                    discovered.append(str(v))
        aid = discovered[0] if discovered else ""
    if not aid:
        raise ConnectorError(
            "Could not auto-detect your Merchant Center account ID. Pass merchant_id "
            "(find it in Merchant Center → top-right account picker, digits only)."
        )

    body = {"developerEmail": email} if email else {}
    url = f"{ACCOUNTS_API}/accounts/{_seg(aid)}/developerRegistration:registerGcp"
    res = await _api_post(conn, db, url, body=body, label="register_developer")
    return {
        "registered": True,
        "account_id": aid,
        "developer_email": email or None,
        "discovered_account_ids": discovered or None,
        "registration": res,
        "note": "Registration can take up to 5 minutes to propagate before other tools work.",
    }


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
async def list_products(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    limit = _clamp(args.get("limit", args.get("max_results", 25)), 25, 1, 250)
    page_token = args.get("page_token")

    async def _loader():
        params: dict = {"pageSize": limit}
        if page_token:
            params["pageToken"] = page_token
        data = await _api_get(
            conn, db, f"{PRODUCTS_API}/accounts/{_seg(aid)}/products",
            params=params, label="list_products",
        )
        rows = [_serialize_product(p) for p in data.get("products", []) or []]
        return {
            "merchant_id": aid,
            "count": len(rows),
            "products": rows,
            "next_page_token": data.get("nextPageToken"),
        }

    return await cached(
        "google_merchant", conn.id, "list_products", TTL_MEDIUM, _loader,
        args={"a": aid, "lim": limit, "pt": page_token or ""},
    )


def _product_id(args: dict) -> str:
    return str(args.get("product_id") or args.get("productId") or args.get("id") or "").strip()


async def get_product(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    pid = _product_id(args)
    if not pid:
        raise ConnectorError("product_id is required (REST id, e.g. 'online~en~US~SKU123').")

    async def _loader():
        d = await _api_get(
            conn, db, f"{PRODUCTS_API}/accounts/{_seg(aid)}/products/{_seg(pid)}",
            label="get_product",
        )
        out = _serialize_product(d)
        out["product_status"] = _serialize_status(d.get("productStatus") or {})
        return out

    return await cached(
        "google_merchant", conn.id, "get_product", TTL_MEDIUM, _loader,
        args={"a": aid, "pid": pid},
    )


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
async def list_product_statuses(conn: Connection, db, args: dict) -> dict:
    """In the Merchant API, item status is embedded on the Product resource, so this
    lists products and projects each down to its status + issues."""
    aid = _merchant_id(conn, args)
    limit = _clamp(args.get("limit", args.get("max_results", 25)), 25, 1, 250)
    page_token = args.get("page_token")

    async def _loader():
        params: dict = {"pageSize": limit}
        if page_token:
            params["pageToken"] = page_token
        data = await _api_get(
            conn, db, f"{PRODUCTS_API}/accounts/{_seg(aid)}/products",
            params=params, label="list_product_statuses",
        )
        rows = []
        for p in data.get("products", []) or []:
            s = _serialize_status(p.get("productStatus") or {})
            attrs = p.get("productAttributes") or p.get("attributes") or {}
            rows.append({
                "offer_id": p.get("offerId"),
                "name": p.get("name"),
                "title": attrs.get("title"),
                **s,
            })
        return {
            "merchant_id": aid,
            "count": len(rows),
            "statuses": rows,
            "next_page_token": data.get("nextPageToken"),
        }

    return await cached(
        "google_merchant", conn.id, "list_product_statuses", TTL_SHORT, _loader,
        args={"a": aid, "lim": limit, "pt": page_token or ""},
    )


async def get_product_status(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    pid = _product_id(args)
    if not pid:
        raise ConnectorError("product_id is required (REST id, e.g. 'online~en~US~SKU123').")

    async def _loader():
        d = await _api_get(
            conn, db, f"{PRODUCTS_API}/accounts/{_seg(aid)}/products/{_seg(pid)}",
            label="get_product_status",
        )
        attrs = d.get("productAttributes") or d.get("attributes") or {}
        return {
            "offer_id": d.get("offerId"),
            "name": d.get("name"),
            "title": attrs.get("title"),
            **_serialize_status(d.get("productStatus") or {}),
        }

    return await cached(
        "google_merchant", conn.id, "get_product_status", TTL_SHORT, _loader,
        args={"a": aid, "pid": pid},
    )


async def account_issues(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)

    async def _loader():
        data = await _api_get(
            conn, db, f"{ACCOUNTS_API}/accounts/{_seg(aid)}/issues",
            params={"pageSize": 100}, label="account_issues",
        )
        issues = [{
            "title": i.get("title"),
            "severity": i.get("severity"),
            "impacted_destinations": i.get("impactedDestinations"),
            "detail": i.get("detail"),
            "documentation": i.get("documentationUri"),
        } for i in data.get("accountIssues", []) or []]
        return {"merchant_id": aid, "issue_count": len(issues), "account_issues": issues}

    return await cached(
        "google_merchant", conn.id, "account_issues", TTL_SHORT, _loader, args={"a": aid},
    )


async def list_data_sources(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)

    async def _loader():
        data = await _api_get(
            conn, db, f"{DATASOURCES_API}/accounts/{_seg(aid)}/dataSources",
            params={"pageSize": 200}, label="list_data_sources",
        )
        rows = []
        for d in data.get("dataSources", []) or []:
            kind = ("primary" if d.get("primaryProductDataSource") else
                    "supplemental" if d.get("supplementalProductDataSource") else
                    "other")
            rows.append({
                "name": d.get("name"),
                "display_name": d.get("displayName"),
                "type": kind,
                "input": d.get("input"),
            })
        return {"merchant_id": aid, "count": len(rows), "data_sources": rows}

    return await cached(
        "google_merchant", conn.id, "list_data_sources", TTL_MEDIUM, _loader, args={"a": aid},
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
async def report_search(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    return await _report_search(conn, db, aid, args.get("query"), page_token=args.get("page_token"))


async def product_performance(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    days = _clamp(args.get("days", 30), 30, 1, 3650)
    limit = _clamp(args.get("limit", 50), 50, 1, 1000)
    sd, ed = _date_window(days, args.get("start_date"), args.get("end_date"))
    query = (
        "SELECT segments.offer_id, segments.title, metrics.clicks, metrics.impressions, "
        "metrics.click_through_rate, metrics.conversions, metrics.conversion_value_micros "
        "FROM product_performance_view "
        f"WHERE segments.date BETWEEN '{sd}' AND '{ed}' "
        f"ORDER BY metrics.clicks DESC LIMIT {limit}"
    )
    out = await _report_search(conn, db, aid, query)
    out["start_date"], out["end_date"] = sd, ed
    return out


async def best_sellers(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    limit = _clamp(args.get("limit", 50), 50, 1, 1000)
    query = (
        "SELECT best_sellers.rank, best_sellers.previous_rank, best_sellers.report_date, "
        "best_sellers.report_granularity, product_cluster.title, product_cluster.brand "
        "FROM best_sellers_product_cluster_view "
        "WHERE best_sellers.report_granularity = 'WEEKLY' "
        f"ORDER BY best_sellers.rank LIMIT {limit}"
    )
    return await _report_search(conn, db, aid, query)


async def price_competitiveness(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    limit = _clamp(args.get("limit", 50), 50, 1, 1000)
    query = (
        "SELECT price_competitiveness.country_code, price_competitiveness.benchmark_price_micros, "
        "product_view.id, product_view.title, product_view.brand, product_view.price_micros "
        "FROM price_competitiveness_product_view "
        f"LIMIT {limit}"
    )
    return await _report_search(conn, db, aid, query)


async def price_insights(conn: Connection, db, args: dict) -> dict:
    aid = _merchant_id(conn, args)
    limit = _clamp(args.get("limit", 50), 50, 1, 1000)
    query = (
        "SELECT price_insights.suggested_price_micros, price_insights.predicted_impressions_change_fraction, "
        "price_insights.predicted_clicks_change_fraction, price_insights.predicted_conversions_change_fraction, "
        "product_view.id, product_view.title, product_view.price_micros "
        "FROM price_insights_product_view "
        f"LIMIT {limit}"
    )
    return await _report_search(conn, db, aid, query)


# --------------------------------------------------------------------------- #
# Catalog + registration
# --------------------------------------------------------------------------- #
_MERCHANT_ID_PROP = {"type": "string", "description": "Override the saved Merchant Center account ID (digits only)."}
_LIMIT_PROP = {"type": "integer", "description": "Max rows to return."}

CATALOG = {
    # Accounts
    "register_developer": {
        "description": "One-time setup: register this app's GCP project as a Merchant API developer for your "
                       "account. Required by Merchant API v1 — fixes the GCP_NOT_REGISTERED error. Auto-detects "
                       "your account ID; takes up to 5 min to propagate.",
        "write": True,
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "developer_email": {"type": "string", "description": "Developer email to register (defaults to the connected Google account)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "list_accounts": {
        "description": "Merchant Center accounts the connected Google user can access (Merchant API).",
        "input": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_account": {
        "description": "Details for a single Merchant Center account: name, time zone, test/adult flags.",
        "input": {
            "type": "object",
            "properties": {"merchant_id": _MERCHANT_ID_PROP},
            "required": [],
            "additionalProperties": False,
        },
    },
    "list_subaccounts": {
        "description": "Sub-accounts under an advanced (multi-client) account.",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "limit": {"type": "integer", "description": "Page size (default 50, max 250)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    # Products
    "list_products": {
        "description": "List products in the catalog (title, price, availability, identifiers). Paged.",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "limit": {"type": "integer", "description": "Page size (default 25, max 250)."},
                "page_token": {"type": "string", "description": "next_page_token from a prior call."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_product": {
        "description": "Full detail + status for one product by its REST id (e.g. 'online~en~US~SKU123').",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "product_id": {"type": "string", "description": "REST product id, e.g. 'online~en~US~SKU123'."},
            },
            "required": ["product_id"],
            "additionalProperties": False,
        },
    },
    # Diagnostics
    "list_product_statuses": {
        "description": "Per-item approval status and item-level issues across the catalog. Paged.",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "limit": {"type": "integer", "description": "Page size (default 25, max 250)."},
                "page_token": {"type": "string", "description": "next_page_token from a prior call."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "get_product_status": {
        "description": "Approval status and item-level issues for a single product.",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "product_id": {"type": "string", "description": "REST product id, e.g. 'online~en~US~SKU123'."},
            },
            "required": ["product_id"],
            "additionalProperties": False,
        },
    },
    "account_issues": {
        "description": "Account-level policy / data-quality issues with severity and documentation links.",
        "input": {
            "type": "object",
            "properties": {"merchant_id": _MERCHANT_ID_PROP},
            "required": [],
            "additionalProperties": False,
        },
    },
    "list_data_sources": {
        "description": "Product data sources / feeds (name, type, input).",
        "input": {
            "type": "object",
            "properties": {"merchant_id": _MERCHANT_ID_PROP},
            "required": [],
            "additionalProperties": False,
        },
    },
    # Reporting
    "report_search": {
        "description": "Run a raw Merchant API Query Language SELECT against reports.search.",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "query": {"type": "string", "description": "A Merchant API Query Language SELECT statement."},
                "page_token": {"type": "string", "description": "next_page_token from a prior call."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "product_performance": {
        "description": "Clicks, impressions, CTR, conversions per offer over a date window.",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "days": {"type": "integer", "description": "Lookback window in days (default 30)."},
                "start_date": {"type": "string", "description": "ISO start date (YYYY-MM-DD); overrides days when paired with end_date."},
                "end_date": {"type": "string", "description": "ISO end date (YYYY-MM-DD)."},
                "limit": {"type": "integer", "description": "Max rows (default 50, max 1000)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "best_sellers": {
        "description": "Best-selling product clusters (requires Best Sellers / Market Insights access).",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "limit": {"type": "integer", "description": "Max rows (default 50, max 1000)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "price_competitiveness": {
        "description": "Your price vs market benchmark per product (requires Price Competitiveness access).",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "limit": {"type": "integer", "description": "Max rows (default 50, max 1000)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "price_insights": {
        "description": "Suggested prices and predicted clicks/conversions impact (requires Price Insights access).",
        "input": {
            "type": "object",
            "properties": {
                "merchant_id": _MERCHANT_ID_PROP,
                "limit": {"type": "integer", "description": "Max rows (default 50, max 1000)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "register_developer": register_developer,
    "list_accounts": list_accounts,
    "get_account": get_account,
    "list_subaccounts": list_subaccounts,
    "list_products": list_products,
    "get_product": get_product,
    "list_product_statuses": list_product_statuses,
    "get_product_status": get_product_status,
    "account_issues": account_issues,
    "list_data_sources": list_data_sources,
    "report_search": report_search,
    "product_performance": product_performance,
    "best_sellers": best_sellers,
    "price_competitiveness": price_competitiveness,
    "price_insights": price_insights,
}

registry.register(
    Connector(
        slug="google_merchant",
        label="Google Merchant Center",
        auth="google_oauth",
        scopes=["https://www.googleapis.com/auth/content"],
        catalog=CATALOG,
        handlers=HANDLERS,
        description="Merchant Center catalog, product approval status and Shopping performance reports via the Merchant API v1.",
        category="Commerce",
    )
)
