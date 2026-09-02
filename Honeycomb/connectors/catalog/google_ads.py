"""Google Ads connector (Google Ads API REST, google_oauth).

A full port of falcon's `app/connectors/google_ads/` package -- catalog
(`tools.py`), handlers (`tools_impl.py`) and service layer (`services.py`) --
collapsed into one Honeycomb catalog module.

WHY the port is a straight copy: falcon's handlers touch the connection row for
exactly one thing, `conn.id`, and only ever as a cache-namespace key. Every
Google Ads identifier they use (customer_id, login_customer_id) arrives in the
tool `args` or is derived from `list_accounts`, never from a stored column, and
the developer token is server-level configuration. So the whole connection
surface those 2,370 lines depend on is the OAuth refresh token, which lives just
as happily in Honeycomb's encrypted creds blob as in falcon's dedicated table.
Only the four functions below the token line differ from the originals; the
handler bodies are byte-identical apart from their type annotations.

Auth: google_oauth, scope https://www.googleapis.com/auth/adwords. The OAuth
callback stores {refresh_token, scope, email} in the connection's creds. A
server-level developer token (settings.GOOGLE_ADS_DEVELOPER_TOKEN) is required
on top -- Google gates the Ads API on it independently of the user's grant.

The six mutating tools are flagged `write` in the catalog, so registry.register
derives them into write_tools and the OAuth callback switches them off on every
fresh connection. Nothing an LLM does can turn them back on -- only a human, in
the dashboard.

Endpoints used:
    POST {base}/{v}/customers/{cid}/googleAds:search        (GAQL, paginated)
    POST {base}/{v}/customers/{cid}/<resource>:mutate       (write tools)
    GET  {base}/{v}/customers:listAccessibleCustomers
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from django.conf import settings

from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, cached, invalidate
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post
from connections.models import Connection

class GoogleAdsApiError(ConnectorError):
    """Raised when a Google Ads API call fails, or an argument is unusable.

    Kept as its own type because `_with_mcc_fallback` catches it specifically to
    decide whether a PERMISSION_DENIED is worth retrying through a manager
    account. Subclassing ConnectorError is what makes the MCP endpoint render it
    as a clean tool error rather than an internal 500.
    """


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _base() -> str:
    return getattr(settings, 'GOOGLE_ADS_BASE_URL', 'https://googleads.googleapis.com').rstrip('/')


def _version() -> str:
    return getattr(settings, 'GOOGLE_ADS_API_VERSION', 'v18')


def _api_url(path: str) -> str:
    return f'{_base()}/{_version()}{path}'


def _ensure_dev_token() -> str:
    dev = getattr(settings, 'GOOGLE_ADS_DEVELOPER_TOKEN', '')
    if not dev:
        raise GoogleAdsApiError('Google Ads developer token is not configured on the server.')
    return dev


def _headers(access_token: str, login_customer_id: str | None = None) -> dict:
    """The three headers every Google Ads REST call needs.

    `login-customer-id` is how a manager (MCC) account is told apart from the
    account being queried: without it, a customer reachable only *through* an
    MCC answers PERMISSION_DENIED. Dashes are stripped because Google accepts
    only the bare digits here, while people paste the dashed form.
    """
    headers = {
        'Authorization': f'Bearer {access_token}',
        'developer-token': _ensure_dev_token(),
        'Content-Type': 'application/json',
    }
    if login_customer_id:
        headers['login-customer-id'] = str(login_customer_id).replace('-', '')
    return headers


# --------------------------------------------------------------------------- #
# Access tokens
# --------------------------------------------------------------------------- #
# Short-lived access tokens, keyed by connection id: {conn_id: (token, expiry)}.
# falcon stored the access token encrypted on its ga_connections row and wrote a
# new one back on every refresh. Honeycomb's Connection is not writable from a
# handler -- the data plane hands handlers `db=None` deliberately -- and a single
# Google Ads tool call fans out into dozens of upstream requests (the account
# tree alone queries every accessible customer), so refreshing per request would
# add a token round trip to each one. Holding the token in process memory for its
# own lifetime keeps that to one refresh per hour per connection, and the token
# never reaches the shared cache, the database, or a log line.
_TOKENS: dict[Any, tuple[str, float]] = {}
_TOKEN_SKEW = 60.0  # refresh a minute early rather than race the expiry


async def get_valid_access_token(conn: Connection, db) -> str:
    """Exchange the stored refresh token for an access token, reusing a live one.

    `db` is accepted and ignored: it is the falcon handler signature, and the
    Honeycomb endpoint passes None for it.
    """
    cached_token = _TOKENS.get(conn.id)
    if cached_token and cached_token[1] > time.monotonic():
        return cached_token[0]

    refresh = (conn.creds() or {}).get('refresh_token')
    if not refresh:
        raise GoogleAdsApiError(
            'Not connected: no Google refresh token stored. Reconnect this Google Ads '
            'connection from the Honeycomb dashboard.'
        )
    token_uri = getattr(settings, 'GOOGLE_OAUTH_TOKEN_URI', 'https://oauth2.googleapis.com/token')
    try:
        async with limit_for(token_uri):
            res = await http_post(
                token_uri,
                data={
                    'client_id': getattr(settings, 'GOOGLE_CLIENT_ID', ''),
                    'client_secret': getattr(settings, 'GOOGLE_CLIENT_SECRET', ''),
                    'refresh_token': refresh,
                    'grant_type': 'refresh_token',
                },
            )
    except UpstreamUnavailable as e:
        raise GoogleAdsApiError(f'Refresh network error: {e}') from e

    if res.status_code != 200:
        # A revoked or expired grant is the common case, and the only fix is a
        # reconnect -- say so instead of echoing Google's opaque JSON.
        _TOKENS.pop(conn.id, None)
        raise GoogleAdsApiError(
            f'Google refused the refresh token ({res.status_code}). The Google account '
            'may have revoked access -- reconnect this connection. '
            f'{res.text[:200]}'
        )

    td = res.json()
    access = td['access_token']
    try:
        ttl = int(td.get('expires_in') or 3600)
    except (TypeError, ValueError):
        ttl = 3600
    _TOKENS[conn.id] = (access, time.monotonic() + max(ttl - _TOKEN_SKEW, 30.0))
    return access


# --------------------------------------------------------------------------- #
# Service layer (ported from falcon services.py)
# --------------------------------------------------------------------------- #
async def search(customer_id: str, query: str, access_token: str,
                 login_customer_id: str | None = None) -> list[dict]:
    """Run a GAQL query via googleAds:search and return ALL result rows.

    Follows `nextPageToken` so large result sets (big MCC trees, long reports)
    are returned in full, not truncated to the first page. Bounded by a page cap
    as a safety valve against runaway loops.
    """
    url = _api_url(f'/customers/{customer_id}/googleAds:search')
    headers = _headers(access_token, login_customer_id=login_customer_id or customer_id)
    results: list[dict] = []
    page_token: str | None = None
    for _ in range(200):  # hard cap (200 * 10k rows) -- far beyond any real account
        body: dict = {'query': query}
        if page_token:
            body['pageToken'] = page_token
        try:
            async with limit_for(url):
                res = await http_post(url, headers=headers, json=body)
        except UpstreamUnavailable as e:
            raise GoogleAdsApiError(f'search {customer_id} unavailable: {e}') from e
        if res.status_code >= 400:
            raise GoogleAdsApiError(
                f'search {customer_id} failed {res.status_code}: {res.text[:1200]}')
        data = res.json()
        results.extend(data.get('results', []))
        page_token = data.get('nextPageToken')
        if not page_token:
            break
    return results


async def list_accessible_customer_ids(access_token: str) -> list[str]:
    url = _api_url('/customers:listAccessibleCustomers')
    try:
        async with limit_for(url):
            res = await http_get(url, headers=_headers(access_token))
    except UpstreamUnavailable as e:
        raise GoogleAdsApiError(f'listAccessibleCustomers unavailable: {e}') from e
    if res.status_code >= 400:
        raise GoogleAdsApiError(
            f'listAccessibleCustomers failed {res.status_code}: {res.text[:300]}')
    return [n.split('/')[-1] for n in res.json().get('resourceNames', [])]


async def get_customer_info(customer_id: str, access_token: str) -> dict:
    rows = await search(
        customer_id,
        'SELECT customer.id, customer.descriptive_name, customer.currency_code, '
        'customer.time_zone, customer.manager, customer.test_account, customer.status FROM customer',
        access_token,
        login_customer_id=customer_id,
    )
    if not rows:
        return {'id': customer_id, 'name': '', 'manager': False, 'currency': '',
                'time_zone': '', 'test_account': False, 'status': 'UNKNOWN'}
    c = rows[0].get('customer', {})
    return {
        'id': str(c.get('id', customer_id)),
        'name': c.get('descriptiveName', '') or '',
        'manager': bool(c.get('manager', False)),
        'currency': c.get('currencyCode', ''),
        'time_zone': c.get('timeZone', ''),
        'test_account': bool(c.get('testAccount', False)),
        'status': c.get('status', ''),
    }


async def list_mcc_descendants(mcc_id: str, access_token: str) -> list[dict]:
    rows = await search(
        mcc_id,
        'SELECT customer_client.id, customer_client.descriptive_name, customer_client.manager, '
        'customer_client.currency_code, customer_client.time_zone, customer_client.test_account, '
        'customer_client.status, customer_client.level FROM customer_client '
        'WHERE customer_client.level >= 1',
        access_token,
        login_customer_id=mcc_id,
    )
    out = []
    for row in rows:
        c = row.get('customerClient', {})
        out.append({
            'id': str(c.get('id', '')),
            'name': c.get('descriptiveName', '') or '',
            'manager': bool(c.get('manager', False)),
            'currency': c.get('currencyCode', ''),
            'time_zone': c.get('timeZone', ''),
            'test_account': bool(c.get('testAccount', False)),
            'status': c.get('status', ''),
            'level': int(c.get('level', 1)),
        })
    out.sort(key=lambda a: (a['level'], a['name'].lower(), a['id']))
    return out


async def fetch_account_tree(conn: Connection, db) -> dict:
    """Return {'accounts': [...]} -- each top-level account with its full MCC subtree.

    Cached for TTL_LONG (6h): hierarchies change rarely. Concurrency note: each
    accessible top-level account is fetched concurrently via asyncio.gather.
    """
    async def _load() -> dict:
        import asyncio

        access_token = await get_valid_access_token(conn, db)
        ids = await list_accessible_customer_ids(access_token)

        async def one(cid: str):
            try:
                info = await get_customer_info(cid, access_token)
            except GoogleAdsApiError as e:
                return None, {'customer_id': cid, 'error': str(e)[:200]}
            node = {**info, 'children': []}
            if info['manager']:
                try:
                    node['children'] = await list_mcc_descendants(cid, access_token)
                except GoogleAdsApiError as e:
                    return node, {'customer_id': cid, 'error': str(e)[:200]}
            return node, None

        results = await asyncio.gather(*(one(cid) for cid in ids))
        accounts = [n for n, _ in results if n]
        errors = [e for _, e in results if e]

        # Flatten to one row per queryable account, each with the MCC to log in
        # through, so callers see EVERY account (not just the top-level managers).
        fields = ('id', 'name', 'manager', 'currency', 'time_zone', 'status', 'test_account')
        all_accounts: list[dict] = []
        for acc in accounts:
            all_accounts.append({**{k: acc.get(k) for k in fields},
                                 'level': 0, 'login_customer_id': None})
            mcc = acc.get('id') if acc.get('manager') else None
            for ch in acc.get('children', []) or []:
                all_accounts.append({
                    **{k: ch.get(k) for k in fields},
                    'level': int(ch.get('level', 1)),
                    'login_customer_id': mcc,  # query a child by logging in through its MCC
                })

        by_status: dict[str, int] = {}
        for a in all_accounts:
            by_status[a.get('status') or 'UNKNOWN'] = by_status.get(a.get('status') or 'UNKNOWN', 0) + 1
        summary = {
            'total_accounts': len(all_accounts),
            'managers': sum(1 for a in all_accounts if a.get('manager')),
            'clients': sum(1 for a in all_accounts if not a.get('manager')),
            'enabled': by_status.get('ENABLED', 0),
            'by_status': by_status,
        }
        return {'accounts': accounts, 'all_accounts': all_accounts,
                'summary': summary, 'errors': errors}

    return await cached('google_ads', conn.id, 'fetch_account_tree', TTL_LONG, _load)


# ============================================================
# Handlers -- ported verbatim from falcon tools_impl.py.
# The ONLY edits below this line are the type annotations:
# `conn: GoogleAdsConnection` -> `conn: Connection`, and the dropped
# `db: AsyncSession` annotation. No tool's logic was touched.
# ============================================================

DATE_RANGES = {
    "TODAY",
    "YESTERDAY",
    "LAST_7_DAYS",
    "LAST_14_DAYS",
    "LAST_30_DAYS",
    "LAST_90_DAYS",
    "THIS_MONTH",
    "LAST_MONTH",
    "THIS_WEEK_SUN_TODAY",
    "LAST_WEEK_SUN_SAT",
    "ALL_TIME",
}

STATUS_VALUES = {"ENABLED", "PAUSED", "REMOVED"}


# ---------- helpers ----------

def _customer_id(args: dict) -> str:
    cid = str((args or {}).get("customer_id", "")).replace("-", "").strip()
    if not cid or not cid.isdigit() or len(cid) != 10:
        raise GoogleAdsApiError(
            "customer_id is required and must be a 10-digit number (no dashes). "
            "Call list_accounts first to get one, then pass it as customer_id."
        )
    return cid


async def _any_customer_id(conn: Connection, db, args: dict) -> str:
    """Used by account-agnostic tools (lookups). Returns the caller's
    customer_id if provided, else picks any accessible customer from the
    cached account tree. The Google Ads API requires *some* customer_id in
    the URL even for lookup-only queries like geo_target_constant."""
    raw = str((args or {}).get("customer_id", "")).replace("-", "").strip()
    if raw and raw.isdigit() and len(raw) == 10:
        return raw
    try:
        tree = await fetch_account_tree(conn, db)
    except Exception:  # noqa: BLE001
        tree = {}
    for acc in tree.get("accounts", []) or []:
        aid = str(acc.get("id", ""))
        if aid.isdigit() and len(aid) == 10:
            return aid
        for child in acc.get("children", []) or []:
            cid = str(child.get("id", ""))
            if cid.isdigit() and len(cid) == 10 and not child.get("manager"):
                return cid
    raise GoogleAdsApiError(
        "No accessible customer_id available — pass one explicitly."
    )


def _login_customer_id(args: dict) -> Optional[str]:
    lcid = str((args or {}).get("login_customer_id", "")).replace("-", "").strip()
    return lcid if lcid and lcid.isdigit() else None


_GA_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_range(args: dict, default: str = "LAST_30_DAYS") -> str:
    """Label for the date window. Explicit start_date+end_date -> 'sd..ed';
    otherwise the validated predefined range."""
    sd = str((args or {}).get("start_date", "")).strip()
    ed = str((args or {}).get("end_date", "")).strip()
    if sd and ed:
        return f"{sd}..{ed}"
    dr = str((args or {}).get("date_range", default)).upper().strip()
    if dr not in DATE_RANGES:
        raise GoogleAdsApiError(
            f"date_range must be one of: {', '.join(sorted(DATE_RANGES))}"
        )
    return dr


def _date_clause(args: dict, default: str = "LAST_30_DAYS") -> str:
    """GAQL `segments.date` predicate body. Explicit start_date+end_date ->
    BETWEEN (any window, no preset cap); otherwise DURING <preset>.

    GAQL has NO ALL_TIME or LAST_90_DAYS presets (those were AWQL) — Google
    400s on them — so we emulate both with BETWEEN so callers get the full
    account history they asked for."""
    sd = str((args or {}).get("start_date", "")).strip()
    ed = str((args or {}).get("end_date", "")).strip()
    if sd and ed:
        for label, val in (("start_date", sd), ("end_date", ed)):
            if not _GA_DATE_RE.match(val):
                raise GoogleAdsApiError(f"{label} must be YYYY-MM-DD (got '{val}').")
        return f"BETWEEN '{sd}' AND '{ed}'"
    dr = _date_range(args, default)
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    if dr == "ALL_TIME":
        # Google Ads launched in 2000 — this BETWEEN covers any account's lifetime.
        return f"BETWEEN '2000-01-01' AND '{today.isoformat()}'"
    if dr == "LAST_90_DAYS":
        return f"BETWEEN '{(today - _td(days=90)).isoformat()}' AND '{today.isoformat()}'"
    return f"DURING {dr}"


def _limit(args: dict, default: int = 100, max_value: int = 500) -> int:
    try:
        n = int(args.get("limit", default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, max_value))


def _micros(v: Any) -> float:
    try:
        return round(int(v or 0) / 1_000_000, 2)
    except (TypeError, ValueError):
        return 0.0


def _metrics(row: dict) -> dict:
    m = row.get("metrics", {}) or {}
    return {
        "impressions": int(m.get("impressions", 0) or 0),
        "clicks": int(m.get("clicks", 0) or 0),
        "cost": _micros(m.get("costMicros")),
        "conversions": float(m.get("conversions", 0) or 0),
        "conversion_value": float(m.get("conversionsValue", 0) or 0),
        "ctr": round(float(m.get("ctr", 0) or 0) * 100, 2),  # ratio → percent
        "avg_cpc": _micros(m.get("averageCpc")),
    }


MCC_NOTE = (
    "This is a manager (MCC) account — managers don't run ads directly so "
    "they have no campaigns / ad groups / keywords / search terms to report on. "
    "Use list_accounts to find a client account under this MCC, then query "
    "that customer_id instead."
)


async def _is_manager_account(conn: Connection, db, customer_id: str) -> bool:
    """Return True if customer_id corresponds to a manager (MCC) account in
    the cached account tree. Returns False if unknown — let the API call
    proceed and surface its own error."""
    try:
        tree = await fetch_account_tree(conn, db)
    except Exception:  # noqa: BLE001
        return False
    cid = str(customer_id)
    for acc in tree.get("accounts", []) or []:
        if str(acc.get("id", "")) == cid:
            return bool(acc.get("manager"))
        for child in acc.get("children", []) or []:
            if str(child.get("id", "")) == cid:
                return bool(child.get("manager"))
    return False


async def _manager_chain(conn: Connection, db, customer_id: str) -> list[str]:
    """Manager (MCC) ids that lead to `customer_id`, nearest ancestor first.

    Walks the (possibly multi-level) cached account tree. For a customer nested
    under sub-MCCs this returns [immediate_mcc, ..., top_mcc] so callers can try
    each as login-customer-id. Empty list if the customer isn't found.
    """
    try:
        tree = await fetch_account_tree(conn, db)
    except Exception:  # noqa: BLE001
        return []
    cid = str(customer_id)

    def dfs(nodes, ancestors):
        for n in nodes:
            if str(n.get("id", "")) == cid:
                return list(ancestors)              # [top, ..., immediate]
            hit = dfs(n.get("children") or [], ancestors + [str(n.get("id", ""))])
            if hit is not None:
                return hit
        return None

    chain = dfs(tree.get("accounts", []) or [], [])
    return list(reversed(chain)) if chain else []   # nearest MCC first


async def _all_manager_ids(conn: Connection, db) -> list[str]:
    """Every manager (MCC) id anywhere in the cached tree — a last-resort pool
    of login-customer-id candidates when the chain lookup comes up empty (e.g.
    a partial/stale tree)."""
    try:
        tree = await fetch_account_tree(conn, db)
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []

    def walk(nodes):
        for n in nodes:
            if n.get("manager"):
                out.append(str(n.get("id", "")))
            walk(n.get("children") or [])

    walk(tree.get("accounts", []) or [])
    return out


async def _with_mcc_fallback(conn: Connection, db, args: dict, fn):
    """Call `await fn(login_customer_id)`, resolving login-customer-id automatically.

    - If the caller passed an explicit login_customer_id, use it verbatim.
    - Otherwise try login=customer_id first (correct for direct-access accounts).
    - On PERMISSION_DENIED, walk the customer's manager chain (nearest MCC
      first), then any other accessible MCC, retrying until one works. This is
      what makes accounts nested under a sub-MCC reachable without the caller
      having to know which manager to log in through.

    If every candidate still denies permission, the most recent error is
    re-raised so the (legitimate) failure surfaces honestly.
    """
    cid = _customer_id(args)
    user_lcid = _login_customer_id(args)
    if user_lcid:
        return await fn(user_lcid)

    tried = {cid}
    try:
        return await fn(cid)
    except GoogleAdsApiError as e:
        if "PERMISSION_DENIED" not in str(e):
            raise
        last_error = e

    candidates = await _manager_chain(conn, db, cid)
    for mgr in await _all_manager_ids(conn, db):
        if mgr not in candidates:
            candidates.append(mgr)

    for lcid in candidates:
        if not lcid or lcid in tried:
            continue
        tried.add(lcid)
        try:
            return await fn(lcid)
        except GoogleAdsApiError as e:
            if "PERMISSION_DENIED" not in str(e):
                raise
            last_error = e

    raise last_error


async def _execute_search(conn: Connection, db, args: dict, query: str) -> list[dict]:
    cid = _customer_id(args)
    token = await get_valid_access_token(conn, db)

    async def _do(lcid: str) -> list[dict]:
        return await search(cid, query, token, login_customer_id=lcid)

    return await _with_mcc_fallback(conn, db, args, _do)


async def _mutate(
    conn: Connection,
    db,
    args: dict,
    *,
    resource_path: str,
    operations: list[dict],
) -> dict:
    """POST to /customers/{cid}/<resource_path>:mutate with the given operations.

    If args.validate_only is True, the request is sent with validateOnly=true
    so Google validates the operation without actually applying it — useful
    for dry-runs from tools that mutate live ad accounts.
    """
    cid = _customer_id(args)
    token = await get_valid_access_token(conn, db)
    url = _api_url(f"/customers/{cid}/{resource_path}:mutate")
    validate_only = bool(args.get("validate_only", False))
    body: dict[str, Any] = {"operations": operations}
    if validate_only:
        body["validateOnly"] = True

    async def _do(lcid: str) -> dict:
        try:
            async with limit_for(url):
                res = await http_post(
                    url,
                    headers=_headers(token, login_customer_id=lcid),
                    json=body,
                )
        except UpstreamUnavailable as e:
            raise GoogleAdsApiError(f"mutate unavailable: {e}") from e
        if res.status_code >= 400:
            raise GoogleAdsApiError(f"mutate failed {res.status_code}: {res.text[:600]}")
        data = res.json() or {}
        if validate_only:
            data["_validate_only"] = True
        return data

    result = await _with_mcc_fallback(conn, db, args, _do)
    await invalidate("google_ads", conn.id)
    return result


def _normalize_status(value: str) -> str:
    v = str(value or "").upper().strip()
    if v in {"ON", "ENABLE", "ENABLED", "RESUME", "ACTIVE"}:
        return "ENABLED"
    if v in {"OFF", "PAUSE", "PAUSED", "DISABLED"}:
        return "PAUSED"
    raise GoogleAdsApiError("status must be ENABLED/PAUSED (or pause/resume)")


# ============================================================
# ACCOUNTS
# ============================================================

async def list_accounts(conn: Connection, db, args: dict) -> dict:
    # `refresh: true` busts the 6h hierarchy cache — use it if newly-added child
    # accounts (or ones that errored on first connect) aren't showing up yet.
    if args and args.get("refresh"):
        await invalidate("google_ads", conn.id)
    return await fetch_account_tree(conn, db)


# ============================================================
# LIST tools
# ============================================================

async def list_campaigns(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args, 100, 500)
    status = (args.get("status") or "").upper().strip()
    where = f"WHERE campaign.status = '{status}'" if status in STATUS_VALUES else ""
    query = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.resource_name, "
        "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
        "campaign_budget.resource_name, campaign_budget.id, campaign_budget.amount_micros "
        f"FROM campaign {where} ORDER BY campaign.id LIMIT {limit}"
    ).strip()

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            c = r.get("campaign", {})
            b = r.get("campaignBudget", {}) or {}
            out.append({
                "id": str(c.get("id", "")),
                "name": c.get("name", ""),
                "status": c.get("status", ""),
                "resource_name": c.get("resourceName", ""),  # pass to pause_resume_campaign
                "channel_type": c.get("advertisingChannelType", ""),
                "bidding_strategy_type": c.get("biddingStrategyType", ""),
                "daily_budget": _micros(b.get("amountMicros")),
                "budget_id": str(b.get("id", "")),
                "budget_resource_name": b.get("resourceName", ""),  # pass to update_budget
            })
        return {"customer_id": cid, "count": len(out), "campaigns": out}

    return await cached("google_ads", conn.id, "list_campaigns", TTL_MEDIUM, _load, args=args)


async def list_ad_groups(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args)
    campaign_id = str(args.get("campaign_id", "")).strip()
    where_parts = []
    if campaign_id and campaign_id.isdigit():
        where_parts.append(f"campaign.id = {campaign_id}")
    status = (args.get("status") or "").upper().strip()
    if status in STATUS_VALUES:
        where_parts.append(f"ad_group.status = '{status}'")
    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    query = (
        "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.type, "
        "ad_group.cpc_bid_micros, campaign.id, campaign.name "
        f"FROM ad_group {where} ORDER BY ad_group.id LIMIT {limit}"
    ).strip()

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            a = r.get("adGroup", {})
            c = r.get("campaign", {})
            out.append({
                "id": str(a.get("id", "")),
                "name": a.get("name", ""),
                "status": a.get("status", ""),
                "type": a.get("type", ""),
                "default_cpc_bid": _micros(a.get("cpcBidMicros")),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
            })
        return {"customer_id": cid, "count": len(out), "ad_groups": out}

    return await cached("google_ads", conn.id, "list_ad_groups", TTL_MEDIUM, _load, args=args)


async def list_ads(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args)
    ad_group_id = str(args.get("ad_group_id", "")).strip()
    where_parts = []
    if ad_group_id and ad_group_id.isdigit():
        where_parts.append(f"ad_group.id = {ad_group_id}")
    status = (args.get("status") or "").upper().strip()
    if status in STATUS_VALUES:
        where_parts.append(f"ad_group_ad.status = '{status}'")
    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    query = (
        "SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.ad.type, "
        "ad_group_ad.status, ad_group_ad.policy_summary.approval_status, "
        "ad_group_ad.ad.final_urls, "
        "ad_group.id, ad_group.name "
        f"FROM ad_group_ad {where} ORDER BY ad_group_ad.ad.id LIMIT {limit}"
    ).strip()

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            aga = r.get("adGroupAd", {}) or {}
            ad = aga.get("ad", {}) or {}
            ag = r.get("adGroup", {}) or {}
            out.append({
                "id": str(ad.get("id", "")),
                "name": ad.get("name", ""),
                "type": ad.get("type", ""),
                "status": aga.get("status", ""),
                "approval_status": (aga.get("policySummary", {}) or {}).get("approvalStatus", ""),
                "final_urls": ad.get("finalUrls", []),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
            })
        return {"customer_id": cid, "count": len(out), "ads": out}

    return await cached("google_ads", conn.id, "list_ads", TTL_MEDIUM, _load, args=args)


async def list_keywords(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args)
    ad_group_id = str(args.get("ad_group_id", "")).strip()
    where_parts = ["ad_group_criterion.type = 'KEYWORD'", "ad_group_criterion.negative = FALSE"]
    if ad_group_id and ad_group_id.isdigit():
        where_parts.append(f"ad_group.id = {ad_group_id}")
    status = (args.get("status") or "").upper().strip()
    if status in STATUS_VALUES:
        where_parts.append(f"ad_group_criterion.status = '{status}'")
    where = "WHERE " + " AND ".join(where_parts)
    query = (
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
        "ad_group_criterion.cpc_bid_micros, ad_group_criterion.quality_info.quality_score, "
        "ad_group.id, ad_group.name "
        f"FROM keyword_view {where} ORDER BY ad_group_criterion.criterion_id LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            agc = r.get("adGroupCriterion", {}) or {}
            kw = agc.get("keyword", {}) or {}
            qi = agc.get("qualityInfo", {}) or {}
            ag = r.get("adGroup", {}) or {}
            out.append({
                "criterion_id": str(agc.get("criterionId", "")),
                "text": kw.get("text", ""),
                "match_type": kw.get("matchType", ""),
                "status": agc.get("status", ""),
                "max_cpc": _micros(agc.get("cpcBidMicros")),
                "quality_score": qi.get("qualityScore"),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
            })
        return {"customer_id": cid, "count": len(out), "keywords": out}

    return await cached("google_ads", conn.id, "list_keywords", TTL_MEDIUM, _load, args=args)


async def list_negative_keywords(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args, 200, 1000)
    camp_q = (
        "SELECT campaign_criterion.criterion_id, campaign_criterion.keyword.text, "
        "campaign_criterion.keyword.match_type, campaign.id, campaign.name "
        "FROM campaign_criterion "
        "WHERE campaign_criterion.type = 'KEYWORD' AND campaign_criterion.negative = TRUE "
        f"LIMIT {limit}"
    )
    ag_q = (
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group.id, ad_group.name, campaign.id "
        "FROM ad_group_criterion "
        "WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.negative = TRUE "
        f"LIMIT {limit}"
    )

    async def _load() -> dict:
        camp_rows = await _execute_search(conn, db, args, camp_q)
        ag_rows = await _execute_search(conn, db, args, ag_q)

        campaign_negs = []
        for r in camp_rows:
            cc = r.get("campaignCriterion", {}) or {}
            kw = cc.get("keyword", {}) or {}
            c = r.get("campaign", {}) or {}
            campaign_negs.append({
                "criterion_id": str(cc.get("criterionId", "")),
                "text": kw.get("text", ""),
                "match_type": kw.get("matchType", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
            })
        ad_group_negs = []
        for r in ag_rows:
            agc = r.get("adGroupCriterion", {}) or {}
            kw = agc.get("keyword", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            ad_group_negs.append({
                "criterion_id": str(agc.get("criterionId", "")),
                "text": kw.get("text", ""),
                "match_type": kw.get("matchType", ""),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
            })
        return {
            "customer_id": cid,
            "campaign_negatives": campaign_negs,
            "ad_group_negatives": ad_group_negs,
            "counts": {
                "campaign": len(campaign_negs),
                "ad_group": len(ad_group_negs),
            },
        }

    return await cached("google_ads", conn.id, "list_negative_keywords", TTL_MEDIUM, _load, args=args)


async def list_assets(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args)
    asset_type = (args.get("asset_type") or "").upper().strip()
    where = f"WHERE asset.type = '{asset_type}'" if asset_type else ""
    query = (
        "SELECT asset.id, asset.name, asset.type, asset.resource_name, "
        "asset.text_asset.text, asset.image_asset.full_size.url, "
        "asset.sitelink_asset.link_text, asset.sitelink_asset.description1, "
        "asset.callout_asset.callout_text "
        f"FROM asset {where} ORDER BY asset.id LIMIT {limit}"
    ).strip()

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            a = r.get("asset", {}) or {}
            text = (a.get("textAsset", {}) or {}).get("text", "")
            sitelink = a.get("sitelinkAsset", {}) or {}
            callout = (a.get("calloutAsset", {}) or {}).get("calloutText", "")
            image_url = ((a.get("imageAsset", {}) or {}).get("fullSize", {}) or {}).get("url", "")
            out.append({
                "id": str(a.get("id", "")),
                "name": a.get("name", ""),
                "type": a.get("type", ""),
                "resource_name": a.get("resourceName", ""),
                "text": text or sitelink.get("linkText", "") or callout,
                "description": sitelink.get("description1", ""),
                "image_url": image_url,
            })
        return {"customer_id": cid, "count": len(out), "assets": out}

    return await cached("google_ads", conn.id, "list_assets", TTL_MEDIUM, _load, args=args)


async def list_conversion_actions(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args)
    query = (
        "SELECT conversion_action.id, conversion_action.name, conversion_action.category, "
        "conversion_action.status, conversion_action.type, conversion_action.include_in_conversions_metric, "
        "conversion_action.value_settings.default_value "
        f"FROM conversion_action ORDER BY conversion_action.id LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            ca = r.get("conversionAction", {}) or {}
            vs = ca.get("valueSettings", {}) or {}
            out.append({
                "id": str(ca.get("id", "")),
                "name": ca.get("name", ""),
                "category": ca.get("category", ""),
                "status": ca.get("status", ""),
                "type": ca.get("type", ""),
                "include_in_conversions": bool(ca.get("includeInConversionsMetric", False)),
                "default_value": float(vs.get("defaultValue", 0) or 0),
            })
        return {"customer_id": cid, "count": len(out), "conversion_actions": out}

    return await cached("google_ads", conn.id, "list_conversion_actions", TTL_MEDIUM, _load, args=args)


async def list_recommendations(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args)
    rec_type = (args.get("type") or "").upper().strip()
    where = f"WHERE recommendation.type = '{rec_type}'" if rec_type else ""
    query = (
        "SELECT recommendation.resource_name, recommendation.type "
        f"FROM recommendation {where} LIMIT {limit}"
    ).strip()

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            rec = r.get("recommendation", {}) or {}
            out.append({
                "resource_name": rec.get("resourceName", ""),
                "type": rec.get("type", ""),
            })
        return {"customer_id": cid, "count": len(out), "recommendations": out}

    return await cached("google_ads", conn.id, "list_recommendations", TTL_MEDIUM, _load, args=args)


# ============================================================
# PERFORMANCE tools
# ============================================================

def _segment_clause(args: dict) -> str:
    seg = (args.get("segment") or "").upper().strip()
    if seg == "DATE":
        return ", segments.date"
    if seg == "DEVICE":
        return ", segments.device"
    if seg == "WEEK":
        return ", segments.week"
    if seg == "MONTH":
        return ", segments.month"
    return ""


async def get_campaign_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    seg = _segment_clause(args)
    query = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc"
        + seg +
        f" FROM campaign WHERE segments.date {_date_clause(args)} ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            c = r.get("campaign", {}) or {}
            segs = r.get("segments", {}) or {}
            entry = {
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                "status": c.get("status", ""),
                **_metrics(r),
            }
            if "date" in segs: entry["date"] = segs["date"]
            if "device" in segs: entry["device"] = segs["device"]
            if "week" in segs: entry["week"] = segs["week"]
            if "month" in segs: entry["month"] = segs["month"]
            out.append(entry)
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_campaign_performance", TTL_MEDIUM, _load, args=args)


async def get_ad_group_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    seg = _segment_clause(args)
    query = (
        "SELECT ad_group.id, ad_group.name, campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc"
        + seg +
        f" FROM ad_group WHERE segments.date {_date_clause(args)} ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            out.append({
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **_metrics(r),
                **(r.get("segments", {}) or {}),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_ad_group_performance", TTL_MEDIUM, _load, args=args)


async def get_keyword_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.quality_info.quality_score, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM keyword_view WHERE segments.date {_date_clause(args)} ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            agc = r.get("adGroupCriterion", {}) or {}
            kw = agc.get("keyword", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            out.append({
                "criterion_id": str(agc.get("criterionId", "")),
                "text": kw.get("text", ""),
                "match_type": kw.get("matchType", ""),
                "quality_score": (agc.get("qualityInfo", {}) or {}).get("qualityScore"),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_keyword_performance", TTL_MEDIUM, _load, args=args)


async def get_search_terms(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT search_term_view.search_term, segments.search_term_match_type, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM search_term_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.impressions DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            stv = r.get("searchTermView", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            segs = r.get("segments", {}) or {}
            out.append({
                "search_term": stv.get("searchTerm", ""),
                "match_type": segs.get("searchTermMatchType", ""),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_search_terms", TTL_MEDIUM, _load, args=args)


async def get_geo_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT geographic_view.country_criterion_id, geographic_view.location_type, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM geographic_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            gv = r.get("geographicView", {}) or {}
            out.append({
                "country_criterion_id": str(gv.get("countryCriterionId", "")),
                "location_type": gv.get("locationType", ""),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_geo_performance", TTL_MEDIUM, _load, args=args)


async def get_device_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    query = (
        "SELECT segments.device, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM customer WHERE segments.date {_date_clause(args)}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            segs = r.get("segments", {}) or {}
            out.append({"device": segs.get("device", "UNKNOWN"), **_metrics(r)})
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_device_performance", TTL_MEDIUM, _load, args=args)


async def get_audience_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.type, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM ad_group_audience_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            agc = r.get("adGroupCriterion", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            out.append({
                "criterion_id": str(agc.get("criterionId", "")),
                "audience_type": agc.get("type", ""),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_audience_performance", TTL_MEDIUM, _load, args=args)


# ============================================================
# DEMOGRAPHIC + EXTRA SEGMENT BREAKDOWNS
# ============================================================

# breakdown key -> (GAQL view, criterion type field, response object key)
_DEMOGRAPHIC_VIEWS = {
    "age_range":       ("age_range_view",       "ad_group_criterion.age_range.type",       "ageRange"),
    "gender":          ("gender_view",          "ad_group_criterion.gender.type",          "gender"),
    "parental_status": ("parental_status_view", "ad_group_criterion.parental_status.type", "parentalStatus"),
    "income_range":    ("income_range_view",    "ad_group_criterion.income_range.type",    "incomeRange"),
}


async def get_demographic_performance(conn: Connection, db, args: dict) -> dict:
    """Performance by demographic segment. `breakdown` picks the dimension:
    age_range (default), gender, parental_status, or income_range."""
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    breakdown = (args.get("breakdown") or "age_range").lower().strip()
    if breakdown not in _DEMOGRAPHIC_VIEWS:
        raise GoogleAdsApiError(
            "breakdown must be one of: " + ", ".join(sorted(_DEMOGRAPHIC_VIEWS))
        )
    view, type_field, obj_key = _DEMOGRAPHIC_VIEWS[breakdown]
    query = (
        f"SELECT {type_field}, ad_group.id, ad_group.name, campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM {view} WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            agc = r.get("adGroupCriterion", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            out.append({
                "segment": (agc.get(obj_key, {}) or {}).get("type", ""),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "breakdown": breakdown,
                "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_demographic_performance", TTL_MEDIUM, _load, args=args)


async def _segment_perf(conn: Connection, db, args: dict,
                        segment_field: str, segment_key: str, tool: str) -> dict:
    """Helper: query segments.<segment_field> from customer with standard metrics."""
    cid = _customer_id(args)
    dr = _date_range(args)
    query = (
        f"SELECT segments.{segment_field}, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM customer WHERE segments.date {_date_clause(args)}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            s = r.get("segments", {}) or {}
            out.append({segment_key: s.get(segment_key, s.get(segment_field)), **_metrics(r)})
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, tool, TTL_MEDIUM, _load, args=args)


async def get_network_performance(conn: Connection, db, args: dict) -> dict:
    """Split by ad network type: Search, Search Partners, Display/Content, YouTube."""
    return await _segment_perf(conn, db, args, "ad_network_type", "adNetworkType", "get_network_performance")


async def get_click_type_performance(conn: Connection, db, args: dict) -> dict:
    """Split by click type: headline, sitelink, call, and other click interactions."""
    return await _segment_perf(conn, db, args, "click_type", "clickType", "get_click_type_performance")


async def get_hourly_performance(conn: Connection, db, args: dict) -> dict:
    return await _segment_perf(conn, db, args, "hour", "hour", "get_hourly_performance")


async def get_day_of_week_performance(conn: Connection, db, args: dict) -> dict:
    return await _segment_perf(conn, db, args, "day_of_week", "dayOfWeek", "get_day_of_week_performance")


async def get_ad_position_performance(conn: Connection, db, args: dict) -> dict:
    """Top / absolute-top impression rate per campaign (modern position metric)."""
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT campaign.id, campaign.name, "
        "metrics.top_impression_percentage, metrics.absolute_top_impression_percentage, "
        "metrics.search_top_impression_share, metrics.search_absolute_top_impression_share, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM campaign WHERE segments.date {_date_clause(args)} "
        "AND metrics.impressions > 0 "
        f"ORDER BY metrics.impressions DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            c = r.get("campaign", {}) or {}
            m = r.get("metrics", {}) or {}
            out.append({
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                "top_impression_pct": round(float(m.get("topImpressionPercentage", 0) or 0) * 100, 2),
                "absolute_top_impression_pct": round(float(m.get("absoluteTopImpressionPercentage", 0) or 0) * 100, 2),
                "search_top_is": round(float(m.get("searchTopImpressionShare", 0) or 0) * 100, 2),
                "search_abs_top_is": round(float(m.get("searchAbsoluteTopImpressionShare", 0) or 0) * 100, 2),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_ad_position_performance", TTL_MEDIUM, _load, args=args)


async def get_placement_performance(conn: Connection, db, args: dict) -> dict:
    """Display / Video placements where ads actually showed."""
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT group_placement_view.display_name, group_placement_view.placement, "
        "group_placement_view.placement_type, group_placement_view.target_url, "
        "campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM group_placement_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            gpv = r.get("groupPlacementView", {}) or {}
            c = r.get("campaign", {}) or {}
            out.append({
                "display_name": gpv.get("displayName", ""),
                "placement": gpv.get("placement", ""),
                "placement_type": gpv.get("placementType", ""),
                "target_url": gpv.get("targetUrl", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_placement_performance", TTL_MEDIUM, _load, args=args)


async def get_topic_performance(conn: Connection, db, args: dict) -> dict:
    """Display Network topic / content-targeting performance (topic_view)."""
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT ad_group_criterion.topic.path, ad_group_criterion.criterion_id, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM topic_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            agc = r.get("adGroupCriterion", {}) or {}
            path = (agc.get("topic", {}) or {}).get("path", []) or []
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            out.append({
                "criterion_id": str(agc.get("criterionId", "")),
                "topic": " > ".join(path) if isinstance(path, list) else str(path),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_topic_performance", TTL_MEDIUM, _load, args=args)


async def get_geo_presence_performance(conn: Connection, db, args: dict) -> dict:
    """Physical location (presence) vs location of interest split."""
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT geographic_view.location_type, geographic_view.country_criterion_id, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value "
        f"FROM geographic_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        buckets: dict[str, dict] = {}
        out = []
        for r in rows:
            gv = r.get("geographicView", {}) or {}
            ltype = gv.get("locationType", "UNKNOWN")
            m = _metrics(r)
            b = buckets.setdefault(ltype, {
                "impressions": 0, "clicks": 0, "cost": 0.0,
                "conversions": 0.0, "conversion_value": 0.0, "locations": 0,
            })
            b["impressions"] += m["impressions"]
            b["clicks"] += m["clicks"]
            b["cost"] = round(b["cost"] + m["cost"], 2)
            b["conversions"] = round(b["conversions"] + m["conversions"], 2)
            b["conversion_value"] = round(b["conversion_value"] + m["conversion_value"], 2)
            b["locations"] += 1
            out.append({
                "location_type": ltype,
                "country_criterion_id": str(gv.get("countryCriterionId", "")),
                **m,
            })
        return {
            "customer_id": cid,
            "date_range": dr,
            "summary": buckets,
            "count": len(out),
            "rows": out,
            "note": "LOCATION_OF_PRESENCE = where the user physically was; "
                    "AREA_OF_INTEREST = a location they showed interest in. Compare the "
                    "summary buckets to see how much spend/conversions came from each.",
        }

    return await cached("google_ads", conn.id, "get_geo_presence_performance", TTL_MEDIUM, _load, args=args)


async def get_call_details(conn: Connection, db, args: dict) -> dict:
    """Call-tracking detail records (call_view)."""
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args, 100, 1000)
    query = (
        "SELECT call_view.caller_country_code, call_view.caller_area_code, "
        "call_view.call_duration_seconds, call_view.call_status, call_view.type, "
        "call_view.call_tracking_display_location, call_view.start_call_date_time, "
        "campaign.id, campaign.name "
        f"FROM call_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY call_view.start_call_date_time DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        try:
            rows = await _execute_search(conn, db, args, query)
        except GoogleAdsApiError as e:
            return {
                "customer_id": cid, "date_range": dr, "count": 0, "calls": [],
                "note": f"call_view unavailable for this account ({str(e)[:160]}). "
                        "This usually means no call assets / call reporting is set up.",
            }
        out = []
        for r in rows:
            cv = r.get("callView", {}) or {}
            c = r.get("campaign", {}) or {}
            out.append({
                "start": cv.get("startCallDateTime", ""),
                "duration_seconds": int(cv.get("callDurationSeconds", 0) or 0),
                "status": cv.get("callStatus", ""),
                "type": cv.get("type", ""),
                "display_location": cv.get("callTrackingDisplayLocation", ""),
                "caller_country_code": cv.get("callerCountryCode", ""),
                "caller_area_code": cv.get("callerAreaCode", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "calls": out}

    return await cached("google_ads", conn.id, "get_call_details", TTL_MEDIUM, _load, args=args)


# ============================================================
# DEEP PERFORMANCE TOOLS
# ============================================================

async def search_query_analysis(conn: Connection, db, args: dict) -> dict:
    """Bucket search terms into wasteful vs winning (high ROAS)."""
    cid = _customer_id(args)
    dr = _date_range(args)
    pull_limit = _limit(args, 200, 2000)
    min_cost = float(args.get("min_cost", 0.5))
    query = (
        "SELECT search_term_view.search_term, segments.search_term_match_type, "
        "ad_group.name, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, "
        "metrics.conversions, metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM search_term_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {pull_limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        flat = []
        for r in rows:
            m = _metrics(r)
            flat.append({
                "term": (r.get("searchTermView", {}) or {}).get("searchTerm", ""),
                "match_type": (r.get("segments", {}) or {}).get("searchTermMatchType", ""),
                "ad_group": (r.get("adGroup", {}) or {}).get("name", ""),
                "campaign": (r.get("campaign", {}) or {}).get("name", ""),
                **m,
                "cost_per_conversion": round(m["cost"] / m["conversions"], 2) if m["conversions"] else None,
                "roas": round(m["conversion_value"] / m["cost"], 2) if m["cost"] else None,
            })
        wasteful = sorted(
            [t for t in flat if t["cost"] >= min_cost and t["conversions"] == 0],
            key=lambda t: -t["cost"],
        )[:50]
        winning = sorted(
            [t for t in flat if t["conversions"] > 0],
            key=lambda t: -(t["roas"] or 0),
        )[:50]
        total_cost = sum(t["cost"] for t in flat)
        wasted_cost = sum(t["cost"] for t in wasteful)
        return {
            "customer_id": cid,
            "date_range": dr,
            "summary": {
                "terms_examined": len(flat),
                "total_cost": round(total_cost, 2),
                "wasted_cost": round(wasted_cost, 2),
                "waste_percent": round(wasted_cost / total_cost * 100, 2) if total_cost else 0.0,
            },
            "top_wasters": wasteful,
            "top_winners": winning,
        }

    return await cached("google_ads", conn.id, "search_query_analysis", TTL_MEDIUM, _load, args=args)


async def get_asset_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    field_type = (args.get("field_type") or "").upper().strip()
    where = [f"segments.date {_date_clause(args)}"]
    if field_type:
        where.append(f"ad_group_ad_asset_view.field_type = '{field_type}'")
    query = (
        "SELECT asset.id, asset.name, asset.type, "
        "asset.text_asset.text, "
        "ad_group_ad_asset_view.field_type, ad_group_ad_asset_view.performance_label, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr "
        f"FROM ad_group_ad_asset_view WHERE {' AND '.join(where)} "
        f"ORDER BY metrics.impressions DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            a = r.get("asset", {}) or {}
            v = r.get("adGroupAdAssetView", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            out.append({
                "asset_id": str(a.get("id", "")),
                "asset_name": a.get("name", ""),
                "asset_type": a.get("type", ""),
                "text": (a.get("textAsset", {}) or {}).get("text", ""),
                "field_type": v.get("fieldType", ""),
                "performance_label": v.get("performanceLabel", ""),
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **_metrics(r),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_asset_performance", TTL_MEDIUM, _load, args=args)


async def get_pmax_asset_group_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT asset_group.id, asset_group.name, asset_group.status, "
        "campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM asset_group WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            ag = r.get("assetGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            m = _metrics(r)
            out.append({
                "asset_group_id": str(ag.get("id", "")),
                "asset_group_name": ag.get("name", ""),
                "status": ag.get("status", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                **m,
                "roas": round(m["conversion_value"] / m["cost"], 2) if m["cost"] else None,
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_pmax_asset_group_performance", TTL_MEDIUM, _load, args=args)


async def get_landing_page_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT landing_page_view.unexpanded_final_url, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr, metrics.average_cpc "
        f"FROM landing_page_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            lpv = r.get("landingPageView", {}) or {}
            m = _metrics(r)
            clicks = m["clicks"]
            conv_rate = round(m["conversions"] / clicks * 100, 2) if clicks else 0.0
            out.append({
                "url": lpv.get("unexpandedFinalUrl", ""),
                **m,
                "conversion_rate": conv_rate,
                "roas": round(m["conversion_value"] / m["cost"], 2) if m["cost"] else None,
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_landing_page_performance", TTL_MEDIUM, _load, args=args)


async def get_product_performance(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args, 100, 1000)
    query = (
        "SELECT segments.product_item_id, segments.product_title, segments.product_brand, "
        "segments.product_type_l1, segments.product_type_l2, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
        "metrics.conversions_value, metrics.ctr "
        f"FROM shopping_performance_view WHERE segments.date {_date_clause(args)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            s = r.get("segments", {}) or {}
            m = _metrics(r)
            out.append({
                "item_id": s.get("productItemId", ""),
                "title": s.get("productTitle", ""),
                "brand": s.get("productBrand", ""),
                "product_type_l1": s.get("productTypeL1", ""),
                "product_type_l2": s.get("productTypeL2", ""),
                **m,
                "roas": round(m["conversion_value"] / m["cost"], 2) if m["cost"] else None,
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_product_performance", TTL_MEDIUM, _load, args=args)


async def get_conversion_lag(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    query = (
        "SELECT segments.conversion_lag_bucket, "
        "metrics.conversions, metrics.conversions_value "
        f"FROM customer WHERE segments.date {_date_clause(args)}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            s = r.get("segments", {}) or {}
            m = r.get("metrics", {}) or {}
            out.append({
                "bucket": s.get("conversionLagBucket", ""),
                "conversions": float(m.get("conversions", 0) or 0),
                "conversion_value": float(m.get("conversionsValue", 0) or 0),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_conversion_lag", TTL_MEDIUM, _load, args=args)


# ============================================================
# OPTIMIZATION (mutates + suggestions)
# ============================================================

async def pause_resume_campaign(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    rn = str(args.get("resource_name", "")).strip()
    if not rn.startswith(f"customers/{cid}/campaigns/"):
        raise GoogleAdsApiError(
            "resource_name must look like 'customers/<cid>/campaigns/<campaign_id>'"
        )
    new_status = _normalize_status(args.get("status") or args.get("action"))
    op = {
        "update": {"resourceName": rn, "status": new_status},
        "updateMask": "status",
    }
    res = await _mutate(conn, db, args, resource_path="campaigns", operations=[op])
    return {"customer_id": cid, "resource_name": rn, "new_status": new_status, "result": res}


async def pause_resume_ad_group(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    rn = str(args.get("resource_name", "")).strip()
    if not rn.startswith(f"customers/{cid}/adGroups/"):
        raise GoogleAdsApiError(
            "resource_name must look like 'customers/<cid>/adGroups/<ad_group_id>'"
        )
    new_status = _normalize_status(args.get("status") or args.get("action"))
    op = {
        "update": {"resourceName": rn, "status": new_status},
        "updateMask": "status",
    }
    res = await _mutate(conn, db, args, resource_path="adGroups", operations=[op])
    return {"customer_id": cid, "resource_name": rn, "new_status": new_status, "result": res}


async def pause_resume_keyword(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    rn = str(args.get("resource_name", "")).strip()
    if not rn.startswith(f"customers/{cid}/adGroupCriteria/"):
        raise GoogleAdsApiError(
            "resource_name must look like 'customers/<cid>/adGroupCriteria/<ad_group_id>~<criterion_id>'"
        )
    new_status = _normalize_status(args.get("status") or args.get("action"))
    op = {
        "update": {"resourceName": rn, "status": new_status},
        "updateMask": "status",
    }
    res = await _mutate(conn, db, args, resource_path="adGroupCriteria", operations=[op])
    return {"customer_id": cid, "resource_name": rn, "new_status": new_status, "result": res}


async def update_budget(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    rn = str(args.get("resource_name", "")).strip()
    if not rn.startswith(f"customers/{cid}/campaignBudgets/"):
        raise GoogleAdsApiError(
            "resource_name must be a campaign_budget like 'customers/<cid>/campaignBudgets/<id>'. "
            "Tip: list_campaigns returns campaign_budget.amount_micros — fetch the budget resource_name via GAQL if needed."
        )
    daily_amount = args.get("daily_amount")
    if daily_amount is None:
        raise GoogleAdsApiError("daily_amount is required (in account currency, e.g. 50.0).")
    try:
        micros = int(round(float(daily_amount) * 1_000_000))
    except (TypeError, ValueError):
        raise GoogleAdsApiError("daily_amount must be a number")
    op = {
        "update": {"resourceName": rn, "amountMicros": str(micros)},
        "updateMask": "amount_micros",
    }
    res = await _mutate(conn, db, args, resource_path="campaignBudgets", operations=[op])
    return {"customer_id": cid, "resource_name": rn, "new_daily_amount": float(daily_amount), "result": res}


async def update_bid(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    rn = str(args.get("resource_name", "")).strip()
    bid = args.get("cpc_bid")
    if bid is None:
        raise GoogleAdsApiError("cpc_bid is required (in account currency, e.g. 1.25).")
    try:
        micros = int(round(float(bid) * 1_000_000))
    except (TypeError, ValueError):
        raise GoogleAdsApiError("cpc_bid must be a number")

    if rn.startswith(f"customers/{cid}/adGroupCriteria/"):
        path = "adGroupCriteria"
    elif rn.startswith(f"customers/{cid}/adGroups/"):
        path = "adGroups"
    else:
        raise GoogleAdsApiError(
            "resource_name must be an ad_group_criterion or ad_group under this customer."
        )
    op = {
        "update": {"resourceName": rn, "cpcBidMicros": str(micros)},
        "updateMask": "cpc_bid_micros",
    }
    res = await _mutate(conn, db, args, resource_path=path, operations=[op])
    return {"customer_id": cid, "resource_name": rn, "new_cpc_bid": float(bid), "result": res}


async def negative_keyword_suggestions(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    min_cost = float(args.get("min_cost", 1.0))  # account currency
    max_conversions = float(args.get("max_conversions", 0.0))
    limit = _limit(args, 50, 500)

    query = (
        "SELECT search_term_view.search_term, segments.search_term_match_type, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name, "
        "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions "
        f"FROM search_term_view WHERE segments.date {_date_clause(args)} "
        f"AND metrics.cost_micros >= {int(min_cost * 1_000_000)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit * 3}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        suggestions = []
        for r in rows:
            m = r.get("metrics", {}) or {}
            cost = _micros(m.get("costMicros"))
            conv = float(m.get("conversions", 0) or 0)
            if cost < min_cost or conv > max_conversions:
                continue
            stv = r.get("searchTermView", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            confidence = min(1.0, cost / max(min_cost * 5, 1.0))
            suggestions.append({
                "term": stv.get("searchTerm", ""),
                "wasted_cost": cost,
                "clicks": int(m.get("clicks", 0) or 0),
                "conversions": conv,
                "ad_group_id": str(ag.get("id", "")),
                "ad_group_name": ag.get("name", ""),
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                "confidence": round(confidence, 2),
                "suggested_match_type": "PHRASE",
            })
            if len(suggestions) >= limit:
                break
        return {
            "customer_id": cid,
            "date_range": dr,
            "criteria": {"min_cost": min_cost, "max_conversions": max_conversions},
            "count": len(suggestions),
            "suggestions": suggestions,
        }

    return await cached("google_ads", conn.id, "negative_keyword_suggestions", TTL_MEDIUM, _load, args=args)


async def keyword_ideas(conn: Connection, db, args: dict) -> dict:
    """Use the Google Ads `:generateKeywordIdeas` endpoint."""
    cid = _customer_id(args)
    seeds = args.get("keywords") or args.get("seed_keywords") or []
    url_seed = str(args.get("page_url", "")).strip()
    if isinstance(seeds, str):
        seeds = [s.strip() for s in seeds.split(",") if s.strip()]
    if not seeds and not url_seed:
        raise GoogleAdsApiError("Provide `keywords` (list) and/or `page_url` to seed ideas.")
    language_id = str(args.get("language_id", "1000"))  # English default
    geo_target_ids = args.get("geo_target_ids") or []
    if isinstance(geo_target_ids, str):
        geo_target_ids = [g.strip() for g in geo_target_ids.split(",") if g.strip()]
    if not geo_target_ids:
        geo_target_ids = ["2840"]  # United States default

    payload: dict[str, Any] = {
        "language": f"languageConstants/{language_id}",
        "geoTargetConstants": [f"geoTargetConstants/{g}" for g in geo_target_ids],
        "includeAdultKeywords": False,
        "pageSize": _limit(args, 50, 1000),
    }
    if seeds and url_seed:
        payload["keywordAndUrlSeed"] = {"url": url_seed, "keywords": seeds}
    elif seeds:
        payload["keywordSeed"] = {"keywords": seeds}
    else:
        payload["urlSeed"] = {"url": url_seed}

    token = await get_valid_access_token(conn, db)
    url = _api_url(f"/customers/{cid}:generateKeywordIdeas")

    async def _do(lcid: str) -> dict:
        try:
            async with limit_for(url):
                res = await http_post(
                    url,
                    headers=_headers(token, login_customer_id=lcid),
                    json=payload,
                )
        except UpstreamUnavailable as e:
            raise GoogleAdsApiError(f"keyword ideas unavailable: {e}") from e
        if res.status_code >= 400:
            raise GoogleAdsApiError(f"generateKeywordIdeas {res.status_code}: {res.text[:600]}")
        return res.json() or {}

    async def _load() -> dict:
        data = await _with_mcc_fallback(conn, db, args, _do)
        out = []
        for r in data.get("results", []):
            m = r.get("keywordIdeaMetrics", {}) or {}
            out.append({
                "keyword": r.get("text", ""),
                "avg_monthly_searches": int(m.get("avgMonthlySearches", 0) or 0),
                "competition": m.get("competition", ""),
                "competition_index": int(m.get("competitionIndex", 0) or 0),
                "low_top_of_page_bid": _micros(m.get("lowTopOfPageBidMicros")),
                "high_top_of_page_bid": _micros(m.get("highTopOfPageBidMicros")),
            })
        return {"customer_id": cid, "count": len(out), "ideas": out}

    return await cached("google_ads", conn.id, "keyword_ideas", TTL_MEDIUM, _load, args=args)


async def apply_recommendation(conn: Connection, db, args: dict) -> dict:
    """POST /customers/{cid}/recommendations:apply with a list of resource_names.

    Google's ApplyRecommendation endpoint does NOT accept a `validateOnly`
    field. When validate_only=true we instead run a GAQL lookup to confirm each
    resource_name exists in the customer's current recommendations.
    """
    cid = _customer_id(args)
    rn = args.get("resource_name")
    rns = args.get("resource_names") or ([rn] if rn else [])
    if not rns:
        raise GoogleAdsApiError("Provide resource_name (single) or resource_names (list).")
    validate_only = bool(args.get("validate_only", False))

    if validate_only:
        in_list = ", ".join(f"'{r}'" for r in rns)
        check_q = (
            "SELECT recommendation.resource_name, recommendation.type "
            f"FROM recommendation WHERE recommendation.resource_name IN ({in_list})"
        )
        try:
            rows = await _execute_search(conn, db, args, check_q)
        except GoogleAdsApiError as e:
            return {
                "customer_id": cid,
                "validate_only": True,
                "applied": 0,
                "ok": False,
                "error": str(e)[:400],
                "note": "Dry-run lookup failed — the resource_name format may be wrong, or the customer doesn't have these recommendations.",
            }
        found = {
            (r.get("recommendation") or {}).get("resourceName"): (r.get("recommendation") or {}).get("type")
            for r in rows
        }
        missing = [r for r in rns if r not in found]
        return {
            "customer_id": cid,
            "validate_only": True,
            "applied": 0,
            "ok": not missing,
            "would_apply": [{"resource_name": r, "type": found[r]} for r in rns if r in found],
            "missing": missing,
            "note": "Google's ApplyRecommendation endpoint has no native validateOnly flag. We verified each resource exists via GAQL instead. Re-run with validate_only=false to actually apply.",
        }

    ops = [{"resourceName": r} for r in rns]
    body: dict[str, Any] = {"operations": ops}
    token = await get_valid_access_token(conn, db)
    url = _api_url(f"/customers/{cid}/recommendations:apply")

    async def _do(lcid: str) -> dict:
        try:
            async with limit_for(url):
                res = await http_post(
                    url,
                    headers=_headers(token, login_customer_id=lcid),
                    json=body,
                )
        except UpstreamUnavailable as e:
            raise GoogleAdsApiError(f"apply_recommendation unavailable: {e}") from e
        if res.status_code >= 400:
            raise GoogleAdsApiError(f"apply_recommendation {res.status_code}: {res.text[:600]}")
        return res.json() or {}

    result = await _with_mcc_fallback(conn, db, args, _do)
    await invalidate("google_ads", conn.id)
    return {
        "customer_id": cid,
        "validate_only": False,
        "applied": len(rns),
        "result": result,
    }


# ============================================================
# CONVERSIONS
# ============================================================

async def get_conversion_data(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args, 50, 500)
    query = (
        "SELECT segments.conversion_action, segments.conversion_action_name, "
        "segments.conversion_action_category, "
        "metrics.all_conversions, metrics.all_conversions_value, "
        "metrics.conversions, metrics.conversions_value "
        f"FROM customer WHERE segments.date {_date_clause(args)} LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            segs = r.get("segments", {}) or {}
            m = r.get("metrics", {}) or {}
            out.append({
                "conversion_action_resource": segs.get("conversionAction", ""),
                "name": segs.get("conversionActionName", ""),
                "category": segs.get("conversionActionCategory", ""),
                "conversions": float(m.get("conversions", 0) or 0),
                "conversion_value": float(m.get("conversionsValue", 0) or 0),
                "all_conversions": float(m.get("allConversions", 0) or 0),
                "all_conversion_value": float(m.get("allConversionsValue", 0) or 0),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_conversion_data", TTL_MEDIUM, _load, args=args)


# ============================================================
# LOOKUPS
# ============================================================

async def lookup_geo_target(conn: Connection, db, args: dict) -> dict:
    """Search geo_target_constant by name. customer_id is OPTIONAL."""
    cid = await _any_customer_id(conn, db, args)
    args = {**(args or {}), "customer_id": cid}
    name = str(args.get("name", "")).strip()
    if not name:
        raise GoogleAdsApiError("name is required (e.g. 'India', 'Mumbai').")
    limit = _limit(args, 20, 200)
    query = (
        "SELECT geo_target_constant.id, geo_target_constant.name, "
        "geo_target_constant.country_code, geo_target_constant.target_type, "
        "geo_target_constant.canonical_name, geo_target_constant.status "
        f"FROM geo_target_constant WHERE geo_target_constant.name LIKE '%{name}%' "
        f"LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            g = r.get("geoTargetConstant", {}) or {}
            out.append({
                "id": str(g.get("id", "")),
                "name": g.get("name", ""),
                "canonical_name": g.get("canonicalName", ""),
                "country_code": g.get("countryCode", ""),
                "target_type": g.get("targetType", ""),
                "status": g.get("status", ""),
            })
        return {"query": name, "count": len(out), "results": out}

    return await cached("google_ads", conn.id, "lookup_geo_target", TTL_MEDIUM, _load, args=args)


async def lookup_language(conn: Connection, db, args: dict) -> dict:
    """Account-agnostic; customer_id is optional and auto-picked if absent."""
    cid = await _any_customer_id(conn, db, args)
    args = {**(args or {}), "customer_id": cid}
    name = str(args.get("name", "")).strip()
    if not name:
        raise GoogleAdsApiError("name is required (e.g. 'English', 'Hindi').")
    limit = _limit(args, 20, 200)
    query = (
        "SELECT language_constant.id, language_constant.code, language_constant.name, "
        "language_constant.targetable "
        f"FROM language_constant WHERE language_constant.name LIKE '%{name}%' "
        f"LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            lang = r.get("languageConstant", {}) or {}
            out.append({
                "id": str(lang.get("id", "")),
                "code": lang.get("code", ""),
                "name": lang.get("name", ""),
                "targetable": bool(lang.get("targetable", False)),
            })
        return {"query": name, "count": len(out), "results": out}

    return await cached("google_ads", conn.id, "lookup_language", TTL_MEDIUM, _load, args=args)


# ============================================================
# RECOMMENDATIONS / COMPOSITE
# ============================================================

async def account_health_check(conn: Connection, db, args: dict) -> dict:
    """Composite signal: low-QS keywords, disapproved ads, near-limit budgets."""
    cid = _customer_id(args)

    low_qs_q = (
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.quality_info.quality_score, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name "
        "FROM keyword_view "
        "WHERE ad_group_criterion.status = 'ENABLED' "
        "LIMIT 1000"
    )
    disapproved_q = (
        "SELECT ad_group_ad.ad.id, ad_group_ad.policy_summary.approval_status, "
        "ad_group_ad.status, ad_group.id, ad_group.name, campaign.id, campaign.name "
        "FROM ad_group_ad "
        "WHERE ad_group_ad.policy_summary.approval_status = 'DISAPPROVED' "
        "LIMIT 50"
    )
    paused_camp_q = (
        "SELECT campaign.id, campaign.name FROM campaign "
        "WHERE campaign.status = 'PAUSED' LIMIT 50"
    )

    async def _load() -> dict:
        low_qs = await _execute_search(conn, db, args, low_qs_q)
        disapproved = await _execute_search(conn, db, args, disapproved_q)
        paused = await _execute_search(conn, db, args, paused_camp_q)

        low_qs_out = []
        for r in low_qs:
            qs = ((r.get("adGroupCriterion") or {}).get("qualityInfo") or {}).get("qualityScore")
            if qs is None or qs > 4:
                continue
            low_qs_out.append({
                "criterion_id": str((r.get("adGroupCriterion") or {}).get("criterionId", "")),
                "text": ((r.get("adGroupCriterion") or {}).get("keyword") or {}).get("text", ""),
                "quality_score": qs,
                "ad_group": (r.get("adGroup") or {}).get("name", ""),
                "campaign": (r.get("campaign") or {}).get("name", ""),
            })
        low_qs_out.sort(key=lambda x: x["quality_score"])
        low_qs_out = low_qs_out[:50]

        disapproved_out = [{
            "ad_id": str(((r.get("adGroupAd") or {}).get("ad") or {}).get("id", "")),
            "approval_status": ((r.get("adGroupAd") or {}).get("policySummary") or {}).get("approvalStatus", ""),
            "ad_group": (r.get("adGroup") or {}).get("name", ""),
            "campaign": (r.get("campaign") or {}).get("name", ""),
        } for r in disapproved]

        paused_out = [{
            "campaign_id": str((r.get("campaign") or {}).get("id", "")),
            "campaign_name": (r.get("campaign") or {}).get("name", ""),
        } for r in paused]

        return {
            "customer_id": cid,
            "summary": {
                "low_quality_keywords": len(low_qs_out),
                "disapproved_ads": len(disapproved_out),
                "paused_campaigns": len(paused_out),
            },
            "low_quality_keywords": low_qs_out,
            "disapproved_ads": disapproved_out,
            "paused_campaigns": paused_out,
        }

    return await cached("google_ads", conn.id, "account_health_check", TTL_MEDIUM, _load, args=args)


# ============================================================
# COMPETITIVE + DIAGNOSTICS
# ============================================================

async def get_auction_insights(conn: Connection, db, args: dict) -> dict:
    """Per-campaign impression-share metrics exposing competitor pressure."""
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT campaign.id, campaign.name, "
        "metrics.search_impression_share, "
        "metrics.search_rank_lost_impression_share, "
        "metrics.search_budget_lost_impression_share, "
        "metrics.search_top_impression_share, "
        "metrics.search_absolute_top_impression_share, "
        "metrics.cost_micros, metrics.impressions "
        f"FROM campaign WHERE segments.date {_date_clause(args)} "
        "AND metrics.impressions > 0 "
        f"ORDER BY metrics.search_rank_lost_impression_share DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            c = r.get("campaign", {}) or {}
            m = r.get("metrics", {}) or {}
            is_ = float(m.get("searchImpressionShare", 0) or 0) * 100
            rank_lost = float(m.get("searchRankLostImpressionShare", 0) or 0) * 100
            budget_lost = float(m.get("searchBudgetLostImpressionShare", 0) or 0) * 100
            pressure = "high" if rank_lost > 40 else "moderate" if rank_lost > 20 else "low"
            out.append({
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                "impression_share": round(is_, 2),
                "lost_to_rank_competitive_pressure": round(rank_lost, 2),
                "lost_to_budget": round(budget_lost, 2),
                "top_is": round(float(m.get("searchTopImpressionShare", 0) or 0) * 100, 2),
                "abs_top_is": round(float(m.get("searchAbsoluteTopImpressionShare", 0) or 0) * 100, 2),
                "impressions": int(m.get("impressions", 0) or 0),
                "cost": _micros(m.get("costMicros")),
                "competitor_pressure": pressure,
            })
        return {
            "customer_id": cid,
            "date_range": dr,
            "note": "Domain-level competitor overlap is restricted in Google Ads API v20. "
                    "Per-campaign rank-lost IS is the closest GAQL-accessible signal — "
                    "high values mean competitors are outbidding you. For domain names, "
                    "use the Auction Insights report in the Google Ads UI.",
            "count": len(out),
            "rows": out,
        }

    return await cached("google_ads", conn.id, "get_auction_insights", TTL_MEDIUM, _load, args=args)


async def get_impression_share(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    dr = _date_range(args)
    limit = _limit(args)
    query = (
        "SELECT campaign.id, campaign.name, "
        "metrics.search_impression_share, "
        "metrics.search_budget_lost_impression_share, "
        "metrics.search_rank_lost_impression_share, "
        "metrics.search_top_impression_share, "
        "metrics.search_absolute_top_impression_share, "
        "metrics.cost_micros "
        f"FROM campaign WHERE segments.date {_date_clause(args)} "
        "AND metrics.search_impression_share > 0 "
        f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            c = r.get("campaign", {}) or {}
            m = r.get("metrics", {}) or {}
            out.append({
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                "impression_share": round(float(m.get("searchImpressionShare", 0) or 0) * 100, 2),
                "lost_is_budget": round(float(m.get("searchBudgetLostImpressionShare", 0) or 0) * 100, 2),
                "lost_is_rank": round(float(m.get("searchRankLostImpressionShare", 0) or 0) * 100, 2),
                "top_is": round(float(m.get("searchTopImpressionShare", 0) or 0) * 100, 2),
                "abs_top_is": round(float(m.get("searchAbsoluteTopImpressionShare", 0) or 0) * 100, 2),
                "cost": _micros(m.get("costMicros")),
            })
        return {"customer_id": cid, "date_range": dr, "count": len(out), "rows": out}

    return await cached("google_ads", conn.id, "get_impression_share", TTL_MEDIUM, _load, args=args)


async def get_quality_score_breakdown(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args, 200, 1000)
    query = (
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, "
        "ad_group_criterion.quality_info.quality_score, "
        "ad_group_criterion.quality_info.creative_quality_score, "
        "ad_group_criterion.quality_info.post_click_quality_score, "
        "ad_group_criterion.quality_info.search_predicted_ctr, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name "
        "FROM keyword_view "
        "WHERE ad_group_criterion.status = 'ENABLED' "
        f"LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        buckets = {"poor": 0, "average": 0, "great": 0, "unscored": 0}
        for r in rows:
            agc = r.get("adGroupCriterion", {}) or {}
            kw = agc.get("keyword", {}) or {}
            qi = agc.get("qualityInfo", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            qs = qi.get("qualityScore")
            if qs is None:
                buckets["unscored"] += 1
            elif qs <= 4:
                buckets["poor"] += 1
            elif qs <= 7:
                buckets["average"] += 1
            else:
                buckets["great"] += 1
            out.append({
                "criterion_id": str(agc.get("criterionId", "")),
                "keyword": kw.get("text", ""),
                "match_type": kw.get("matchType", ""),
                "quality_score": qs,
                "expected_ctr": qi.get("searchPredictedCtr", ""),
                "ad_relevance": qi.get("creativeQualityScore", ""),
                "landing_page_experience": qi.get("postClickQualityScore", ""),
                "ad_group": ag.get("name", ""),
                "campaign": c.get("name", ""),
            })
        out.sort(key=lambda x: (x["quality_score"] if x["quality_score"] is not None else 99))
        return {
            "customer_id": cid,
            "summary": {
                "scored_keywords": len(out) - buckets["unscored"],
                "unscored_keywords": buckets["unscored"],
                "poor_qs_1_4": buckets["poor"],
                "average_qs_5_7": buckets["average"],
                "great_qs_8_10": buckets["great"],
            },
            "count": len(out),
            "keywords": out,
        }

    return await cached("google_ads", conn.id, "get_quality_score_breakdown", TTL_MEDIUM, _load, args=args)


async def get_asset_diagnostics(conn: Connection, db, args: dict) -> dict:
    cid = _customer_id(args)
    limit = _limit(args, 200, 1000)
    query = (
        "SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.ad_strength, "
        "ad_group_ad.policy_summary.approval_status, "
        "ad_group.id, ad_group.name, campaign.id, campaign.name "
        "FROM ad_group_ad "
        "WHERE ad_group_ad.status = 'ENABLED' "
        f"LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        strength_counts = {"POOR": 0, "AVERAGE": 0, "GOOD": 0, "EXCELLENT": 0, "UNKNOWN": 0, "NO_ADS": 0}
        for r in rows:
            aga = r.get("adGroupAd", {}) or {}
            ad = aga.get("ad", {}) or {}
            ag = r.get("adGroup", {}) or {}
            c = r.get("campaign", {}) or {}
            strength = aga.get("adStrength", "UNKNOWN")
            if strength not in strength_counts:
                strength_counts[strength] = 0
            strength_counts[strength] += 1
            out.append({
                "ad_id": str(ad.get("id", "")),
                "ad_type": ad.get("type", ""),
                "ad_strength": strength,
                "approval_status": (aga.get("policySummary", {}) or {}).get("approvalStatus", ""),
                "ad_group": ag.get("name", ""),
                "campaign": c.get("name", ""),
            })
        weak = [a for a in out if a["ad_strength"] in ("POOR", "AVERAGE")]
        return {
            "customer_id": cid,
            "summary": {
                "ads_examined": len(out),
                "strength_breakdown": strength_counts,
                "weak_ads_count": len(weak),
            },
            "weak_ads": weak[:50],
            "count": len(out),
        }

    return await cached("google_ads", conn.id, "get_asset_diagnostics", TTL_MEDIUM, _load, args=args)


async def get_change_history(conn: Connection, db, args: dict) -> dict:
    """Audit log: bid/budget/status edits over the last N days (max 29)."""
    cid = _customer_id(args)
    limit = _limit(args, 100, 1000)
    days = max(1, min(int(args.get("days", 14)), 29))
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    end_dt = _dt.now(_tz.utc)
    start_dt = end_dt - _td(days=days)
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    query = (
        "SELECT change_event.change_date_time, change_event.user_email, "
        "change_event.client_type, change_event.change_resource_type, "
        "change_event.resource_change_operation, change_event.changed_fields, "
        "change_event.campaign, change_event.ad_group "
        "FROM change_event "
        f"WHERE change_event.change_date_time >= '{start_str}' "
        f"AND change_event.change_date_time <= '{end_str}' "
        f"ORDER BY change_event.change_date_time DESC LIMIT {limit}"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            e = r.get("changeEvent", {}) or {}
            out.append({
                "when": e.get("changeDateTime", ""),
                "user": e.get("userEmail", ""),
                "client_type": e.get("clientType", ""),
                "resource_type": e.get("changeResourceType", ""),
                "operation": e.get("resourceChangeOperation", ""),
                "changed_fields": e.get("changedFields", ""),
                "campaign": e.get("campaign", ""),
                "ad_group": e.get("adGroup", ""),
            })
        return {"customer_id": cid, "window_days": days, "count": len(out), "events": out}

    return await cached("google_ads", conn.id, "get_change_history", TTL_MEDIUM, _load, args=args)


async def get_budget_pacing(conn: Connection, db, args: dict) -> dict:
    """Compare month-to-date spend against daily budget and project month-end."""
    cid = _customer_id(args)
    from datetime import datetime as _dt
    today = _dt.utcnow()
    days_in_month = 30  # rough — good enough for projection
    days_elapsed = today.day

    query = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign_budget.amount_micros, "
        "metrics.cost_micros "
        "FROM campaign WHERE segments.date DURING THIS_MONTH "
        "AND campaign.status IN ('ENABLED', 'PAUSED') "
        "ORDER BY metrics.cost_micros DESC LIMIT 200"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        out = []
        for r in rows:
            c = r.get("campaign", {}) or {}
            b = r.get("campaignBudget", {}) or {}
            m = r.get("metrics", {}) or {}
            daily = _micros(b.get("amountMicros"))
            mtd = _micros(m.get("costMicros"))
            expected_so_far = round(daily * days_elapsed, 2)
            projected_eom = round(mtd / days_elapsed * days_in_month, 2) if days_elapsed else 0.0
            target_eom = round(daily * days_in_month, 2)
            variance = round(projected_eom - target_eom, 2)
            status = "on_pace"
            if target_eom and variance > target_eom * 0.1:
                status = "overpacing"
            elif target_eom and variance < -target_eom * 0.1:
                status = "underpacing"
            out.append({
                "campaign_id": str(c.get("id", "")),
                "campaign_name": c.get("name", ""),
                "status": c.get("status", ""),
                "daily_budget": daily,
                "mtd_spend": mtd,
                "expected_mtd_at_daily": expected_so_far,
                "target_eom_spend": target_eom,
                "projected_eom_spend": projected_eom,
                "variance": variance,
                "pace": status,
            })
        out = [r for r in out if r["daily_budget"] > 0 or r["mtd_spend"] > 0]
        return {
            "customer_id": cid,
            "as_of": today.strftime("%Y-%m-%d"),
            "days_elapsed": days_elapsed,
            "count": len(out),
            "campaigns": out,
        }

    return await cached("google_ads", conn.id, "get_budget_pacing", TTL_MEDIUM, _load, args=args)


async def get_recommendation_impact(conn: Connection, db, args: dict) -> dict:
    """Group available recommendations by type so the user can prioritize."""
    cid = _customer_id(args)
    query = (
        "SELECT recommendation.resource_name, recommendation.type "
        "FROM recommendation LIMIT 500"
    )

    async def _load() -> dict:
        rows = await _execute_search(conn, db, args, query)
        by_type: dict[str, list[str]] = {}
        for r in rows:
            rec = r.get("recommendation", {}) or {}
            t = rec.get("type", "UNKNOWN")
            by_type.setdefault(t, []).append(rec.get("resourceName", ""))
        breakdown = [
            {"type": k, "count": len(v), "resource_names": v[:10]}
            for k, v in sorted(by_type.items(), key=lambda x: -len(x[1]))
        ]
        return {
            "customer_id": cid,
            "total_recommendations": sum(len(v) for v in by_type.values()),
            "type_breakdown": breakdown,
        }

    return await cached("google_ads", conn.id, "get_recommendation_impact", TTL_MEDIUM, _load, args=args)


# ============================================================
# DISPATCH MAP
# ============================================================

# Performance/reporting tools that should short-circuit to a friendly empty
# result when called on an MCC, instead of leaking Google's raw 400.
_MCC_BLOCKED = {
    "get_campaign_performance",
    "get_ad_group_performance",
    "get_keyword_performance",
    "get_search_terms",
    "get_geo_performance",
    "get_device_performance",
    "get_audience_performance",
    "get_demographic_performance",
    "get_network_performance",
    "get_click_type_performance",
    "get_ad_position_performance",
    "get_placement_performance",
    "get_topic_performance",
    "get_geo_presence_performance",
    "get_call_details",
    "get_conversion_data",
    "negative_keyword_suggestions",
    "search_query_analysis",
    "get_asset_performance",
    "get_pmax_asset_group_performance",
    "get_landing_page_performance",
    "get_product_performance",
    "get_hourly_performance",
    "get_day_of_week_performance",
    "get_conversion_lag",
    "get_auction_insights",
    "get_impression_share",
    "get_budget_pacing",
}


def _block_mcc(fn):
    """Wrap a perf/reporting handler so it returns a clean empty payload with a
    friendly note when the target customer_id is a manager (MCC)."""
    async def wrapper(conn: Connection, db, args: dict) -> dict:
        cid = _customer_id(args)
        if await _is_manager_account(conn, db, cid):
            payload: dict[str, Any] = {
                "customer_id": cid,
                "count": 0,
                "rows": [],
                "note": MCC_NOTE,
            }
            dr = (args or {}).get("date_range")
            if dr:
                payload["date_range"] = str(dr).upper()
            return payload
        return await fn(conn, db, args)
    return wrapper


_RAW_HANDLERS: dict[str, Any] = {
    # ACCOUNTS
    "list_accounts": list_accounts,
    # LIST
    "list_campaigns": list_campaigns,
    "list_ad_groups": list_ad_groups,
    "list_ads": list_ads,
    "list_keywords": list_keywords,
    "list_negative_keywords": list_negative_keywords,
    "list_assets": list_assets,
    "list_conversion_actions": list_conversion_actions,
    "list_recommendations": list_recommendations,
    # PERFORMANCE
    "get_campaign_performance": get_campaign_performance,
    "get_ad_group_performance": get_ad_group_performance,
    "get_keyword_performance": get_keyword_performance,
    "get_search_terms": get_search_terms,
    "get_geo_performance": get_geo_performance,
    "get_device_performance": get_device_performance,
    "get_audience_performance": get_audience_performance,
    "get_demographic_performance": get_demographic_performance,
    "get_network_performance": get_network_performance,
    "get_click_type_performance": get_click_type_performance,
    "get_ad_position_performance": get_ad_position_performance,
    "get_placement_performance": get_placement_performance,
    "get_topic_performance": get_topic_performance,
    "get_geo_presence_performance": get_geo_presence_performance,
    "get_call_details": get_call_details,
    # DEEP PERFORMANCE
    "search_query_analysis": search_query_analysis,
    "get_asset_performance": get_asset_performance,
    "get_pmax_asset_group_performance": get_pmax_asset_group_performance,
    "get_landing_page_performance": get_landing_page_performance,
    "get_product_performance": get_product_performance,
    "get_hourly_performance": get_hourly_performance,
    "get_day_of_week_performance": get_day_of_week_performance,
    "get_conversion_lag": get_conversion_lag,
    # OPTIMIZATION
    "negative_keyword_suggestions": negative_keyword_suggestions,
    "keyword_ideas": keyword_ideas,
    "pause_resume_keyword": pause_resume_keyword,
    "pause_resume_ad_group": pause_resume_ad_group,
    "pause_resume_campaign": pause_resume_campaign,
    "update_budget": update_budget,
    "update_bid": update_bid,
    # RECOMMENDATIONS
    "apply_recommendation": apply_recommendation,
    "account_health_check": account_health_check,
    # CONVERSIONS
    "get_conversion_data": get_conversion_data,
    # LOOKUPS
    "lookup_geo_target": lookup_geo_target,
    "lookup_language": lookup_language,
    # COMPETITIVE + DIAGNOSTICS
    "get_auction_insights": get_auction_insights,
    "get_impression_share": get_impression_share,
    "get_quality_score_breakdown": get_quality_score_breakdown,
    "get_asset_diagnostics": get_asset_diagnostics,
    "get_change_history": get_change_history,
    "get_budget_pacing": get_budget_pacing,
    "get_recommendation_impact": get_recommendation_impact,
}

TOOL_HANDLERS: dict[str, Any] = {
    name: (_block_mcc(fn) if name in _MCC_BLOCKED else fn)
    for name, fn in _RAW_HANDLERS.items()
}

# ============================================================
# CATALOG -- ported verbatim from falcon tools.py. Each entry is
# {"description", "input" (JSON Schema), "write"?}, which is exactly the
# shape registry.Connector expects, so nothing needed reshaping.
# ============================================================

_CID = {
    "type": "string",
    "description": "10-digit Google Ads customer ID (no dashes). Get one from list_accounts.",
}
_LOGIN = {
    "type": "string",
    "description": "Manager (MCC) ID to use as login-customer-id. Required when accessing a customer through an MCC; omit for direct-access accounts.",
}
_DATE = {
    "type": "string",
    "description": "Google Ads predefined date range.",
    "enum": [
        "TODAY", "YESTERDAY", "LAST_7_DAYS", "LAST_14_DAYS",
        "LAST_30_DAYS", "LAST_90_DAYS",
        "THIS_MONTH", "LAST_MONTH",
        "THIS_WEEK_SUN_TODAY", "LAST_WEEK_SUN_SAT", "ALL_TIME",
    ],
    "default": "LAST_30_DAYS",
}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}
_SD = {"type": "string", "description": "Optional explicit start date YYYY-MM-DD. With end_date, overrides date_range for any custom window (no 12-month limit). For full history you can also use date_range=ALL_TIME."}
_ED = {"type": "string", "description": "Optional explicit end date YYYY-MM-DD. Use together with start_date."}
_STATUS = {"type": "string", "enum": ["ENABLED", "PAUSED", "REMOVED"]}
_SEGMENT = {"type": "string", "enum": ["DATE", "DEVICE", "WEEK", "MONTH"], "description": "Optional segment for time/device split."}
_VALIDATE = {
    "type": "boolean",
    "default": False,
    "description": "If true, Google validates the operation but does NOT apply it. Use this for safe dry-runs against live accounts.",
}


def _schema(props: dict, required: list[str] | None = None, *, extra: bool = False) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": extra,
    }


# name -> catalog entry. `input` is the JSON Schema; `write` flags mutating tools.
CATALOG: dict[str, dict] = {
    # ============================================================
    # ACCOUNTS & STRUCTURE
    # ============================================================
    "list_accounts": {
        "description": (
            "Every Google Ads account the user can access — manager (MCC) accounts AND all "
            "their child/client accounts at every level. Returns `accounts` (nested tree), "
            "`all_accounts` (flat list of every account, each with the `login_customer_id` MCC "
            "to query it through), a `summary` (counts by status), and `errors`. To query a child "
            "account in another tool, pass its `id` as customer_id and its `login_customer_id`. "
            "Set `refresh: true` if accounts seem missing (busts the 6h cache)."
        ),
        "input": _schema({
            "refresh": {"type": "boolean", "default": False,
                        "description": "Re-fetch the account hierarchy, bypassing the cache."},
        }),
    },
    "list_campaigns": {
        "description": "Campaigns under a customer with type, status, budget, and bidding strategy.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN, "status": _STATUS, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "list_ad_groups": {
        "description": "Ad groups under selected campaigns with default CPC and status.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "campaign_id": {"type": "string", "description": "Filter to one campaign."},
             "status": _STATUS, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "list_ads": {
        "description": "Ads with creative previews, status, and approval state.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "ad_group_id": {"type": "string"}, "status": _STATUS, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "list_keywords": {
        "description": "Active keywords with match type, status, max CPC, and quality score.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "ad_group_id": {"type": "string"}, "status": _STATUS, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "list_negative_keywords": {
        "description": "Campaign and ad-group level negative keyword lists.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "list_assets": {
        "description": "Sitelinks, callouts, structured snippets, and other ad assets.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "asset_type": {"type": "string", "description": "E.g. SITELINK, CALLOUT, IMAGE, TEXT, STRUCTURED_SNIPPET. Omit for all."},
             "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "list_conversion_actions": {
        "description": "All conversion actions configured on the account.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "list_recommendations": {
        "description": "Google's own optimization recommendations for the account.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "type": {"type": "string", "description": "Recommendation type (e.g. KEYWORD, CAMPAIGN_BUDGET)."},
             "limit": _LIMIT},
            ["customer_id"],
        ),
    },

    # ============================================================
    # PERFORMANCE
    # ============================================================
    "get_campaign_performance": {
        "description": "Cost, clicks, impressions, conversions per campaign over a date range.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "segment": _SEGMENT, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_ad_group_performance": {
        "description": "Ad-group level metrics with date / device segments.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "segment": _SEGMENT, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_keyword_performance": {
        "description": "Per-keyword performance with CPC, CTR, quality score, and conversions.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_search_terms": {
        "description": "Search-term queries that triggered ads, with match type and impressions.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_geo_performance": {
        "description": "Performance broken down by location (country, region, city).",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_device_performance": {
        "description": "Desktop, mobile, and tablet performance split.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED},
            ["customer_id"],
        ),
    },
    "get_audience_performance": {
        "description": "Performance by audience segment (in-market, affinity, custom).",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_demographic_performance": {
        "description": "Performance by demographic: age, gender, parental status, or household income (set breakdown=).",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "breakdown": {"type": "string",
                           "enum": ["age_range", "gender", "parental_status", "income_range"],
                           "default": "age_range",
                           "description": "Which demographic dimension to break down by."},
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_network_performance": {
        "description": "Split by ad network: Search, Search Partners, Display/Content, YouTube.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED},
            ["customer_id"],
        ),
    },
    "get_click_type_performance": {
        "description": "Split by click type: headline, sitelink, call, and other click interactions.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED},
            ["customer_id"],
        ),
    },
    "get_ad_position_performance": {
        "description": "Top vs absolute-top impression rate per campaign (modern replacement for average position).",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_placement_performance": {
        "description": "Display/Video placements where ads ran: website domains, YouTube channels, apps.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_topic_performance": {
        "description": "Display Network topic / content-targeting performance.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_geo_presence_performance": {
        "description": "Physical location (presence) vs location of interest split — beyond what get_geo_performance shows.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_call_details": {
        "description": "Call-tracking detail records: duration, status, type, caller area/country code per call.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED,
             "limit": {"type": "integer", "default": 100, "maximum": 1000}},
            ["customer_id"],
        ),
    },

    # ============================================================
    # DEEP PERFORMANCE
    # ============================================================
    "search_query_analysis": {
        "description": "Search-term insights split into wasteful (high cost, low conv) and winning (high ROAS) buckets.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED,
             "min_cost": {"type": "number", "default": 0.5, "description": "Minimum spend for a term to count as 'wasteful' when conversions=0."},
             "limit": {"type": "integer", "default": 200, "maximum": 2000, "description": "How many search terms to pull and analyze."}},
            ["customer_id"],
        ),
    },
    "get_asset_performance": {
        "description": "Per-asset performance: headlines, descriptions, images, videos with Google's BEST/GOOD/LOW labels.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED,
             "field_type": {"type": "string", "description": "Filter to one field type: HEADLINE_1, DESCRIPTION_1, MARKETING_IMAGE, YOUTUBE_VIDEO, etc."},
             "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_pmax_asset_group_performance": {
        "description": "Performance Max asset group metrics (impressions, clicks, cost, conversions, ROAS).",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_landing_page_performance": {
        "description": "Landing page URLs ranked by spend, with click-to-conversion rate.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_product_performance": {
        "description": "Shopping / Performance Max product-level metrics (item id, title, brand).",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED,
             "limit": {"type": "integer", "default": 100, "maximum": 1000}},
            ["customer_id"],
        ),
    },
    "get_hourly_performance": {
        "description": "Performance segmented by hour of day to find best converting hours.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED},
            ["customer_id"],
        ),
    },
    "get_day_of_week_performance": {
        "description": "Performance segmented by day of week.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED},
            ["customer_id"],
        ),
    },
    "get_conversion_lag": {
        "description": "Conversion volume bucketed by days-from-click-to-conversion.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED},
            ["customer_id"],
        ),
    },

    # ============================================================
    # OPTIMIZATION
    # ============================================================
    "negative_keyword_suggestions": {
        "description": "Identify wasteful search terms and propose negatives with confidence.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED,
             "min_cost": {"type": "number", "default": 1.0, "description": "Minimum spend (account currency) for a term to be considered wasteful."},
             "max_conversions": {"type": "number", "default": 0.0, "description": "Max conversions a term can have to still be considered wasteful."},
             "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "keyword_ideas": {
        "description": "Keyword Planner ideas with search volume, competition, CPC estimates.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "keywords": {"type": "array", "items": {"type": "string"}, "description": "Seed keywords (REQUIRED unless page_url is given). Comma-separated string also accepted."},
             "page_url": {"type": "string", "description": "Seed page URL (REQUIRED unless keywords are given). Can be combined with keywords for url+keyword seed."},
             "language_id": {"type": "string", "default": "1000", "description": "Language constant ID (1000 = English). Use lookup_language to find others."},
             "geo_target_ids": {"type": "array", "items": {"type": "string"}, "description": "Geo target constant IDs (defaults to 2840 = US). Use lookup_geo_target to find others."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50}},
            ["customer_id"],
        ),
    },

    # ---------- WRITE (disabled by default) ----------
    "pause_resume_keyword": {
        "write": True,
        "description": "Pause or enable a keyword by resource name. Disabled by default — user must opt in.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "resource_name": {"type": "string", "description": "Full resource name in the form customers/<cid>/adGroupCriteria/<ag_id>~<criterion_id>. Get one from list_keywords (concat ad_group_id + '~' + criterion_id) or from get_quality_score_breakdown."},
             "status": {"type": "string", "enum": ["ENABLED", "PAUSED", "pause", "resume"], "description": "Either an explicit ENABLED/PAUSED, or the verbs pause/resume."},
             "validate_only": _VALIDATE},
            ["customer_id", "resource_name", "status"],
        ),
    },
    "pause_resume_ad_group": {
        "write": True,
        "description": "Pause or enable an ad group. Disabled by default — user must opt in.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "resource_name": {"type": "string", "description": "Full resource name customers/<cid>/adGroups/<ad_group_id>. Get one from list_ad_groups."},
             "status": {"type": "string", "enum": ["ENABLED", "PAUSED", "pause", "resume"]},
             "validate_only": _VALIDATE},
            ["customer_id", "resource_name", "status"],
        ),
    },
    "pause_resume_campaign": {
        "write": True,
        "description": "Pause or enable a campaign. Disabled by default — user must opt in.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "resource_name": {"type": "string", "description": "Full resource name customers/<cid>/campaigns/<campaign_id>. Get one from list_campaigns."},
             "status": {"type": "string", "enum": ["ENABLED", "PAUSED", "pause", "resume"]},
             "validate_only": _VALIDATE},
            ["customer_id", "resource_name", "status"],
        ),
    },
    "update_budget": {
        "write": True,
        "description": "Change the daily budget on a campaign. Disabled by default — user must opt in.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "resource_name": {"type": "string", "description": "Full resource name customers/<cid>/campaignBudgets/<budget_id> — this is a campaign_budget resource, NOT a campaign. To find one: GAQL 'SELECT campaign_budget.resource_name, campaign.id FROM campaign WHERE campaign.id = <id>'."},
             "daily_amount": {"type": "number", "description": "New daily budget in the account currency (e.g. 50.0 = ₹50 / $50)."},
             "validate_only": _VALIDATE},
            ["customer_id", "resource_name", "daily_amount"],
        ),
    },
    "update_bid": {
        "write": True,
        "description": "Modify a keyword's or ad group's max CPC bid. Disabled by default — user must opt in.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "resource_name": {"type": "string", "description": "Either customers/<cid>/adGroupCriteria/<ag_id>~<criterion_id> (keyword-level bid) or customers/<cid>/adGroups/<id> (default ad-group bid)."},
             "cpc_bid": {"type": "number", "description": "Max CPC bid in account currency (e.g. 1.25 = ₹1.25 / $1.25)."},
             "validate_only": _VALIDATE},
            ["customer_id", "resource_name", "cpc_bid"],
        ),
    },

    # ============================================================
    # RECOMMENDATIONS & INSIGHTS
    # ============================================================
    "apply_recommendation": {
        "write": True,
        "description": "Apply a recommendation by ID (e.g., add suggested keyword). Disabled by default — user must opt in.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "resource_name": {"type": "string", "description": "Single recommendation resource name from list_recommendations (or use resource_names for a batch)."},
             "resource_names": {"type": "array", "items": {"type": "string"}, "description": "List of recommendation resource names to apply in one call."},
             "validate_only": _VALIDATE},
            ["customer_id"],
        ),
    },
    "account_health_check": {
        "description": "Surface low-quality keywords, disapproved ads, and budget issues.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN},
            ["customer_id"],
        ),
    },
    "get_auction_insights": {
        "description": "Competitor domain overlap and impression-share comparison from auction_insight_domain_view.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_impression_share": {
        "description": "Search impression share + IS lost to budget vs rank, per campaign.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },
    "get_quality_score_breakdown": {
        "description": "Per-keyword Quality Score with expected CTR, ad relevance, and landing-page experience components.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "limit": {"type": "integer", "default": 200, "maximum": 1000}},
            ["customer_id"],
        ),
    },
    "get_asset_diagnostics": {
        "description": "Ad strength + missing-asset diagnostics across enabled ads.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "limit": {"type": "integer", "default": 200, "maximum": 1000}},
            ["customer_id"],
        ),
    },
    "get_change_history": {
        "description": "Who changed what and when (bids, budgets, status). Last 29 days max.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "days": {"type": "integer", "default": 14, "minimum": 1, "maximum": 29, "description": "Days back to query (max 29 — Google's hard limit)."},
             "limit": {"type": "integer", "default": 100, "maximum": 1000}},
            ["customer_id"],
        ),
    },
    "get_budget_pacing": {
        "description": "Month-to-date spend vs daily budget, with projected month-end overspend/underspend per campaign.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN},
            ["customer_id"],
        ),
    },
    "get_recommendation_impact": {
        "description": "Recommendation counts grouped by type, so you can see which categories to focus on.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN},
            ["customer_id"],
        ),
    },

    # ============================================================
    # CONVERSIONS
    # ============================================================
    "get_conversion_data": {
        "description": "Per-action conversion volume and value over a date range.",
        "input": _schema(
            {"customer_id": _CID, "login_customer_id": _LOGIN,
             "date_range": _DATE, "start_date": _SD, "end_date": _ED, "limit": _LIMIT},
            ["customer_id"],
        ),
    },

    # ============================================================
    # LOOKUPS
    # ============================================================
    "lookup_geo_target": {
        "description": "Find geo target IDs by location name (city, region, country).",
        "input": _schema(
            {"customer_id": {**_CID, "description": "OPTIONAL — geo lookups are account-agnostic. If omitted, any accessible customer is used."},
             "login_customer_id": _LOGIN,
             "name": {"type": "string", "description": "Substring of the location name, e.g. 'India', 'Mumbai'."},
             "limit": {"type": "integer", "default": 20, "maximum": 200}},
            ["name"],
        ),
    },
    "lookup_language": {
        "description": "Find language constant IDs by language name.",
        "input": _schema(
            {"customer_id": {**_CID, "description": "OPTIONAL — language lookups are account-agnostic. If omitted, any accessible customer is used."},
             "login_customer_id": _LOGIN,
             "name": {"type": "string", "description": "Substring of the language name, e.g. 'English', 'Hindi'."},
             "limit": {"type": "integer", "default": 20, "maximum": 200}},
            ["name"],
        ),
    },
}


registry.register(
    Connector(
        slug='google_ads',
        label='Google Ads',
        auth='google_oauth',
        scopes=['https://www.googleapis.com/auth/adwords'],
        description=(
            'Reads and manages Google Ads accounts through the Ads API - the full account '
            'hierarchy, campaigns, ad groups, ads, keywords and assets, performance across '
            'every segment Google reports on (device, geo, network, audience, demographic, '
            'hour, day, placement, product, landing page), search terms, auction insights, '
            'quality score, change history, budget pacing and recommendations. Six write '
            'tools can pause or resume entities and adjust bids and budgets; they are '
            'switched off until you turn them on.'
        ),
        category='Advertising',
        catalog=CATALOG,
        handlers=TOOL_HANDLERS,
    )
)
