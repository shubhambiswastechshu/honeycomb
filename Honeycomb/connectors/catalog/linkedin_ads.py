"""LinkedIn Ads connector — LinkedIn Marketing (Versioned REST) API.

Auth: linkedin_oauth. "Continue with LinkedIn" stores the member's access
token (60 days), its expiry and, where LinkedIn issues one, a refresh token
(a year). The access token is used until it is nearly spent and then renewed
with the refresh token, and the renewed one is saved back to the connection,
so a renewal happens once per token lifetime rather than once per call.
Connections made earlier by pasting an access token keep working until that
token expires.

Endpoints used (base https://api.linkedin.com/rest):
  - me                   GET /v2/userinfo  | GET /v2/me   (legacy, no version header)
  - list_ad_accounts     GET /adAccounts?q=search
  - get_ad_account       GET /adAccounts/{id}
  - list_campaign_groups GET /adAccounts/{id}/adCampaignGroups?q=search
  - list_campaigns       GET /adAccounts/{id}/adCampaigns?q=search
  - list_creatives       GET /adAccounts/{id}/creatives?q=criteria
  - analytics            GET /adAnalytics?q=analytics&pivot=...

All requests send the versioned + Rest.li-2.0.0 headers LinkedIn requires.
Monetary metrics (costInLocalCurrency) come back as decimal strings already in
account currency, so they are surfaced as-is (no micros conversion needed).
"""
import asyncio
import datetime
import time
from collections import Counter
from urllib.parse import quote

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post
from connections import linkedin
from connections.models import Connection

BASE = "https://api.linkedin.com/rest"
#: LinkedIn retires each monthly version about a year after release, and a
#: retired one answers 426 "Requested version ... is not active". Bump it
#: before then; https://learn.microsoft.com/linkedin/marketing/versioning lists
#: the live ones.
LINKEDIN_VERSION = "202609"

#: Renew this long before LinkedIn's expiry rather than race it.
RENEW_BEFORE_SECONDS = 24 * 3600

STATUS_LABELS = {
    "ACTIVE": "ACTIVE",
    "CANCELED": "CANCELED",
    "DRAFT": "DRAFT",
    "PENDING_DELETION": "PENDING_DELETION",
    "REMOVED": "REMOVED",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
#: One renewal at a time per connection: an analytics call fans out, and
#: every branch finding the same spent token must not each spend the refresh.
_RENEW_LOCKS: dict[int, asyncio.Lock] = {}

RECONNECT = "Reconnect it with Continue with LinkedIn in the Honeycomb dashboard."


async def _access_token(conn: Connection) -> str:
    creds = conn.creds() or {}
    token = creds.get("access_token")
    if not token:
        raise ConnectorError("This LinkedIn connection has no access token. " + RECONNECT)
    expires_at = _as_float(creds.get("expires_at"))
    # No expiry recorded: a token pasted before sign-in existed. LinkedIn is
    # the only one who knows whether it still works, so ask it.
    if not expires_at or expires_at - time.time() > RENEW_BEFORE_SECONDS:
        return token

    lock = _RENEW_LOCKS.setdefault(conn.id, asyncio.Lock())
    async with lock:
        # Another call may have renewed it while this one waited.
        creds = conn.creds() or {}
        if _as_float(creds.get("expires_at")) - time.time() > RENEW_BEFORE_SECONDS:
            return creds["access_token"]
        refresh = creds.get("refresh_token")
        if not refresh:
            if expires_at > time.time():
                return token  # still valid for a few hours; nothing to renew with
            raise ConnectorError("This LinkedIn connection has expired. " + RECONNECT)
        renewed = await _renew(refresh)
        creds = linkedin.creds_from_token(renewed, previous=creds)
        conn.set_creds(creds)
        await _save_creds(conn)
        return creds["access_token"]


async def _renew(refresh_token: str) -> dict:
    client_id = getattr(settings, "LINKEDIN_CLIENT_ID", "")
    client_secret = getattr(settings, "LINKEDIN_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise ConnectorError("LinkedIn sign-in is not configured on this server, so the token cannot be renewed.")
    token_uri = getattr(settings, "LINKEDIN_OAUTH_TOKEN_URI", "https://www.linkedin.com/oauth/v2/accessToken")
    try:
        async with limit_for(token_uri):
            res = await http_post(
                token_uri,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                retries=0,
            )
    except UpstreamUnavailable as e:
        raise ConnectorError(f"Could not reach LinkedIn to renew the token: {e}")
    if res.status_code != 200:
        # The body is not echoed: it can quote the request, which carried the
        # client secret. A refused refresh means revoked or a year old.
        raise ConnectorError(
            f"LinkedIn refused to renew this connection ({res.status_code}). " + RECONNECT
        )
    try:
        body = res.json()
    except ValueError:
        body = {}
    if not body.get("access_token"):
        raise ConnectorError("LinkedIn renewed the connection without a token. " + RECONNECT)
    return body


@sync_to_async
def _save_creds(conn: Connection) -> None:
    # Only the credential column: the dashboard may have renamed the connection
    # or switched a tool off since this request loaded it.
    Connection.objects.filter(pk=conn.pk).update(creds_enc=conn.creds_enc, updated_at=timezone.now())


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _headers(conn: Connection) -> dict:
    return {
        "Authorization": f"Bearer {await _access_token(conn)}",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }


async def _get(conn: Connection, path: str, params: dict | None = None):
    url = f"{BASE}{path}"
    try:
        async with limit_for(url):
            res = await http_get(url, headers=await _headers(conn), params=params or {})
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code >= 400:
        # Surface LinkedIn's structured error JSON (message/serviceErrorCode) verbatim.
        try:
            err = res.json() or {}
            msg = err.get("message") or err.get("error_description") or res.text[:300]
        except ValueError:
            msg = res.text[:300]
        raise ConnectorError(f"LinkedIn API error {res.status_code}: {msg}")
    try:
        return res.json()
    except ValueError:
        raise ConnectorError(f"LinkedIn API returned non-JSON {res.status_code}: {res.text[:300]}")


def _account_id(urn_or_id) -> str:
    """Strip a `urn:li:sponsoredAccount:123` URN down to the bare numeric id."""
    s = str(urn_or_id or "").strip()
    return s.rsplit(":", 1)[-1] if ":" in s else s


def _require_account(args: dict) -> str:
    aid = _account_id((args or {}).get("account_id") or (args or {}).get("account"))
    if not aid:
        raise ConnectorError("account_id is required (from list_ad_accounts, e.g. 500839718).")
    return aid


def _parse_ymd(value):
    """Parse a 'YYYY-MM-DD' string into a date, or None if absent."""
    if not value:
        return None
    try:
        return datetime.datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ConnectorError(f"start_date/end_date must be YYYY-MM-DD (got '{value}').")


def _date_range_param(days, start_date=None, end_date=None) -> str:
    end = _parse_ymd(end_date) or datetime.date.today()
    start = _parse_ymd(start_date)
    if start is None:
        start = end - datetime.timedelta(days=max(1, int(days or 30)))
    return (
        f"(start:(year:{start.year},month:{start.month},day:{start.day}),"
        f"end:(year:{end.year},month:{end.month},day:{end.day}))"
    )


async def _elements(conn: Connection, path: str, q: str, limit, extra: dict | None = None) -> list:
    p = {"q": q, "pageSize": max(1, min(int(limit or 100), 1000))}
    if extra:
        p.update(extra)
    data = await _get(conn, path, p)
    return data.get("elements", []) or []


def _amount(v):
    return (v or {}).get("amount") if isinstance(v, dict) else v


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
async def _load_ad_accounts(conn: Connection) -> list[dict]:
    data = await _get(conn, "/adAccounts", {"q": "search", "pageSize": 100})
    out = []
    for a in data.get("elements", []) or []:
        aid = a.get("id")
        out.append({
            "id": f"urn:li:sponsoredAccount:{aid}" if aid else None,
            "account_id": aid,
            "name": a.get("name"),
            "status": STATUS_LABELS.get(a.get("status"), a.get("status") or ""),
            "type": a.get("type"),
            "currency": a.get("currency"),
            "reference": a.get("reference"),
            "test": a.get("test"),
        })
    return out


async def me(conn: Connection, db, args: dict) -> dict:
    """Authenticated member profile. Tries OpenID userinfo, then /v2/me.
    These are legacy/v2 endpoints, so no LinkedIn-Version header."""
    async def _loader():
        legacy = {"Authorization": f"Bearer {await _access_token(conn)}"}
        last_status = None
        for url in (
            "https://api.linkedin.com/v2/userinfo",
            "https://api.linkedin.com/v2/me",
        ):
            try:
                async with limit_for(url):
                    res = await http_get(url, headers=legacy)
            except UpstreamUnavailable as e:
                raise ConnectorError(f"LinkedIn /me unavailable: {e}")
            last_status = res.status_code
            if res.status_code < 400:
                try:
                    return res.json()
                except ValueError:
                    continue
        raise ConnectorError(
            "Could not fetch profile — the token's scopes may not include profile "
            f"access (r_basicprofile / openid). Last status {last_status}. Ad tools still work."
        )

    return await cached("linkedin_ads", conn.id, "me", TTL_LONG, _loader, args={})


async def list_ad_accounts(conn: Connection, db, args: dict) -> dict:
    async def _loader():
        return {"ad_accounts": await _load_ad_accounts(conn)}

    return await cached(
        "linkedin_ads", conn.id, "list_ad_accounts", TTL_LONG, _loader, args={}
    )


async def list_all_ad_accounts(conn: Connection, db, args: dict) -> dict:
    async def _loader():
        return {"ad_accounts": await _load_ad_accounts(conn)}

    return await cached(
        "linkedin_ads", conn.id, "list_all_ad_accounts", TTL_LONG, _loader, args={}
    )


async def get_ad_account(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)

    async def _loader():
        a = await _get(conn, f"/adAccounts/{aid}")
        return {
            "id": f"urn:li:sponsoredAccount:{aid}",
            "account_id": a.get("id") or aid,
            "name": a.get("name"),
            "status": a.get("status"),
            "type": a.get("type"),
            "currency": a.get("currency"),
            "reference": a.get("reference"),
            "test": a.get("test"),
        }

    return await cached(
        "linkedin_ads", conn.id, "get_ad_account", TTL_LONG, _loader, args={"account_id": aid}
    )


async def account_inventory(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)

    async def _loader():
        camps = (await _load_campaigns(conn, aid, 1000))["campaigns"]
        creatives = (await _load_creatives(conn, aid, 1000))["creatives"]
        active = [c for c in camps if c.get("status") == "ACTIVE"]
        return {
            "account_id": aid,
            "campaign_count": len(camps),
            "active_campaigns": len(active),
            "creative_count": len(creatives),
            "campaigns_by_status": dict(
                Counter((c.get("status") or "UNKNOWN") for c in camps)
            ),
            "active": active[:50],
        }

    return await cached(
        "linkedin_ads", conn.id, "account_inventory", TTL_MEDIUM, _loader, args={"account_id": aid}
    )


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
async def list_campaign_groups(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)
    limit = (args or {}).get("limit", 100)

    async def _loader():
        els = await _elements(conn, f"/adAccounts/{aid}/adCampaignGroups", "search", limit)
        out = [{
            "id": e.get("id"),
            "name": e.get("name"),
            "status": e.get("status"),
            "total_budget": _amount(e.get("totalBudget")),
            "run_schedule": e.get("runSchedule"),
        } for e in els]
        return {"account_id": aid, "count": len(out), "campaign_groups": out}

    return await cached(
        "linkedin_ads", conn.id, "list_campaign_groups", TTL_LONG, _loader,
        args={"account_id": aid, "limit": limit},
    )


async def _load_campaigns(conn: Connection, aid: str, limit) -> dict:
    els = await _elements(conn, f"/adAccounts/{aid}/adCampaigns", "search", limit)
    out = [{
        "id": e.get("id"),
        "name": e.get("name"),
        "status": e.get("status"),
        "type": e.get("type"),
        "objective_type": e.get("objectiveType"),
        "cost_type": e.get("costType"),
        "daily_budget": _amount(e.get("dailyBudget")),
        "unit_cost": _amount(e.get("unitCost")),
        "campaign_group": e.get("campaignGroup"),
        "run_schedule": e.get("runSchedule"),
    } for e in els]
    return {"account_id": aid, "count": len(out), "campaigns": out}


async def list_campaigns(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)
    limit = (args or {}).get("limit", 100)

    async def _loader():
        return await _load_campaigns(conn, aid, limit)

    return await cached(
        "linkedin_ads", conn.id, "list_campaigns", TTL_LONG, _loader,
        args={"account_id": aid, "limit": limit},
    )


async def _load_creatives(conn: Connection, aid: str, limit) -> dict:
    els = await _elements(conn, f"/adAccounts/{aid}/creatives", "criteria", limit)
    out = [{
        "id": e.get("id"),
        "status": e.get("intendedStatus") or e.get("status"),
        "campaign": e.get("campaign"),
        "review_status": e.get("reviewStatus"),
        "is_serving": e.get("isServing"),
        "content": e.get("content"),
    } for e in els]
    return {"account_id": aid, "count": len(out), "creatives": out}


async def list_creatives(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)
    limit = (args or {}).get("limit", 100)

    async def _loader():
        return await _load_creatives(conn, aid, limit)

    return await cached(
        "linkedin_ads", conn.id, "list_creatives", TTL_LONG, _loader,
        args={"account_id": aid, "limit": limit},
    )


# --------------------------------------------------------------------------- #
# Performance (adAnalytics finder)
# --------------------------------------------------------------------------- #
async def _run_analytics(conn, aid, pivot, days, fields=None, start_date=None, end_date=None) -> dict:
    """adAnalytics finder. The restli query is built by hand and appended raw to
    the URL: LinkedIn needs literal commas in `fields`/`dateRange` and a
    percent-encoded URN inside accounts=List(...), which the default param
    encoder mangles (it %2C-encodes the field separators)."""
    default_fields = (
        "impressions,clicks,costInLocalCurrency,externalWebsiteConversions,"
        "oneClickLeads,likes,comments,shares,follows,pivotValues"
    )
    account_urn = quote(f"urn:li:sponsoredAccount:{aid}", safe="")
    query = (
        "q=analytics"
        f"&pivot={pivot}"
        "&timeGranularity=ALL"
        f"&dateRange={_date_range_param(days, start_date, end_date)}"
        f"&accounts=List({account_urn})"
        f"&fields={fields or default_fields}"
    )
    url = f"{BASE}/adAnalytics?{query}"
    try:
        async with limit_for(url):
            res = await http_get(url, headers=await _headers(conn))
    except UpstreamUnavailable as e:
        raise ConnectorError(f"LinkedIn adAnalytics unavailable: {e}")
    if res.status_code >= 400:
        try:
            msg = (res.json() or {}).get("message") or res.text[:300]
        except (ValueError, AttributeError):
            msg = res.text[:300]
        raise ConnectorError(f"LinkedIn API {res.status_code}: {msg}")
    try:
        data = res.json()
    except ValueError:
        raise ConnectorError("Invalid JSON from LinkedIn adAnalytics.")
    window = {"days": days}
    if start_date or end_date:
        window = {"start_date": start_date, "end_date": end_date}
    return {"account_id": aid, "pivot": pivot, **window, "rows": data.get("elements", []) or []}


async def analytics(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)
    a = args or {}
    pivot = a.get("pivot", "CAMPAIGN")
    days = a.get("days", 30)
    fields = a.get("fields")
    start_date = a.get("start_date")
    end_date = a.get("end_date")

    async def _loader():
        return await _run_analytics(conn, aid, pivot, days, fields, start_date, end_date)

    return await cached(
        "linkedin_ads", conn.id, "analytics", TTL_SHORT, _loader,
        args={"account_id": aid, "pivot": pivot, "days": days, "fields": fields,
              "start_date": start_date, "end_date": end_date},
    )


async def campaign_analytics(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)
    a = args or {}
    days = a.get("days", 30)
    start_date = a.get("start_date")
    end_date = a.get("end_date")

    async def _loader():
        return await _run_analytics(conn, aid, "CAMPAIGN", days, None, start_date, end_date)

    return await cached(
        "linkedin_ads", conn.id, "campaign_analytics", TTL_SHORT, _loader,
        args={"account_id": aid, "days": days, "start_date": start_date, "end_date": end_date},
    )


async def creative_analytics(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)
    a = args or {}
    days = a.get("days", 30)
    start_date = a.get("start_date")
    end_date = a.get("end_date")

    async def _loader():
        return await _run_analytics(conn, aid, "CREATIVE", days, None, start_date, end_date)

    return await cached(
        "linkedin_ads", conn.id, "creative_analytics", TTL_SHORT, _loader,
        args={"account_id": aid, "days": days, "start_date": start_date, "end_date": end_date},
    )


async def audience_breakdown(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)
    a = args or {}
    days = a.get("days", 30)
    pivot = a.get("pivot") or "MEMBER_INDUSTRY"
    start_date = a.get("start_date")
    end_date = a.get("end_date")

    async def _loader():
        return await _run_analytics(conn, aid, pivot, days, None, start_date, end_date)

    return await cached(
        "linkedin_ads", conn.id, "audience_breakdown", TTL_SHORT, _loader,
        args={"account_id": aid, "days": days, "pivot": pivot,
              "start_date": start_date, "end_date": end_date},
    )


async def geo_breakdown(conn: Connection, db, args: dict) -> dict:
    aid = _require_account(args)
    a = args or {}
    days = a.get("days", 30)
    start_date = a.get("start_date")
    end_date = a.get("end_date")

    async def _loader():
        return await _run_analytics(conn, aid, "MEMBER_REGION_V2", days, None, start_date, end_date)

    return await cached(
        "linkedin_ads", conn.id, "geo_breakdown", TTL_SHORT, _loader,
        args={"account_id": aid, "days": days, "start_date": start_date, "end_date": end_date},
    )


# --------------------------------------------------------------------------- #
# catalog + registration
# --------------------------------------------------------------------------- #
_ACCOUNT_PROP = {"type": "string", "description": "Sponsored ad account id (numeric, e.g. 500839718) or urn."}
_LIMIT_PROP = {"type": "integer", "description": "Max items (default 100).", "default": 100}
_DAYS_PROP = {"type": "integer", "description": "Trailing window in days when start/end omitted (default 30).", "default": 30}
_START_PROP = {"type": "string", "description": "Range start, YYYY-MM-DD (overrides days)."}
_END_PROP = {"type": "string", "description": "Range end, YYYY-MM-DD (overrides days)."}


def _analytics_input(extra: dict | None = None) -> dict:
    props = {
        "account_id": _ACCOUNT_PROP,
        "days": _DAYS_PROP,
        "start_date": _START_PROP,
        "end_date": _END_PROP,
    }
    if extra:
        props.update(extra)
    return {
        "type": "object",
        "properties": props,
        "required": ["account_id"],
        "additionalProperties": False,
    }


CATALOG = {
    "me": {
        "description": "Profile of the authenticated LinkedIn user.",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "list_ad_accounts": {
        "description": "List LinkedIn sponsored ad accounts the token can access.",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "list_all_ad_accounts": {
        "description": "All ad accounts including those owned by managed organisations.",
        "input": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "get_ad_account": {
        "description": "Detailed info for a single ad account.",
        "input": {
            "type": "object",
            "properties": {"account_id": _ACCOUNT_PROP},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    "account_inventory": {
        "description": "Active campaigns and creatives summary by account.",
        "input": {
            "type": "object",
            "properties": {"account_id": _ACCOUNT_PROP},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    "list_campaign_groups": {
        "description": "Campaign groups under an ad account with status and budget.",
        "input": {
            "type": "object",
            "properties": {"account_id": _ACCOUNT_PROP, "limit": _LIMIT_PROP},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    "list_campaigns": {
        "description": "Campaigns with objective, type, audience, schedule.",
        "input": {
            "type": "object",
            "properties": {"account_id": _ACCOUNT_PROP, "limit": _LIMIT_PROP},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    "list_creatives": {
        "description": "Ad creatives (single image, video, carousel, document).",
        "input": {
            "type": "object",
            "properties": {"account_id": _ACCOUNT_PROP, "limit": _LIMIT_PROP},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    "analytics": {
        "description": "Run generic analytics finder with pivots: CAMPAIGN, CREATIVE, MEMBER_*, etc.",
        "input": _analytics_input({
            "pivot": {"type": "string", "description": "Analytics pivot (default CAMPAIGN).", "default": "CAMPAIGN"},
            "fields": {"type": "string", "description": "Comma-separated metric fields to override the defaults."},
        }),
    },
    "campaign_analytics": {
        "description": "Performance per campaign — spend, impressions, clicks, leads.",
        "input": _analytics_input(),
    },
    "creative_analytics": {
        "description": "Per-creative performance breakdown.",
        "input": _analytics_input(),
    },
    "audience_breakdown": {
        "description": "Performance by member function, seniority, industry, company size.",
        "input": _analytics_input({
            "pivot": {"type": "string", "description": "Member pivot (default MEMBER_INDUSTRY)."},
        }),
    },
    "geo_breakdown": {
        "description": "Performance by member country / region (MEMBER_REGION_V2).",
        "input": _analytics_input(),
    },
}

HANDLERS = {
    "me": me,
    "list_ad_accounts": list_ad_accounts,
    "list_all_ad_accounts": list_all_ad_accounts,
    "get_ad_account": get_ad_account,
    "account_inventory": account_inventory,
    "list_campaign_groups": list_campaign_groups,
    "list_campaigns": list_campaigns,
    "list_creatives": list_creatives,
    "analytics": analytics,
    "campaign_analytics": campaign_analytics,
    "creative_analytics": creative_analytics,
    "audience_breakdown": audience_breakdown,
    "geo_breakdown": geo_breakdown,
}

registry.register(Connector(
    slug="linkedin_ads",
    label="LinkedIn Ads",
    auth=linkedin.LINKEDIN_AUTH,
    catalog=CATALOG,
    handlers=HANDLERS,
    description="Reads LinkedIn ad accounts, campaign groups, campaigns, creatives and analytics from the LinkedIn Marketing REST API.",
    category="Advertising",
))
