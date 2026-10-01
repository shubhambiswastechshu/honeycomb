"""OpenAI Ads connector — the ChatGPT Ads Advertiser API (``api.ads.openai.com/v1``).

Covers as much of the API as a key can reach:

  * **Structure**: the ad account, campaigns, ad groups and ads — list, read,
    create, update, activate / pause / archive, and ad previews.
  * **Reporting**: the four scoped Insights endpoints (account, campaign, ad
    group, ad) with segments for product, country, device and platform, plus
    the dedicated conversion-insights report.
  * **Budget guards**: account-wide date-range and daily spending limits.
  * **Audiences**: custom audiences and their asynchronous membership
    operations (add, remove, replace, merge, resume, cancel).
  * **Measurement**: pixels, conversion event settings and the recent-event
    stream used while testing a pixel.
  * **Product feeds**: feeds, upload history, product queries, the Delta API
    for price and stock, and SSH-key SFTP access.
  * **Bulk jobs**, **audit logs**, **file uploads** and **location lookup**.

Auth: ``api_key``. One Advertiser API key per ad account, created in Ads
Manager under Settings -> API Keys and sent as a bearer token. There is no
OAuth and no refresh; a key for platform.openai.com does NOT work here.

Every mutating tool carries ``'write': True``, so each one arrives switched
off on a new connection and has to be enabled deliberately. Creates default to
``paused`` so nothing starts spending the moment it exists.

Money: the API keeps budgets and bids in **micros** (millionths of the account
currency). Tools accept major units (``25`` for USD 25) and convert; responses
keep every ``*_micros`` field as sent and add a major-unit twin beside it.
Insights metrics (spend, CPC, CPM) already arrive in major units.

Deliberately NOT exposed, because each would put a live secret into the AI
client's context and the activity log:

  * ``POST /conversions/api_keys`` returns a server-side Conversions API key.
  * SFTP **password** authentication returns the password. SSH-key access is
    exposed instead, because the private half never leaves the caller.

Create requests carry an ``Idempotency-Key`` (generated unless the caller
passes one), so the shared HTTP client's retry on 5xx/429 cannot create a
resource twice.
"""
import json
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.cache import TTL_LONG, TTL_MEDIUM, TTL_SHORT, cached, invalidate
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError, redact_text
from connectors.shims.http import UpstreamUnavailable, request as http_request

SLUG = "openai_ads"
BASE = "https://api.ads.openai.com/v1"

RECONNECT = (
    "Reconnect it in the Honeycomb dashboard with an Advertiser API key from "
    "Ads Manager -> Settings -> API Keys (platform.openai.com keys do not work)."
)

MICROS = 1_000_000

#: Named periods, resolved in the ad account's own time zone.
DATE_PRESETS = (
    "today",
    "yesterday",
    "last_7_days",
    "last_14_days",
    "last_30_days",
    "last_90_days",
    "this_month",
    "last_month",
    "last_365_days",
)

LEVELS = ("ad_account", "campaign", "ad_group", "ad")
SEGMENTS = ("product", "country", "device", "platform")
DELIVERY_METRICS = ["impressions", "clicks", "spend", "ctr", "cpc", "cpm"]
CONVERSION_METRICS = [
    "conversions",
    "cpa",
    "post_click_cvr",
    "order_created_attributed_sales",
    "order_created_roas",
]
SEGMENT_METADATA = {
    "product": ["product.feed_id", "product.item_id", "product.title", "product.price"],
    "country": ["country.name"],
    "device": ["device.type"],
    "platform": ["platform"],
}

#: Largest insights page, and how many pages ``all_pages`` will follow.
INSIGHTS_PAGE_MAX = 2000
MAX_PAGES = 10

#: Upper bound on an audience file passed inline through a tool call.
MAX_INLINE_FILE_BYTES = 20 * 1024 * 1024


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _key(conn: Connection) -> str:
    key = str((conn.creds() or {}).get("api_key") or "").strip()
    if not key:
        raise ConnectorError(f"This OpenAI Ads connection has no api_key. {RECONNECT}")
    return key


def _error_detail(res) -> tuple[str, str]:
    """Pull ``(message, code)`` out of an error body of any reasonable shape."""
    try:
        body = res.json()
    except ValueError:
        return res.text[:300], ""
    if not isinstance(body, dict):
        return str(body)[:300], ""
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)[:400], str(err.get("code") or err.get("type") or "")
    if isinstance(err, str):
        return err[:400], str(body.get("code") or "")
    message = body.get("message") or body.get("detail") or body
    return str(message)[:400], str(body.get("code") or "")


def _fail(res, method: str) -> None:
    message, code = _error_detail(res)
    message = redact_text(message)
    status = res.status_code
    tag = f" ({code})" if code else ""
    if status == 401:
        raise ConnectorError(f"OpenAI Ads rejected the API key (401). {RECONNECT}")
    if status == 403:
        raise ConnectorError(
            f"OpenAI Ads refused this call (403){tag}. The key's account may lack the "
            f"permission, or the feature is not enabled for it. {message}"
        )
    if status == 404:
        raise ConnectorError(
            f"OpenAI Ads returned 404{tag}: {message}. Either the id does not exist in this "
            "ad account, or the feature is not enabled for it (contact the OpenAI account team)."
        )
    if status == 409:
        raise ConnectorError(
            f"OpenAI Ads reported a conflict (409){tag}: {message}. Re-read the resource "
            "for its current state or revision, then retry."
        )
    if status == 413:
        raise ConnectorError(
            f"The result is too large for OpenAI Ads (413){tag}. Narrow the date range, "
            "entities or event names."
        )
    if status == 429:
        raise ConnectorError("OpenAI Ads rate limit hit (429). Try again shortly.")
    if status == 503 and method != "GET":
        raise ConnectorError(
            f"OpenAI Ads was unavailable (503){tag}. The change may still have been saved: "
            "read the resource back before retrying."
        )
    raise ConnectorError(f"OpenAI Ads {status}{tag}: {message}")


async def _call(
    conn: Connection,
    method: str,
    path: str,
    *,
    params=None,
    body: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    headers = {"Authorization": "Bearer " + _key(conn), "Accept": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    kwargs: dict = {"headers": headers}
    if params:
        kwargs["params"] = params
    if files is not None:
        kwargs["files"] = files
        if data:
            kwargs["data"] = data
    elif body is not None:
        kwargs["json"] = body
    url = BASE + "/" + path.lstrip("/")
    async with limit_for(BASE):
        try:
            res = await http_request(method, url, **kwargs)
        except UpstreamUnavailable as exc:
            raise ConnectorError(str(exc))
    if res.status_code >= 400:
        _fail(res, method)
    if not res.content:
        return {}
    try:
        return res.json()
    except ValueError:
        raise ConnectorError("OpenAI Ads returned a non-JSON response.")


async def _read(conn: Connection, tool: str, path: str, params=None, ttl: int = TTL_SHORT) -> dict:
    """A cached GET. ``params`` may be a dict or a list of pairs."""
    cache_args = {"path": path, "params": params if isinstance(params, dict) else list(params or [])}

    async def _load():
        return _with_major(await _call(conn, "GET", path, params=params))

    return await cached(SLUG, conn.id, tool, ttl, _load, args=cache_args)


async def _fresh(conn: Connection, path: str, params=None) -> dict:
    """An uncached GET, for things polled while they change (jobs, operations)."""
    return _with_major(await _call(conn, "GET", path, params=params))


async def _write(
    conn: Connection,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    idempotency_key: str | None = None,
    files: dict | None = None,
    data: dict | None = None,
) -> dict:
    result = await _call(
        conn, method, path, body=body, files=files, data=data, idempotency_key=idempotency_key
    )
    # Every cached list and report for this connection may now be stale.
    await invalidate(SLUG, conn.id)
    return _with_major(result)


def _idem(args: dict) -> str:
    supplied = str((args or {}).get("idempotency_key") or "").strip()
    return supplied or f"honeycomb-{uuid.uuid4()}"


# --------------------------------------------------------------------------- #
# money
# --------------------------------------------------------------------------- #
def _to_micros(value, name: str) -> int:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ConnectorError(f"{name} must be a number in the account currency (e.g. 25 or 0.5).")
    if amount < 0:
        raise ConnectorError(f"{name} cannot be negative.")
    return int(round(amount * MICROS))


def _with_major(value):
    """Beside every ``x_micros`` number, add ``x`` in major currency units."""
    if isinstance(value, list):
        return [_with_major(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        out[key] = _with_major(item)
        if isinstance(key, str) and key.endswith("_micros") and isinstance(item, (int, float)) \
                and not isinstance(item, bool):
            twin = key[: -len("_micros")]
            if twin not in value:
                out[twin] = round(item / MICROS, 6)
    return out


# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #
def _require(args: dict, key: str, example: str) -> str:
    value = str((args or {}).get(key) or "").strip()
    if not value:
        raise ConnectorError(f"{key} is required (e.g. '{example}').")
    return value


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    raw = (args or {}).get(key)
    if raw is None or raw == "":
        return default
    try:
        return max(low, min(int(raw), high))
    except (TypeError, ValueError):
        raise ConnectorError(f"{key} must be a whole number between {low} and {high}.")


def _choice(args: dict, key: str, options, default=None):
    value = (args or {}).get(key)
    if value is None or value == "":
        return default
    if value not in options:
        raise ConnectorError(f"{key} must be one of: {', '.join(options)} (got '{value}').")
    return value


def _page(args: dict, max_limit: int = 500, default: int = 50) -> dict:
    params = {"limit": _int(args, "limit", default, 1, max_limit)}
    for key in ("after", "before"):
        value = str((args or {}).get(key) or "").strip()
        if value:
            params[key] = value
    order = _choice(args, "order", ("asc", "desc"))
    if order:
        params["order"] = order
    if params.get("after") and params.get("before"):
        raise ConnectorError("Send either after or before, not both.")
    return params


def _pick(args: dict, keys) -> dict:
    """Only the keys the caller actually sent — an explicit null is kept, to clear a field."""
    return {k: args[k] for k in keys if k in (args or {})}


def _str_list(value, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    if not isinstance(value, list):
        raise ConnectorError(f"{name} must be a list of strings.")
    return [str(v).strip() for v in value if str(v).strip()]


# --------------------------------------------------------------------------- #
# time
# --------------------------------------------------------------------------- #
async def _account(conn: Connection) -> dict:
    return await _read(conn, "ad_account", "ad_account", ttl=TTL_LONG)


async def _tz(conn: Connection) -> ZoneInfo:
    try:
        name = (await _account(conn)).get("timezone") or "UTC"
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown zone falls back rather than failing a report
        return ZoneInfo("UTC")


def _parse_date(value, name: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ConnectorError(f"{name} must be a date in YYYY-MM-DD form (got '{value}').")


def _time_range(args: dict, today: date, now_hour: int) -> tuple[str, bool]:
    """``(JSON time-range, whole_days)`` for the request.

    ``whole_days`` is False only for ranges ending inside today, which the API
    refuses for conversion metrics.
    """
    start, end = (args or {}).get("start_date"), (args or {}).get("end_date")
    if start or end:
        if not (start and end):
            raise ConnectorError("Send both start_date and end_date, or neither.")
        since, until = _parse_date(start, "start_date"), _parse_date(end, "end_date")
        if until < since:
            raise ConnectorError("end_date is before start_date.")
        if until > today:
            raise ConnectorError("end_date cannot be in the future.")
        if until == today:
            return _hour_range(since, today, now_hour), False
        return json.dumps({"type": "date_range", "since": since.isoformat(), "until": until.isoformat()}), True

    preset = _choice(args, "date_range", DATE_PRESETS, "last_7_days")
    yesterday = today - timedelta(days=1)
    if preset == "today":
        return _hour_range(today, today, now_hour), False
    if preset == "this_month":
        first = today.replace(day=1)
        if first == today:
            return _hour_range(today, today, now_hour), False
        since, until = first, yesterday
    elif preset == "last_month":
        until = today.replace(day=1) - timedelta(days=1)
        since = until.replace(day=1)
    elif preset == "yesterday":
        since = until = yesterday
    else:
        days = int(preset.split("_")[1])
        since, until = today - timedelta(days=days), yesterday
    return json.dumps({"type": "date_range", "since": since.isoformat(), "until": until.isoformat()}), True


def _hour_range(since: date, today: date, now_hour: int) -> str:
    return json.dumps({
        "type": "hour_range",
        "since": f"{since.isoformat()}T00",
        "until": f"{today.isoformat()}T{now_hour:02d}",
    })


async def _to_unix(conn: Connection, value, name: str) -> int | None:
    """A Unix timestamp, or YYYY-MM-DD read as midnight in the account's zone."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    day = _parse_date(text, name)
    tz = await _tz(conn)
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())


# --------------------------------------------------------------------------- #
# account & spending limits
# --------------------------------------------------------------------------- #
async def get_ad_account(conn: Connection, db, args: dict) -> dict:
    # Its own short-lived entry: the long one behind _tz would show a status
    # changed in Ads Manager for hours.
    return await _read(conn, "get_ad_account", "ad_account")


async def list_spend_limits(conn: Connection, db, args: dict) -> dict:
    return await _read(conn, "spend_limits", "ad_account/spend_limit_windows")


async def update_account_brand(conn: Connection, db, args: dict) -> dict:
    body = _pick(args, ("legal_name", "account_name", "brand_name", "favicon_file_id"))
    if not body:
        raise ConnectorError("Send at least one of legal_name, account_name, brand_name, favicon_file_id.")
    return await _write(conn, "POST", "ad_account/brand", body)


async def pause_account(conn: Connection, db, args: dict) -> dict:
    return await _write(conn, "POST", "ad_account/pause")


async def activate_account(conn: Connection, db, args: dict) -> dict:
    return await _write(conn, "POST", "ad_account/activate")


async def create_spend_limit_window(conn: Connection, db, args: dict) -> dict:
    body = {
        "start_date": _parse_date(_require(args, "start_date", "2026-10-01"), "start_date").isoformat(),
        "end_date": _parse_date(_require(args, "end_date", "2026-10-08"), "end_date").isoformat(),
        "amount_micros": _to_micros(args.get("amount"), "amount"),
    }
    body.update(_pick(args, ("name", "io_id")))
    return await _write(conn, "POST", "ad_account/spend_limit_windows", body)


async def update_spend_limit_window(conn: Connection, db, args: dict) -> dict:
    window_id = _require(args, "window_id", "slw_123")
    body = _pick(args, ("start_date", "end_date", "name", "io_id"))
    if args.get("amount") is not None:
        body["amount_micros"] = _to_micros(args["amount"], "amount")
    if not body:
        raise ConnectorError("Send at least one of start_date, end_date, amount, name, io_id.")
    return await _write(conn, "POST", f"ad_account/spend_limit_windows/{window_id}", body)


async def delete_spend_limit_window(conn: Connection, db, args: dict) -> dict:
    window_id = _require(args, "window_id", "slw_123")
    return await _write(conn, "POST", f"ad_account/spend_limit_windows/{window_id}/delete")


async def _revision(conn: Connection, args: dict) -> int:
    if args.get("expected_revision") is not None:
        return int(args["expected_revision"])
    current = await _fresh(conn, "ad_account/spend_limit_windows")
    return int(current.get("revision") or 0)


async def set_daily_spend_limit(conn: Connection, db, args: dict) -> dict:
    body = {
        "amount_micros": _to_micros(args.get("amount"), "amount"),
        "expected_revision": await _revision(conn, args),
    }
    body.update(_pick(args, ("start_date", "end_date")))
    return await _write(conn, "POST", "ad_account/daily_spend_limit", body)


async def remove_daily_spend_limit(conn: Connection, db, args: dict) -> dict:
    body = {"expected_revision": await _revision(conn, args)}
    return await _write(conn, "POST", "ad_account/daily_spend_limit/delete", body)


# --------------------------------------------------------------------------- #
# campaigns, ad groups, ads
# --------------------------------------------------------------------------- #
def _serving(args: dict) -> dict | None:
    return {"include[]": "serving_issues"} if (args or {}).get("include_serving_issues") else None


async def list_campaigns(conn: Connection, db, args: dict) -> dict:
    return await _read(conn, "list_campaigns", "campaigns", _page(args))


async def get_campaign(conn: Connection, db, args: dict) -> dict:
    campaign_id = _require(args, "campaign_id", "cmpn_101")
    return await _read(conn, "get_campaign", f"campaigns/{campaign_id}", _serving(args))


async def list_ad_groups(conn: Connection, db, args: dict) -> dict:
    params = {"campaign_id": _require(args, "campaign_id", "cmpn_101"), **_page(args)}
    return await _read(conn, "list_ad_groups", "ad_groups", params)


async def get_ad_group(conn: Connection, db, args: dict) -> dict:
    ad_group_id = _require(args, "ad_group_id", "adgrp_301")
    return await _read(conn, "get_ad_group", f"ad_groups/{ad_group_id}", _serving(args))


async def list_ads(conn: Connection, db, args: dict) -> dict:
    params = {"ad_group_id": _require(args, "ad_group_id", "adgrp_301"), **_page(args)}
    return await _read(conn, "list_ads", "ads", params)


async def get_ad(conn: Connection, db, args: dict) -> dict:
    ad_id = _require(args, "ad_id", "ad_501")
    return await _read(conn, "get_ad", f"ads/{ad_id}", _serving(args))


async def preview_ad(conn: Connection, db, args: dict) -> dict:
    """A preview link that expires after 24 hours. Changes nothing in the account."""
    ad_id = _require(args, "ad_id", "ad_501")
    return _with_major(await _call(conn, "POST", f"ads/{ad_id}/preview"))


async def account_structure(conn: Connection, db, args: dict) -> dict:
    """Campaigns -> ad groups (-> ads), capped so a large account stays one call."""
    per_level = _int(args, "max_per_level", 25, 1, 100)
    include_ads = bool((args or {}).get("include_ads"))
    campaigns = (await _read(conn, "list_campaigns", "campaigns", {"limit": per_level})).get("data") or []
    tree, truncated = [], False
    for campaign in campaigns:
        groups_page = await _read(
            conn, "list_ad_groups", "ad_groups", {"campaign_id": campaign["id"], "limit": per_level}
        )
        truncated = truncated or bool(groups_page.get("has_more"))
        groups = []
        for group in groups_page.get("data") or []:
            node = {k: group.get(k) for k in ("id", "name", "status", "bidding_config")}
            if include_ads:
                ads_page = await _read(conn, "list_ads", "ads", {"ad_group_id": group["id"], "limit": per_level})
                truncated = truncated or bool(ads_page.get("has_more"))
                node["ads"] = [
                    {k: ad.get(k) for k in ("id", "name", "status", "review_status")}
                    for ad in ads_page.get("data") or []
                ]
            groups.append(node)
        tree.append({
            **{k: campaign.get(k) for k in ("id", "name", "status", "bidding_type", "budget", "mode")},
            "ad_groups": groups,
        })
    return {
        "campaigns": _with_major(tree),
        "campaign_count": len(tree),
        "truncated": truncated,
        "note": "Each level is capped at max_per_level; truncated=true means some children were not listed.",
    }


def _budget(args: dict, required: bool) -> dict | None:
    budget = {}
    if args.get("lifetime_budget") is not None:
        budget["lifetime_spend_limit_micros"] = _to_micros(args["lifetime_budget"], "lifetime_budget")
    if args.get("daily_budget") is not None:
        budget["daily_spend_limit_micros"] = _to_micros(args["daily_budget"], "daily_budget")
    if required and not budget:
        raise ConnectorError("Send lifetime_budget or daily_budget, in the account currency (e.g. 250).")
    return budget or None


def _targeting(args: dict) -> dict | None:
    """Raw ``targeting`` wins; otherwise build one from the convenience fields."""
    if "targeting" in args:
        return args["targeting"]
    targeting: dict = {}
    countries = _str_list(args.get("countries"), "countries")
    location_ids = _str_list(args.get("location_ids"), "location_ids")
    if countries:
        targeting.setdefault("locations", {})["countries"] = [c.upper() for c in countries]
    if location_ids:
        targeting.setdefault("locations", {})["include"] = [{"id": i} for i in location_ids]
    platforms = _str_list(args.get("platforms"), "platforms")
    if platforms:
        targeting["platforms"] = {"included": platforms}
    include = _str_list(args.get("include_audience_ids"), "include_audience_ids")
    if include:
        targeting["custom_audiences"] = {"ids": include}
    exclude = _str_list(args.get("exclude_audience_ids"), "exclude_audience_ids")
    if exclude:
        targeting["excluded_custom_audiences"] = {"ids": exclude}
    return targeting or None


async def create_campaign(conn: Connection, db, args: dict) -> dict:
    body = {
        "name": _require(args, "name", "Spring launch"),
        "status": _choice(args, "status", ("active", "paused"), "paused"),
        "budget": _budget(args, required=True),
    }
    bidding = _choice(args, "bidding_type", ("impressions", "clicks", "conversions"))
    if bidding:
        body["bidding_type"] = bidding
    if bidding == "conversions":
        ids = _str_list(args.get("conversion_event_setting_ids"), "conversion_event_setting_ids")
        if len(ids) != 1:
            raise ConnectorError("A conversions campaign needs exactly one conversion_event_setting_ids entry.")
        body["conversion_event_setting_ids"] = ids
    for key in ("description", "mode", "product_feed_id"):
        if args.get(key):
            body[key] = args[key]
    for key in ("start_time", "end_time"):
        stamp = await _to_unix(conn, args.get(key), key)
        if stamp is not None:
            body[key] = stamp
    targeting = _targeting(args)
    if targeting:
        body["targeting"] = targeting
    return await _write(conn, "POST", "campaigns", body, idempotency_key=_idem(args))


async def update_campaign(conn: Connection, db, args: dict) -> dict:
    campaign_id = _require(args, "campaign_id", "cmpn_101")
    body = _pick(args, ("name", "description", "status"))
    for key in ("start_time", "end_time"):
        if key in args:
            body[key] = await _to_unix(conn, args[key], key)
    budget = _budget(args, required=False)
    if budget:
        body["budget"] = budget
    targeting = _targeting(args)
    if targeting is not None or "targeting" in args:
        body["targeting"] = targeting
    if not body:
        raise ConnectorError("Nothing to update: send at least one field.")
    return await _write(conn, "POST", f"campaigns/{campaign_id}", body)


async def _set_state(conn: Connection, kind: str, object_id: str, action: str) -> dict:
    return await _write(conn, "POST", f"{kind}/{object_id}/{action}")


_STATES = ("activate", "pause", "archive")


async def set_campaign_status(conn: Connection, db, args: dict) -> dict:
    campaign_id = _require(args, "campaign_id", "cmpn_101")
    action = _choice(args, "action", _STATES)
    if not action:
        raise ConnectorError("action is required: activate, pause or archive.")
    return await _set_state(conn, "campaigns", campaign_id, action)


async def _billing_event(conn: Connection, campaign_id: str) -> str:
    campaign = await _read(conn, "get_campaign", f"campaigns/{campaign_id}")
    return "impression" if (campaign.get("bidding_type") or "impressions") == "impressions" else "click"


def _bid_multipliers(args: dict) -> list | None:
    raw = args.get("audience_bid_multipliers")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ConnectorError("audience_bid_multipliers must be a list of {custom_audience_id, multiplier}.")
    out = []
    for item in raw:
        try:
            multiplier = float(item["multiplier"])
            audience = str(item["custom_audience_id"])
        except (KeyError, TypeError, ValueError):
            raise ConnectorError("Each audience_bid_multipliers entry needs custom_audience_id and multiplier.")
        if not 0.1 <= multiplier <= 10:
            raise ConnectorError("A bid multiplier must be between 0.1 and 10.")
        out.append({"custom_audience_id": audience, "bid_multiplier_micros": int(round(multiplier * MICROS))})
    return out


async def _bidding_config(conn: Connection, args: dict, campaign_id: str | None) -> dict | None:
    if "bidding_config" in args:
        return args["bidding_config"]
    if args.get("max_bid") is None:
        return None
    config = {"max_bid_micros": _to_micros(args["max_bid"], "max_bid")}
    event = _choice(args, "billing_event_type", ("impression", "click"))
    if not event:
        if not campaign_id:
            raise ConnectorError("billing_event_type is required: impression or click.")
        event = await _billing_event(conn, campaign_id)
    config["billing_event_type"] = event
    if args.get("strategy"):
        config["strategy"] = args["strategy"]
    multipliers = _bid_multipliers(args)
    if multipliers is not None:
        config["custom_audience_bid_multipliers"] = multipliers
    return config


async def create_ad_group(conn: Connection, db, args: dict) -> dict:
    campaign_id = _require(args, "campaign_id", "cmpn_101")
    config = await _bidding_config(conn, args, campaign_id)
    if not config:
        raise ConnectorError("max_bid is required, in the account currency per billing event (e.g. 2.00).")
    body = {
        "campaign_id": campaign_id,
        "name": _require(args, "name", "US English"),
        "status": _choice(args, "status", ("active", "paused"), "paused"),
        "bidding_config": config,
    }
    hints = _str_list(args.get("context_hints"), "context_hints")
    if hints:
        body["context_hints"] = hints
    for key in ("description", "product_set"):
        if args.get(key):
            body[key] = args[key]
    return await _write(conn, "POST", "ad_groups", body, idempotency_key=_idem(args))


async def update_ad_group(conn: Connection, db, args: dict) -> dict:
    ad_group_id = _require(args, "ad_group_id", "adgrp_301")
    body = _pick(args, ("name", "description", "status", "product_set"))
    if "context_hints" in args:
        body["context_hints"] = _str_list(args["context_hints"], "context_hints")
    if "bidding_config" in args or args.get("max_bid") is not None:
        campaign_id = None
        if args.get("max_bid") is not None and not args.get("billing_event_type") and "bidding_config" not in args:
            current = await _read(conn, "get_ad_group", f"ad_groups/{ad_group_id}")
            args = {**args, "billing_event_type": (current.get("bidding_config") or {}).get("billing_event_type")}
            campaign_id = current.get("campaign_id")
        body["bidding_config"] = await _bidding_config(conn, args, campaign_id)
    if not body:
        raise ConnectorError("Nothing to update: send at least one field.")
    return await _write(conn, "POST", f"ad_groups/{ad_group_id}", body)


async def set_ad_group_status(conn: Connection, db, args: dict) -> dict:
    ad_group_id = _require(args, "ad_group_id", "adgrp_301")
    action = _choice(args, "action", _STATES)
    if not action:
        raise ConnectorError("action is required: activate, pause or archive.")
    return await _set_state(conn, "ad_groups", ad_group_id, action)


async def _upload_from_url(conn: Connection, image_url: str, purpose: str | None = None) -> str:
    body = {"image_url": image_url}
    if purpose:
        body["purpose"] = purpose
    result = await _write(conn, "POST", "upload", body)
    file_id = result.get("file_id")
    if not file_id:
        raise ConnectorError("OpenAI Ads accepted the upload but returned no file_id.")
    return file_id


async def _creative(conn: Connection, args: dict, required: bool) -> dict | None:
    if "creative" in args:
        return args["creative"]
    keys = ("title", "body", "target_url", "file_id", "price", "image_url")
    if not required and not any(k in args for k in keys):
        return None
    kind = _choice(args, "creative_type", ("chat_card", "product_ad_template"), "chat_card")
    creative = {"type": kind}
    creative["title"] = _require(args, "title", "Try the new workspace planner")
    creative["body"] = str(args.get("body") or "")
    if len(creative["title"]) > 50:
        raise ConnectorError("title allows at most 50 characters.")
    if len(creative["body"]) > 100:
        raise ConnectorError("body allows at most 100 characters.")
    if args.get("price"):
        creative["price"] = str(args["price"])
    if kind == "chat_card":
        creative["target_url"] = _require(args, "target_url", "https://example.com/landing")
        file_id = str(args.get("file_id") or "").strip()
        if not file_id and args.get("image_url"):
            file_id = await _upload_from_url(conn, str(args["image_url"]))
        if not file_id:
            raise ConnectorError("A chat_card needs file_id, or image_url to upload (at least 640x640).")
        creative["file_id"] = file_id
    return creative


async def create_ad(conn: Connection, db, args: dict) -> dict:
    body = {
        "ad_group_id": _require(args, "ad_group_id", "adgrp_301"),
        "name": _require(args, "name", "Planner launch card"),
        "status": _choice(args, "status", ("active", "paused"), "paused"),
        "creative": await _creative(conn, args, required=True),
    }
    return await _write(conn, "POST", "ads", body, idempotency_key=_idem(args))


async def update_ad(conn: Connection, db, args: dict) -> dict:
    ad_id = _require(args, "ad_id", "ad_501")
    body = _pick(args, ("name", "status"))
    creative = await _creative(conn, args, required=False)
    if creative:
        body["creative"] = creative
    if not body:
        raise ConnectorError("Nothing to update. A creative change must send the full creative.")
    return await _write(conn, "POST", f"ads/{ad_id}", body)


async def set_ad_status(conn: Connection, db, args: dict) -> dict:
    ad_id = _require(args, "ad_id", "ad_501")
    action = _choice(args, "action", _STATES)
    if not action:
        raise ConnectorError("action is required: activate, pause or archive.")
    return await _set_state(conn, "ads", ad_id, action)


async def upload_image(conn: Connection, db, args: dict) -> dict:
    image_url = _require(args, "image_url", "https://example.com/card.png")
    purpose = _choice(args, "purpose", ("creative", "account_favicon"), "creative")
    file_id = await _upload_from_url(conn, image_url, "account_favicon" if purpose == "account_favicon" else None)
    return {"file_id": file_id, "purpose": purpose}


# --------------------------------------------------------------------------- #
# insights
# --------------------------------------------------------------------------- #
def _json_items(value, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    out = []
    for item in value:
        if isinstance(item, (dict, list)):
            out.append(json.dumps(item))
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
        else:
            raise ConnectorError(f"Each {name} entry must be an object.")
    return out


def _attribution(args: dict, params: list) -> None:
    window = (args or {}).get("attribution_window_days")
    if window is not None:
        if int(window) not in (7, 14, 30):
            raise ConnectorError("attribution_window_days must be 7, 14 or 30.")
        params.append(("attribution_window_days", str(int(window))))
    view = (args or {}).get("view_through_attribution_window_days")
    if view is not None:
        if int(view) not in (0, 1):
            raise ConnectorError("view_through_attribution_window_days must be 0 or 1.")
        params.append(("view_through_attribution_window_days", str(int(view))))
    basis = _choice(args, "attribution_time_basis", ("ad_event_time", "conversion_time"))
    if basis:
        params.append(("attribution_time_basis", basis))


def _default_fields(level: str, segment: str | None, granularity: str, conversions: bool) -> list[str]:
    fields: list[str] = []
    if granularity != "none":
        fields.append("readable_time")
    if level != "ad_account":
        fields.append(f"{level}_id")
    if segment:
        fields.extend(SEGMENT_METADATA[segment])
        fields.extend(f"{segment}.{m}" for m in DELIVERY_METRICS)
        return fields
    if level != "ad_account":
        fields.append(f"{level}_name")
    fields.extend(DELIVERY_METRICS)
    if conversions:
        fields.extend(CONVERSION_METRICS)
    return fields


async def _insights(conn: Connection, tool: str, path: str, args: dict, level: str,
                    segment: str | None = None, default_granularity: str = "none") -> dict:
    args = args or {}
    tz = await _tz(conn)
    now = datetime.now(dt_timezone.utc).astimezone(tz)
    time_range, whole_days = _time_range(args, now.date(), now.hour)

    granularity_options = ("none", "daily", "monthly") if segment else ("none", "hourly", "daily", "monthly")
    granularity = _choice(args, "time_granularity", granularity_options, default_granularity)
    include_conversions = args.get("include_conversions")
    if include_conversions is None:
        include_conversions = whole_days
    elif include_conversions and not whole_days:
        raise ConnectorError(
            "Conversion metrics need whole days: choose a range that ends yesterday or earlier."
        )

    params: list[tuple[str, str]] = [
        ("aggregation_level", level),
        ("time_granularity", granularity),
        ("time_ranges[]", time_range),
        ("limit", str(_int(args, "limit", 200, 1, INSIGHTS_PAGE_MAX))),
    ]
    fields = _str_list(args.get("fields"), "fields") or _default_fields(
        level, segment, granularity, bool(include_conversions)
    )
    params.extend(("fields[]", f) for f in fields)
    if segment:
        params.append(("segments[]", segment))
    for order in _str_list(args.get("override_segment_group_order"), "override_segment_group_order"):
        params.append(("override_segment_group_order[]", order))
    for item in _json_items(args.get("filters"), "filters"):
        params.append(("filters[]", item))
    for item in _json_items(args.get("sort"), "sort"):
        params.append(("sort[]", item))
    for include in _str_list(args.get("includes"), "includes"):
        params.append(("includes[]", include))
    if whole_days:
        _attribution(args, params)
    for key in ("after", "before"):
        if args.get(key):
            params.append((key, str(args[key])))

    rows: list = []
    page = await _read(conn, tool, path, params, ttl=TTL_MEDIUM)
    rows.extend(page.get("data") or [])
    pages = 1
    while args.get("all_pages") and page.get("has_more") and page.get("last_id") and pages < MAX_PAGES:
        next_params = [p for p in params if p[0] not in ("after", "before")] + [("after", page["last_id"])]
        page = await _read(conn, tool, path, next_params, ttl=TTL_MEDIUM)
        rows.extend(page.get("data") or [])
        pages += 1
    return {
        "level": level,
        "segment": segment,
        "time_range": json.loads(time_range),
        "timezone": str(tz),
        "currency": (await _account(conn)).get("currency_code"),
        "row_count": len(rows),
        "rows": rows,
        "has_more": bool(page.get("has_more")),
        "last_id": page.get("last_id"),
    }


def _scope(args: dict) -> tuple[str, str]:
    """Which Insights endpoint to call, from whichever entity id was given."""
    for key, kind, level in (("ad_id", "ads", "ad"), ("ad_group_id", "ad_groups", "ad_group"),
                             ("campaign_id", "campaigns", "campaign")):
        value = str((args or {}).get(key) or "").strip()
        if value:
            return f"{kind}/{value}/insights", level
    return "ad_account/insights", "ad_account"


def _check_level(scope_level: str, level: str) -> None:
    if LEVELS.index(level) < LEVELS.index(scope_level):
        raise ConnectorError(
            f"aggregation_level '{level}' is above the {scope_level} scope; "
            f"use {scope_level} or a level below it."
        )


async def insights(conn: Connection, db, args: dict) -> dict:
    path, scope_level = _scope(args)
    level = _choice(args, "aggregation_level", LEVELS, scope_level)
    _check_level(scope_level, level)
    segment = _choice(args, "segment", SEGMENTS)
    return await _insights(conn, "insights", path, args, level, segment, default_granularity="daily")


async def account_performance(conn: Connection, db, args: dict) -> dict:
    return await _insights(conn, "account_performance", "ad_account/insights", args, "ad_account")


async def campaign_performance(conn: Connection, db, args: dict) -> dict:
    return await _insights(conn, "campaign_performance", "ad_account/insights", args, "campaign")


async def ad_group_performance(conn: Connection, db, args: dict) -> dict:
    path, _ = _scope({"campaign_id": (args or {}).get("campaign_id")})
    return await _insights(conn, "ad_group_performance", path, args, "ad_group")


async def ad_performance(conn: Connection, db, args: dict) -> dict:
    path, _ = _scope({k: (args or {}).get(k) for k in ("campaign_id", "ad_group_id")})
    return await _insights(conn, "ad_performance", path, args, "ad")


async def performance_by_segment(conn: Connection, db, args: dict) -> dict:
    segment = _choice(args, "segment", SEGMENTS)
    if not segment:
        raise ConnectorError(f"segment is required: {', '.join(SEGMENTS)}.")
    path, scope_level = _scope(args)
    level = _choice(args, "aggregation_level", LEVELS, scope_level)
    _check_level(scope_level, level)
    return await _insights(conn, "performance_by_segment", path, args, level, segment)


async def conversion_insights(conn: Connection, db, args: dict) -> dict:
    args = args or {}
    tz = await _tz(conn)
    now = datetime.now(dt_timezone.utc).astimezone(tz)
    time_range, whole_days = _time_range(args, now.date(), now.hour)
    if not whole_days:
        raise ConnectorError("Conversion insights need whole days: choose a range that ends yesterday or earlier.")
    level = _choice(args, "aggregation_level", ("campaign", "ad_group", "ad"), "campaign")
    body: dict = {
        "aggregation_level": level,
        "time_ranges": [time_range],
        "time_granularity": _choice(args, "time_granularity", ("none", "daily"), "none"),
    }
    entity_ids = _str_list(args.get("entity_ids"), "entity_ids")
    if entity_ids:
        body["entity_ids"] = entity_ids
    else:
        body["group_by_entity"] = False
    breakdown = _choice(args, "breakdown", ("country", "device"))
    if breakdown:
        body["breakdown"] = breakdown
    for key in ("attribution_window_days", "view_through_attribution_window_days",
                "attribution_time_basis", "include_zero_rows"):
        if args.get(key) is not None:
            body[key] = args[key]
    names = _str_list(args.get("event_names"), "event_names")
    if args.get("include_event_details") or names:
        body["include"] = ["attributed_events"]
    if names:
        body["event_names"] = names

    async def _load():
        return await _call(conn, "POST", "conversions/insights", body=body)

    result = await cached(SLUG, conn.id, "conversion_insights", TTL_MEDIUM, _load, args=body)
    result["time_range"] = json.loads(time_range)
    result["timezone"] = str(tz)
    return result


# --------------------------------------------------------------------------- #
# custom audiences
# --------------------------------------------------------------------------- #
IDENTIFIER_TYPES = ("email", "phone", "email_sha256", "phone_number_sha256", "gaid")


async def list_custom_audiences(conn: Connection, db, args: dict) -> dict:
    params: list[tuple[str, str]] = list(_page(args, max_limit=100, default=50).items())
    use = _choice(args, "intended_use", ("inclusion", "exclusion", "bid_multiplier"))
    if use:
        params.append(("intended_use", use))
    for audience_id in _str_list(args.get("custom_audience_ids"), "custom_audience_ids"):
        params.append(("custom_audience_ids[]", audience_id))
    if args.get("granular_counts"):
        params.append(("matched_count_granularity", "granular"))
    return await _read(conn, "list_custom_audiences", "custom_audiences", params)


async def get_custom_audience(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    params = {"matched_count_granularity": "granular"} if args.get("granular_counts") else None
    return await _fresh(conn, f"custom_audiences/{audience_id}", params)


async def list_audience_operations(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    params = {"limit": _int(args, "limit", 20, 1, 100)}
    if args.get("cursor"):
        params["cursor"] = str(args["cursor"])
    return await _fresh(conn, f"custom_audiences/{audience_id}/operations", params)


async def get_audience_operation(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    operation_id = _require(args, "operation_id", "caudop_123")
    return await _fresh(conn, f"custom_audiences/{audience_id}/operations/{operation_id}")


async def upload_audience_file(conn: Connection, db, args: dict) -> dict:
    content = (args or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise ConnectorError("content is required: the CSV or TXT text, UTF-8.")
    filename = _require(args, "filename", "audience.csv")
    if not filename.lower().endswith((".csv", ".txt")):
        raise ConnectorError("filename must end in .csv or .txt.")
    mimetype = "text/csv" if filename.lower().endswith(".csv") else "text/plain"
    raw = content.encode("utf-8")
    if len(raw) > MAX_INLINE_FILE_BYTES:
        raise ConnectorError("That file is too large to pass through a tool call; upload it in parts.")
    result = await _write(
        conn, "POST", "uploads",
        files={"file": (filename, raw, mimetype)}, data={"purpose": "custom_audience"},
    )
    return {"file_id": result.get("file_id"), "filename": filename, "mimetype": mimetype, "file_size": len(raw)}


async def create_custom_audience(conn: Connection, db, args: dict) -> dict:
    body = {"name": _require(args, "name", "High-value customers")}
    for key in ("description", "file_id", "filename", "mimetype", "file_size"):
        if args.get(key) is not None:
            body[key] = args[key]
    if body.get("file_id") and not all(body.get(k) for k in ("filename", "mimetype", "file_size")):
        raise ConnectorError("A file audience needs filename, mimetype and file_size from upload_audience_file.")
    _identifier_options(args, body)
    return await _write(conn, "POST", "custom_audiences", body, idempotency_key=_idem(args))


def _identifier_options(args: dict, body: dict) -> None:
    kind = _choice(args, "identifier_type", IDENTIFIER_TYPES)
    if kind:
        body["identifier_type"] = kind
    if args.get("auto_resolve_identifiers"):
        body["identifier_resolution"] = "auto"


def _membership_body(args: dict, allow_inline: bool) -> dict:
    body: dict = {}
    file_id = str((args or {}).get("file_id") or "").strip()
    identifiers = (args or {}).get("identifiers")
    if file_id and identifiers:
        raise ConnectorError("Send file_id or identifiers, not both.")
    if file_id:
        body["file_id"] = file_id
        _identifier_options(args, body)
    elif identifiers and allow_inline:
        if not isinstance(identifiers, list):
            raise ConnectorError("identifiers must be a list of {identifier_type, identifier}.")
        for item in identifiers:
            if not isinstance(item, dict) or item.get("identifier_type") not in IDENTIFIER_TYPES \
                    or not item.get("identifier"):
                raise ConnectorError(
                    f"Each identifier needs identifier_type ({', '.join(IDENTIFIER_TYPES)}) and identifier."
                )
        body["identifiers"] = identifiers
    else:
        raise ConnectorError("file_id is required." if not allow_inline else "Send file_id or identifiers.")
    if args.get("expected_revision") is not None:
        body["expected_revision"] = int(args["expected_revision"])
    return body


async def add_audience_members(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    return await _write(conn, "POST", f"custom_audiences/{audience_id}/add",
                        _membership_body(args, True), idempotency_key=_idem(args))


async def remove_audience_members(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    return await _write(conn, "POST", f"custom_audiences/{audience_id}/remove",
                        _membership_body(args, True), idempotency_key=_idem(args))


async def replace_audience_members(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    body = _membership_body(args, False)
    if "expected_revision" not in body:
        current = await _fresh(conn, f"custom_audiences/{audience_id}")
        body["expected_revision"] = int(current.get("membership_revision") or 0)
    return await _write(conn, "POST", f"custom_audiences/{audience_id}/replace",
                        body, idempotency_key=_idem(args))


async def merge_custom_audiences(conn: Connection, db, args: dict) -> dict:
    ids = _str_list(args.get("custom_audience_ids"), "custom_audience_ids")
    if not 2 <= len(set(ids)) <= 64:
        raise ConnectorError("Merge needs 2 to 64 distinct custom_audience_ids.")
    body = {"name": _require(args, "name", "All qualified customers"), "custom_audience_ids": ids}
    return await _write(conn, "POST", "custom_audiences/merge", body, idempotency_key=_idem(args))


async def archive_custom_audience(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    return await _write(conn, "POST", f"custom_audiences/{audience_id}/archive")


async def resume_audience_operation(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    operation_id = _require(args, "operation_id", "caudop_123")
    return await _write(conn, "POST", f"custom_audiences/{audience_id}/operations/{operation_id}/resume")


async def cancel_audience_operation(conn: Connection, db, args: dict) -> dict:
    audience_id = _require(args, "custom_audience_id", "caud_123")
    operation_id = _require(args, "operation_id", "caudop_123")
    return await _write(conn, "POST", f"custom_audiences/{audience_id}/operations/{operation_id}/cancel")


# --------------------------------------------------------------------------- #
# conversion setup
# --------------------------------------------------------------------------- #
async def list_conversion_event_settings(conn: Connection, db, args: dict) -> dict:
    return await _read(conn, "list_event_settings", "conversions/event_settings", _page(args))


async def recent_pixel_events(conn: Connection, db, args: dict) -> dict:
    pixel_id = _require(args, "pixel_id", "134534...")
    return await _fresh(conn, "conversions/events", {"pid": pixel_id})


async def create_pixel(conn: Connection, db, args: dict) -> dict:
    body = {"name": _require(args, "name", "Acme website"), "client_type": "web"}
    return await _write(conn, "POST", "conversions/pixels", body, idempotency_key=_idem(args))


async def create_conversion_event_setting(conn: Connection, db, args: dict) -> dict:
    event_type = _require(args, "event_type", "order_created")
    body = {
        "name": _require(args, "name", "Purchases"),
        "event_type": event_type,
        "attribution_window_days": 30,
        "source_ids": [_require(args, "source_id", "clidsrc_123")],
    }
    if event_type == "custom":
        body["custom_event_name"] = _require(args, "custom_event_name", "demo_booked")
    return await _write(conn, "POST", "conversions/event_settings", body, idempotency_key=_idem(args))


# --------------------------------------------------------------------------- #
# product feeds
# --------------------------------------------------------------------------- #
async def list_product_feeds(conn: Connection, db, args: dict) -> dict:
    params = {"include[]": "product_count"} if (args or {}).get("include_product_count", True) else None
    return await _read(conn, "list_feeds", "feeds", params)


async def list_feed_uploads(conn: Connection, db, args: dict) -> dict:
    result = await _fresh(conn, "feeds/uploads")
    feed_id = str((args or {}).get("feed_id") or "").strip()
    if feed_id and isinstance(result.get("data"), list):
        result["data"] = [u for u in result["data"] if u.get("feed_id") == feed_id]
    return result


async def query_feed_products(conn: Connection, db, args: dict) -> dict:
    """POST, but a read: it only previews which products a filter set matches."""
    feed_id = _require(args, "feed_id", "fd_123")
    body = {"filters": args.get("filters") or [], "limit": _int(args, "limit", 20, 1, 500)}

    async def _load():
        return await _call(conn, "POST", f"feeds/{feed_id}/products/query", body=body)

    return await cached(SLUG, conn.id, "query_feed_products", TTL_SHORT, _load, args={"feed": feed_id, **body})


async def create_product_feed(conn: Connection, db, args: dict) -> dict:
    countries = [c.upper() for c in _str_list(args.get("countries"), "countries")]
    if not countries:
        raise ConnectorError("countries is required, e.g. ['US'].")
    body = {"name": _require(args, "name", "Acme spring catalog"), "countries": countries}
    return await _write(conn, "POST", "feeds", body, idempotency_key=_idem(args))


async def archive_product_feed(conn: Connection, db, args: dict) -> dict:
    feed_id = _require(args, "feed_id", "fd_123")
    return await _write(conn, "POST", f"feeds/{feed_id}/archive")


async def set_feed_sftp_access(conn: Connection, db, args: dict) -> dict:
    feed_id = _require(args, "feed_id", "fd_123")
    action = _choice(args, "action", ("configure_ssh_key", "pause", "activate"))
    if action == "pause":
        return await _write(conn, "POST", f"feeds/{feed_id}/sftp_access/pause")
    if action == "activate":
        return await _write(conn, "POST", f"feeds/{feed_id}/sftp_access/activate")
    if action != "configure_ssh_key":
        raise ConnectorError("action is required: configure_ssh_key, pause or activate.")
    key = _require(args, "ssh_public_key", "ssh-ed25519 AAAA... you@host")
    if "PRIVATE KEY" in key:
        raise ConnectorError("That is a private key. Send only the public key.")
    body = {"authentication_method": "ssh_key", "ssh_public_key": key}
    return await _write(conn, "POST", f"feeds/{feed_id}/sftp_access", body)


async def update_feed_products(conn: Connection, db, args: dict) -> dict:
    feed_id = _require(args, "feed_id", "fd_123")
    products = (args or {}).get("products")
    if not isinstance(products, list) or not products:
        raise ConnectorError("products is required: [{id, variants: [{id, price?, availability?}]}].")
    return await _write(conn, "PATCH", f"feeds/{feed_id}/products", {"products": products})


# --------------------------------------------------------------------------- #
# bulk jobs, audit logs, locations
# --------------------------------------------------------------------------- #
async def submit_bulk_job(conn: Connection, db, args: dict) -> dict:
    operations = (args or {}).get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 1000:
        raise ConnectorError("operations must be a list of 1 to 1,000 bulk operations.")
    body = {
        "operations": operations,
        "validate_only": bool(args.get("validate_only", False)),
        "partial_failure": bool(args.get("partial_failure", True)),
    }
    return await _write(conn, "POST", "bulk_mutation_jobs", body, idempotency_key=_idem(args))


async def get_bulk_job(conn: Connection, db, args: dict) -> dict:
    job_id = _require(args, "job_id", "blkmtnjob_123")
    return await _fresh(conn, f"bulk_mutation_jobs/{job_id}")


async def list_bulk_job_operations(conn: Connection, db, args: dict) -> dict:
    job_id = _require(args, "job_id", "blkmtnjob_123")
    params = {"limit": _int(args, "limit", 100, 1, 100)}
    if args.get("after"):
        params["after"] = str(args["after"])
    return await _fresh(conn, f"bulk_mutation_jobs/{job_id}/operations", params)


async def list_audit_logs(conn: Connection, db, args: dict) -> dict:
    params = _page(args, max_limit=100, default=50)
    for key in ("campaign_id", "ad_group_id", "ad_id", "actor_id"):
        if args.get(key):
            params[key] = str(args[key])
    for key in ("start_time", "end_time"):
        stamp = await _to_unix(conn, args.get(key), key)
        if stamp is not None:
            params[key] = stamp
    return await _read(conn, "audit_logs", "audit_logs", params)


async def search_locations(conn: Connection, db, args: dict) -> dict:
    params = {"q": _require(args, "query", "California"), "limit": _int(args, "limit", 10, 1, 50)}
    return await _read(conn, "geo_lookup", "geo_lookup/search", params, ttl=TTL_LONG)


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def _schema(properties: dict | None = None, required: list | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _s(description: str) -> dict:
    return {"type": "string", "description": description}


def _n(description: str) -> dict:
    return {"type": "number", "description": description}


def _i(description: str) -> dict:
    return {"type": "integer", "description": description}


def _b(description: str) -> dict:
    return {"type": "boolean", "description": description}


def _list(description: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _obj(description: str) -> dict:
    return {"type": "object", "description": description}


def _objs(description: str) -> dict:
    return {"type": "array", "items": {"type": "object"}, "description": description}


_PAGE = {
    "limit": _i("Rows per page, 1-500. Default 50."),
    "after": _s("Cursor: the previous page's last_id, for the next page."),
    "before": _s("Cursor: the previous page's first_id, for the page before."),
    "order": {"type": "string", "enum": ["asc", "desc"], "description": "Sort order by creation."},
}

_IDEM = {"idempotency_key": _s("Optional. Reuse the same key only to retry this exact request.")}

_TIME = {
    "date_range": {
        "type": "string", "enum": list(DATE_PRESETS),
        "description": "Named period in the account's time zone. Default last_7_days (ends yesterday).",
    },
    "start_date": _s("Custom range start, YYYY-MM-DD (inclusive). Needs end_date."),
    "end_date": _s("Custom range end, YYYY-MM-DD (inclusive). Needs start_date."),
}

_ATTRIBUTION = {
    "attribution_window_days": {"type": "integer", "enum": [7, 14, 30], "description": "Click window. Default 30."},
    "view_through_attribution_window_days": {"type": "integer", "enum": [0, 1],
                                             "description": "View window. Default 1; 0 excludes views."},
    "attribution_time_basis": {"type": "string", "enum": ["ad_event_time", "conversion_time"],
                               "description": "Date conversions by the ad interaction (default) or the conversion."},
}

_REPORT = {
    **_TIME,
    **_ATTRIBUTION,
    "time_granularity": {"type": "string", "enum": ["none", "hourly", "daily", "monthly"],
                         "description": "Time buckets. Default none (one total per row)."},
    "include_conversions": _b("Add conversions, CPA, post-click CVR, attributed sales and ROAS. "
                              "Default true for whole-day ranges."),
    "fields": _list("Override the returned fields (canonical names, e.g. campaign.id, spend)."),
    "filters": _objs("Insights filters, e.g. {field:'campaign.status', operator:'IN', value:['active']}."),
    "sort": _objs("Sorts, e.g. {field:'spend', direction:'desc'}."),
    "includes": _list("zero_impression_items to add entities with no impressions."),
    "limit": _i("Rows per page, 1-2000. Default 200."),
    "after": _s("Cursor from a previous page's last_id."),
    "all_pages": _b("Follow has_more for up to 10 pages."),
}

_SCOPE = {
    "campaign_id": _s("Scope to one campaign."),
    "ad_group_id": _s("Scope to one ad group."),
    "ad_id": _s("Scope to one ad."),
}

_STATE_ACTION = {
    "type": "string", "enum": list(_STATES),
    "description": "activate, pause, or archive. Archiving cannot be undone.",
}

_TARGETING = {
    "countries": _list("ISO country codes to target, e.g. ['US']."),
    "location_ids": _list("Region or market ids from search_locations."),
    "platforms": _list("ChatGPT platforms: android_app, ios_app, desktop_web, android_web, ios_web, web."),
    "include_audience_ids": _list("Custom audiences to target (must be inclusion-eligible)."),
    "exclude_audience_ids": _list("Custom audiences to exclude."),
    "targeting": _obj("Raw targeting object; overrides the convenience fields above. Null clears it on update."),
}

_CAMPAIGN_FIELDS = {
    "name": _s("3-1000 characters."),
    "description": _s("Campaign description."),
    "lifetime_budget": _n("Lifetime spend cap in the account currency (e.g. 250)."),
    "daily_budget": _n("Daily spend cap in the account currency."),
    "start_time": _s("Unix seconds or YYYY-MM-DD (midnight, account time zone)."),
    "end_time": _s("Unix seconds or YYYY-MM-DD (midnight, account time zone)."),
    **_TARGETING,
}

_BID_FIELDS = {
    "max_bid": _n("Max bid per billing event in the account currency. For impressions this is per "
                  "impression (0.06 = $60 CPM); for conversions it is the CPA target."),
    "billing_event_type": {"type": "string", "enum": ["impression", "click"],
                           "description": "Default: taken from the campaign's bidding type."},
    "strategy": _s("Bid strategy, e.g. fixed_bid."),
    "audience_bid_multipliers": _objs("[{custom_audience_id, multiplier}] with multiplier 0.1-10."),
    "bidding_config": _obj("Raw bidding_config; overrides the fields above."),
    "context_hints": _list("Phrases describing when the ad is useful, e.g. ['team productivity']."),
    "product_set": _obj("Product-feed filters: {product_feed_id, filters:[{field, operator, values}]}."),
}

_CREATIVE_FIELDS = {
    "creative_type": {"type": "string", "enum": ["chat_card", "product_ad_template"],
                      "description": "Default chat_card."},
    "title": _s("3-50 characters. Product templates may use {{product.title}} and {{brand}}."),
    "body": _s("Up to 100 characters."),
    "target_url": _s("Destination URL (chat_card)."),
    "file_id": _s("Image file id from upload_image (chat_card)."),
    "image_url": _s("Instead of file_id: a public image URL (at least 640x640) to upload first."),
    "price": _s("Price text, or {{product.price}} in a product template."),
    "creative": _obj("Raw creative object; overrides the fields above."),
}

_READ, _WRITE = False, True


def _entry(description: str, schema: dict, write: bool = _READ) -> dict:
    entry = {"description": description, "input": schema}
    if write:
        entry["write"] = True
    return entry


CATALOG: dict[str, dict] = {
    # --- account
    "get_ad_account": _entry(
        "The ad account: names, destination URL, status, time zone, currency and brand review status.",
        _schema()),
    "list_spend_limits": _entry(
        "Account-wide spending limits: date-range windows and the daily limit, with spend so far and the "
        "revision needed to change the daily limit. Needs billing permission.",
        _schema()),
    "update_account_brand": _entry(
        "Change the legal, internal or public brand name, or the advertiser icon. Changing a legal or "
        "brand name pauses delivery until the account passes review.",
        _schema({
            "legal_name": _s("Legal business name."),
            "account_name": _s("Internal name shown in Ads Manager."),
            "brand_name": _s("Public advertiser name shown in ads."),
            "favicon_file_id": _s("File id from upload_image with purpose account_favicon."),
        }), _WRITE),
    "pause_account": _entry("Pause the whole ad account: nothing in it delivers.", _schema(), _WRITE),
    "activate_account": _entry("Reactivate a paused ad account.", _schema(), _WRITE),
    "create_spend_limit_window": _entry(
        "Cap total spend across all campaigns for a date range (postpaid invoice accounts only).",
        _schema({
            "start_date": _s("YYYY-MM-DD, today or later."),
            "end_date": _s("YYYY-MM-DD, exclusive."),
            "amount": _n("Total allowance in the account currency."),
            "name": _s("Optional label."),
            "io_id": _s("Optional insertion-order reference."),
        }, ["start_date", "end_date", "amount"]), _WRITE),
    "update_spend_limit_window": _entry(
        "Change a date-range spending limit's dates, amount, name or IO reference.",
        _schema({
            "window_id": _s("From list_spend_limits."),
            "start_date": _s("YYYY-MM-DD (upcoming windows only)."),
            "end_date": _s("YYYY-MM-DD, exclusive."),
            "amount": _n("New total allowance in the account currency."),
            "name": _s("Label; null clears it."),
            "io_id": _s("IO reference; null clears it."),
        }, ["window_id"]), _WRITE),
    "delete_spend_limit_window": _entry(
        "Delete an active or upcoming date-range spending limit.",
        _schema({"window_id": _s("From list_spend_limits.")}, ["window_id"]), _WRITE),
    "set_daily_spend_limit": _entry(
        "Create or change the account's daily spending allowance. The revision is read automatically "
        "unless given.",
        _schema({
            "amount": _n("Allowance per day in the account currency."),
            "start_date": _s("YYYY-MM-DD; only when creating. Defaults to the earliest allowed day."),
            "end_date": _s("YYYY-MM-DD exclusive, or null for no end."),
            "expected_revision": _i("Optional: revision from list_spend_limits."),
        }, ["amount"]), _WRITE),
    "remove_daily_spend_limit": _entry(
        "Remove the account's daily spending allowance.",
        _schema({"expected_revision": _i("Optional: revision from list_spend_limits.")}), _WRITE),

    # --- structure
    "account_structure": _entry(
        "The account as a tree: campaigns with their ad groups (and optionally ads), with status, "
        "budgets and bids. Capped per level.",
        _schema({
            "include_ads": _b("Also list ads under each ad group."),
            "max_per_level": _i("Most children per level, 1-100. Default 25."),
        })),
    "list_campaigns": _entry(
        "Campaigns in the account with status, bidding type, budget, schedule and targeting.",
        _schema(dict(_PAGE))),
    "get_campaign": _entry(
        "One campaign in full, optionally with the serving issues blocking delivery.",
        _schema({"campaign_id": _s("Campaign id."),
                 "include_serving_issues": _b("Add delivery blockers.")}, ["campaign_id"])),
    "create_campaign": _entry(
        "Create a campaign (paused by default). Budgets are in the account currency. For "
        "conversion-optimized bidding send bidding_type=conversions with one event setting id.",
        _schema({
            **_CAMPAIGN_FIELDS,
            "status": {"type": "string", "enum": ["active", "paused"], "description": "Default paused."},
            "bidding_type": {"type": "string", "enum": ["impressions", "clicks", "conversions"],
                             "description": "Cannot be changed later. Default impressions."},
            "conversion_event_setting_ids": _list("Exactly one standard event setting id for conversions."),
            "mode": _s("product_feed for a product-feed campaign."),
            "product_feed_id": _s("The feed, when mode is product_feed."),
            **_IDEM,
        }, ["name"]), _WRITE),
    "update_campaign": _entry(
        "Change a campaign's name, description, status, schedule, budget or targeting. Bidding type "
        "cannot change.",
        _schema({
            "campaign_id": _s("Campaign id."),
            **_CAMPAIGN_FIELDS,
            "status": {"type": "string", "enum": ["active", "paused", "archived"]},
        }, ["campaign_id"]), _WRITE),
    "set_campaign_status": _entry(
        "Activate, pause or archive a campaign.",
        _schema({"campaign_id": _s("Campaign id."), "action": _STATE_ACTION},
                ["campaign_id", "action"]), _WRITE),
    "list_ad_groups": _entry(
        "Ad groups in one campaign, with context hints, status and bidding config.",
        _schema({"campaign_id": _s("Parent campaign id."), **_PAGE}, ["campaign_id"])),
    "get_ad_group": _entry(
        "One ad group in full, including its product set, optionally with serving issues.",
        _schema({"ad_group_id": _s("Ad group id."),
                 "include_serving_issues": _b("Add delivery blockers.")}, ["ad_group_id"])),
    "create_ad_group": _entry(
        "Create an ad group in a campaign (paused by default). max_bid is in the account currency.",
        _schema({
            "campaign_id": _s("Parent campaign id."),
            "name": _s("3-1000 characters."),
            "description": _s("Ad group description."),
            "status": {"type": "string", "enum": ["active", "paused"], "description": "Default paused."},
            **_BID_FIELDS,
            **_IDEM,
        }, ["campaign_id", "name", "max_bid"]), _WRITE),
    "update_ad_group": _entry(
        "Change an ad group's name, description, status, context hints, bid or product set.",
        _schema({
            "ad_group_id": _s("Ad group id."),
            "name": _s("New name."),
            "description": _s("Description; null clears it."),
            "status": {"type": "string", "enum": ["active", "paused", "archived"]},
            **_BID_FIELDS,
        }, ["ad_group_id"]), _WRITE),
    "set_ad_group_status": _entry(
        "Activate, pause or archive an ad group.",
        _schema({"ad_group_id": _s("Ad group id."), "action": _STATE_ACTION},
                ["ad_group_id", "action"]), _WRITE),
    "list_ads": _entry(
        "Ads in one ad group with creative, status and review status (in_review, approved, rejected).",
        _schema({"ad_group_id": _s("Parent ad group id."), **_PAGE}, ["ad_group_id"])),
    "get_ad": _entry(
        "One ad in full, optionally with serving issues.",
        _schema({"ad_id": _s("Ad id."), "include_serving_issues": _b("Add delivery blockers.")}, ["ad_id"])),
    "preview_ad": _entry(
        "A preview of an existing ad as it would appear in ChatGPT. The link expires after 24 hours.",
        _schema({"ad_id": _s("Ad id.")}, ["ad_id"])),
    "create_ad": _entry(
        "Create an ad (paused by default): a chat card with title, body, link and image, or a "
        "product-ad template for a product-feed ad group. Pass image_url to upload the image in one step.",
        _schema({
            "ad_group_id": _s("Parent ad group id."),
            "name": _s("Internal name, 3-1000 characters; not shown to users."),
            "status": {"type": "string", "enum": ["active", "paused"], "description": "Default paused."},
            **_CREATIVE_FIELDS,
            **_IDEM,
        }, ["ad_group_id", "name", "title"]), _WRITE),
    "update_ad": _entry(
        "Change an ad's name, status or creative. A creative change sends the full creative again and "
        "re-enters review.",
        _schema({
            "ad_id": _s("Ad id."),
            "name": _s("New internal name."),
            "status": {"type": "string", "enum": ["active", "paused", "archived"]},
            **_CREATIVE_FIELDS,
        }, ["ad_id"]), _WRITE),
    "set_ad_status": _entry(
        "Activate, pause or archive an ad.",
        _schema({"ad_id": _s("Ad id."), "action": _STATE_ACTION}, ["ad_id", "action"]), _WRITE),
    "upload_image": _entry(
        "Upload an image from a public URL and get a reusable file_id: an ad creative (at least 640x640) "
        "or the account icon (at least 128x128; a website URL resolves its icon).",
        _schema({
            "image_url": _s("Public image URL, or a website URL for purpose account_favicon."),
            "purpose": {"type": "string", "enum": ["creative", "account_favicon"], "description": "Default creative."},
        }, ["image_url"]), _WRITE),

    # --- reporting
    "account_performance": _entry(
        "Account totals: impressions, clicks, spend, CTR, CPC, CPM and, for whole days, conversions, CPA, "
        "post-click CVR, attributed sales and ROAS.",
        _schema(dict(_REPORT))),
    "campaign_performance": _entry(
        "The same metrics per campaign, with names.",
        _schema(dict(_REPORT))),
    "ad_group_performance": _entry(
        "The same metrics per ad group, optionally within one campaign.",
        _schema({**_REPORT, "campaign_id": _s("Limit to one campaign.")})),
    "ad_performance": _entry(
        "The same metrics per ad, optionally within one campaign or ad group.",
        _schema({**_REPORT, "campaign_id": _s("Limit to one campaign."),
                 "ad_group_id": _s("Limit to one ad group.")})),
    "performance_by_segment": _entry(
        "Delivery metrics broken down by product, country, device or ChatGPT platform "
        "(android_app, ios_app, desktop_web, android_web, ios_web, web).",
        _schema({
            "segment": {"type": "string", "enum": list(SEGMENTS)},
            "aggregation_level": {"type": "string", "enum": list(LEVELS),
                                  "description": "Row entity. Default: the scope's own level."},
            **_SCOPE,
            **_REPORT,
            "time_granularity": {"type": "string", "enum": ["none", "daily", "monthly"]},
            "override_segment_group_order": _list("E.g. ['product', 'ad_account'] for product-first rows."),
        }, ["segment"])),
    "insights": _entry(
        "Full-control Insights query at any level and scope: choose fields, filters, sorts, a segment, "
        "time buckets and attribution windows. Scope comes from whichever id is given.",
        _schema({
            **_SCOPE,
            "aggregation_level": {"type": "string", "enum": list(LEVELS),
                                  "description": "Row entity; the scope's level or lower."},
            "segment": {"type": "string", "enum": list(SEGMENTS)},
            "override_segment_group_order": _list("Row entity plus segment, in grouping order."),
            **_REPORT,
            "time_granularity": {"type": "string", "enum": ["none", "hourly", "daily", "monthly"],
                                 "description": "Default daily."},
        })),
    "conversion_insights": _entry(
        "Goal conversions (click-through and view-through) and attributed purchase value per campaign, "
        "ad group or ad, optionally by country or device and with every attributed event, goal or not.",
        _schema({
            **_TIME,
            **_ATTRIBUTION,
            "aggregation_level": {"type": "string", "enum": ["campaign", "ad_group", "ad"],
                                  "description": "Default campaign."},
            "entity_ids": _list("Entities at that level. Omit for one account-wide total."),
            "time_granularity": {"type": "string", "enum": ["none", "daily"]},
            "breakdown": {"type": "string", "enum": ["country", "device"]},
            "include_event_details": _b("Add per-event counts and values (standard and custom events)."),
            "event_names": _list("Only these events in the details."),
            "include_zero_rows": _b("Keep rows with nothing attributed. Default true."),
        })),

    # --- audiences
    "list_custom_audiences": _entry(
        "Custom audiences with status and privacy-safe size ranges. Set intended_use to list only those "
        "eligible for inclusion, exclusion or bid multipliers.",
        _schema({
            "intended_use": {"type": "string", "enum": ["inclusion", "exclusion", "bid_multiplier"]},
            "custom_audience_ids": _list("Check only these audiences."),
            "granular_counts": _b("Finer count ranges for large audiences."),
            "limit": _i("1-100. Default 50."),
            "after": _s("Cursor."),
        })),
    "get_custom_audience": _entry(
        "One audience: processing status, size ranges and membership_revision.",
        _schema({"custom_audience_id": _s("Audience id."),
                 "granular_counts": _b("Finer count ranges.")}, ["custom_audience_id"])),
    "list_audience_operations": _entry(
        "The add, remove, replace and merge operations recorded for an audience.",
        _schema({"custom_audience_id": _s("Audience id."), "limit": _i("1-100. Default 20."),
                 "cursor": _s("next_cursor from the previous page.")}, ["custom_audience_id"])),
    "get_audience_operation": _entry(
        "Status of one membership operation: processing, succeeded or failed.",
        _schema({"custom_audience_id": _s("Audience id."), "operation_id": _s("Operation id.")},
                ["custom_audience_id", "operation_id"])),
    "upload_audience_file": _entry(
        "Upload a customer list (CSV with an identifier header, or TXT with one identifier per line) "
        "and get the file_id, filename, mimetype and file_size that create_custom_audience needs. "
        "First-party data you have the right to use only.",
        _schema({"content": _s("The file's text, UTF-8."),
                 "filename": _s("Ends in .csv or .txt.")}, ["content", "filename"]), _WRITE),
    "create_custom_audience": _entry(
        "Create a custom audience from an uploaded file, or empty to fill later. Not available for "
        "campaigns targeting the EEA or Switzerland.",
        _schema({
            "name": _s("At least three characters."),
            "description": _s("Description."),
            "file_id": _s("From upload_audience_file; omit for an empty audience."),
            "filename": _s("From upload_audience_file."),
            "mimetype": _s("From upload_audience_file."),
            "file_size": _i("From upload_audience_file."),
            "identifier_type": {"type": "string", "enum": list(IDENTIFIER_TYPES),
                                "description": "Single-type files. Default email."},
            "auto_resolve_identifiers": _b("For CSVs mixing identifier columns."),
            **_IDEM,
        }, ["name"]), _WRITE),
    "add_audience_members": _entry(
        "Add people to an audience from a file_id or up to 10,000 inline identifiers. Asynchronous: "
        "poll get_audience_operation.",
        _schema({
            "custom_audience_id": _s("Audience id."),
            "file_id": _s("From upload_audience_file."),
            "identifiers": _objs("[{identifier_type, identifier}] - email, phone, email_sha256, "
                                 "phone_number_sha256 or gaid."),
            "identifier_type": {"type": "string", "enum": list(IDENTIFIER_TYPES)},
            "auto_resolve_identifiers": _b("For CSVs mixing identifier columns."),
            "expected_revision": _i("Optional membership_revision guard."),
            **_IDEM,
        }, ["custom_audience_id"]), _WRITE),
    "remove_audience_members": _entry(
        "Remove people from an audience, from a file_id or inline identifiers. Asynchronous.",
        _schema({
            "custom_audience_id": _s("Audience id."),
            "file_id": _s("From upload_audience_file."),
            "identifiers": _objs("[{identifier_type, identifier}]."),
            "identifier_type": {"type": "string", "enum": list(IDENTIFIER_TYPES)},
            "auto_resolve_identifiers": _b("For CSVs mixing identifier columns."),
            "expected_revision": _i("Optional membership_revision guard."),
            **_IDEM,
        }, ["custom_audience_id"]), _WRITE),
    "replace_audience_members": _entry(
        "Replace an audience's whole membership with an uploaded file, keeping its id and campaign links. "
        "The current revision is read automatically unless given.",
        _schema({
            "custom_audience_id": _s("Audience id."),
            "file_id": _s("The full desired list, from upload_audience_file."),
            "identifier_type": {"type": "string", "enum": list(IDENTIFIER_TYPES)},
            "auto_resolve_identifiers": _b("For CSVs mixing identifier columns."),
            "expected_revision": _i("membership_revision to guard against."),
            **_IDEM,
        }, ["custom_audience_id", "file_id"]), _WRITE),
    "merge_custom_audiences": _entry(
        "Merge 2-64 ready audiences into a new, independent audience.",
        _schema({"name": _s("Name for the merged audience."),
                 "custom_audience_ids": _list("Source audience ids."), **_IDEM},
                ["name", "custom_audience_ids"]), _WRITE),
    "archive_custom_audience": _entry(
        "Archive an audience permanently. It can no longer be targeted.",
        _schema({"custom_audience_id": _s("Audience id.")}, ["custom_audience_id"]), _WRITE),
    "resume_audience_operation": _entry(
        "Resume an interrupted add or remove operation from where it stopped.",
        _schema({"custom_audience_id": _s("Audience id."), "operation_id": _s("Operation id.")},
                ["custom_audience_id", "operation_id"]), _WRITE),
    "cancel_audience_operation": _entry(
        "Cancel an add or remove operation before it starts applying changes.",
        _schema({"custom_audience_id": _s("Audience id."), "operation_id": _s("Operation id.")},
                ["custom_audience_id", "operation_id"]), _WRITE),

    # --- conversions
    "list_conversion_event_settings": _entry(
        "Conversion definitions in the account: event type, source, attribution window and the "
        "campaigns using each.",
        _schema(dict(_PAGE))),
    "recent_pixel_events": _entry(
        "Up to 50 events a pixel sent in the last 15 minutes, for checking an install. Not for reporting.",
        _schema({"pixel_id": _s("The pixel_id from create_pixel.")}, ["pixel_id"])),
    "create_pixel": _entry(
        "Create a web Measurement Pixel. Returns the source id (for event settings) and the pixel_id "
        "(for the website snippet).",
        _schema({"name": _s("3-1000 characters."), **_IDEM}, ["name"]), _WRITE),
    "create_conversion_event_setting": _entry(
        "Define a conversion (e.g. order_created, or a custom event) on one pixel source, so campaigns "
        "can report and optimize on it.",
        _schema({
            "name": _s("Display name, e.g. Purchases."),
            "event_type": _s("A supported event such as order_created, or custom."),
            "custom_event_name": _s("Required when event_type is custom."),
            "source_id": _s("The pixel's source id (clidsrc_...) from create_pixel."),
            **_IDEM,
        }, ["name", "event_type", "source_id"]), _WRITE),

    # --- product feeds
    "list_product_feeds": _entry(
        "Product feeds in the account, with the count of ads-eligible products.",
        _schema({"include_product_count": _b("Default true.")})),
    "list_feed_uploads": _entry(
        "Catalog upload history: status, rows accepted and rejected, and diagnostics.",
        _schema({"feed_id": _s("Only this feed's uploads.")})),
    "query_feed_products": _entry(
        "Preview which feed products a set of filters matches, before using them in an ad group.",
        _schema({
            "feed_id": _s("Feed id."),
            "filters": _objs("[{field, operator (in, gt, gte, lt, lte), values:[strings]}]; [] for all."),
            "limit": _i("1-500. Default 20."),
        }, ["feed_id"])),
    "create_product_feed": _entry(
        "Create a product feed for the countries a catalog supports.",
        _schema({"name": _s("Feed name."), "countries": _list("ISO codes, e.g. ['US']."), **_IDEM},
                ["name", "countries"]), _WRITE),
    "archive_product_feed": _entry(
        "Archive a feed no campaign or ad group still uses.",
        _schema({"feed_id": _s("Feed id.")}, ["feed_id"]), _WRITE),
    "set_feed_sftp_access": _entry(
        "Configure SFTP catalog uploads with an SSH public key, or pause / reactivate SFTP access. "
        "Password access is not offered because it would expose the password.",
        _schema({
            "feed_id": _s("Feed id."),
            "action": {"type": "string", "enum": ["configure_ssh_key", "pause", "activate"]},
            "ssh_public_key": _s("The full public key, for configure_ssh_key."),
        }, ["feed_id", "action"]), _WRITE),
    "update_feed_products": _entry(
        "Delta API: change existing products' price (integer minor units) or availability without "
        "re-uploading the catalog.",
        _schema({
            "feed_id": _s("Feed id."),
            "products": _objs("[{id, variants:[{id, price:{amount, currency}, availability:{status}}]}]."),
        }, ["feed_id", "products"]), _WRITE),

    # --- bulk, audit, locations
    "submit_bulk_job": _entry(
        "Create or update up to 1,000 campaigns, ad groups and ads in one asynchronous job (limited "
        "preview). Use validate_only to check first.",
        _schema({
            "operations": _objs("[{operation_id, type, idempotency_key?, target_resource_id?, input}]; "
                                "types campaign/ad_group/ad .create or .update."),
            "validate_only": _b("Check without changing anything."),
            "partial_failure": _b("Keep going after an error. Default true."),
            **_IDEM,
        }, ["operations"]), _WRITE),
    "get_bulk_job": _entry(
        "A bulk job's status: pending, in_progress, completed, partially_failed or failed.",
        _schema({"job_id": _s("Job id.")}, ["job_id"])),
    "list_bulk_job_operations": _entry(
        "Per-operation results of a bulk job, with resource ids and errors.",
        _schema({"job_id": _s("Job id."), "limit": _i("1-100. Default 100."), "after": _s("Cursor.")},
                ["job_id"])),
    "list_audit_logs": _entry(
        "Who changed what: the audit trail of campaign, ad group, ad and account changes, with "
        "before/after values.",
        _schema({
            "campaign_id": _s("Changes to this campaign and its children."),
            "ad_group_id": _s("Changes to this ad group and its ads."),
            "ad_id": _s("Changes to this ad."),
            "actor_id": _s("Changes made by this actor."),
            "start_time": _s("Unix seconds or YYYY-MM-DD."),
            "end_time": _s("Unix seconds or YYYY-MM-DD."),
            "limit": _i("1-100. Default 50."),
            "after": _s("Cursor."),
            "before": _s("Cursor."),
            "order": {"type": "string", "enum": ["asc", "desc"]},
        })),
    "search_locations": _entry(
        "Find country, region and market ids for campaign location targeting.",
        _schema({"query": _s("Place name, e.g. California."), "limit": _i("1-50. Default 10.")},
                ["query"])),
}

HANDLERS = {
    "get_ad_account": get_ad_account,
    "list_spend_limits": list_spend_limits,
    "update_account_brand": update_account_brand,
    "pause_account": pause_account,
    "activate_account": activate_account,
    "create_spend_limit_window": create_spend_limit_window,
    "update_spend_limit_window": update_spend_limit_window,
    "delete_spend_limit_window": delete_spend_limit_window,
    "set_daily_spend_limit": set_daily_spend_limit,
    "remove_daily_spend_limit": remove_daily_spend_limit,
    "account_structure": account_structure,
    "list_campaigns": list_campaigns,
    "get_campaign": get_campaign,
    "create_campaign": create_campaign,
    "update_campaign": update_campaign,
    "set_campaign_status": set_campaign_status,
    "list_ad_groups": list_ad_groups,
    "get_ad_group": get_ad_group,
    "create_ad_group": create_ad_group,
    "update_ad_group": update_ad_group,
    "set_ad_group_status": set_ad_group_status,
    "list_ads": list_ads,
    "get_ad": get_ad,
    "preview_ad": preview_ad,
    "create_ad": create_ad,
    "update_ad": update_ad,
    "set_ad_status": set_ad_status,
    "upload_image": upload_image,
    "account_performance": account_performance,
    "campaign_performance": campaign_performance,
    "ad_group_performance": ad_group_performance,
    "ad_performance": ad_performance,
    "performance_by_segment": performance_by_segment,
    "insights": insights,
    "conversion_insights": conversion_insights,
    "list_custom_audiences": list_custom_audiences,
    "get_custom_audience": get_custom_audience,
    "list_audience_operations": list_audience_operations,
    "get_audience_operation": get_audience_operation,
    "upload_audience_file": upload_audience_file,
    "create_custom_audience": create_custom_audience,
    "add_audience_members": add_audience_members,
    "remove_audience_members": remove_audience_members,
    "replace_audience_members": replace_audience_members,
    "merge_custom_audiences": merge_custom_audiences,
    "archive_custom_audience": archive_custom_audience,
    "resume_audience_operation": resume_audience_operation,
    "cancel_audience_operation": cancel_audience_operation,
    "list_conversion_event_settings": list_conversion_event_settings,
    "recent_pixel_events": recent_pixel_events,
    "create_pixel": create_pixel,
    "create_conversion_event_setting": create_conversion_event_setting,
    "list_product_feeds": list_product_feeds,
    "list_feed_uploads": list_feed_uploads,
    "query_feed_products": query_feed_products,
    "create_product_feed": create_product_feed,
    "archive_product_feed": archive_product_feed,
    "set_feed_sftp_access": set_feed_sftp_access,
    "update_feed_products": update_feed_products,
    "submit_bulk_job": submit_bulk_job,
    "get_bulk_job": get_bulk_job,
    "list_bulk_job_operations": list_bulk_job_operations,
    "list_audit_logs": list_audit_logs,
    "search_locations": search_locations,
}

registry.register(
    Connector(
        slug=SLUG,
        label="ChatGPT Ads (OpenAI)",
        auth="api_key",
        cred_fields=["api_key"],
        description=(
            "Runs ChatGPT Ads through the OpenAI Ads API: campaigns, ad groups and ads, performance "
            "and conversion reports by product, country, device and platform, custom audiences, "
            "pixels, product feeds, spending limits, bulk jobs and the audit log. Write tools start "
            "switched off."
        ),
        category="Advertising",
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
