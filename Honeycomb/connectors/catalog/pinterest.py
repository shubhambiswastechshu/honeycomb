"""Pinterest connector (Pinterest REST API v5, api_key).

Reads a Pinterest business account on both sides of the platform:

  Organic   the profile and its analytics, top Pins and top video Pins,
            boards, Pins, and analytics for a single Pin
  Ads       ad accounts, campaigns, ad groups, ads and audiences, with
            analytics at account, campaign, ad group and ad level plus a
            targeting breakdown (age, gender, country, keyword, ...)

Auth is a Pinterest access token sent as a bearer token, generated in the
Pinterest developer portal with the read scopes ``user_accounts:read``,
``boards:read``, ``pins:read`` and ``ads:read``. Nothing here writes: no catalog
entry carries ``write``, so the registry derives an empty ``write_tools``.
Pinterest tokens expire, so a 401 says to paste a fresh one rather than
reporting a bare status code.

Three things shape this file.

**Analytics reach back 90 days, no further.** Every synchronous analytics
endpoint Pinterest serves -- organic and ads alike -- refuses a start date more
than 90 days ago. ``_window`` applies the same rule before the call, so a
caller gets one sentence naming the earliest date instead of Pinterest's 400.

**Money arrives in micro-units.** Reporting columns ending
``_IN_MICRO_DOLLAR`` and budgets ending ``_in_micro_currency`` are millionths of
the ad account's currency -- the "dollar" in Pinterest's column names is
historical, not a currency. Each column comes back as sent *and* divided by a
million under its name without the suffix (``SPEND_IN_MICRO_DOLLAR`` ->
``SPEND``), and every ads response names the account's ``currency`` so the
figure is never read as USD by default.

**Rollups say what they counted.** Campaign, ad group and ad analytics fetched
without explicit ids cover the entities on one page of the listing -- the 250
most recent -- and return ``complete`` rather than presenting a partial set as
the whole account.

Ids are pinned to digits before they reach a URL path. The host is fixed, so
this is not the SSRF exposure Shopify's shop domain was, but an id of
``../ad_accounts`` would otherwise walk the path to an endpoint the tool never
meant to call.
"""
import asyncio
import datetime
import re

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, cached
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get

SLUG = "pinterest"
BASE = "https://api.pinterest.com/v5"

#: Pinterest's synchronous analytics refuse a start date further back than this.
MAX_DAYS = 90
#: Page size ceiling on every Pinterest list endpoint.
MAX_PAGE = 250
#: Ids per analytics request. Pinterest takes 250, but ids travel as repeated
#: query parameters, and 250 of them push the URL toward the 8 KB many proxies
#: refuse.
ID_BATCH = 100
#: Most rows an analytics tool returns. A DAY report over 250 campaigns is
#: thousands of rows -- more than any reader of the answer wants.
MAX_ROWS = 500

_ID = re.compile(r"\d{1,32}")
_COLUMN = re.compile(r"[A-Z0-9_]{1,80}")
_MICRO = "_IN_MICRO_DOLLAR"

# --- Pinterest's own enum values, as the v5 spec lists them -----------------
ORGANIC_METRICS = (
    "ENGAGEMENT", "ENGAGEMENT_RATE", "IMPRESSION", "OUTBOUND_CLICK",
    "OUTBOUND_CLICK_RATE", "PIN_CLICK", "PIN_CLICK_RATE", "SAVE", "SAVE_RATE",
)
TOP_PIN_SORTS = ("IMPRESSION", "ENGAGEMENT", "SAVE", "OUTBOUND_CLICK", "PIN_CLICK")
VIDEO_METRICS = (
    "IMPRESSION", "SAVE", "OUTBOUND_CLICK", "VIDEO_MRC_VIEW", "VIDEO_AVG_WATCH_TIME",
    "VIDEO_V50_WATCH_TIME", "QUARTILE_95_PERCENT_VIEW", "VIDEO_10S_VIEW", "VIDEO_START",
)
PIN_METRICS = (
    "IMPRESSION", "OUTBOUND_CLICK", "PIN_CLICK", "SAVE", "SAVE_RATE", "TOTAL_COMMENTS",
    "TOTAL_REACTIONS", "USER_FOLLOW", "PROFILE_VISIT", "VIDEO_MRC_VIEW", "VIDEO_10S_VIEW",
    "QUARTILE_95_PERCENT_VIEW", "VIDEO_V50_WATCH_TIME", "VIDEO_START", "VIDEO_AVG_WATCH_TIME",
)
DEFAULT_PIN_METRICS = ("IMPRESSION", "PIN_CLICK", "OUTBOUND_CLICK", "SAVE", "SAVE_RATE")
ACCOUNT_SPLITS = ("NO_SPLIT", "APP_TYPE", "OWNED_CONTENT", "SOURCE", "PIN_FORMAT")
PIN_SPLITS = ("NO_SPLIT", "APP_TYPE")

#: The filters every organic analytics endpoint shares.
ORGANIC_FILTERS = {
    "from_claimed_content": ("BOTH", "CLAIMED", "OTHER"),
    "pin_format": (
        "ALL", "ORGANIC_IMAGE", "ORGANIC_PRODUCT", "ORGANIC_VIDEO",
        "ADS_STANDARD", "ADS_PRODUCT", "ADS_VIDEO", "ADS_IDEA",
    ),
    "app_types": ("ALL", "MOBILE", "TABLET", "WEB"),
    "content_type": ("ALL", "PAID", "ORGANIC"),
    "source": ("ALL", "YOUR_PINS", "OTHER_PINS"),
}

BOARD_PRIVACY = ("ALL", "PUBLIC", "PROTECTED", "SECRET", "PUBLIC_AND_SECRET")
PIN_FILTERS = ("exclude_native", "exclude_repins", "has_been_promoted")
STATUSES = ("ACTIVE", "PAUSED", "ARCHIVED", "DRAFT", "DELETED_DRAFT")
#: What a rollup lists when no ids are given: everything that can have spent.
DELIVERED = ("ACTIVE", "PAUSED", "ARCHIVED")
#: HOUR is left out on purpose: it reaches back 8 days with a 3-day window, a
#: different rule from every other report here.
GRANULARITIES = ("TOTAL", "DAY", "WEEK", "MONTH")
TARGETING_TYPES = (
    "AGE_BUCKET", "GENDER", "COUNTRY", "REGION", "GEO", "LOCATION", "APPTYPE",
    "PLACEMENT", "KEYWORD", "TARGETED_INTEREST", "PINNER_INTEREST",
    "AUDIENCE_INCLUDE", "AGE_BUCKET_AND_GENDER",
)

DEFAULT_COLUMNS = (
    "SPEND_IN_MICRO_DOLLAR", "PAID_IMPRESSION", "CLICKTHROUGH_1", "OUTBOUND_CLICK_1",
    "CTR", "CPC_IN_MICRO_DOLLAR", "CPM_IN_MICRO_DOLLAR", "TOTAL_ENGAGEMENT",
    "TOTAL_CONVERSIONS", "TOTAL_CHECKOUT", "TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR",
    "CHECKOUT_ROAS",
)
TARGETING_COLUMNS = (
    "SPEND_IN_MICRO_DOLLAR", "PAID_IMPRESSION", "CLICKTHROUGH_1", "OUTBOUND_CLICK_1",
    "CTR", "TOTAL_CONVERSIONS",
)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _headers(conn: Connection) -> dict:
    token = str((conn.creds() or {}).get("access_token") or "").strip()
    if not token:
        raise ConnectorError("Not connected: missing access_token.")
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


def _fail(res) -> None:
    try:
        body = res.json()
    except ValueError:
        body = None
    message = body.get("message") if isinstance(body, dict) else None
    message = str(message or res.text[:300])[:300]
    if res.status_code == 401:
        raise ConnectorError(
            "Pinterest rejected the access token (401) -- it has expired or been "
            "revoked. Paste a fresh token into this connection in the Honeycomb dashboard."
        )
    if res.status_code == 403:
        raise ConnectorError(
            "Pinterest refused this call (403) -- the token is probably missing a "
            "read scope (user_accounts:read, boards:read, pins:read, ads:read) or "
            f"has no role on that ad account. {message}"
        )
    if res.status_code == 429:
        raise ConnectorError("Pinterest rate limit hit (429). Try again shortly.")
    raise ConnectorError(f"Pinterest {res.status_code}: {message}")


async def _get(conn: Connection, path: str, params: dict | None = None):
    """GET one Pinterest path. Lists in `params` go out as repeated keys."""
    url = BASE + path
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    async with limit_for(BASE):
        try:
            res = await http_get(url, headers=_headers(conn), params=clean)
        except UpstreamUnavailable as e:
            raise ConnectorError(str(e))
    if res.status_code != 200:
        _fail(res)
    try:
        return res.json()
    except ValueError:
        raise ConnectorError("Pinterest answered with something that is not JSON.")


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #
def _text(args: dict, key: str) -> str:
    return str((args or {}).get(key) or "").strip()


def _id(args: dict, key: str, example: str, required: bool = True) -> str:
    """One numeric Pinterest id. Digits only, because it lands in the URL path."""
    value = _text(args, key)
    if not value:
        if required:
            raise ConnectorError(f"{key} is required (a numeric id, e.g. '{example}').")
        return ""
    if not _ID.fullmatch(value):
        raise ConnectorError(f"{key} must be a numeric Pinterest id, e.g. '{example}'.")
    return value


def _id_list(args: dict, key: str) -> list[str]:
    """Numeric ids from a JSON list or a comma-separated string, deduplicated."""
    raw = (args or {}).get(key)
    if raw in (None, "", []):
        return []
    items = raw if isinstance(raw, list) else str(raw).split(",")
    out: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value:
            continue
        if not _ID.fullmatch(value):
            raise ConnectorError(f"{key} must hold numeric Pinterest ids.")
        if value not in out:
            out.append(value)
    if len(out) > MAX_PAGE:
        raise ConnectorError(f"{key} takes at most {MAX_PAGE} ids.")
    return out


def _choice(args: dict, key: str, allowed: tuple, default: str | None = None) -> str | None:
    value = _text(args, key).upper()
    if not value:
        return default
    if value not in allowed:
        raise ConnectorError(f"{key} must be one of {', '.join(allowed)}.")
    return value


def _choices(args: dict, key: str, allowed: tuple | None, default: tuple = ()) -> list[str]:
    """Upper-case codes from a JSON list or a comma-separated string.

    ``allowed=None`` accepts any well-formed code -- used for reporting columns,
    where Pinterest's list runs to 185 names and its own 400 names a bad one.
    """
    raw = (args or {}).get(key)
    if raw in (None, "", []):
        return list(default)
    items = raw if isinstance(raw, list) else str(raw).split(",")
    out: list[str] = []
    for item in items:
        value = str(item).strip().upper()
        if not value:
            continue
        if allowed is not None and value not in allowed:
            raise ConnectorError(f"Unknown {key} value '{value[:40]}'. Allowed: {', '.join(allowed)}.")
        if allowed is None and not _COLUMN.fullmatch(value):
            raise ConnectorError(f"'{value[:40]}' is not a Pinterest column name.")
        if value not in out:
            out.append(value)
    return out or list(default)


def _flag(args: dict, key: str, default: bool = False) -> bool:
    value = (args or {}).get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _count(args: dict, key: str, default: int, ceiling: int) -> int:
    try:
        return max(1, min(int((args or {}).get(key) or default), ceiling))
    except (TypeError, ValueError):
        return default


def _today() -> datetime.date:
    # Pinterest dates every analytics window in UTC.
    return datetime.datetime.now(datetime.timezone.utc).date()


def _day(args: dict, key: str) -> datetime.date | None:
    value = _text(args, key)
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ConnectorError(f"{key} must be YYYY-MM-DD (got '{value[:40]}').")


def _window(args: dict, default_days: int = 30) -> tuple[str, str]:
    """(start, end) as YYYY-MM-DD, inside the 90 days Pinterest reports on.

    end defaults to today and is clamped to it: the future has no data, and
    Pinterest refuses to be asked for it. start defaults to ``days`` back from
    end, counting both ends.
    """
    today = _today()
    end = min(_day(args, "end_date") or today, today)
    start = _day(args, "start_date")
    if start is None:
        days = _count(args, "days", default_days, MAX_DAYS)
        start = end - datetime.timedelta(days=days - 1)
    if start > end:
        raise ConnectorError("start_date must be on or before end_date.")
    earliest = today - datetime.timedelta(days=MAX_DAYS)
    if start < earliest:
        raise ConnectorError(
            f"Pinterest only reports the last {MAX_DAYS} days, so the window can "
            f"start on {earliest.isoformat()} at the earliest."
        )
    return start.isoformat(), end.isoformat()


def _business_access(args: dict) -> dict:
    """Organic tools read the token's own profile, or -- with Business Access --
    the profile that owns the ad account named here."""
    aid = _id(args, "ad_account_id", "549755885175", required=False)
    return {"ad_account_id": aid} if aid else {}


def _organic_filters(args: dict) -> dict:
    params = {}
    for key, allowed in ORGANIC_FILTERS.items():
        value = _choice(args, key, allowed)
        if value:
            params[key] = value
    return params


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --------------------------------------------------------------------------- #
# Shaping
# --------------------------------------------------------------------------- #
def _micro(value):
    """Micro-units -> the currency's standard unit. None when absent."""
    if value is None or value == "":
        return None
    try:
        return round(float(value) / 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def _money(row: dict) -> dict:
    """Add a standard-unit twin beside every *_IN_MICRO_DOLLAR column."""
    out = dict(row)
    for key, value in row.items():
        if key.endswith(_MICRO):
            out.setdefault(key[: -len(_MICRO)], _micro(value))
    return out


def _spend(row: dict) -> float:
    # SPEND is _money's twin of the default column; SPEND_IN_DOLLAR covers a
    # caller who asked for Pinterest's standard-unit column instead.
    value = row.get("SPEND")
    if value is None:
        value = row.get("SPEND_IN_DOLLAR")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_order(row: dict):
    """Newest period first, then biggest spender -- so a cap keeps what matters."""
    return (str(row.get("DATE") or ""), _spend(row))


def _when(ts) -> str | None:
    """Pinterest's ads timestamps are Unix seconds; ISO 8601 UTC reads better."""
    try:
        seconds = int(ts)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _clip(value, size: int = 300):
    text = str(value or "").strip()
    return text[:size] if text else None


def _image_url(media: dict) -> str | None:
    images = media.get("images") if isinstance(media.get("images"), dict) else {}
    for size in ("600x", "400x300", "1200x", "150x150"):
        found = images.get(size)
        if isinstance(found, dict) and found.get("url"):
            return found["url"]
    return media.get("cover_image_url")


def _pin(p: dict) -> dict:
    pid = p.get("id")
    media = p.get("media") if isinstance(p.get("media"), dict) else {}
    row = {
        "id": pid,
        "url": f"https://www.pinterest.com/pin/{pid}/" if pid else None,
        "title": p.get("title") or None,
        "description": _clip(p.get("description")),
        "link": p.get("link"),
        "board_id": p.get("board_id"),
        "created_at": p.get("created_at"),
        "creative_type": p.get("creative_type"),
        "media_type": media.get("media_type"),
        "image_url": _image_url(media),
        "has_been_promoted": p.get("has_been_promoted"),
    }
    if p.get("pin_metrics"):
        row["pin_metrics"] = p["pin_metrics"]
    return row


def _board(b: dict) -> dict:
    return {
        "id": b.get("id"),
        "name": b.get("name"),
        "description": _clip(b.get("description")),
        "privacy": b.get("privacy"),
        "pin_count": b.get("pin_count"),
        "follower_count": b.get("follower_count"),
        "collaborator_count": b.get("collaborator_count"),
        "created_at": b.get("created_at"),
        "pins_modified_at": b.get("board_pins_modified_at"),
        "owner": (b.get("owner") or {}).get("username"),
    }


def _ad_account(a: dict) -> dict:
    return {
        "id": str(a.get("id") or ""),
        "name": a.get("name"),
        "currency": a.get("currency"),
        "country": a.get("country"),
        "time_zone": a.get("time_zone"),
        "owner": (a.get("owner") or {}).get("username"),
        "permissions": a.get("permissions") or [],
        "created_time": _when(a.get("created_time")),
    }


def _campaign(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "name": c.get("name"),
        "status": c.get("status"),
        "summary_status": c.get("summary_status"),
        "objective_type": c.get("objective_type"),
        "daily_spend_cap": _micro(c.get("daily_spend_cap")),
        "lifetime_spend_cap": _micro(c.get("lifetime_spend_cap")),
        "budget_optimization": c.get("is_campaign_budget_optimization"),
        "performance_plus": c.get("is_performance_plus"),
        "start_time": _when(c.get("start_time")),
        "end_time": _when(c.get("end_time")),
        "created_time": _when(c.get("created_time")),
    }


def _ad_group(g: dict) -> dict:
    return {
        "id": g.get("id"),
        "name": g.get("name"),
        "campaign_id": g.get("campaign_id"),
        "status": g.get("status"),
        "summary_status": g.get("summary_status"),
        "budget": _micro(g.get("budget_in_micro_currency")),
        "budget_type": g.get("budget_type"),
        "bid": _micro(g.get("bid_in_micro_currency")),
        "bid_strategy_type": g.get("bid_strategy_type"),
        "billable_event": g.get("billable_event"),
        "placement_group": g.get("placement_group"),
        "start_time": _when(g.get("start_time")),
        "end_time": _when(g.get("end_time")),
        "created_time": _when(g.get("created_time")),
    }


def _ad(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "campaign_id": a.get("campaign_id"),
        "ad_group_id": a.get("ad_group_id"),
        "pin_id": a.get("pin_id"),
        "status": a.get("status"),
        "summary_status": a.get("summary_status"),
        "review_status": a.get("review_status"),
        "rejected_reasons": a.get("rejected_reasons") or None,
        "creative_type": a.get("creative_type"),
        "destination_url": a.get("destination_url"),
        "created_time": _when(a.get("created_time")),
    }


def _audience(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "audience_type": a.get("audience_type"),
        "status": a.get("status"),
        "size": a.get("size"),
        "description": _clip(a.get("description")),
        "created_time": _when(a.get("created_timestamp")),
    }


def _paged(body, key: str, shape) -> dict:
    body = body if isinstance(body, dict) else {}
    rows = [shape(item) for item in body.get("items") or [] if isinstance(item, dict)]
    return {"row_count": len(rows), "next_bookmark": body.get("bookmark") or "", key: rows}


def _groups(body) -> list[dict]:
    """Organic analytics, one entry per split value ('all' when unsplit).

    Pinterest nests each day's figures in a ``metrics`` bag; they come up beside
    the date so a day reads as one flat row.
    """
    out = []
    for group, data in (body.items() if isinstance(body, dict) else []):
        if not isinstance(data, dict):
            continue
        entry = {"group": group, "summary": data.get("summary_metrics") or {}}
        if data.get("lifetime_metrics"):
            entry["lifetime"] = data["lifetime_metrics"]
        entry["daily"] = [
            {"date": d.get("date"), "data_status": d.get("data_status"), **(d.get("metrics") or {})}
            for d in data.get("daily_metrics") or []
            if isinstance(d, dict)
        ]
        out.append(entry)
    return out


# =========================================================================== #
# Organic: profile, analytics, boards, Pins
# =========================================================================== #
async def account_info(conn: Connection, db, args: dict) -> dict:
    """The profile this token reads: name, type, followers, monthly views."""
    params = _business_access(args)

    async def _load():
        a = await _get(conn, "/user_account", params)
        return {
            "id": a.get("id"),
            "username": a.get("username"),
            "business_name": a.get("business_name"),
            "account_type": a.get("account_type"),
            "about": _clip(a.get("about"), 500),
            "website_url": a.get("website_url"),
            "profile_image": a.get("profile_image"),
            "follower_count": a.get("follower_count"),
            "following_count": a.get("following_count"),
            "board_count": a.get("board_count"),
            "pin_count": a.get("pin_count"),
            "monthly_views": a.get("monthly_views"),
        }

    return await cached(SLUG, conn.id, "account_info", TTL_MEDIUM, _load, args=params)


async def account_analytics(conn: Connection, db, args: dict) -> dict:
    """Account-wide impressions, saves, clicks and engagement, summary and daily."""
    start, end = _window(args)
    params = {"start_date": start, "end_date": end, **_organic_filters(args), **_business_access(args)}
    metrics = _choices(args, "metric_types", ORGANIC_METRICS)
    if metrics:
        params["metric_types"] = ",".join(metrics)
    split = _choice(args, "split_field", ACCOUNT_SPLITS)
    if split:
        params["split_field"] = split

    async def _load():
        body = await _get(conn, "/user_account/analytics", params)
        return {
            "start_date": start,
            "end_date": end,
            "split_field": split or "NO_SPLIT",
            "groups": _groups(body),
        }

    return await cached(SLUG, conn.id, "account_analytics", TTL_MEDIUM, _load, args=params)


async def _pin_briefs(conn: Connection, ids: list, business: dict) -> dict:
    """Title, link and URL for each Pin id: one GET each, in parallel.

    The per-host limiter bounds the fan-out. A Pin that cannot be read --
    deleted, secret, someone else's -- is left without details rather than
    failing the list it belongs to.
    """
    async def one(pid: str):
        if not _ID.fullmatch(pid):
            return pid, None
        try:
            p = await _get(conn, f"/pins/{pid}", business)
        except ConnectorError:
            return pid, None
        row = _pin(p if isinstance(p, dict) else {})
        return pid, {k: row[k] for k in ("url", "title", "link", "creative_type", "created_at")}

    pairs = await asyncio.gather(*(one(str(pid)) for pid in ids))
    return {pid: brief for pid, brief in pairs if brief}


async def _top(conn: Connection, args: dict, path: str, sorts: tuple,
               metrics_allowed: tuple, tool: str) -> dict:
    start, end = _window(args)
    sort_by = _choice(args, "sort_by", sorts, "IMPRESSION")
    count = _count(args, "num_of_pins", 10, 50)
    business = _business_access(args)
    params = {"start_date": start, "end_date": end, "sort_by": sort_by, "num_of_pins": count,
              **_organic_filters(args), **business}
    metrics = _choices(args, "metric_types", metrics_allowed)
    if metrics:
        params["metric_types"] = ",".join(metrics)
    details = _flag(args, "with_details", True)

    async def _load():
        body = await _get(conn, path, params)
        body = body if isinstance(body, dict) else {}
        found = [p for p in body.get("pins") or [] if isinstance(p, dict)]
        ids = [str(p.get("pin_id")) for p in found if p.get("pin_id")]
        briefs = await _pin_briefs(conn, ids, business) if details and ids else {}
        rows = [
            {"pin_id": p.get("pin_id"), **(briefs.get(str(p.get("pin_id"))) or {}), **(p.get("metrics") or {})}
            for p in found
        ]
        out = {
            "start_date": start,
            "end_date": end,
            "sort_by": sort_by,
            "date_availability": body.get("date_availability"),
            "row_count": len(rows),
            "pins": rows,
        }
        if details and len(briefs) < len(ids):
            out["details_unavailable"] = len(ids) - len(briefs)
        return out

    return await cached(SLUG, conn.id, tool, TTL_MEDIUM, _load, args={**params, "details": details})


async def top_pins(conn: Connection, db, args: dict) -> dict:
    return await _top(conn, args, "/user_account/analytics/top_pins",
                      TOP_PIN_SORTS, ORGANIC_METRICS, "top_pins")


async def top_video_pins(conn: Connection, db, args: dict) -> dict:
    return await _top(conn, args, "/user_account/analytics/top_video_pins",
                      VIDEO_METRICS, VIDEO_METRICS, "top_video_pins")


async def list_boards(conn: Connection, db, args: dict) -> dict:
    params = {
        "page_size": _count(args, "limit", 25, MAX_PAGE),
        "privacy": _choice(args, "privacy", BOARD_PRIVACY),
        "bookmark": _text(args, "bookmark"),
        **_business_access(args),
    }

    async def _load():
        return _paged(await _get(conn, "/boards", params), "boards", _board)

    return await cached(SLUG, conn.id, "list_boards", TTL_MEDIUM, _load, args=params)


async def list_board_pins(conn: Connection, db, args: dict) -> dict:
    board_id = _id(args, "board_id", "549755885175")
    params = {
        "page_size": _count(args, "limit", 25, MAX_PAGE),
        "pin_metrics": "true" if _flag(args, "pin_metrics") else None,
        "bookmark": _text(args, "bookmark"),
        **_business_access(args),
    }

    async def _load():
        out = _paged(await _get(conn, f"/boards/{board_id}/pins", params), "pins", _pin)
        return {"board_id": board_id, **out}

    return await cached(SLUG, conn.id, "list_board_pins", TTL_MEDIUM, _load,
                        args={"b": board_id, **params})


async def list_pins(conn: Connection, db, args: dict) -> dict:
    pin_filter = _text(args, "pin_filter").lower()
    if pin_filter and pin_filter not in PIN_FILTERS:
        raise ConnectorError(f"pin_filter must be one of {', '.join(PIN_FILTERS)}.")
    params = {
        "page_size": _count(args, "limit", 25, MAX_PAGE),
        "pin_filter": pin_filter,
        "pin_metrics": "true" if _flag(args, "pin_metrics") else None,
        "include_protected_pins": "true" if _flag(args, "include_protected_pins") else None,
        "bookmark": _text(args, "bookmark"),
        **_business_access(args),
    }

    async def _load():
        return _paged(await _get(conn, "/pins", params), "pins", _pin)

    return await cached(SLUG, conn.id, "list_pins", TTL_MEDIUM, _load, args=params)


async def get_pin(conn: Connection, db, args: dict) -> dict:
    pin_id = _id(args, "pin_id", "813744226420795884")
    params = {
        "pin_metrics": "true" if _flag(args, "pin_metrics", True) else None,
        **_business_access(args),
    }

    async def _load():
        p = await _get(conn, f"/pins/{pin_id}", params)
        return _pin(p if isinstance(p, dict) else {})

    return await cached(SLUG, conn.id, "get_pin", TTL_MEDIUM, _load, args={"p": pin_id, **params})


async def pin_analytics(conn: Connection, db, args: dict) -> dict:
    pin_id = _id(args, "pin_id", "813744226420795884")
    start, end = _window(args)
    metrics = _choices(args, "metric_types", PIN_METRICS, DEFAULT_PIN_METRICS)
    params = {
        "start_date": start,
        "end_date": end,
        "metric_types": ",".join(metrics),
        "app_types": _choice(args, "app_types", ORGANIC_FILTERS["app_types"]),
        "split_field": _choice(args, "split_field", PIN_SPLITS),
        **_business_access(args),
    }

    async def _load():
        body = await _get(conn, f"/pins/{pin_id}/analytics", params)
        return {
            "pin_id": pin_id,
            "start_date": start,
            "end_date": end,
            "metric_types": metrics,
            "groups": _groups(body),
        }

    return await cached(SLUG, conn.id, "pin_analytics", TTL_MEDIUM, _load,
                        args={"p": pin_id, **params})


# =========================================================================== #
# Ads: accounts and structure
# =========================================================================== #
async def _ad_accounts(conn: Connection) -> dict:
    """Every ad account the token can see, shared ones included (first 250)."""
    async def _load():
        body = await _get(conn, "/ad_accounts",
                          {"page_size": MAX_PAGE, "include_shared_accounts": "true"})
        out = _paged(body, "ad_accounts", _ad_account)
        return {
            "row_count": out["row_count"],
            "complete": not out["next_bookmark"],
            "ad_accounts": out["ad_accounts"],
        }

    return await cached(SLUG, conn.id, "ad_accounts", TTL_LONG, _load)


async def _account(conn: Connection, args: dict) -> dict:
    """The ad account a call is about: the one named, or the only one there is.

    With several accounts and none named, this refuses and lists them rather
    than picking one -- a report on the wrong account reads exactly like a
    report on the right one.
    """
    aid = _id(args, "ad_account_id", "549755885175", required=False)
    accounts = (await _ad_accounts(conn))["ad_accounts"]
    if aid:
        for a in accounts:
            if a["id"] == aid:
                return a

        async def _load():
            a = await _get(conn, f"/ad_accounts/{aid}")
            return _ad_account(a if isinstance(a, dict) else {})

        return await cached(SLUG, conn.id, "ad_account", TTL_LONG, _load, args={"a": aid})
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        raise ConnectorError(
            "This token can see no Pinterest ad accounts. The ads tools need the "
            "ads:read scope and a role on at least one ad account."
        )
    listed = "; ".join(f"{a['id']} ({a.get('name') or 'unnamed'})" for a in accounts[:10])
    more = f" and {len(accounts) - 10} more" if len(accounts) > 10 else ""
    raise ConnectorError(
        f"This token can see {len(accounts)} ad accounts, so pass ad_account_id "
        f"to pick one: {listed}{more}."
    )


async def list_ad_accounts(conn: Connection, db, args: dict) -> dict:
    return await _ad_accounts(conn)


async def _entities(conn: Connection, aid: str, kind: str, params: dict) -> tuple[list, str]:
    body = await _get(conn, f"/ad_accounts/{aid}/{kind}", params)
    body = body if isinstance(body, dict) else {}
    items = [i for i in body.get("items") or [] if isinstance(i, dict)]
    return items, body.get("bookmark") or ""


async def _listing(conn: Connection, args: dict, kind: str, filters: tuple, shape) -> dict:
    """One page of campaigns / ad groups / ads, newest first."""
    acct = await _account(conn, args)
    params = {
        "page_size": _count(args, "limit", 25, MAX_PAGE),
        "order": "DESCENDING",
        "entity_statuses": _choices(args, "entity_statuses", STATUSES),
        "bookmark": _text(args, "bookmark"),
    }
    for key in filters:
        params[key] = _id_list(args, key)

    async def _load():
        items, nxt = await _entities(conn, acct["id"], kind, params)
        rows = [shape(i) for i in items]
        return {
            "ad_account_id": acct["id"],
            "currency": acct["currency"],
            "row_count": len(rows),
            "next_bookmark": nxt,
            kind: rows,
        }

    return await cached(SLUG, conn.id, "list_" + kind, TTL_MEDIUM, _load,
                        args={"a": acct["id"], **params})


async def list_campaigns(conn: Connection, db, args: dict) -> dict:
    return await _listing(conn, args, "campaigns", (), _campaign)


async def list_ad_groups(conn: Connection, db, args: dict) -> dict:
    return await _listing(conn, args, "ad_groups", ("campaign_ids",), _ad_group)


async def list_ads(conn: Connection, db, args: dict) -> dict:
    return await _listing(conn, args, "ads", ("campaign_ids", "ad_group_ids"), _ad)


async def list_audiences(conn: Connection, db, args: dict) -> dict:
    acct = await _account(conn, args)
    params = {
        "page_size": _count(args, "limit", 25, MAX_PAGE),
        "order": "DESCENDING",
        "ownership_type": _choice(args, "ownership_type", ("OWNED", "RECEIVED")),
        "bookmark": _text(args, "bookmark"),
    }

    async def _load():
        items, nxt = await _entities(conn, acct["id"], "audiences", params)
        rows = [_audience(i) for i in items]
        return {"ad_account_id": acct["id"], "row_count": len(rows), "next_bookmark": nxt,
                "audiences": rows}

    return await cached(SLUG, conn.id, "list_audiences", TTL_MEDIUM, _load,
                        args={"a": acct["id"], **params})


# =========================================================================== #
# Ads: analytics
# =========================================================================== #
def _report_params(args: dict, default_columns: tuple = DEFAULT_COLUMNS) -> dict:
    start, end = _window(args)
    return {
        "start_date": start,
        "end_date": end,
        "granularity": _choice(args, "granularity", GRANULARITIES, "TOTAL"),
        "columns": ",".join(_choices(args, "columns", None, default_columns)),
    }


def _capped(rows: list, args: dict) -> dict:
    limit = _count(args, "limit", 100, MAX_ROWS)
    rows = sorted(rows, key=_row_order, reverse=True)
    return {"row_count": len(rows), "truncated": len(rows) > limit, "rows": rows[:limit]}


async def ad_account_analytics(conn: Connection, db, args: dict) -> dict:
    """Whole-account spend, delivery and conversions for a window."""
    acct = await _account(conn, args)
    params = _report_params(args)
    limit = _count(args, "limit", 100, MAX_ROWS)

    async def _load():
        body = await _get(conn, f"/ad_accounts/{acct['id']}/analytics", params)
        rows = [_money(r) for r in (body if isinstance(body, list) else []) if isinstance(r, dict)]
        return {
            "ad_account_id": acct["id"],
            "currency": acct["currency"],
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "granularity": params["granularity"],
            **_capped(rows, args),
        }

    return await cached(SLUG, conn.id, "ad_account_analytics", TTL_MEDIUM, _load,
                        args={"a": acct["id"], "lim": limit, **params})


#: level -> (listing path, id parameter, id column, name column, listing filters)
LEVELS = {
    "campaign": ("campaigns", "campaign_ids", "CAMPAIGN_ID", "CAMPAIGN_NAME", ()),
    "ad_group": ("ad_groups", "ad_group_ids", "AD_GROUP_ID", "AD_GROUP_NAME", ("campaign_ids",)),
    "ad": ("ads", "ad_ids", "AD_ID", "AD_NAME", ("campaign_ids", "ad_group_ids")),
}


async def _level_analytics(conn: Connection, args: dict, level: str) -> dict:
    """Analytics per campaign, ad group or ad.

    Pinterest reports only on ids it is handed. Without ids from the caller,
    the most recent 250 entities that can have spent (active, paused,
    archived) are listed first and reported on -- and ``complete`` says
    whether that listing held everything.
    """
    kind, id_key, id_col, name_col, filters = LEVELS[level]
    acct = await _account(conn, args)
    aid = acct["id"]
    params = _report_params(args)
    ids = _id_list(args, id_key)
    scope = {key: _id_list(args, key) for key in filters}
    limit = _count(args, "limit", 100, MAX_ROWS)

    async def _load():
        names: dict = {}
        complete = True
        target = ids
        if not target:
            items, nxt = await _entities(conn, aid, kind, {
                "page_size": MAX_PAGE,
                "order": "DESCENDING",
                "entity_statuses": list(DELIVERED),
                **scope,
            })
            names = {str(i.get("id")): i.get("name") for i in items if i.get("id")}
            target = list(names)
            complete = not nxt
        rows: list = []
        for batch in _chunks(target, ID_BATCH):
            body = await _get(conn, f"/ad_accounts/{aid}/{kind}/analytics", {**params, id_key: batch})
            rows.extend(_money(r) for r in (body if isinstance(body, list) else []) if isinstance(r, dict))
        for r in rows:
            name = names.get(str(r.get(id_col)))
            if name:
                r.setdefault(name_col, name)
        out = {
            "ad_account_id": aid,
            "currency": acct["currency"],
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "granularity": params["granularity"],
            f"{kind}_counted": len(target),
            "complete": complete,
            **_capped(rows, args),
        }
        if not complete:
            out["note"] = (
                f"More {kind.replace('_', ' ')} exist than were counted -- this covers "
                f"the {MAX_PAGE} most recent. Pass {id_key} to report on others."
            )
        return out

    return await cached(SLUG, conn.id, f"{level}_analytics", TTL_MEDIUM, _load,
                        args={"a": aid, "ids": ids, "lim": limit, **scope, **params})


async def campaign_analytics(conn: Connection, db, args: dict) -> dict:
    return await _level_analytics(conn, args, "campaign")


async def ad_group_analytics(conn: Connection, db, args: dict) -> dict:
    return await _level_analytics(conn, args, "ad_group")


async def ad_analytics(conn: Connection, db, args: dict) -> dict:
    return await _level_analytics(conn, args, "ad")


async def targeting_analytics(conn: Connection, db, args: dict) -> dict:
    """Account performance broken down by who and where: age, gender, country,
    keyword, interest and the rest. Each breakdown is ranked by spend."""
    acct = await _account(conn, args)
    params = _report_params(args, TARGETING_COLUMNS)
    params["granularity"] = "TOTAL"
    types = _choices(args, "targeting_types", TARGETING_TYPES, ("AGE_BUCKET", "GENDER", "COUNTRY"))
    params["targeting_types"] = ",".join(types)
    per_type = _count(args, "limit", 25, MAX_PAGE)

    async def _load():
        body = await _get(conn, f"/ad_accounts/{acct['id']}/targeting_analytics", params)
        grouped: dict = {}
        for item in (body.get("data") if isinstance(body, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            row = {"value": item.get("targeting_value"), **_money(item.get("metrics") or {})}
            grouped.setdefault(item.get("targeting_type") or "UNKNOWN", []).append(row)
        breakdowns = []
        for ttype, rows in grouped.items():
            rows.sort(key=_spend, reverse=True)
            breakdowns.append({
                "targeting_type": ttype,
                "value_count": len(rows),
                "truncated": len(rows) > per_type,
                "values": rows[:per_type],
            })
        return {
            "ad_account_id": acct["id"],
            "currency": acct["currency"],
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "breakdowns": breakdowns,
        }

    return await cached(SLUG, conn.id, "targeting_analytics", TTL_MEDIUM, _load,
                        args={"a": acct["id"], "lim": per_type, **params})


# =========================================================================== #
# Catalog
# =========================================================================== #
_LIMIT = {"type": "integer", "description": "Rows per page (1-250). Default 25."}
_BOOKMARK = {"type": "string", "description": "next_bookmark from a previous call, to continue."}
_DAYS = {"type": "integer", "description": "Window length in days ending today (UTC), 1-90. Default 30. Ignored when start_date is given."}
_START = {"type": "string", "description": "YYYY-MM-DD. Pinterest reports the last 90 days only."}
_END = {"type": "string", "description": "YYYY-MM-DD. Defaults to today (UTC)."}
_BUSINESS = {
    "type": "string",
    "description": (
        "Business Access: read the profile that owns this ad account instead of "
        "the token's own. Optional."
    ),
}
_AD_ACCOUNT = {
    "type": "string",
    "description": (
        "Ad account id, from list_ad_accounts. Optional when the token can see "
        "exactly one ad account."
    ),
}
_IDS = {"type": "array", "items": {"type": "string"}}
_STATUSES = {
    "type": "array",
    "items": {"type": "string", "enum": list(STATUSES)},
    "description": "Filter by status. Pinterest's default is ACTIVE and PAUSED.",
}
_GRANULARITY = {
    "type": "string",
    "enum": list(GRANULARITIES),
    "description": "TOTAL (default) is one row for the window; DAY, WEEK and MONTH break it down.",
}
_COLUMNS = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Pinterest reporting columns to return instead of the defaults, e.g. "
        "SPEND_IN_MICRO_DOLLAR, PAID_IMPRESSION, CLICKTHROUGH_1, OUTBOUND_CLICK_1, "
        "CTR, TOTAL_CONVERSIONS, TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR, CHECKOUT_ROAS."
    ),
}
_ROWS = {"type": "integer", "description": "Most rows to return (1-500). Default 100."}
_ORGANIC_FILTER_PROPS = {
    key: {"type": "string", "enum": list(values)} for key, values in ORGANIC_FILTERS.items()
}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        **({"required": required} if required else {}),
        "additionalProperties": False,
    }


_WINDOW = {"days": _DAYS, "start_date": _START, "end_date": _END}
_REPORT = {
    "ad_account_id": _AD_ACCOUNT,
    **_WINDOW,
    "granularity": _GRANULARITY,
    "columns": _COLUMNS,
    "limit": _ROWS,
}

CATALOG = {
    # --- organic ----------------------------------------------------------- #
    "account_info": {
        "description": "The Pinterest profile this token reads: business name, account type, followers, monthly views, Pin and board counts.",
        "input": _obj({"ad_account_id": _BUSINESS}),
    },
    "account_analytics": {
        "description": (
            "Account-wide impressions, saves, Pin clicks, outbound clicks and "
            "engagement for a window in the last 90 days -- a summary plus one row per day."
        ),
        "input": _obj({
            **_WINDOW,
            "metric_types": {
                "type": "array",
                "items": {"type": "string", "enum": list(ORGANIC_METRICS)},
                "description": "Metrics to return. Default: all of them.",
            },
            "split_field": {
                "type": "string",
                "enum": list(ACCOUNT_SPLITS),
                "description": "Break the figures down by app type, owned content, source or Pin format.",
            },
            **_ORGANIC_FILTER_PROPS,
            "ad_account_id": _BUSINESS,
        }),
    },
    "top_pins": {
        "description": (
            "The best-performing Pins in a window (up to 50), ranked by one metric, "
            "each with its title and link."
        ),
        "input": _obj({
            **_WINDOW,
            "sort_by": {"type": "string", "enum": list(TOP_PIN_SORTS), "description": "Default IMPRESSION."},
            "num_of_pins": {"type": "integer", "description": "1-50. Default 10."},
            "metric_types": {"type": "array", "items": {"type": "string", "enum": list(ORGANIC_METRICS)}},
            "with_details": {
                "type": "boolean",
                "description": "Look up each Pin's title and link (one extra call per Pin). Default true.",
            },
            **_ORGANIC_FILTER_PROPS,
            "ad_account_id": _BUSINESS,
        }),
    },
    "top_video_pins": {
        "description": "The best-performing video Pins in a window (up to 50), ranked by views, watch time or another video metric.",
        "input": _obj({
            **_WINDOW,
            "sort_by": {"type": "string", "enum": list(VIDEO_METRICS), "description": "Default IMPRESSION."},
            "num_of_pins": {"type": "integer", "description": "1-50. Default 10."},
            "metric_types": {"type": "array", "items": {"type": "string", "enum": list(VIDEO_METRICS)}},
            "with_details": {
                "type": "boolean",
                "description": "Look up each Pin's title and link (one extra call per Pin). Default true.",
            },
            **_ORGANIC_FILTER_PROPS,
            "ad_account_id": _BUSINESS,
        }),
    },
    "list_boards": {
        "description": "Boards with Pin and follower counts and privacy.",
        "input": _obj({
            "privacy": {"type": "string", "enum": list(BOARD_PRIVACY)},
            "limit": _LIMIT,
            "bookmark": _BOOKMARK,
            "ad_account_id": _BUSINESS,
        }),
    },
    "list_board_pins": {
        "description": "The Pins on one board.",
        "input": _obj({
            "board_id": {"type": "string", "description": "From list_boards."},
            "pin_metrics": {"type": "boolean", "description": "Include 90-day and lifetime metrics per Pin."},
            "limit": _LIMIT,
            "bookmark": _BOOKMARK,
            "ad_account_id": _BUSINESS,
        }, ["board_id"]),
    },
    "list_pins": {
        "description": "The account's own Pins, newest first, optionally with 90-day and lifetime metrics.",
        "input": _obj({
            "pin_filter": {"type": "string", "enum": list(PIN_FILTERS)},
            "pin_metrics": {"type": "boolean", "description": "Include 90-day and lifetime metrics per Pin."},
            "include_protected_pins": {"type": "boolean"},
            "limit": _LIMIT,
            "bookmark": _BOOKMARK,
            "ad_account_id": _BUSINESS,
        }),
    },
    "get_pin": {
        "description": "One Pin with its title, link, board and -- by default -- 90-day and lifetime metrics.",
        "input": _obj({
            "pin_id": {"type": "string"},
            "pin_metrics": {"type": "boolean", "description": "Default true."},
            "ad_account_id": _BUSINESS,
        }, ["pin_id"]),
    },
    "pin_analytics": {
        "description": "Daily and summary metrics for one Pin over a window in the last 90 days.",
        "input": _obj({
            "pin_id": {"type": "string"},
            **_WINDOW,
            "metric_types": {
                "type": "array",
                "items": {"type": "string", "enum": list(PIN_METRICS)},
                "description": "Default IMPRESSION, PIN_CLICK, OUTBOUND_CLICK, SAVE, SAVE_RATE. Video metrics apply to video Pins only.",
            },
            "app_types": {"type": "string", "enum": list(ORGANIC_FILTERS["app_types"])},
            "split_field": {"type": "string", "enum": list(PIN_SPLITS)},
            "ad_account_id": _BUSINESS,
        }, ["pin_id"]),
    },
    # --- ads --------------------------------------------------------------- #
    "list_ad_accounts": {
        "description": "Ad accounts the token can see, shared ones included, with currency and the token's permissions.",
        "input": _obj({}),
    },
    "ad_account_analytics": {
        "description": (
            "Whole-account ad spend, impressions, clicks, CTR, CPC, CPM, conversions, "
            "checkout value and ROAS for a window. Money is in the account's currency."
        ),
        "input": _obj(dict(_REPORT)),
    },
    "list_campaigns": {
        "description": "Campaigns, newest first, with status, objective and spend caps.",
        "input": _obj({
            "ad_account_id": _AD_ACCOUNT,
            "entity_statuses": _STATUSES,
            "limit": _LIMIT,
            "bookmark": _BOOKMARK,
        }),
    },
    "campaign_analytics": {
        "description": (
            "Spend, delivery and conversions per campaign, biggest spender first. "
            "Without campaign_ids it covers the 250 most recent campaigns and says whether that was all."
        ),
        "input": _obj({**_REPORT, "campaign_ids": _IDS}),
    },
    "list_ad_groups": {
        "description": "Ad groups, newest first, with budget, bid strategy and placement.",
        "input": _obj({
            "ad_account_id": _AD_ACCOUNT,
            "campaign_ids": _IDS,
            "entity_statuses": _STATUSES,
            "limit": _LIMIT,
            "bookmark": _BOOKMARK,
        }),
    },
    "ad_group_analytics": {
        "description": (
            "Spend, delivery and conversions per ad group, biggest spender first. "
            "Without ad_group_ids it covers the 250 most recent ad groups (optionally within campaign_ids)."
        ),
        "input": _obj({**_REPORT, "ad_group_ids": _IDS, "campaign_ids": _IDS}),
    },
    "list_ads": {
        "description": "Ads, newest first, with their Pin, review status and destination URL.",
        "input": _obj({
            "ad_account_id": _AD_ACCOUNT,
            "campaign_ids": _IDS,
            "ad_group_ids": _IDS,
            "entity_statuses": _STATUSES,
            "limit": _LIMIT,
            "bookmark": _BOOKMARK,
        }),
    },
    "ad_analytics": {
        "description": (
            "Spend, delivery and conversions per ad, biggest spender first. Without "
            "ad_ids it covers the 250 most recent ads (optionally within campaign_ids or ad_group_ids)."
        ),
        "input": _obj({**_REPORT, "ad_ids": _IDS, "campaign_ids": _IDS, "ad_group_ids": _IDS}),
    },
    "targeting_analytics": {
        "description": (
            "Ad performance broken down by age, gender, country, region, keyword, "
            "interest, placement or device, each ranked by spend."
        ),
        "input": _obj({
            "ad_account_id": _AD_ACCOUNT,
            **_WINDOW,
            "targeting_types": {
                "type": "array",
                "items": {"type": "string", "enum": list(TARGETING_TYPES)},
                "description": "Default AGE_BUCKET, GENDER, COUNTRY.",
            },
            "columns": _COLUMNS,
            "limit": {"type": "integer", "description": "Most values per breakdown (1-250). Default 25."},
        }),
    },
    "list_audiences": {
        "description": "Audiences -- customer lists, site visitors, engagement and actalike -- with size and status.",
        "input": _obj({
            "ad_account_id": _AD_ACCOUNT,
            "ownership_type": {"type": "string", "enum": ["OWNED", "RECEIVED"]},
            "limit": _LIMIT,
            "bookmark": _BOOKMARK,
        }),
    },
}

HANDLERS = {
    "account_info": account_info,
    "account_analytics": account_analytics,
    "top_pins": top_pins,
    "top_video_pins": top_video_pins,
    "list_boards": list_boards,
    "list_board_pins": list_board_pins,
    "list_pins": list_pins,
    "get_pin": get_pin,
    "pin_analytics": pin_analytics,
    "list_ad_accounts": list_ad_accounts,
    "ad_account_analytics": ad_account_analytics,
    "list_campaigns": list_campaigns,
    "campaign_analytics": campaign_analytics,
    "list_ad_groups": list_ad_groups,
    "ad_group_analytics": ad_group_analytics,
    "list_ads": list_ads,
    "ad_analytics": ad_analytics,
    "targeting_analytics": targeting_analytics,
    "list_audiences": list_audiences,
}

registry.register(
    Connector(
        slug=SLUG,
        label="Pinterest",
        auth="api_key",
        description=(
            "Reads a Pinterest business account: the profile and its analytics, top "
            "Pins, boards and Pins, and on the ads side ad accounts, campaigns, ad "
            "groups, ads and audiences with spend, clicks, conversions and a "
            "targeting breakdown."
        ),
        category="Social",
        cred_fields=["access_token"],
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
