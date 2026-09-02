"""Meta Ads connector — Meta Marketing API (Graph API).

Reads accounts, structure (campaigns / ad sets / ads / creatives / audiences),
account- and entity-level insights, breakdowns, and conversion data from the
Facebook Marketing API. Authentication is a single long-lived user/system access
token, pasted by the user (auth="api_key"). The ad account id is NOT required at
setup — it is auto-discovered from the token (/me/adaccounts) on first use, so
every account-scoped tool works out of the box against the primary account. A
per-call `account_id`/`ad_account_id` arg still targets a specific account (use
list_ad_accounts to enumerate them). The access token is passed as a query
parameter on every Graph API call.

Budget fields (daily_budget / lifetime_budget) come back in the account's minor
currency units (e.g. cents). Spend/cpc/cpm from the insights endpoint are already
returned as decimal currency strings by Graph. This connector mirrors RAVEN's
deployed Meta Ads tool set faithfully — same endpoints, fields, breakdowns,
units, and response parsing.
"""
from __future__ import annotations

import json
import re
from datetime import datetime as _dt
from typing import Any, Optional

from connectors import registry
from connectors.registry import Connector
from connections.models import Connection
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached
from connectors.shims.concurrency import limit_for

BASE = "https://graph.facebook.com/v23.0"


# --------------------------------------------------------------------------- #
# Auth / creds helpers
# --------------------------------------------------------------------------- #
def _creds(conn: Connection) -> dict:
    return conn.creds() or {}


def _access_token(conn: Connection) -> str:
    token = _creds(conn).get("access_token")
    if not token:
        raise ConnectorError("Not connected: missing access_token.")
    return token


def _normalize_act(raw: Any) -> str:
    """Coerce a bare/numeric/act_-prefixed id into canonical 'act_<numeric>' form."""
    raw = str(raw).strip()
    if not raw.startswith("act_"):
        raw = f"act_{raw.lstrip('act_')}"
    rest = raw[4:]
    if not rest.isdigit():
        raise ConnectorError("account_id must be 'act_<numeric>' or a numeric id.")
    return raw


async def _default_act_id(conn: Connection) -> Optional[str]:
    """Auto-discover which ad account to use when the caller didn't name one.

    Lists every ad account the token can see (/me/adaccounts) and picks a sensible
    default: the highest-spend ACTIVE account, else the highest-spend account, else
    simply the first one. Cached (TTL_LONG) so we don't re-query Graph on every call.
    Returns None only if the token can see no ad accounts at all.
    """
    async def _loader():
        data = await _request(conn, "/me/adaccounts", {
            "fields": "id,account_status,amount_spent", "limit": 500,
        })
        accts = data.get("data") or []
        if not accts:
            return {"default": None}

        def _spent(a: dict) -> int:
            try:
                return int(a.get("amount_spent") or 0)
            except (TypeError, ValueError):
                return 0

        active = [a for a in accts if a.get("account_status") == 1]
        pool = active or accts
        pool = sorted(pool, key=_spent, reverse=True)
        return {"default": pool[0].get("id")}

    res = await cached("meta_ads", conn.id, "default_act_id", TTL_LONG, _loader, args={})
    return (res or {}).get("default")


async def _act_id(conn: Connection, args: dict) -> str:
    """Resolve the ad account id for a call, ensuring canonical 'act_<numeric>' form.

    Precedence:
      1. Explicit per-call arg — `account_id` (RAVEN's name) or `ad_account_id`.
      2. A stored `ad_account_id` credential (legacy connections that pinned one).
      3. Auto-discovered default from the access token (/me/adaccounts).

    So the user never has to paste an ad account id: connect the token and every
    account-scoped tool just works against the primary account, while a per-call
    arg still targets any specific account (see list_ad_accounts).
    """
    raw = (args or {}).get("account_id") or (args or {}).get("ad_account_id") or _creds(conn).get("ad_account_id")
    if not raw:
        raw = await _default_act_id(conn)
    if not raw:
        raise ConnectorError(
            "No ad account is accessible with this access token. Make sure the token "
            "has the ads_read permission and access to at least one ad account, then "
            "reconnect. (You can also pass account_id explicitly on the tool call.)"
        )
    return _normalize_act(raw)


def _id_from_resource(arg_val: Any, prefix: str = "") -> str:
    """Accept either a bare numeric id or a Graph API resource path."""
    s = str(arg_val or "").strip()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if prefix and s.startswith(prefix + "_"):
        s = s[len(prefix) + 1:]
    return s


# --------------------------------------------------------------------------- #
# Date / param helpers
# --------------------------------------------------------------------------- #
# Meta's predefined date presets.
DATE_PRESETS = {
    "today", "yesterday", "this_month", "last_month",
    "this_quarter", "last_quarter", "lifetime",
    "last_3d", "last_7d", "last_14d", "last_28d", "last_30d", "last_90d",
    "last_week_mon_sun", "last_week_sun_sat",
    "this_week_mon_today", "this_week_sun_today",
    "maximum",
}

# Standard fields requested on insights queries.
INSIGHT_FIELDS = (
    "impressions,reach,frequency,clicks,unique_clicks,cpc,cpm,cpp,ctr,"
    "spend,conversions,conversion_values,cost_per_conversion,"
    "actions,action_values,cost_per_action_type"
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_range(args: dict, default: str = "last_30d") -> str:
    dr = str((args or {}).get("date_range", default)).lower().strip()
    if dr not in DATE_PRESETS:
        raise ConnectorError(
            f"date_range must be one of: {', '.join(sorted(DATE_PRESETS))}"
        )
    return dr


def _custom_window(args: dict) -> Optional[tuple[str, str]]:
    """Return (since, until) if both start_date and end_date are supplied, else None."""
    sd = str((args or {}).get("start_date", "")).strip()
    ed = str((args or {}).get("end_date", "")).strip()
    if not (sd and ed):
        return None
    for label, val in (("start_date", sd), ("end_date", ed)):
        if not _DATE_RE.match(val):
            raise ConnectorError(f"{label} must be YYYY-MM-DD (got '{val}').")
    return sd, ed


def _date_params(args: dict) -> dict:
    """Meta insights date param: explicit time_range for a custom window,
    otherwise the predefined date_preset."""
    window = _custom_window(args)
    if window:
        return {"time_range": json.dumps({"since": window[0], "until": window[1]})}
    return {"date_preset": _date_range(args)}


def _date_label(args: dict) -> str:
    """Human-readable label for the date window used (for response payloads)."""
    window = _custom_window(args)
    if window:
        return f"{window[0]}..{window[1]}"
    return _date_range(args)


def _limit(args: dict, default: int = 100, max_value: int = 500) -> int:
    try:
        n = int((args or {}).get("limit", default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, max_value))


# --------------------------------------------------------------------------- #
# Graph API request helpers (async)
# --------------------------------------------------------------------------- #
async def _graph_get(url: str, params: dict) -> dict:
    """GET a Graph API endpoint, surfacing Graph's structured error message."""
    try:
        async with limit_for(url):
            res = await http_get(url, params=params)
    except UpstreamUnavailable as e:
        raise ConnectorError(f"Meta API unavailable: {e}")
    if res.status_code >= 400:
        msg = None
        code = sub = "?"
        try:
            err = (res.json() or {}).get("error") or {}
            if isinstance(err, dict):
                msg = err.get("message")
                code = err.get("code", "?")
                sub = err.get("error_subcode", "?")
        except Exception:
            msg = None
        if msg:
            raise ConnectorError(f"Meta API {res.status_code}: {msg} (code={code} sub={sub})")
        raise ConnectorError(f"Meta API {res.status_code}: {res.text[:300]}")
    return res.json() or {}


async def _request(conn: Connection, path: str, params: dict | None = None) -> dict:
    """GET /<path> on the Graph API with the access token attached."""
    token = _access_token(conn)
    p = dict(params or {})
    p["access_token"] = token
    url = f"{BASE}{path}"
    return await _graph_get(url, p)


async def _paginate(conn: Connection, path: str, params: dict, max_pages: int = 5) -> list[dict]:
    """Walk through Graph API pages following `next` cursors, capped at max_pages."""
    out: list[dict] = []
    data = await _request(conn, path, params)
    out.extend(data.get("data", []) or [])
    for _ in range(max_pages - 1):
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        # next URL is fully formed (including access_token); GET it directly.
        try:
            async with limit_for(nxt):
                res = await http_get(nxt)
        except UpstreamUnavailable:
            break
        if res.status_code >= 400:
            break
        data = res.json() or {}
        out.extend(data.get("data", []) or [])
    return out


async def _insights(
    conn: Connection,
    resource_path: str,
    args: dict,
    *,
    level: Optional[str] = None,
    breakdowns: Optional[str] = None,
    action_breakdowns: Optional[str] = None,
    extra_fields: Optional[str] = None,
    extra_params: Optional[dict] = None,
) -> list[dict]:
    """Hit /<resource>/insights with the standard params and return rows."""
    p: dict[str, Any] = {
        "fields": INSIGHT_FIELDS + (f",{extra_fields}" if extra_fields else ""),
        "limit": _limit(args, 100, 500),
    }
    p.update(_date_params(args))
    if level:
        p["level"] = level
    if breakdowns:
        p["breakdowns"] = breakdowns
    if action_breakdowns:
        p["action_breakdowns"] = action_breakdowns
    if extra_params:
        p.update(extra_params)
    data = await _request(conn, f"/{resource_path}/insights", p)
    return data.get("data", []) or []


def _normalize_insight(row: dict) -> dict:
    """Flatten a Meta insights row into something AI-friendly."""

    def _num(v, kind=float):
        try:
            return kind(v)
        except (TypeError, ValueError):
            return 0 if kind is int else 0.0

    actions = {a.get("action_type", ""): _num(a.get("value")) for a in row.get("actions", []) or []}
    action_values = {a.get("action_type", ""): _num(a.get("value")) for a in row.get("action_values", []) or []}
    cost_per_action = {a.get("action_type", ""): _num(a.get("value")) for a in row.get("cost_per_action_type", []) or []}
    return {
        "impressions": _num(row.get("impressions"), int),
        "reach": _num(row.get("reach"), int),
        "frequency": _num(row.get("frequency")),
        "clicks": _num(row.get("clicks"), int),
        "unique_clicks": _num(row.get("unique_clicks"), int),
        "spend": _num(row.get("spend")),
        "cpc": _num(row.get("cpc")),
        "cpm": _num(row.get("cpm")),
        "cpp": _num(row.get("cpp")),
        "ctr": _num(row.get("ctr")),
        "conversions": _num(row.get("conversions")),
        "conversion_value": _num(row.get("conversion_values")),
        "cost_per_conversion": _num(row.get("cost_per_conversion")),
        "actions": actions,
        "action_values": action_values,
        "cost_per_action": cost_per_action,
    }


def _cache_args(act: str, args: dict, *, extra: dict | None = None) -> dict:
    """Build the cache-key args dict: account + date window + limit + extras."""
    out: dict[str, Any] = {"act": act}
    window = _custom_window(args)
    if window:
        out["start_date"] = window[0]
        out["end_date"] = window[1]
    else:
        out["date_range"] = _date_range(args)
    if extra:
        out.update(extra)
    return out


# ============================================================
# ACCOUNT-LEVEL TOOLS
# ============================================================
_ACCOUNT_STATUS_MAP = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT", 9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE",
    101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED",
}


async def list_ad_accounts(conn: Connection, db, args: dict) -> dict:
    async def _loader():
        fields = "id,name,account_status,currency,timezone_name,amount_spent,balance,business{id,name}"
        data = await _request(conn, "/me/adaccounts", {"fields": fields, "limit": 500})
        out = []
        for a in data.get("data", []) or []:
            biz = a.get("business") or {}
            out.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "status_code": a.get("account_status"),
                "status": _ACCOUNT_STATUS_MAP.get(a.get("account_status"), str(a.get("account_status") or "")),
                "currency": a.get("currency"),
                "timezone": a.get("timezone_name"),
                "amount_spent": a.get("amount_spent"),
                "balance": a.get("balance"),
                "business_id": biz.get("id"),
                "business_name": biz.get("name"),
            })
        return {"ad_accounts": out}

    return await cached("meta_ads", conn.id, "list_ad_accounts", TTL_LONG, _loader, args={})


async def list_business_accounts(conn: Connection, db, args: dict) -> dict:
    async def _loader():
        data = await _request(conn, "/me/businesses", {
            "fields": "id,name,verification_status,primary_page,profile_picture_uri",
        })
        return {"businesses": data.get("data", []) or []}

    return await cached("meta_ads", conn.id, "list_business_accounts", TTL_LONG, _loader, args={})


async def list_pages(conn: Connection, db, args: dict) -> dict:
    async def _loader():
        data = await _request(conn, "/me/accounts", {
            "fields": "id,name,category,tasks,fan_count,link,verification_status",
            "limit": 200,
        })
        return {"pages": data.get("data", []) or []}

    return await cached("meta_ads", conn.id, "list_pages", TTL_LONG, _loader, args={})


async def list_instagram_accounts(conn: Connection, db, args: dict) -> dict:
    async def _loader():
        pages = (await _request(conn, "/me/accounts", {"fields": "id,name", "limit": 200})).get("data", []) or []
        out = []
        for p in pages[:50]:  # cap to avoid 50 follow-up requests
            pid = p.get("id")
            try:
                ig = await _request(conn, f"/{pid}", {
                    "fields": "instagram_business_account{id,username,profile_picture_url,followers_count,media_count}",
                })
            except ConnectorError:
                continue
            iba = ig.get("instagram_business_account")
            if iba:
                out.append({
                    "page_id": pid,
                    "page_name": p.get("name"),
                    "ig_id": iba.get("id"),
                    "ig_username": iba.get("username"),
                    "ig_followers": iba.get("followers_count"),
                    "ig_media_count": iba.get("media_count"),
                })
        return {"instagram_accounts": out, "count": len(out)}

    return await cached("meta_ads", conn.id, "list_instagram_accounts", TTL_LONG, _loader, args={})


async def account_insights(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account")
        norm = [_normalize_insight(r) for r in rows]
        return {
            "account_id": act,
            "date_range": dr,
            "totals": norm[0] if norm else {},
        }

    return await cached("meta_ads", conn.id, "account_insights", TTL_SHORT, _loader, args=_cache_args(act, args))


async def account_health_check(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)

    async def _loader():
        info = await _request(conn, f"/{act}", {
            "fields": "name,account_status,disable_reason,balance,amount_spent,spend_cap,currency",
        })
        camps = await _paginate(conn, f"/{act}/campaigns", {"fields": "id,effective_status", "limit": 500}, max_pages=2)
        status_counts: dict[str, int] = {}
        for c in camps:
            s = c.get("effective_status", "UNKNOWN")
            status_counts[s] = status_counts.get(s, 0) + 1
        ads = await _paginate(conn, f"/{act}/ads", {
            "fields": "id,effective_status,issues_info",
            "limit": 500,
        }, max_pages=2)
        ads_with_issues = [a for a in ads if a.get("issues_info")]
        return {
            "account_id": act,
            "summary": {
                "name": info.get("name"),
                "status": info.get("account_status"),
                "currency": info.get("currency"),
                "balance": info.get("balance"),
                "amount_spent": info.get("amount_spent"),
                "spend_cap": info.get("spend_cap"),
                "campaign_count": len(camps),
                "campaign_status_counts": status_counts,
                "ads_with_issues": len(ads_with_issues),
            },
            "ads_needing_attention": [
                {"id": a.get("id"), "status": a.get("effective_status"), "issues": a.get("issues_info")}
                for a in ads_with_issues[:30]
            ],
        }

    return await cached("meta_ads", conn.id, "account_health_check", TTL_SHORT, _loader, args={"act": act})


# ============================================================
# STRUCTURE
# ============================================================
async def list_campaigns(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    limit = _limit(args, 100, 500)
    status_filter = str((args or {}).get("status") or "").upper().strip()

    async def _loader():
        p = {
            "fields": "id,name,status,effective_status,objective,buying_type,daily_budget,lifetime_budget,bid_strategy,start_time,stop_time,created_time,updated_time,special_ad_categories",
            "limit": limit,
        }
        if status_filter:
            p["effective_status"] = f"['{status_filter}']"
        rows = await _paginate(conn, f"/{act}/campaigns", p, max_pages=3)
        out = []
        for c in rows:
            out.append({
                "id": c.get("id"),
                "name": c.get("name"),
                "status": c.get("status"),
                "effective_status": c.get("effective_status"),
                "objective": c.get("objective"),
                "buying_type": c.get("buying_type"),
                "daily_budget": c.get("daily_budget"),
                "lifetime_budget": c.get("lifetime_budget"),
                "bid_strategy": c.get("bid_strategy"),
                "start_time": c.get("start_time"),
                "stop_time": c.get("stop_time"),
                "created_time": c.get("created_time"),
                "special_ad_categories": c.get("special_ad_categories", []),
            })
        return {"account_id": act, "count": len(out), "campaigns": out}

    return await cached("meta_ads", conn.id, "list_campaigns", TTL_MEDIUM, _loader,
                        args={"act": act, "limit": limit, "status": status_filter})


async def list_ad_sets(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    limit = _limit(args, 100, 500)
    campaign_id = str((args or {}).get("campaign_id", "")).strip()

    async def _loader():
        p = {
            "fields": "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,bid_amount,billing_event,optimization_goal,start_time,end_time,targeting{age_min,age_max,genders,geo_locations,interests,custom_audiences}",
            "limit": limit,
        }
        path = f"/{campaign_id}/adsets" if campaign_id else f"/{act}/adsets"
        rows = await _paginate(conn, path, p, max_pages=3)
        out = []
        for a in rows:
            t = a.get("targeting") or {}
            out.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "campaign_id": a.get("campaign_id"),
                "status": a.get("status"),
                "effective_status": a.get("effective_status"),
                "daily_budget": a.get("daily_budget"),
                "lifetime_budget": a.get("lifetime_budget"),
                "bid_amount": a.get("bid_amount"),
                "billing_event": a.get("billing_event"),
                "optimization_goal": a.get("optimization_goal"),
                "start_time": a.get("start_time"),
                "end_time": a.get("end_time"),
                "targeting_summary": {
                    "age_min": t.get("age_min"),
                    "age_max": t.get("age_max"),
                    "genders": t.get("genders"),
                    "country_count": len((t.get("geo_locations") or {}).get("countries", []) or []),
                    "interest_count": len(t.get("interests") or []),
                    "custom_audience_count": len(t.get("custom_audiences") or []),
                },
            })
        return {"account_id": act, "count": len(out), "ad_sets": out}

    return await cached("meta_ads", conn.id, "list_ad_sets", TTL_MEDIUM, _loader,
                        args={"act": act, "limit": limit, "campaign_id": campaign_id})


async def list_ads(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    limit = _limit(args, 100, 500)
    ad_set_id = str((args or {}).get("ad_set_id", "")).strip()

    async def _loader():
        p = {
            "fields": "id,name,adset_id,campaign_id,status,effective_status,creative{id,name,thumbnail_url,object_story_spec},preview_shareable_link",
            "limit": limit,
        }
        path = f"/{ad_set_id}/ads" if ad_set_id else f"/{act}/ads"
        rows = await _paginate(conn, path, p, max_pages=3)
        out = []
        for a in rows:
            cr = a.get("creative") or {}
            out.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "ad_set_id": a.get("adset_id"),
                "campaign_id": a.get("campaign_id"),
                "status": a.get("status"),
                "effective_status": a.get("effective_status"),
                "creative_id": cr.get("id"),
                "creative_name": cr.get("name"),
                "thumbnail": cr.get("thumbnail_url"),
                "preview_link": a.get("preview_shareable_link"),
            })
        return {"account_id": act, "count": len(out), "ads": out}

    return await cached("meta_ads", conn.id, "list_ads", TTL_MEDIUM, _loader,
                        args={"act": act, "limit": limit, "ad_set_id": ad_set_id})


async def list_creatives(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    limit = _limit(args, 100, 500)

    async def _loader():
        p = {
            "fields": "id,name,title,body,call_to_action_type,thumbnail_url,object_story_spec,image_url,video_id,status",
            "limit": limit,
        }
        rows = await _paginate(conn, f"/{act}/adcreatives", p, max_pages=3)
        out = []
        for c in rows:
            oss = c.get("object_story_spec") or {}
            out.append({
                "id": c.get("id"),
                "name": c.get("name"),
                "title": c.get("title"),
                "body": c.get("body"),
                "cta": c.get("call_to_action_type"),
                "thumbnail": c.get("thumbnail_url"),
                "image_url": c.get("image_url"),
                "video_id": c.get("video_id"),
                "page_id": oss.get("page_id"),
                "ig_actor_id": oss.get("instagram_actor_id"),
                "status": c.get("status"),
            })
        return {"account_id": act, "count": len(out), "creatives": out}

    return await cached("meta_ads", conn.id, "list_creatives", TTL_MEDIUM, _loader,
                        args={"act": act, "limit": limit})


async def list_audiences(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    limit = _limit(args, 100, 500)

    async def _loader():
        p = {
            "fields": "id,name,description,subtype,approximate_count_lower_bound,approximate_count_upper_bound,delivery_status,permission_for_actions,operation_status,time_created,time_updated",
            "limit": limit,
        }
        rows = await _paginate(conn, f"/{act}/customaudiences", p, max_pages=3)
        out = []
        for a in rows:
            out.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "description": a.get("description"),
                "subtype": a.get("subtype"),
                "size_min": a.get("approximate_count_lower_bound"),
                "size_max": a.get("approximate_count_upper_bound"),
                "delivery_status": (a.get("delivery_status") or {}).get("code"),
                "operation_status": (a.get("operation_status") or {}).get("code"),
                "created": a.get("time_created"),
                "updated": a.get("time_updated"),
            })
        return {"account_id": act, "count": len(out), "audiences": out}

    return await cached("meta_ads", conn.id, "list_audiences", TTL_MEDIUM, _loader,
                        args={"act": act, "limit": limit})


async def saved_audiences(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)

    async def _loader():
        p = {"fields": "id,name,description,run_status,sentence_lines,targeting,time_created", "limit": 200}
        rows = await _paginate(conn, f"/{act}/saved_audiences", p, max_pages=2)
        out = []
        for s in rows:
            out.append({
                "id": s.get("id"),
                "name": s.get("name"),
                "description": s.get("description"),
                "run_status": s.get("run_status"),
                "summary": s.get("sentence_lines"),
                "created": s.get("time_created"),
            })
        return {"account_id": act, "count": len(out), "saved_audiences": out}

    return await cached("meta_ads", conn.id, "saved_audiences", TTL_MEDIUM, _loader, args={"act": act})


async def list_pixels(conn: Connection, db, args: dict) -> dict:
    """List Meta Pixels on this ad account. Requires `ads_pixel_management`
    (or `ads_management`) permission — if missing, return an empty list with a
    friendly note rather than leaking Meta's raw 'Missing Permission' error."""
    act = await _act_id(conn, args)

    async def _loader():
        p = {"fields": "id,name,last_fired_time,is_unavailable,can_proxy", "limit": 100}
        try:
            rows = await _paginate(conn, f"/{act}/adspixels", p, max_pages=2)
        except ConnectorError as e:
            msg = str(e)
            if "Missing Permission" in msg or "permission" in msg.lower():
                return {
                    "account_id": act,
                    "count": 0,
                    "pixels": [],
                    "note": "Token doesn't have `ads_pixel_management` (or equivalent) permission for this account. Reconnect Meta with that scope to see pixels here. Other tools that don't need pixel scope still work.",
                }
            raise
        out = []
        for px in rows:
            out.append({
                "id": px.get("id"),
                "name": px.get("name"),
                "last_fired": px.get("last_fired_time"),
                "is_unavailable": px.get("is_unavailable"),
                "can_proxy": px.get("can_proxy"),
            })
        return {"account_id": act, "count": len(out), "pixels": out}

    return await cached("meta_ads", conn.id, "list_pixels", TTL_MEDIUM, _loader, args={"act": act})


async def list_custom_conversions(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)

    async def _loader():
        p = {"fields": "id,name,custom_event_type,event_source_id,rule,creation_time,is_archived", "limit": 200}
        rows = await _paginate(conn, f"/{act}/customconversions", p, max_pages=2)
        out = []
        for cc in rows:
            out.append({
                "id": cc.get("id"),
                "name": cc.get("name"),
                "event_type": cc.get("custom_event_type"),
                "pixel_id": cc.get("event_source_id"),
                "rule": cc.get("rule"),
                "archived": cc.get("is_archived"),
                "created": cc.get("creation_time"),
            })
        return {"account_id": act, "count": len(out), "custom_conversions": out}

    return await cached("meta_ads", conn.id, "list_custom_conversions", TTL_MEDIUM, _loader, args={"act": act})


# ============================================================
# PERFORMANCE / INSIGHTS
# ============================================================
async def campaign_insights(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="campaign", extra_fields="campaign_id,campaign_name")
        out = []
        for r in rows:
            out.append({
                "campaign_id": r.get("campaign_id"),
                "campaign_name": r.get("campaign_name"),
                **_normalize_insight(r),
            })
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "campaign_insights", TTL_SHORT, _loader, args=_cache_args(act, args))


async def ad_set_insights(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="adset",
                               extra_fields="adset_id,adset_name,campaign_id,campaign_name")
        out = []
        for r in rows:
            out.append({
                "ad_set_id": r.get("adset_id"),
                "ad_set_name": r.get("adset_name"),
                "campaign_id": r.get("campaign_id"),
                "campaign_name": r.get("campaign_name"),
                **_normalize_insight(r),
            })
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "ad_set_insights", TTL_SHORT, _loader, args=_cache_args(act, args))


async def ad_insights(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="ad",
                               extra_fields="ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name")
        out = []
        for r in rows:
            out.append({
                "ad_id": r.get("ad_id"),
                "ad_name": r.get("ad_name"),
                "ad_set_id": r.get("adset_id"),
                "ad_set_name": r.get("adset_name"),
                "campaign_id": r.get("campaign_id"),
                "campaign_name": r.get("campaign_name"),
                **_normalize_insight(r),
            })
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "ad_insights", TTL_SHORT, _loader, args=_cache_args(act, args))


async def ad_creative_performance(conn: Connection, db, args: dict) -> dict:
    """Top ads by spend, joined with their creative thumbnails / headlines."""
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        insight_rows = await _insights(conn, act, args, level="ad", extra_fields="ad_id,ad_name")
        insight_rows.sort(key=lambda r: -float(r.get("spend") or 0))
        top = insight_rows[:30]
        ad_ids = [r.get("ad_id") for r in top if r.get("ad_id")]

        creative_by_ad: dict[str, dict] = {}
        if ad_ids:
            ids_csv = ",".join(ad_ids)
            try:
                data = await _request(conn, "/", {
                    "ids": ids_csv,
                    "fields": "id,name,creative{id,name,title,body,call_to_action_type,thumbnail_url,image_url}",
                })
                for ad_id, ad in (data or {}).items():
                    if ad_id == "access_token" or not isinstance(ad, dict):
                        continue
                    creative_by_ad[ad_id] = ad.get("creative", {}) or {}
            except ConnectorError:
                # Fallback to individual lookups (slower)
                for aid_ in ad_ids[:30]:
                    try:
                        ad = await _request(conn, f"/{aid_}", {
                            "fields": "creative{id,name,title,body,call_to_action_type,thumbnail_url,image_url}"
                        })
                        creative_by_ad[aid_] = ad.get("creative", {}) or {}
                    except ConnectorError:
                        continue

        out = []
        for r in top:
            ad_id = r.get("ad_id")
            cr = creative_by_ad.get(ad_id, {})
            out.append({
                "ad_id": ad_id,
                "ad_name": r.get("ad_name"),
                "creative_id": cr.get("id"),
                "creative_name": cr.get("name"),
                "headline": cr.get("title"),
                "body": (cr.get("body") or "")[:200],
                "cta": cr.get("call_to_action_type"),
                "thumbnail": cr.get("thumbnail_url"),
                **_normalize_insight(r),
            })
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "ad_creative_performance", TTL_SHORT, _loader, args=_cache_args(act, args))


async def action_breakdown(conn: Connection, db, args: dict) -> dict:
    """Pivot a normal insights call by action_type (leads, conversions, link_clicks, etc.)."""
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account", action_breakdowns="action_type")
        out: dict[str, dict] = {}
        for r in rows:
            for a in r.get("actions", []) or []:
                t = a.get("action_type", "?")
                try:
                    v = float(a.get("value") or 0)
                except (TypeError, ValueError):
                    v = 0
                agg = out.setdefault(t, {"action_type": t, "count": 0, "value": 0.0})
                agg["count"] += v
            for a in r.get("action_values", []) or []:
                t = a.get("action_type", "?")
                try:
                    v = float(a.get("value") or 0)
                except (TypeError, ValueError):
                    v = 0
                agg = out.setdefault(t, {"action_type": t, "count": 0, "value": 0.0})
                agg["value"] += v
        rows_out = sorted(out.values(), key=lambda x: -x.get("count", 0))
        return {"account_id": act, "date_range": dr, "actions": rows_out, "count": len(rows_out)}

    return await cached("meta_ads", conn.id, "action_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def conversion_data(conn: Connection, db, args: dict) -> dict:
    """Conversion-only insights with per-conversion-action breakdown."""
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account")
        norm = [_normalize_insight(r) for r in rows]
        total_conv = sum(r["conversions"] for r in norm)
        total_value = sum(r["conversion_value"] for r in norm)
        total_spend = sum(r["spend"] for r in norm)
        return {
            "account_id": act,
            "date_range": dr,
            "totals": {
                "conversions": total_conv,
                "conversion_value": round(total_value, 2),
                "spend": round(total_spend, 2),
                "cost_per_conversion": round(total_spend / total_conv, 2) if total_conv else 0,
                "roas": round(total_value / total_spend, 2) if total_spend else 0,
            },
            "actions_breakdown": (norm[0].get("actions") if norm else {}),
            "action_values_breakdown": (norm[0].get("action_values") if norm else {}),
        }

    return await cached("meta_ads", conn.id, "conversion_data", TTL_SHORT, _loader, args=_cache_args(act, args))


# ---------- breakdown tools ----------
async def demographic_breakdown(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account", breakdowns="age,gender")
        out = [{"age": r.get("age"), "gender": r.get("gender"), **_normalize_insight(r)} for r in rows]
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "demographic_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def age_breakdown(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account", breakdowns="age")
        out = [{"age": r.get("age"), **_normalize_insight(r)} for r in rows]
        out.sort(key=lambda x: x.get("age") or "")
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "age_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def gender_breakdown(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account", breakdowns="gender")
        out = [{"gender": r.get("gender"), **_normalize_insight(r)} for r in rows]
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "gender_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def placement_breakdown(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account",
                               breakdowns="publisher_platform,platform_position,impression_device")
        out = []
        for r in rows:
            out.append({
                "publisher": r.get("publisher_platform"),
                "position": r.get("platform_position"),
                "device": r.get("impression_device"),
                **_normalize_insight(r),
            })
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "placement_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def publisher_platform_breakdown(conn: Connection, db, args: dict) -> dict:
    """Just FB vs Instagram vs Audience Network vs Messenger — no positions."""
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account", breakdowns="publisher_platform")
        out = [{"publisher": r.get("publisher_platform"), **_normalize_insight(r)} for r in rows]
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "publisher_platform_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def device_breakdown(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account", breakdowns="impression_device")
        out = [{"device": r.get("impression_device"), **_normalize_insight(r)} for r in rows]
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "device_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def country_breakdown(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account", breakdowns="country")
        out = [{"country": r.get("country"), **_normalize_insight(r)} for r in rows]
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "country_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def region_breakdown(conn: Connection, db, args: dict) -> dict:
    """Sub-country regions (state / province)."""
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account", breakdowns="region")
        out = [{"region": r.get("region"), **_normalize_insight(r)} for r in rows]
        out.sort(key=lambda x: -x.get("spend", 0))
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "region_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def hourly_breakdown(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        rows = await _insights(conn, act, args, level="account",
                               breakdowns="hourly_stats_aggregated_by_advertiser_time_zone")
        out = []
        for r in rows:
            out.append({
                "hour": r.get("hourly_stats_aggregated_by_advertiser_time_zone"),
                **_normalize_insight(r),
            })
        out.sort(key=lambda x: x.get("hour") or "")
        return {"account_id": act, "date_range": dr, "count": len(out), "rows": out}

    return await cached("meta_ads", conn.id, "hourly_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


async def day_of_week_breakdown(conn: Connection, db, args: dict) -> dict:
    act = await _act_id(conn, args)
    dr = _date_label(args)

    async def _loader():
        # Segment by day (time_increment=1) and group client-side.
        rows = await _insights(conn, act, args, level="account", extra_params={"time_increment": 1})
        buckets: dict[int, dict] = {}
        for r in rows:
            d = r.get("date_start") or ""
            try:
                dow = _dt.strptime(d, "%Y-%m-%d").weekday()  # 0=Mon
            except ValueError:
                continue
            n = _normalize_insight(r)
            b = buckets.setdefault(dow, {"impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0.0, "conversion_value": 0.0})
            b["impressions"] += n["impressions"]
            b["clicks"] += n["clicks"]
            b["spend"] += n["spend"]
            b["conversions"] += n["conversions"]
            b["conversion_value"] += n["conversion_value"]
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        out = []
        for i in range(7):
            b = buckets.get(i, {})
            out.append({
                "day": names[i],
                "weekday": i,
                "impressions": b.get("impressions", 0),
                "clicks": b.get("clicks", 0),
                "spend": round(b.get("spend", 0), 2),
                "conversions": b.get("conversions", 0),
                "conversion_value": round(b.get("conversion_value", 0), 2),
                "ctr": round(b.get("clicks", 0) / b.get("impressions", 1) * 100, 2) if b.get("impressions") else 0,
            })
        return {"account_id": act, "date_range": dr, "rows": out}

    return await cached("meta_ads", conn.id, "day_of_week_breakdown", TTL_SHORT, _loader, args=_cache_args(act, args))


# ============================================================
# CONVERSIONS / LEADS
# ============================================================
async def lead_form_data(conn: Connection, db, args: dict) -> dict:
    """Pull Lead Ads form metadata (and recent lead counts where permitted)
    for the pages this user manages."""
    async def _loader():
        pages = (await _request(conn, "/me/accounts", {"fields": "id,name", "limit": 50})).get("data", []) or []
        out = []
        for p in pages[:25]:
            pid = p.get("id")
            try:
                data = await _request(conn, f"/{pid}/leadgen_forms", {
                    "fields": "id,name,status,leads_count,created_time,questions",
                    "limit": 50,
                })
            except ConnectorError:
                continue
            for f in (data.get("data") or []):
                out.append({
                    "page_id": pid,
                    "page_name": p.get("name"),
                    "form_id": f.get("id"),
                    "form_name": f.get("name"),
                    "status": f.get("status"),
                    "leads_count": f.get("leads_count"),
                    "question_count": len(f.get("questions") or []),
                    "created": f.get("created_time"),
                })
        return {"count": len(out), "lead_forms": out}

    return await cached("meta_ads", conn.id, "lead_form_data", TTL_MEDIUM, _loader, args={})


# --------------------------------------------------------------------------- #
# Catalog + registration
# --------------------------------------------------------------------------- #
_ACCOUNT_PROP = {"type": "string", "description": "Ad account id (act_ prefix optional). Optional — defaults to the token's primary ad account (auto-detected); call list_ad_accounts to see all and target a specific one."}
_DATE_PROPS = {
    "date_range": {
        "type": "string",
        "description": "Meta date preset (last_30d, last_7d, today, lifetime, etc.). Ignored if start_date/end_date given.",
        "default": "last_30d",
    },
    "start_date": {"type": "string", "description": "Custom window start (YYYY-MM-DD). Requires end_date."},
    "end_date": {"type": "string", "description": "Custom window end (YYYY-MM-DD). Requires start_date."},
}


def _account_input(extra: dict | None = None) -> dict:
    props = {"account_id": _ACCOUNT_PROP, "ad_account_id": _ACCOUNT_PROP}
    if extra:
        props.update(extra)
    return {"type": "object", "properties": props, "required": [], "additionalProperties": False}


def _insights_input(extra: dict | None = None) -> dict:
    props = {"account_id": _ACCOUNT_PROP, "ad_account_id": _ACCOUNT_PROP, **_DATE_PROPS,
             "limit": {"type": "integer", "description": "Max rows to return.", "default": 100}}
    if extra:
        props.update(extra)
    return {"type": "object", "properties": props, "required": [], "additionalProperties": False}


_NO_INPUT = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


CATALOG = {
    # ---------- Accounts ----------
    "list_ad_accounts": {
        "description": "All Meta ad accounts the token has access to (id, name, status, currency, balance, spend).",
        "input": _NO_INPUT,
    },
    "list_business_accounts": {
        "description": "Business Manager accounts (id, name, verification status, primary page).",
        "input": _NO_INPUT,
    },
    "list_pages": {
        "description": "Facebook Pages the user manages (id, name, category, fan count, role tasks).",
        "input": _NO_INPUT,
    },
    "list_instagram_accounts": {
        "description": "Connected Instagram business profiles per managed Page (username, followers, media count).",
        "input": _NO_INPUT,
    },
    "account_insights": {
        "description": "Account-level totals (spend, reach, clicks, conversions, ROAS metrics) over a date range.",
        "input": _insights_input(),
    },
    "account_health_check": {
        "description": "Snapshot of account status, balance, campaign status counts, and ads needing attention.",
        "input": _account_input(),
    },
    # ---------- Structure ----------
    "list_campaigns": {
        "description": "Campaigns under the ad account (objective, status, budgets, bid strategy, schedule).",
        "input": _account_input({
            "limit": {"type": "integer", "description": "Max campaigns to return.", "default": 100},
            "status": {"type": "string", "description": "Filter by effective_status (e.g. ACTIVE, PAUSED)."},
        }),
    },
    "list_ad_sets": {
        "description": "Ad sets with targeting summary, schedule, budget, optimization goal.",
        "input": _account_input({
            "limit": {"type": "integer", "description": "Max ad sets to return.", "default": 100},
            "campaign_id": {"type": "string", "description": "Restrict to one campaign's ad sets."},
        }),
    },
    "list_ads": {
        "description": "Individual ads with creative ids, thumbnails, approval status, preview links.",
        "input": _account_input({
            "limit": {"type": "integer", "description": "Max ads to return.", "default": 100},
            "ad_set_id": {"type": "string", "description": "Restrict to one ad set's ads."},
        }),
    },
    "list_creatives": {
        "description": "Ad creatives — images, videos, titles, bodies, CTAs.",
        "input": _account_input({"limit": {"type": "integer", "description": "Max creatives to return.", "default": 100}}),
    },
    "list_audiences": {
        "description": "Custom and lookalike audiences with size estimates and delivery status.",
        "input": _account_input({"limit": {"type": "integer", "description": "Max audiences to return.", "default": 100}}),
    },
    "saved_audiences": {
        "description": "Saved targeting templates with their run status and summary.",
        "input": _account_input(),
    },
    "list_pixels": {
        "description": "Meta Pixel installations with last-fired time (needs pixel scope; degrades gracefully).",
        "input": _account_input(),
    },
    "list_custom_conversions": {
        "description": "Custom conversion events configured on the account (event type, pixel, rule).",
        "input": _account_input(),
    },
    # ---------- Performance ----------
    "campaign_insights": {
        "description": "Spend, impressions, reach, clicks, CTR, conversions per campaign (sorted by spend).",
        "input": _insights_input(),
    },
    "ad_set_insights": {
        "description": "Ad-set level metrics, sorted by spend.",
        "input": _insights_input(),
    },
    "ad_insights": {
        "description": "Per-ad performance with full ad/ad-set/campaign context, sorted by spend.",
        "input": _insights_input(),
    },
    "ad_creative_performance": {
        "description": "Top ads by spend joined with creative thumbnails, headlines, body, and CTA.",
        "input": _insights_input(),
    },
    "action_breakdown": {
        "description": "Performance pivoted by action_type (leads, conversions, link_clicks, etc.).",
        "input": _insights_input(),
    },
    "conversion_data": {
        "description": "Total conversions, value, cost-per-conversion, and ROAS over a date range.",
        "input": _insights_input(),
    },
    "age_breakdown": {
        "description": "Performance segmented by age bucket.",
        "input": _insights_input(),
    },
    "gender_breakdown": {
        "description": "Performance segmented by gender.",
        "input": _insights_input(),
    },
    "demographic_breakdown": {
        "description": "Spend and conversions by age × gender.",
        "input": _insights_input(),
    },
    "country_breakdown": {
        "description": "Performance by country.",
        "input": _insights_input(),
    },
    "region_breakdown": {
        "description": "Performance by sub-country region (state / province).",
        "input": _insights_input(),
    },
    "device_breakdown": {
        "description": "Performance by impression device (mobile / desktop / app).",
        "input": _insights_input(),
    },
    "placement_breakdown": {
        "description": "Performance by full placement (publisher × position × device).",
        "input": _insights_input(),
    },
    "publisher_platform_breakdown": {
        "description": "Performance split by FB / Instagram / Audience Network / Messenger.",
        "input": _insights_input(),
    },
    "hourly_breakdown": {
        "description": "Performance by hour of day (advertiser time zone).",
        "input": _insights_input(),
    },
    "day_of_week_breakdown": {
        "description": "Performance by day of week (aggregated from the daily breakdown).",
        "input": _insights_input(),
    },
    # ---------- Conversions / leads ----------
    "lead_form_data": {
        "description": "Lead Ads forms across managed Pages with lead counts and question counts.",
        "input": _NO_INPUT,
    },
}

HANDLERS = {
    # Accounts
    "list_ad_accounts": list_ad_accounts,
    "list_business_accounts": list_business_accounts,
    "list_pages": list_pages,
    "list_instagram_accounts": list_instagram_accounts,
    "account_insights": account_insights,
    "account_health_check": account_health_check,
    # Structure
    "list_campaigns": list_campaigns,
    "list_ad_sets": list_ad_sets,
    "list_ads": list_ads,
    "list_creatives": list_creatives,
    "list_audiences": list_audiences,
    "saved_audiences": saved_audiences,
    "list_pixels": list_pixels,
    "list_custom_conversions": list_custom_conversions,
    # Performance
    "campaign_insights": campaign_insights,
    "ad_set_insights": ad_set_insights,
    "ad_insights": ad_insights,
    "ad_creative_performance": ad_creative_performance,
    "action_breakdown": action_breakdown,
    "conversion_data": conversion_data,
    "age_breakdown": age_breakdown,
    "gender_breakdown": gender_breakdown,
    "demographic_breakdown": demographic_breakdown,
    "country_breakdown": country_breakdown,
    "region_breakdown": region_breakdown,
    "device_breakdown": device_breakdown,
    "placement_breakdown": placement_breakdown,
    "publisher_platform_breakdown": publisher_platform_breakdown,
    "hourly_breakdown": hourly_breakdown,
    "day_of_week_breakdown": day_of_week_breakdown,
    # Conversions / leads
    "lead_form_data": lead_form_data,
}

registry.register(Connector(
    slug="meta_ads",
    label="Meta Ads",
    auth="api_key",
    # Only the access token is required — the ad account id is auto-discovered from
    # the token on first use (see _default_act_id), so users never paste it.
    cred_fields=["access_token"],
    catalog=CATALOG,
    handlers=HANDLERS,
    description='Reads Meta ad accounts, campaigns, ad sets, ads, creatives, audiences and insights from the Facebook Marketing API.',
    category='Advertising',
))
