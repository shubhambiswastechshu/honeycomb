"""Meta Business Suite connector — Facebook Pages, Instagram, the business and its ads.

Everything one Meta token with these permissions can reach, as MCP tools:

  ads_read, ads_management, business_management, pages_show_list,
  pages_read_engagement, pages_read_user_content, read_insights,
  instagram_basic, instagram_manage_insights

There is no "Business Suite API". Business Suite is a UI over several Graph APIs,
and this connector calls those directly:

* Pages API -- page profile, page and post insights, posts, comments, reviews,
  tagged posts, videos and video insights, scheduled posts, the inbox.
* Instagram Graph API -- the business account linked to a page: profile,
  insights, audience demographics, when followers are online, media, stories,
  tagged media, comments, and public business accounts by username.
* Business Manager -- businesses and the pages, ad accounts, Instagram accounts
  and people each one owns.
* Marketing API -- every reporting tool of the Meta Ads connector (accounts,
  structure, insights and breakdowns), reused rather than copied, plus ad
  management: pausing, renaming and re-budgeting campaigns, ad sets and ads, and
  creating a campaign (always paused).

Every tool carries the Meta permission it needs and a group, which is how the
dashboard lays out the per-connection switches. The four ad-management tools are
marked write: they start switched off on every connection and the dashboard asks
before turning one on. Publishing to a Page and replying to comments need
pages_manage_posts / pages_manage_engagement, which are not in the list above, so
they are not here.

Auth is one pasted user or system-user access token (auth="api_key"), the same
shape as Meta Ads. Page endpoints need a *page* token, so the connector exchanges
the user token for per-page tokens via /me/accounts and holds them in process
memory only -- never in the shared cache, which is not a place for credentials.
A token that is already a Page token also works: /me then resolves to the page.

Permissions the token needs: pages_show_list, pages_read_engagement,
read_insights, pages_read_user_content, instagram_basic,
instagram_manage_insights, and pages_messaging for the inbox tool.

Metric names are Meta's and they change: Meta retired a large set of page
metrics in 2025. Every insights tool therefore takes a `metrics` argument that is
passed straight through, ships defaults that are current, and surfaces Graph's own
error text when a metric has been retired -- so a deprecation is a readable
message, not a mystery.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from connections.models import Connection
from connectors import registry
from connectors.catalog import meta_ads as _ads
from connectors.registry import Connector
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get, post as http_post

BASE = "https://graph.facebook.com/v23.0"

#: Page insights accept at most ~90 days between since and until.
MAX_DAYS = 90
DEFAULT_DAYS = 28
#: Instagram's follower_count series only reaches back 30 days, and Meta rejects
#: a `since` older than that. The window starts at midnight UTC, so a 30-day cap
#: puts `since` 30 days *plus however much of today has passed* in the past --
#: over the limit on every call after 00:00. 29 keeps it inside.
IG_FOLLOWER_DAYS = 29

MAX_LIMIT = 100
DEFAULT_LIMIT = 25

DEFAULT_PAGE_METRICS = "page_follows,page_post_engagements,page_media_view"
DEFAULT_POST_METRICS = "post_media_view,post_clicks,post_reactions_by_type_total"
DEFAULT_IG_METRICS = "reach,accounts_engaged,total_interactions,views"
DEFAULT_MEDIA_METRICS = "reach,views,saved,shares,total_interactions"

#: Page tokens, keyed by (connection id, page id). Process memory only, and short
#: lived: a revoked token must stop working within minutes, not hours.
_PAGE_TOKENS: dict[tuple[Any, str], tuple[str, float]] = {}
_PAGE_TOKEN_TTL = 600.0


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _user_token(conn: Connection) -> str:
    token = (conn.creds() or {}).get("access_token")
    if not token:
        raise ConnectorError("Not connected: missing access_token.")
    return token


async def _get(path_or_url: str, token: str, params: dict | None = None) -> dict:
    """GET on the Graph API, with Meta's structured error surfaced readably.

    The token is sent as a query parameter, as Graph expects. It is never placed
    in an error message: Graph does not echo it back, and nothing here adds it.
    """
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    query = dict(params or {})
    query["access_token"] = token
    try:
        async with limit_for(url):
            res = await http_get(url, params=query)
    except UpstreamUnavailable as exc:
        raise ConnectorError("Meta API unavailable: {0}".format(exc))
    _raise_for(res)
    return res.json() or {}


async def _post(path: str, token: str, data: dict, *, retry: bool = True) -> dict:
    """POST on the Graph API -- used only by the ad-management tools.

    `retry=False` for anything that creates: the shared client retries a 5xx, and
    retrying a create that actually succeeded upstream would make two campaigns.
    Updates set a field to a value, so repeating one is harmless.
    """
    url = BASE + path
    body = {k: v for k, v in data.items() if v is not None}
    body["access_token"] = token
    try:
        async with limit_for(url):
            res = await http_post(url, data=body, retries=2 if retry else 0)
    except UpstreamUnavailable as exc:
        raise ConnectorError("Meta API unavailable: {0}".format(exc))
    _raise_for(res)
    return res.json() or {}


def _raise_for(res) -> None:
    """Turn a Graph error response into a readable ConnectorError."""
    if res.status_code < 400:
        return
    message, code = None, "?"
    try:
        err = (res.json() or {}).get("error") or {}
        message = err.get("error_user_msg") or err.get("message")
        code = err.get("code", "?")
    except ValueError:
        pass
    if code in (10, 200, 294, 3):
        raise ConnectorError(
            "Meta refused that for missing permissions ({0}). Each tool lists the "
            "permission it needs on the connection's Tools tab in Honeycomb; check "
            "the token was granted it.".format(message or "code {0}".format(code))
        )
    raise ConnectorError("Meta API {0}: {1}".format(
        res.status_code, message or res.text[:300]))


async def _paged(path: str, token: str, params: dict, limit: int) -> list[dict]:
    """Collect up to `limit` rows, following Graph's cursors."""
    rows: list[dict] = []
    query = dict(params)
    query["limit"] = min(limit, MAX_LIMIT)
    data = await _get(path, token, query)
    rows.extend(data.get("data") or [])
    while len(rows) < limit:
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        # `next` is a complete URL that already carries the token.
        try:
            async with limit_for(nxt):
                res = await http_get(nxt)
        except UpstreamUnavailable:
            break
        if res.status_code >= 400:
            break
        data = res.json() or {}
        rows.extend(data.get("data") or [])
    return rows[:limit]


# --------------------------------------------------------------------------- #
# Resolution: which page, which Instagram account, which token
# --------------------------------------------------------------------------- #
async def _pages(conn: Connection) -> list[dict]:
    """Pages this token manages, with their page tokens.

    Falls back to treating the token as a Page token when /me/accounts is empty or
    refused -- a Page token has no pages of its own, but /me is the page.
    """
    token = _user_token(conn)
    fields = ("id,name,category,access_token,fan_count,followers_count,link,"
              "instagram_business_account{id,username}")
    try:
        pages = await _paged("/me/accounts", token, {"fields": fields}, 100)
    except ConnectorError:
        pages = []
    if not pages:
        me = await _get("/me", token, {
            "fields": "id,name,category,fan_count,followers_count,link,"
                      "instagram_business_account{id,username}"})
        if me.get("id"):
            me["access_token"] = token
            pages = [me]
    now = time.monotonic()
    for page in pages:
        if page.get("id") and page.get("access_token"):
            _PAGE_TOKENS[(conn.id, str(page["id"]))] = (page["access_token"], now + _PAGE_TOKEN_TTL)
    return pages


async def _page(conn: Connection, args: dict) -> tuple[str, str]:
    """(page id, page token) for a call: the caller's page, else the saved one, else the first."""
    wanted = str((args or {}).get("page_id") or (conn.creds() or {}).get("page_id") or "").strip()
    if wanted:
        cached = _PAGE_TOKENS.get((conn.id, wanted))
        if cached and cached[1] > time.monotonic():
            return wanted, cached[0]
    pages = await _pages(conn)
    if not pages:
        raise ConnectorError(
            "This token manages no Facebook Pages. It needs pages_show_list and at "
            "least one Page it can administer."
        )
    if wanted:
        match = next((p for p in pages if str(p.get("id")) == wanted), None)
        if match is None:
            raise ConnectorError("This token cannot manage page {0}. Use list_pages.".format(wanted))
    else:
        match = pages[0]
    return str(match["id"]), match["access_token"]


async def _instagram(conn: Connection, args: dict) -> tuple[str, str]:
    """(instagram user id, token) -- the caller's account, else the first linked one."""
    wanted = str((args or {}).get("ig_user_id") or (conn.creds() or {}).get("ig_user_id") or "").strip()
    pages = await _pages(conn)
    for page in pages:
        ig = page.get("instagram_business_account") or {}
        if ig.get("id") and (not wanted or str(ig["id"]) == wanted):
            return str(ig["id"]), page.get("access_token") or _user_token(conn)
    if wanted:
        raise ConnectorError(
            "No Page on this token is linked to Instagram account {0}.".format(wanted))
    raise ConnectorError(
        "None of this token's Pages has an Instagram business account linked. Link "
        "one in Meta Business Suite, and make sure the token has instagram_basic."
    )


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int((args or {}).get(key, default))))
    except (TypeError, ValueError):
        return default


def _window(args: dict, max_days: int = MAX_DAYS) -> tuple[int, int, str, str]:
    """(since, until) as unix seconds, from since/until dates or a day count."""
    args = args or {}
    today = datetime.now(timezone.utc).date()

    def parse(value: Any) -> date | None:
        try:
            return date.fromisoformat(str(value)[:10]) if value else None
        except ValueError:
            raise ConnectorError("Dates must be YYYY-MM-DD, got {0!r}.".format(value))

    until = parse(args.get("until")) or today
    since = parse(args.get("since")) or until - timedelta(days=_int(args, "days", DEFAULT_DAYS, 1, max_days))
    if since > until:
        raise ConnectorError("since must be on or before until.")
    if (until - since).days > max_days:
        since = until - timedelta(days=max_days)
    to_ts = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return to_ts(since), to_ts(until + timedelta(days=1)), since.isoformat(), until.isoformat()


def _metric_rows(data: dict) -> list[dict]:
    """Graph insights -> [{name, title, period, total, values:[{date, value}]}].

    Handles both shapes Graph returns: a time series in `values`, and the
    `total_value` block Instagram uses for metric_type=total_value. A value can
    be a dict (a breakdown by reaction type, say); those are kept as-is and left
    out of the total rather than summed into nonsense.
    """
    out = []
    for item in data.get("data") or []:
        series = []
        total = 0
        numeric = False
        for point in item.get("values") or []:
            value = point.get("value")
            series.append({"date": str(point.get("end_time") or "")[:10], "value": value})
            if isinstance(value, (int, float)):
                total += value
                numeric = True
        block = item.get("total_value")
        if isinstance(block, dict) and isinstance(block.get("value"), (int, float)):
            total, numeric = block["value"], True
        out.append({
            "name": item.get("name"),
            "title": item.get("title"),
            "period": item.get("period"),
            "total": total if numeric else None,
            "values": series,
        })
    return out


def _required(args: dict, key: str) -> str:
    value = str((args or {}).get(key) or "").strip()
    if not value:
        raise ConnectorError("{0} is required.".format(key))
    return value


def _count(obj: Any) -> int | None:
    """`reactions.summary(true)` and friends -> the total_count."""
    if isinstance(obj, dict):
        summary = obj.get("summary") or {}
        if isinstance(summary.get("total_count"), int):
            return summary["total_count"]
        if isinstance(obj.get("count"), int):
            return obj["count"]
    return None


# --------------------------------------------------------------------------- #
# Facebook Pages
# --------------------------------------------------------------------------- #
async def list_pages(conn: Connection, db, args: dict) -> dict:
    pages = await _pages(conn)
    return {"count": len(pages), "pages": [{
        "id": p.get("id"),
        "name": p.get("name"),
        "category": p.get("category"),
        "followers": p.get("followers_count"),
        "likes": p.get("fan_count"),
        "instagram": (p.get("instagram_business_account") or {}).get("username"),
        "link": p.get("link"),
    } for p in pages]}


async def page_overview(conn: Connection, db, args: dict) -> dict:
    page_id, token = await _page(conn, args)
    data = await _get("/" + page_id, token, {
        "fields": "id,name,category,about,fan_count,followers_count,link,website,"
                  "verification_status,rating_count,overall_star_rating,"
                  "instagram_business_account{id,username,followers_count,media_count}"})
    ig = data.get("instagram_business_account") or {}
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "category": data.get("category"),
        "followers": data.get("followers_count"),
        "likes": data.get("fan_count"),
        "rating": data.get("overall_star_rating"),
        "rating_count": data.get("rating_count"),
        "verification": data.get("verification_status"),
        "website": data.get("website"),
        "link": data.get("link"),
        "about": data.get("about"),
        "instagram_username": ig.get("username"),
        "instagram_followers": ig.get("followers_count"),
        "instagram_media_count": ig.get("media_count"),
    }


async def page_insights(conn: Connection, db, args: dict) -> dict:
    page_id, token = await _page(conn, args)
    since, until, since_day, until_day = _window(args)
    metrics = str((args or {}).get("metrics") or DEFAULT_PAGE_METRICS)
    data = await _get("/{0}/insights".format(page_id), token, {
        "metric": metrics,
        "period": str((args or {}).get("period") or "day"),
        "since": since, "until": until,
    })
    return {"page_id": page_id, "since": since_day, "until": until_day,
            "metrics": _metric_rows(data)}


async def page_posts(conn: Connection, db, args: dict) -> dict:
    page_id, token = await _page(conn, args)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/posts".format(page_id), token, {
        "fields": "id,message,created_time,permalink_url,status_type,"
                  "shares,reactions.limit(0).summary(true),comments.limit(0).summary(true)",
    }, limit)
    return {"page_id": page_id, "count": len(rows), "posts": [{
        "id": r.get("id"),
        "created": r.get("created_time"),
        "message": (r.get("message") or "")[:280],
        "type": r.get("status_type"),
        "reactions": _count(r.get("reactions")),
        "comments": _count(r.get("comments")),
        "shares": _count(r.get("shares")),
        "url": r.get("permalink_url"),
    } for r in rows]}


async def post_insights(conn: Connection, db, args: dict) -> dict:
    post_id = _required(args, "post_id")
    # A post id is "<page>_<post>"; its page token is the one that can read it.
    page_hint = post_id.split("_", 1)[0] if "_" in post_id else None
    _, token = await _page(conn, dict(args or {}, page_id=(args or {}).get("page_id") or page_hint))
    data = await _get("/{0}/insights".format(post_id), token, {
        "metric": str((args or {}).get("metrics") or DEFAULT_POST_METRICS)})
    return {"post_id": post_id, "metrics": _metric_rows(data)}


async def post_comments(conn: Connection, db, args: dict) -> dict:
    post_id = _required(args, "post_id")
    page_hint = post_id.split("_", 1)[0] if "_" in post_id else None
    _, token = await _page(conn, dict(args or {}, page_id=(args or {}).get("page_id") or page_hint))
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/comments".format(post_id), token, {
        "fields": "id,message,created_time,like_count,comment_count,from{name}",
        "order": "reverse_chronological",
    }, limit)
    return {"post_id": post_id, "count": len(rows), "comments": [{
        "id": r.get("id"),
        "created": r.get("created_time"),
        "author": (r.get("from") or {}).get("name"),
        "message": r.get("message"),
        "likes": r.get("like_count"),
        "replies": r.get("comment_count"),
    } for r in rows]}


async def scheduled_posts(conn: Connection, db, args: dict) -> dict:
    """The content calendar: what is queued to publish."""
    page_id, token = await _page(conn, args)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/scheduled_posts".format(page_id), token, {
        "fields": "id,message,scheduled_publish_time,created_time,permalink_url",
    }, limit)

    def when(value: Any) -> str | None:
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            return value

    rows.sort(key=lambda r: r.get("scheduled_publish_time") or 0)
    return {"page_id": page_id, "count": len(rows), "scheduled": [{
        "id": r.get("id"),
        "publishes_at": when(r.get("scheduled_publish_time")),
        "message": (r.get("message") or "")[:280],
        "created": r.get("created_time"),
    } for r in rows]}


async def page_conversations(conn: Connection, db, args: dict) -> dict:
    """The inbox: recent Messenger or Instagram Direct threads."""
    page_id, token = await _page(conn, args)
    platform = str((args or {}).get("platform") or "messenger").lower()
    if platform not in ("messenger", "instagram"):
        raise ConnectorError("platform must be 'messenger' or 'instagram'.")
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/conversations".format(page_id), token, {
        "platform": platform,
        "fields": "id,updated_time,message_count,unread_count,snippet,participants",
    }, limit)
    return {"page_id": page_id, "platform": platform, "count": len(rows),
            "unread_threads": sum(1 for r in rows if (r.get("unread_count") or 0) > 0),
            "conversations": [{
                "id": r.get("id"),
                "updated": r.get("updated_time"),
                "with": ", ".join(
                    p.get("name") or p.get("username") or ""
                    for p in ((r.get("participants") or {}).get("data") or [])
                    if str(p.get("id")) != page_id
                ),
                "messages": r.get("message_count"),
                "unread": r.get("unread_count"),
                "snippet": (r.get("snippet") or "")[:200],
            } for r in rows]}


# --------------------------------------------------------------------------- #
# Instagram
# --------------------------------------------------------------------------- #
async def list_instagram_accounts(conn: Connection, db, args: dict) -> dict:
    pages = await _pages(conn)
    accounts = []
    for page in pages:
        ig = page.get("instagram_business_account") or {}
        if ig.get("id"):
            accounts.append({"id": ig.get("id"), "username": ig.get("username"),
                             "page_id": page.get("id"), "page_name": page.get("name")})
    return {"count": len(accounts), "accounts": accounts}


async def instagram_overview(conn: Connection, db, args: dict) -> dict:
    ig_id, token = await _instagram(conn, args)
    data = await _get("/" + ig_id, token, {
        "fields": "id,username,name,biography,website,followers_count,follows_count,media_count"})
    return {
        "id": data.get("id"),
        "username": data.get("username"),
        "name": data.get("name"),
        "followers": data.get("followers_count"),
        "following": data.get("follows_count"),
        "media_count": data.get("media_count"),
        "website": data.get("website"),
        "bio": data.get("biography"),
    }


async def instagram_insights(conn: Connection, db, args: dict) -> dict:
    ig_id, token = await _instagram(conn, args)
    since, until, since_day, until_day = _window(args)
    data = await _get("/{0}/insights".format(ig_id), token, {
        "metric": str((args or {}).get("metrics") or DEFAULT_IG_METRICS),
        "period": "day",
        # total_value is what reach / accounts_engaged / views require since v22;
        # pass metric_type=time_series to get a daily series where Meta allows it.
        "metric_type": str((args or {}).get("metric_type") or "total_value"),
        "since": since, "until": until,
    })
    return {"ig_user_id": ig_id, "since": since_day, "until": until_day,
            "metrics": _metric_rows(data)}


async def instagram_media(conn: Connection, db, args: dict) -> dict:
    ig_id, token = await _instagram(conn, args)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/media".format(ig_id), token, {
        "fields": "id,caption,media_type,media_product_type,timestamp,permalink,"
                  "like_count,comments_count",
    }, limit)
    return {"ig_user_id": ig_id, "count": len(rows), "media": [{
        "id": r.get("id"),
        "posted": r.get("timestamp"),
        "type": r.get("media_product_type") or r.get("media_type"),
        "caption": (r.get("caption") or "")[:280],
        "likes": r.get("like_count"),
        "comments": r.get("comments_count"),
        "url": r.get("permalink"),
    } for r in rows]}


async def media_insights(conn: Connection, db, args: dict) -> dict:
    media_id = _required(args, "media_id")
    _, token = await _instagram(conn, args)
    data = await _get("/{0}/insights".format(media_id), token, {
        "metric": str((args or {}).get("metrics") or DEFAULT_MEDIA_METRICS)})
    return {"media_id": media_id, "metrics": _metric_rows(data)}


async def media_comments(conn: Connection, db, args: dict) -> dict:
    media_id = _required(args, "media_id")
    _, token = await _instagram(conn, args)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/comments".format(media_id), token, {
        "fields": "id,text,username,timestamp,like_count,replies{id}",
    }, limit)
    return {"media_id": media_id, "count": len(rows), "comments": [{
        "id": r.get("id"),
        "posted": r.get("timestamp"),
        "author": r.get("username"),
        "text": r.get("text"),
        "likes": r.get("like_count"),
        "replies": len(((r.get("replies") or {}).get("data")) or []),
    } for r in rows]}


# --------------------------------------------------------------------------- #
# Cross-platform
# --------------------------------------------------------------------------- #
async def follower_growth(conn: Connection, db, args: dict) -> dict:
    """Daily follower movement on the Page and its Instagram account, side by side.

    Each platform is fetched independently and reports its own error, because a
    token with Page access but no Instagram permission is common -- and one
    missing half should not hide the half that works.
    """
    result: dict[str, Any] = {}

    try:
        page_id, token = await _page(conn, args)
        since, until, since_day, until_day = _window(args)
        data = await _get("/{0}/insights".format(page_id), token, {
            "metric": "page_follows,page_daily_follows_unique,page_daily_unfollows_unique",
            "period": "day", "since": since, "until": until})
        result["facebook"] = {"page_id": page_id, "since": since_day, "until": until_day,
                              "metrics": _metric_rows(data)}
    except ConnectorError as exc:
        result["facebook"] = {"error": str(exc)}

    try:
        ig_id, token = await _instagram(conn, args)
        since, until, since_day, until_day = _window(args, max_days=IG_FOLLOWER_DAYS)
        data = await _get("/{0}/insights".format(ig_id), token, {
            "metric": "follower_count", "period": "day", "since": since, "until": until})
        result["instagram"] = {"ig_user_id": ig_id, "since": since_day, "until": until_day,
                               "note": "Instagram keeps only the last 30 days of this series.",
                               "metrics": _metric_rows(data)}
    except ConnectorError as exc:
        result["instagram"] = {"error": str(exc)}

    return result


# --------------------------------------------------------------------------- #
# Facebook Pages: reviews, mentions, video
# --------------------------------------------------------------------------- #
DEFAULT_VIDEO_METRICS = ("total_video_views,total_video_impressions,"
                         "total_video_avg_time_watched,total_video_complete_views")


async def page_reviews(conn: Connection, db, args: dict) -> dict:
    """Recommendations and reviews left on the Page (pages_read_user_content)."""
    page_id, token = await _page(conn, args)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/ratings".format(page_id), token, {
        "fields": "created_time,recommendation_type,review_text,rating,has_rating,has_review",
    }, limit)
    return {
        "page_id": page_id,
        "count": len(rows),
        "recommend": sum(1 for r in rows if r.get("recommendation_type") == "positive"),
        "do_not_recommend": sum(1 for r in rows if r.get("recommendation_type") == "negative"),
        "reviews": [{
            "created": r.get("created_time"),
            "recommends": {"positive": True, "negative": False}.get(r.get("recommendation_type")),
            "rating": r.get("rating"),
            "text": r.get("review_text"),
        } for r in rows],
    }


async def page_tagged_posts(conn: Connection, db, args: dict) -> dict:
    """Posts by other people and Pages that tag this Page."""
    page_id, token = await _page(conn, args)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/tagged".format(page_id), token, {
        "fields": "id,message,created_time,from{id,name},permalink_url",
    }, limit)
    return {"page_id": page_id, "count": len(rows), "posts": [{
        "id": r.get("id"),
        "created": r.get("created_time"),
        "by": (r.get("from") or {}).get("name"),
        "message": (r.get("message") or "")[:280],
        "url": r.get("permalink_url"),
    } for r in rows]}


async def page_videos(conn: Connection, db, args: dict) -> dict:
    page_id, token = await _page(conn, args)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    rows = await _paged("/{0}/videos".format(page_id), token, {
        "fields": "id,title,description,created_time,length,permalink_url",
    }, limit)
    return {"page_id": page_id, "count": len(rows), "videos": [{
        "id": r.get("id"),
        "created": r.get("created_time"),
        "title": r.get("title"),
        "description": (r.get("description") or "")[:280],
        "seconds": r.get("length"),
        "url": ("https://www.facebook.com" + r["permalink_url"])
               if str(r.get("permalink_url") or "").startswith("/") else r.get("permalink_url"),
    } for r in rows]}


async def video_insights(conn: Connection, db, args: dict) -> dict:
    video_id = _required(args, "video_id")
    _, token = await _page(conn, args)
    data = await _get("/{0}/video_insights".format(video_id), token, {
        "metric": str((args or {}).get("metrics") or DEFAULT_VIDEO_METRICS)})
    return {"video_id": video_id, "metrics": _metric_rows(data)}


# --------------------------------------------------------------------------- #
# Instagram: stories, mentions, audience, timing, other accounts
# --------------------------------------------------------------------------- #
AUDIENCE_METRICS = ("follower_demographics", "engaged_audience_demographics",
                    "reached_audience_demographics")
AUDIENCE_BREAKDOWNS = ("age", "gender", "city", "country")
AUDIENCE_TIMEFRAMES = ("this_week", "this_month", "last_14_days", "last_30_days",
                       "last_90_days", "prev_month")
_IG_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")


async def instagram_stories(conn: Connection, db, args: dict) -> dict:
    """Stories live right now. Instagram removes them from the API after 24 hours."""
    ig_id, token = await _instagram(conn, args)
    rows = await _paged("/{0}/stories".format(ig_id), token, {
        "fields": "id,media_type,media_product_type,permalink,timestamp",
    }, _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT))
    return {"ig_user_id": ig_id, "count": len(rows),
            "note": "Story insights: media_insights with metrics reach,replies,navigation,shares.",
            "stories": [{"id": r.get("id"), "posted": r.get("timestamp"),
                         "type": r.get("media_type"), "url": r.get("permalink")} for r in rows]}


async def instagram_tagged_media(conn: Connection, db, args: dict) -> dict:
    """Posts by other accounts that tag this business account."""
    ig_id, token = await _instagram(conn, args)
    rows = await _paged("/{0}/tags".format(ig_id), token, {
        "fields": "id,caption,media_type,permalink,timestamp,username,like_count,comments_count",
    }, _int(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT))
    return {"ig_user_id": ig_id, "count": len(rows), "media": [{
        "id": r.get("id"),
        "posted": r.get("timestamp"),
        "by": r.get("username"),
        "caption": (r.get("caption") or "")[:280],
        "likes": r.get("like_count"),
        "comments": r.get("comments_count"),
        "url": r.get("permalink"),
    } for r in rows]}


async def instagram_audience(conn: Connection, db, args: dict) -> dict:
    """Who the followers (or the engaged / reached audience) are, by one dimension."""
    ig_id, token = await _instagram(conn, args)
    args = args or {}
    metric = str(args.get("metric") or "follower_demographics")
    if metric not in AUDIENCE_METRICS:
        raise ConnectorError("metric must be one of: {0}.".format(", ".join(AUDIENCE_METRICS)))
    breakdown = str(args.get("breakdown") or "age")
    if breakdown not in AUDIENCE_BREAKDOWNS:
        raise ConnectorError("breakdown must be one of: {0}.".format(", ".join(AUDIENCE_BREAKDOWNS)))
    params = {"metric": metric, "period": "lifetime", "metric_type": "total_value",
              "breakdown": breakdown}
    timeframe = args.get("timeframe")
    if timeframe or metric != "follower_demographics":
        timeframe = str(timeframe or "this_month")
        if timeframe not in AUDIENCE_TIMEFRAMES:
            raise ConnectorError("timeframe must be one of: {0}.".format(", ".join(AUDIENCE_TIMEFRAMES)))
        params["timeframe"] = timeframe
    data = await _get("/{0}/insights".format(ig_id), token, params)

    segments = []
    for item in data.get("data") or []:
        for block in ((item.get("total_value") or {}).get("breakdowns") or []):
            for result in block.get("results") or []:
                segments.append({"segment": " / ".join(str(v) for v in result.get("dimension_values") or []),
                                 "value": result.get("value")})
    segments.sort(key=lambda s: -(s["value"] or 0))
    total = sum(s["value"] or 0 for s in segments)
    for s in segments:
        s["share"] = round(100 * (s["value"] or 0) / total, 1) if total else None
    return {"ig_user_id": ig_id, "metric": metric, "breakdown": breakdown,
            "timeframe": params.get("timeframe"), "total": total, "segments": segments,
            "note": "Instagram only reports demographics for accounts with 100+ followers."}


async def instagram_online_followers(conn: Connection, db, args: dict) -> dict:
    """When followers are on Instagram: the average count online per hour of day."""
    ig_id, token = await _instagram(conn, args)
    since, until, since_day, until_day = _window(dict(args or {}, days=(args or {}).get("days") or 7),
                                                 max_days=IG_FOLLOWER_DAYS)
    data = await _get("/{0}/insights".format(ig_id), token, {
        "metric": "online_followers", "period": "lifetime", "since": since, "until": until})
    sums: dict[int, float] = {}
    days = 0
    for item in data.get("data") or []:
        for point in item.get("values") or []:
            value = point.get("value")
            if isinstance(value, dict) and value:
                days += 1
                for hour, count in value.items():
                    try:
                        sums[int(hour)] = sums.get(int(hour), 0) + float(count or 0)
                    except (TypeError, ValueError):
                        continue
    hours = [{"hour": h, "average_online": round(sums[h] / days)} for h in sorted(sums)] if days else []
    best = sorted(hours, key=lambda h: -h["average_online"])[:3]
    return {"ig_user_id": ig_id, "since": since_day, "until": until_day, "days": days,
            "timezone": "Pacific Time (Instagram reports these hours in PT)",
            "best_hours": [h["hour"] for h in best], "hours": hours}


async def instagram_business_discovery(conn: Connection, db, args: dict) -> dict:
    """Public profile and recent posts of another Instagram business or creator account."""
    username = str((args or {}).get("username") or "").strip().lstrip("@")
    if not _IG_USERNAME.match(username):
        raise ConnectorError("username must be an Instagram handle: letters, numbers, '.' and '_' only.")
    ig_id, token = await _instagram(conn, args)
    count = _int(args, "media_limit", 12, 0, 50)
    media = ",media.limit({0}){{id,caption,like_count,comments_count,timestamp,permalink,media_product_type}}".format(count) if count else ""
    data = await _get("/" + ig_id, token, {
        "fields": "business_discovery.username({0}){{username,name,biography,website,"
                  "followers_count,follows_count,media_count{1}}}".format(username, media)})
    found = data.get("business_discovery") or {}
    posts = [{
        "id": m.get("id"),
        "posted": m.get("timestamp"),
        "type": m.get("media_product_type"),
        "caption": (m.get("caption") or "")[:280],
        "likes": m.get("like_count"),
        "comments": m.get("comments_count"),
        "url": m.get("permalink"),
    } for m in ((found.get("media") or {}).get("data") or [])]
    followers = found.get("followers_count") or 0
    engagement = [(p["likes"] or 0) + (p["comments"] or 0) for p in posts]
    return {
        "username": found.get("username"),
        "name": found.get("name"),
        "followers": found.get("followers_count"),
        "following": found.get("follows_count"),
        "media_count": found.get("media_count"),
        "website": found.get("website"),
        "bio": found.get("biography"),
        "average_engagement": round(sum(engagement) / len(engagement)) if engagement else None,
        "engagement_rate_percent": round(100 * sum(engagement) / len(engagement) / followers, 2)
        if engagement and followers else None,
        "recent_posts": posts,
    }


# --------------------------------------------------------------------------- #
# Business Manager
# --------------------------------------------------------------------------- #
async def _business_id(conn: Connection, args: dict) -> tuple[str, str]:
    token = _user_token(conn)
    wanted = str((args or {}).get("business_id") or "").strip()
    if wanted:
        return wanted, token
    rows = await _paged("/me/businesses", token, {"fields": "id,name"}, 1)
    if not rows:
        raise ConnectorError("This token belongs to no Business Manager. It needs business_management.")
    return str(rows[0]["id"]), token


async def _edge(path: str, token: str, fields: str, limit: int = 100) -> dict:
    """One edge, fetched on its own so a refused edge cannot hide the others."""
    try:
        rows = await _paged(path, token, {"fields": fields}, limit)
        return {"count": len(rows), "items": rows}
    except ConnectorError as exc:
        return {"error": str(exc)}


async def business_assets(conn: Connection, db, args: dict) -> dict:
    """What a business owns and manages for clients: Pages, ad accounts, Instagram, catalogs."""
    business_id, token = await _business_id(conn, args)
    base = "/" + business_id
    return {
        "business_id": business_id,
        "owned_pages": await _edge(base + "/owned_pages", token, "id,name,category"),
        "client_pages": await _edge(base + "/client_pages", token, "id,name,category"),
        "owned_ad_accounts": await _edge(base + "/owned_ad_accounts", token, "id,name,account_status,currency"),
        "client_ad_accounts": await _edge(base + "/client_ad_accounts", token, "id,name,account_status,currency"),
        "instagram_accounts": await _edge(base + "/owned_instagram_accounts", token, "id,username"),
        "product_catalogs": await _edge(base + "/owned_product_catalogs", token, "id,name,product_count"),
    }


async def business_people(conn: Connection, db, args: dict) -> dict:
    """People and system users with access to a business, and invitations not yet accepted."""
    business_id, token = await _business_id(conn, args)
    base = "/" + business_id
    return {
        "business_id": business_id,
        "people": await _edge(base + "/business_users", token, "id,name,role"),
        "system_users": await _edge(base + "/system_users", token, "id,name,role"),
        "pending_invitations": await _edge(base + "/pending_users", token, "id,email,role"),
    }


# --------------------------------------------------------------------------- #
# Ad management (ads_management) -- the only tools here that change anything
# --------------------------------------------------------------------------- #
AD_STATUSES = ("ACTIVE", "PAUSED")
OBJECTIVES = ("OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT", "OUTCOME_LEADS",
              "OUTCOME_APP_PROMOTION", "OUTCOME_SALES")
SPECIAL_CATEGORIES = ("CREDIT", "EMPLOYMENT", "HOUSING", "ISSUES_ELECTIONS_POLITICS",
                      "ONLINE_GAMBLING_AND_GAMING", "FINANCIAL_PRODUCTS_SERVICES")
_OBJECT_ID = re.compile(r"^\d{5,25}$")


def _object_id(args: dict, key: str) -> str:
    value = _required(args, key)
    if not _OBJECT_ID.match(value):
        raise ConnectorError("{0} must be a numeric Meta id.".format(key))
    return value


def _budget(args: dict, key: str) -> int | None:
    raw = (args or {}).get(key)
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConnectorError("{0} must be a whole number in the account's minor currency unit "
                             "(cents, paise).".format(key))
    if value <= 0:
        raise ConnectorError("{0} must be greater than zero.".format(key))
    return value


def _changes(args: dict, budgets: bool) -> dict:
    args = args or {}
    changes: dict[str, Any] = {}
    if args.get("status") not in (None, ""):
        status = str(args["status"]).upper()
        if status not in AD_STATUSES:
            raise ConnectorError("status must be ACTIVE or PAUSED.")
        changes["status"] = status
    if args.get("name") not in (None, ""):
        changes["name"] = str(args["name"]).strip()[:400]
    if budgets:
        daily, lifetime = _budget(args, "daily_budget"), _budget(args, "lifetime_budget")
        if daily and lifetime:
            raise ConnectorError("Set daily_budget or lifetime_budget, not both.")
        if daily:
            changes["daily_budget"] = daily
        if lifetime:
            changes["lifetime_budget"] = lifetime
    if not changes:
        raise ConnectorError("Nothing to change. Pass status, name" + (" or a budget." if budgets else "."))
    return changes


async def _update(conn: Connection, kind: str, key: str, args: dict, budgets: bool) -> dict:
    object_id = _object_id(args, key)
    changes = _changes(args, budgets)
    token = _user_token(conn)
    fields = "name,status,effective_status" + (",daily_budget,lifetime_budget" if budgets else "")
    before = await _get("/" + object_id, token, {"fields": fields})
    result = await _post("/" + object_id, token, changes)
    return {
        "id": object_id,
        "type": kind,
        "success": bool(result.get("success", True)),
        "changed": changes,
        "before": {k: before.get(k) for k in fields.split(",")},
        "note": "Budgets are in the ad account's minor currency unit (cents, paise).",
    }


async def update_campaign(conn: Connection, db, args: dict) -> dict:
    return await _update(conn, "campaign", "campaign_id", args, budgets=True)


async def update_ad_set(conn: Connection, db, args: dict) -> dict:
    return await _update(conn, "ad_set", "ad_set_id", args, budgets=True)


async def update_ad(conn: Connection, db, args: dict) -> dict:
    return await _update(conn, "ad", "ad_id", args, budgets=False)


async def create_campaign(conn: Connection, db, args: dict) -> dict:
    """Create a campaign -- always PAUSED, so nothing can spend until a person turns it on."""
    args = args or {}
    name = _required(args, "name")[:400]
    objective = str(args.get("objective") or "").upper()
    if objective not in OBJECTIVES:
        raise ConnectorError("objective must be one of: {0}.".format(", ".join(OBJECTIVES)))
    categories = args.get("special_ad_categories") or []
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(",") if c.strip()]
    categories = [str(c).upper() for c in categories]
    bad = [c for c in categories if c not in SPECIAL_CATEGORIES]
    if bad:
        raise ConnectorError("Unknown special_ad_categories: {0}.".format(", ".join(bad)))
    daily, lifetime = _budget(args, "daily_budget"), _budget(args, "lifetime_budget")
    if daily and lifetime:
        raise ConnectorError("Set daily_budget or lifetime_budget, not both.")

    act = await _ads._act_id(conn, args)
    data: dict[str, Any] = {
        "name": name,
        "objective": objective,
        "status": "PAUSED",
        "buying_type": "AUCTION",
        "special_ad_categories": json.dumps(categories),
    }
    if daily:
        data["daily_budget"] = daily
    elif lifetime:
        data["lifetime_budget"] = lifetime
    else:
        # Without a campaign budget, the budget lives on each ad set, and Graph
        # requires this flag to say so.
        data["is_adset_budget_sharing_enabled"] = "false"
    result = await _post("/{0}/campaigns".format(act), _user_token(conn), data, retry=False)
    return {
        "id": result.get("id"),
        "account_id": act,
        "status": "PAUSED",
        "created": {k: v for k, v in data.items() if k != "special_ad_categories"},
        "note": "Created paused. It spends nothing until it has ad sets and ads and is set "
                "to ACTIVE with update_campaign.",
    }


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
_PAGE = {"type": "string", "description": "Facebook Page id. Defaults to the first Page on the token."}
_IG = {"type": "string", "description": "Instagram business account id. Defaults to the first one linked."}
_LIMIT = {"type": "integer", "description": "Max rows to return (1-100)."}
_DAYS = {"type": "integer", "description": "Days back from until (1-90). Ignored when since is given."}
_SINCE = {"type": "string", "description": "Start date, YYYY-MM-DD."}
_UNTIL = {"type": "string", "description": "End date, YYYY-MM-DD. Defaults to today."}


def _schema(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [],
            "additionalProperties": False}


CATALOG = {
    # ---------- Facebook Pages ----------
    "list_pages": {
        "description": "Facebook Pages this token manages, with followers and the linked Instagram account.",
        "input": _schema({}),
    },
    "page_overview": {
        "description": "One Page's profile: followers, likes, rating, verification, website, linked Instagram.",
        "input": _schema({"page_id": _PAGE}),
    },
    "page_insights": {
        "description": "Page-level insights over a date range (follows, engagement, views by default). "
                       "Metric names are Meta's and are passed through.",
        "input": _schema({
            "page_id": _PAGE, "days": _DAYS, "since": _SINCE, "until": _UNTIL,
            "metrics": {"type": "string", "description": "Comma-separated Meta metric names."},
            "period": {"type": "string", "description": "day (default), week, days_28."},
        }),
    },
    "page_posts": {
        "description": "Recent Page posts with reaction, comment and share counts.",
        "input": _schema({"page_id": _PAGE, "limit": _LIMIT}),
    },
    "post_insights": {
        "description": "Insights for a single Page post.",
        "input": _schema({
            "post_id": {"type": "string", "description": "Post id, '<page>_<post>', from page_posts."},
            "page_id": _PAGE,
            "metrics": {"type": "string", "description": "Comma-separated Meta metric names."},
        }, required=["post_id"]),
    },
    "post_comments": {
        "description": "Comments on a Page post, newest first.",
        "input": _schema({
            "post_id": {"type": "string", "description": "Post id from page_posts."},
            "page_id": _PAGE, "limit": _LIMIT,
        }, required=["post_id"]),
    },
    "scheduled_posts": {
        "description": "The content calendar: posts queued to publish on the Page, soonest first.",
        "input": _schema({"page_id": _PAGE, "limit": _LIMIT}),
    },
    "page_conversations": {
        "description": "The inbox: recent Messenger or Instagram Direct threads, with unread counts.",
        "input": _schema({
            "page_id": _PAGE, "limit": _LIMIT,
            "platform": {"type": "string", "description": "messenger (default) or instagram."},
        }),
    },
    # ---------- Instagram ----------
    "list_instagram_accounts": {
        "description": "Instagram business accounts linked to this token's Pages.",
        "input": _schema({}),
    },
    "instagram_overview": {
        "description": "Instagram profile: followers, following, media count, bio, website.",
        "input": _schema({"ig_user_id": _IG}),
    },
    "instagram_insights": {
        "description": "Instagram account insights over a date range (reach, accounts engaged, "
                       "interactions, views by default).",
        "input": _schema({
            "ig_user_id": _IG, "days": _DAYS, "since": _SINCE, "until": _UNTIL,
            "metrics": {"type": "string", "description": "Comma-separated Meta metric names."},
            "metric_type": {"type": "string", "description": "total_value (default) or time_series."},
        }),
    },
    "instagram_media": {
        "description": "Recent Instagram posts, reels and carousels with like and comment counts.",
        "input": _schema({"ig_user_id": _IG, "limit": _LIMIT}),
    },
    "media_insights": {
        "description": "Insights for a single Instagram post or reel.",
        "input": _schema({
            "media_id": {"type": "string", "description": "Media id from instagram_media."},
            "ig_user_id": _IG,
            "metrics": {"type": "string", "description": "Comma-separated Meta metric names."},
        }, required=["media_id"]),
    },
    "media_comments": {
        "description": "Comments on an Instagram post or reel.",
        "input": _schema({
            "media_id": {"type": "string", "description": "Media id from instagram_media."},
            "ig_user_id": _IG, "limit": _LIMIT,
        }, required=["media_id"]),
    },
    # ---------- Both ----------
    "follower_growth": {
        "description": "Daily follower movement on the Page and its Instagram account side by side. "
                       "Each half reports its own error if permissions are missing.",
        "input": _schema({"page_id": _PAGE, "ig_user_id": _IG, "days": _DAYS,
                          "since": _SINCE, "until": _UNTIL}),
    },
    # ---------- Pages: reviews, mentions, video ----------
    "page_reviews": {
        "description": "Recommendations and reviews people left on the Page, with the recommend / "
                       "don't-recommend split.",
        "input": _schema({"page_id": _PAGE, "limit": _LIMIT}),
    },
    "page_tagged_posts": {
        "description": "Posts by other people and Pages that tag this Page -- mentions.",
        "input": _schema({"page_id": _PAGE, "limit": _LIMIT}),
    },
    "page_videos": {
        "description": "Videos published on the Page, with length and link.",
        "input": _schema({"page_id": _PAGE, "limit": _LIMIT}),
    },
    "video_insights": {
        "description": "Views, impressions, average watch time and complete views for one Page video.",
        "input": _schema({
            "video_id": {"type": "string", "description": "Video id from page_videos."},
            "page_id": _PAGE,
            "metrics": {"type": "string", "description": "Comma-separated Meta video metric names."},
        }, required=["video_id"]),
    },
    # ---------- Instagram: more ----------
    "instagram_stories": {
        "description": "Instagram stories that are live now (Instagram drops them after 24 hours).",
        "input": _schema({"ig_user_id": _IG, "limit": _LIMIT}),
    },
    "instagram_tagged_media": {
        "description": "Posts by other accounts that tag this Instagram account.",
        "input": _schema({"ig_user_id": _IG, "limit": _LIMIT}),
    },
    "instagram_audience": {
        "description": "Audience demographics by age, gender, city or country: followers, or the "
                       "audience engaged or reached over a timeframe.",
        "input": _schema({
            "ig_user_id": _IG,
            "metric": {"type": "string", "description": "follower_demographics (default), "
                                                        "engaged_audience_demographics or reached_audience_demographics."},
            "breakdown": {"type": "string", "description": "age (default), gender, city or country."},
            "timeframe": {"type": "string", "description": "this_week, this_month (default for engaged/reached), "
                                                           "last_14_days, last_30_days, last_90_days, prev_month."},
        }),
    },
    "instagram_online_followers": {
        "description": "When followers are online: average followers online per hour of day, and the best hours to post.",
        "input": _schema({"ig_user_id": _IG, "days": {"type": "integer", "description": "Days to average over (1-29, default 7)."}}),
    },
    "instagram_business_discovery": {
        "description": "Look up another Instagram business or creator account by username: followers, "
                       "bio, recent posts and engagement rate. Useful for competitors.",
        "input": _schema({
            "username": {"type": "string", "description": "Instagram handle, with or without @."},
            "media_limit": {"type": "integer", "description": "Recent posts to include (0-50, default 12)."},
            "ig_user_id": _IG,
        }, required=["username"]),
    },
    # ---------- Business Manager ----------
    "business_assets": {
        "description": "What a Business Manager owns and manages for clients: Pages, ad accounts, "
                       "Instagram accounts and product catalogs.",
        "input": _schema({"business_id": {"type": "string", "description": "Business id. Defaults to the first on the token."}}),
    },
    "business_people": {
        "description": "People and system users with access to a business, their roles, and pending invitations.",
        "input": _schema({"business_id": {"type": "string", "description": "Business id. Defaults to the first on the token."}}),
    },
    # ---------- Ad management (writes) ----------
    "update_campaign": {
        "description": "Pause or activate a campaign, rename it, or change its daily or lifetime budget. "
                       "Returns the values before the change.",
        "write": True,
        "input": _schema({
            "campaign_id": {"type": "string", "description": "Campaign id from list_campaigns."},
            "status": {"type": "string", "description": "ACTIVE or PAUSED."},
            "name": {"type": "string", "description": "New name."},
            "daily_budget": {"type": "integer", "description": "Daily budget in minor currency units (cents, paise)."},
            "lifetime_budget": {"type": "integer", "description": "Lifetime budget in minor currency units."},
        }, required=["campaign_id"]),
    },
    "update_ad_set": {
        "description": "Pause or activate an ad set, rename it, or change its daily or lifetime budget. "
                       "Returns the values before the change.",
        "write": True,
        "input": _schema({
            "ad_set_id": {"type": "string", "description": "Ad set id from list_ad_sets."},
            "status": {"type": "string", "description": "ACTIVE or PAUSED."},
            "name": {"type": "string", "description": "New name."},
            "daily_budget": {"type": "integer", "description": "Daily budget in minor currency units (cents, paise)."},
            "lifetime_budget": {"type": "integer", "description": "Lifetime budget in minor currency units."},
        }, required=["ad_set_id"]),
    },
    "update_ad": {
        "description": "Pause or activate a single ad, or rename it. Returns the values before the change.",
        "write": True,
        "input": _schema({
            "ad_id": {"type": "string", "description": "Ad id from list_ads."},
            "status": {"type": "string", "description": "ACTIVE or PAUSED."},
            "name": {"type": "string", "description": "New name."},
        }, required=["ad_id"]),
    },
    "create_campaign": {
        "description": "Create a new campaign in an ad account. Always created PAUSED, so it spends "
                       "nothing until someone activates it.",
        "write": True,
        "input": _schema({
            "name": {"type": "string", "description": "Campaign name."},
            "objective": {"type": "string", "description": "OUTCOME_AWARENESS, OUTCOME_TRAFFIC, OUTCOME_ENGAGEMENT, "
                                                           "OUTCOME_LEADS, OUTCOME_APP_PROMOTION or OUTCOME_SALES."},
            "account_id": {"type": "string", "description": "Ad account id. Defaults to the token's primary account."},
            "daily_budget": {"type": "integer", "description": "Campaign daily budget in minor currency units. "
                                                               "Omit to set budgets on ad sets instead."},
            "lifetime_budget": {"type": "integer", "description": "Campaign lifetime budget in minor currency units."},
            "special_ad_categories": {"type": "array", "items": {"type": "string"},
                                      "description": "Required for credit, employment, housing, politics, "
                                                     "gambling or financial ads. Empty otherwise."},
        }, required=["name", "objective"]),
    },
}

HANDLERS = {
    "list_pages": list_pages,
    "page_overview": page_overview,
    "page_insights": page_insights,
    "page_posts": page_posts,
    "post_insights": post_insights,
    "post_comments": post_comments,
    "scheduled_posts": scheduled_posts,
    "page_conversations": page_conversations,
    "list_instagram_accounts": list_instagram_accounts,
    "instagram_overview": instagram_overview,
    "instagram_insights": instagram_insights,
    "instagram_media": instagram_media,
    "media_insights": media_insights,
    "media_comments": media_comments,
    "follower_growth": follower_growth,
    "page_reviews": page_reviews,
    "page_tagged_posts": page_tagged_posts,
    "page_videos": page_videos,
    "video_insights": video_insights,
    "instagram_stories": instagram_stories,
    "instagram_tagged_media": instagram_tagged_media,
    "instagram_audience": instagram_audience,
    "instagram_online_followers": instagram_online_followers,
    "instagram_business_discovery": instagram_business_discovery,
    "business_assets": business_assets,
    "business_people": business_people,
    "update_campaign": update_campaign,
    "update_ad_set": update_ad_set,
    "update_ad": update_ad,
    "create_campaign": create_campaign,
}

# Every reporting tool of the Meta Ads connector, reused rather than copied: same
# token, same Graph endpoints, and a fix there is a fix here. The two that overlap
# with tools above are left out -- the versions here resolve Page tokens.
_ADS_ALREADY_HERE = {"list_pages", "list_instagram_accounts"}
for _name, _entry in _ads.CATALOG.items():
    if _name not in _ADS_ALREADY_HERE:
        CATALOG[_name] = dict(_entry)
        HANDLERS[_name] = _ads.HANDLERS[_name]

#: How the dashboard groups the switches, and the permission each tool needs.
GROUPS = ("Facebook Pages", "Instagram", "Business", "Ads: accounts & setup",
          "Ads: performance", "Ads: management")
_ADS_SETUP = ("list_ad_accounts", "account_health_check", "list_campaigns", "list_ad_sets",
              "list_ads", "list_creatives", "list_audiences", "saved_audiences", "list_pixels",
              "list_custom_conversions")
_ADS_PERFORMANCE = ("account_insights", "campaign_insights", "ad_set_insights", "ad_insights",
                    "ad_creative_performance", "action_breakdown", "conversion_data",
                    "age_breakdown", "gender_breakdown", "demographic_breakdown",
                    "country_breakdown", "region_breakdown", "device_breakdown",
                    "placement_breakdown", "publisher_platform_breakdown", "hourly_breakdown",
                    "day_of_week_breakdown")
TOOL_META: dict[str, tuple[str, str]] = {
    "list_pages": ("Facebook Pages", "pages_show_list"),
    "page_overview": ("Facebook Pages", "pages_read_engagement"),
    "page_insights": ("Facebook Pages", "read_insights"),
    "page_posts": ("Facebook Pages", "pages_read_engagement"),
    "post_insights": ("Facebook Pages", "read_insights"),
    "post_comments": ("Facebook Pages", "pages_read_user_content"),
    "scheduled_posts": ("Facebook Pages", "pages_read_engagement"),
    "page_conversations": ("Facebook Pages", "pages_messaging"),
    "page_reviews": ("Facebook Pages", "pages_read_user_content"),
    "page_tagged_posts": ("Facebook Pages", "pages_read_user_content"),
    "page_videos": ("Facebook Pages", "pages_read_engagement"),
    "video_insights": ("Facebook Pages", "read_insights"),
    "follower_growth": ("Facebook Pages", "read_insights"),
    "list_instagram_accounts": ("Instagram", "instagram_basic"),
    "instagram_overview": ("Instagram", "instagram_basic"),
    "instagram_insights": ("Instagram", "instagram_manage_insights"),
    "instagram_media": ("Instagram", "instagram_basic"),
    "media_insights": ("Instagram", "instagram_manage_insights"),
    "media_comments": ("Instagram", "instagram_basic"),
    "instagram_stories": ("Instagram", "instagram_basic"),
    "instagram_tagged_media": ("Instagram", "instagram_basic"),
    "instagram_audience": ("Instagram", "instagram_manage_insights"),
    "instagram_online_followers": ("Instagram", "instagram_manage_insights"),
    "instagram_business_discovery": ("Instagram", "instagram_basic"),
    "list_business_accounts": ("Business", "business_management"),
    "business_assets": ("Business", "business_management"),
    "business_people": ("Business", "business_management"),
    "lead_form_data": ("Ads: performance", "leads_retrieval"),
    "update_campaign": ("Ads: management", "ads_management"),
    "update_ad_set": ("Ads: management", "ads_management"),
    "update_ad": ("Ads: management", "ads_management"),
    "create_campaign": ("Ads: management", "ads_management"),
}
TOOL_META.update({name: ("Ads: accounts & setup", "ads_read") for name in _ADS_SETUP})
TOOL_META.update({name: ("Ads: performance", "ads_read") for name in _ADS_PERFORMANCE})

for _name, _entry in CATALOG.items():
    _group, _permission = TOOL_META.get(_name, ("Other", ""))
    _entry["group"] = _group
    _entry["permission"] = _permission

# Ordered by group, so every client -- the dashboard and an MCP tools/list alike --
# sees Pages, then Instagram, then the business, then ads.
CATALOG = {name: CATALOG[name]
           for group in GROUPS + ("Other",)
           for name in list(CATALOG) if CATALOG[name]["group"] == group}


registry.register(Connector(
    slug="meta_business_suite",
    label="Meta Business Suite",
    auth="api_key",
    cred_fields=["access_token"],
    catalog=CATALOG,
    handlers=HANDLERS,
    description=(
        "Facebook Pages, Instagram, Business Manager and Meta ads with one token: page and "
        "post insights, reviews, videos, Instagram audience and stories, business assets, "
        "ad reporting, and ad management (pause, budgets, new paused campaigns). Switch each "
        "tool on or off per connection."
    ),
    category="Social",
))
