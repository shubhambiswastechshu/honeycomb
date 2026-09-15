"""Meta Business Suite connector — the organic side of Facebook Pages and Instagram.

Meta Ads already covers paid: campaigns, spend, audiences. What it cannot see is
everything a social team does without a budget -- how posts perform, whether
followers are growing, what is scheduled, what people are saying in comments and
the inbox. That is the Business Suite half, and this connector reads it.

There is no "Business Suite API". Business Suite is a UI over two Graph APIs, and
this connector calls those directly:

* Pages API -- page profile, page and post insights, posts, comments, scheduled
  posts, and the Messenger inbox.
* Instagram Graph API -- the Instagram business account linked to a page: its
  profile, account insights, media, media insights and comments.

Read-only by design. Publishing and replying are deliberately not here yet: a tool
that posts to a brand's public page is a different risk from one that reads its
analytics, and deserves to be added on purpose rather than bundled in.

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

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from connections.models import Connection
from connectors import registry
from connectors.registry import Connector
from connectors.shims.concurrency import limit_for
from connectors.shims.errors import ConnectorError
from connectors.shims.http import UpstreamUnavailable, get as http_get

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
    if res.status_code >= 400:
        message, code = None, "?"
        try:
            err = (res.json() or {}).get("error") or {}
            message = err.get("message")
            code = err.get("code", "?")
        except ValueError:
            pass
        if code == 10 or code == 200:
            raise ConnectorError(
                "Meta refused that for missing permissions ({0}). The token needs "
                "pages_read_engagement, read_insights and instagram_manage_insights; "
                "the inbox also needs pages_messaging.".format(message or "code {0}".format(code))
            )
        raise ConnectorError("Meta API {0}: {1}".format(
            res.status_code, message or res.text[:300]))
    return res.json() or {}


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
}


registry.register(Connector(
    slug="meta_business_suite",
    label="Meta Business Suite",
    auth="api_key",
    cred_fields=["access_token"],
    catalog=CATALOG,
    handlers=HANDLERS,
    description=(
        "The organic side of Facebook Pages and Instagram: page and post insights, "
        "follower growth, posts and reels, comments, the scheduled content calendar "
        "and the inbox. Read-only."
    ),
    category="Social",
))
