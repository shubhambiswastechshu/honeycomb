"""YouTube connector — YouTube Data API v3 (full read access).

Comprehensive read access to public YouTube data: search, video & channel details
and statistics, a channel's uploads, playlist items, comments, trending videos and
video categories.

Auth: api_key. The user pastes a **YouTube Data API v3 key** (free, from Google Cloud
Console -> APIs & Services -> Credentials -> "API key", with the "YouTube Data API v3"
enabled). The key is sent as the `key` query parameter on every call. The free quota is
10,000 units/day, which is ample for interactive use.

Docs: https://developers.google.com/youtube/v3/docs

Tools:
  search           : search videos / channels / playlists
  video_details    : full snippet + statistics + contentDetails for video id(s)
  channel_details  : channel info + statistics (by id, @handle, or username)
  channel_videos   : a channel's most recent uploads
  playlist_items   : items in a playlist
  video_comments   : top-level comments on a video
  trending         : most-popular videos for a region (optionally by category)
  video_categories : assignable video categories for a region
"""
from connectors import registry
from connectors.registry import Connector
from connections.models import Connection
from connectors.shims.errors import ConnectorError
from connectors.shims.http import get as http_get, UpstreamUnavailable
from connectors.shims.cache import cached, TTL_SHORT, TTL_MEDIUM, TTL_LONG
from connectors.shims.concurrency import limit_for

BASE = "https://www.googleapis.com/youtube/v3"
_HEADERS = {"Accept": "application/json"}


def _key(conn: Connection) -> str:
    key = (conn.creds() or {}).get("api_key") or (conn.creds() or {}).get("key")
    if not key:
        raise ConnectorError(
            "Not connected: missing YouTube Data API key. Add it in the connector settings "
            "(get one free at Google Cloud Console with the YouTube Data API v3 enabled)."
        )
    return key


def _max(args: dict, default: int = 10) -> int:
    try:
        n = int(args.get("max_results") or args.get("limit") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, 50))


def _ids(args: dict, *keys, label: str) -> str:
    for k in keys:
        v = args.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x).strip() for x in v if str(x).strip())
        v = str(v).strip()
        if v:
            return v
    raise ConnectorError(f"`{label}` is required.")


async def _api(conn: Connection, path: str, params: dict) -> dict:
    params = {**params, "key": _key(conn)}
    url = f"{BASE}/{path}"
    try:
        async with limit_for(url):
            res = await http_get(url, headers=_HEADERS, params=params)
    except UpstreamUnavailable as e:
        raise ConnectorError(str(e))
    if res.status_code == 403:
        # Quota or key problem — surface YouTube's reason.
        reason = res.text[:300]
        raise ConnectorError(f"YouTube rejected the request (403). Often quota exceeded or key/restriction issue: {reason}")
    if res.status_code == 400:
        raise ConnectorError(f"Bad YouTube request (400): {res.text[:300]}")
    if res.status_code >= 400:
        raise ConnectorError(f"YouTube error {res.status_code}: {res.text[:300]}")
    try:
        return res.json()
    except ValueError:
        raise ConnectorError(f"YouTube returned non-JSON: {res.text[:300]}")


def _thumb(snippet: dict) -> str | None:
    th = (snippet or {}).get("thumbnails") or {}
    for size in ("medium", "high", "default", "standard", "maxres"):
        if th.get(size):
            return th[size].get("url")
    return None


# ============================================================
# Tools
# ============================================================

async def search(conn: Connection, db, args: dict) -> dict:
    """Search YouTube for videos, channels or playlists."""
    q = (args.get("query") or args.get("q") or "").strip()
    if not q:
        raise ConnectorError("`query` is required.")
    kind = (args.get("type") or "video").strip().lower()
    if kind not in ("video", "channel", "playlist"):
        raise ConnectorError("`type` must be one of: video, channel, playlist.")
    order = (args.get("order") or "relevance").strip()
    max_results = _max(args)

    async def _loader():
        data = await _api(conn, "search", {
            "part": "snippet", "q": q, "type": kind, "order": order, "maxResults": max_results,
        })
        rows = []
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            idobj = it.get("id", {})
            rid = idobj.get("videoId") or idobj.get("channelId") or idobj.get("playlistId")
            link = (
                f"https://www.youtube.com/watch?v={idobj.get('videoId')}" if idobj.get("videoId")
                else f"https://www.youtube.com/channel/{idobj.get('channelId')}" if idobj.get("channelId")
                else f"https://www.youtube.com/playlist?list={idobj.get('playlistId')}" if idobj.get("playlistId")
                else None
            )
            rows.append({
                "type": kind, "id": rid, "title": sn.get("title"),
                "channel": sn.get("channelTitle"), "channel_id": sn.get("channelId"),
                "published": sn.get("publishedAt"), "description": sn.get("description"),
                "thumbnail": _thumb(sn), "url": link,
            })
        return {"query": q, "type": kind, "count": len(rows), "results": rows}

    return await cached("youtube", conn.id, "search", TTL_SHORT, _loader, args={"q": q, "type": kind, "order": order, "n": max_results})


async def video_details(conn: Connection, db, args: dict) -> dict:
    """Full details + statistics for one or more videos (comma-separated ids or a list)."""
    ids = _ids(args, "video_id", "video_ids", "id", "ids", label="video_id")

    async def _loader():
        data = await _api(conn, "videos", {"part": "snippet,statistics,contentDetails", "id": ids})
        rows = []
        for it in data.get("items", []):
            sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
            rows.append({
                "id": it.get("id"), "title": sn.get("title"), "channel": sn.get("channelTitle"),
                "channel_id": sn.get("channelId"), "published": sn.get("publishedAt"),
                "duration": cd.get("duration"), "tags": sn.get("tags", []),
                "views": st.get("viewCount"), "likes": st.get("likeCount"), "comments": st.get("commentCount"),
                "description": sn.get("description"), "thumbnail": _thumb(sn),
                "url": f"https://www.youtube.com/watch?v={it.get('id')}",
            })
        return {"count": len(rows), "videos": rows}

    return await cached("youtube", conn.id, "video_details", TTL_MEDIUM, _loader, args={"ids": ids})


async def channel_details(conn: Connection, db, args: dict) -> dict:
    """Channel info + subscriber/view counts. Accepts channel_id, @handle, or username."""
    channel_id = args.get("channel_id") or args.get("id")
    handle = args.get("handle")
    username = args.get("username")
    if not (channel_id or handle or username):
        # Allow a bare 'channel' value and route it sensibly.
        ch = (args.get("channel") or "").strip()
        if ch.startswith("@"):
            handle = ch
        elif ch.startswith("UC") and len(ch) >= 20:
            channel_id = ch
        elif ch:
            handle = ch
        else:
            raise ConnectorError("Provide `channel_id`, `handle` (@name) or `username`.")
    if handle and not str(handle).startswith("@"):
        handle = "@" + str(handle)

    params = {"part": "snippet,statistics,contentDetails,brandingSettings"}
    if channel_id:
        params["id"] = channel_id
    elif handle:
        params["forHandle"] = handle
    else:
        params["forUsername"] = username

    async def _loader():
        data = await _api(conn, "channels", params)
        items = data.get("items", [])
        if not items:
            return {"found": False, "note": "No channel found."}
        it = items[0]
        sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
        return {
            "found": True, "id": it.get("id"), "title": sn.get("title"),
            "description": sn.get("description"), "custom_url": sn.get("customUrl"),
            "published": sn.get("publishedAt"), "country": sn.get("country"),
            "subscribers": st.get("subscriberCount"), "views": st.get("viewCount"),
            "videos": st.get("videoCount"),
            "uploads_playlist": (cd.get("relatedPlaylists") or {}).get("uploads"),
            "thumbnail": _thumb(sn),
            "url": f"https://www.youtube.com/channel/{it.get('id')}",
        }

    return await cached("youtube", conn.id, "channel_details", TTL_MEDIUM, _loader, args=params)


async def channel_videos(conn: Connection, db, args: dict) -> dict:
    """A channel's most recent uploads. Accepts channel_id or @handle."""
    channel_id = args.get("channel_id") or args.get("id")
    handle = args.get("handle") or (args.get("channel") if str(args.get("channel") or "").startswith("@") else None)
    max_results = _max(args)

    async def _loader():
        # Resolve the uploads playlist for the channel.
        cparams = {"part": "contentDetails"}
        if channel_id:
            cparams["id"] = channel_id
        elif handle:
            cparams["forHandle"] = handle if str(handle).startswith("@") else "@" + str(handle)
        else:
            raise ConnectorError("Provide `channel_id` or `handle`.")
        ch = await _api(conn, "channels", cparams)
        items = ch.get("items", [])
        if not items:
            return {"found": False, "note": "Channel not found."}
        uploads = (items[0].get("contentDetails", {}).get("relatedPlaylists") or {}).get("uploads")
        if not uploads:
            return {"found": False, "note": "No uploads playlist."}
        pl = await _api(conn, "playlistItems", {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": max_results})
        rows = []
        for it in pl.get("items", []):
            sn = it.get("snippet", {})
            vid = (it.get("contentDetails") or {}).get("videoId")
            rows.append({
                "video_id": vid, "title": sn.get("title"), "published": sn.get("publishedAt"),
                "thumbnail": _thumb(sn), "url": f"https://www.youtube.com/watch?v={vid}" if vid else None,
            })
        return {"found": True, "channel_id": items[0].get("id"), "count": len(rows), "videos": rows}

    return await cached("youtube", conn.id, "channel_videos", TTL_SHORT, _loader, args={"cid": channel_id, "handle": handle, "n": max_results})


async def playlist_items(conn: Connection, db, args: dict) -> dict:
    """List the videos in a playlist."""
    playlist_id = _ids(args, "playlist_id", "id", label="playlist_id")
    max_results = _max(args)

    async def _loader():
        data = await _api(conn, "playlistItems", {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": max_results})
        rows = []
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            vid = (it.get("contentDetails") or {}).get("videoId")
            rows.append({
                "video_id": vid, "title": sn.get("title"), "channel": sn.get("videoOwnerChannelTitle"),
                "published": sn.get("publishedAt"), "thumbnail": _thumb(sn),
                "url": f"https://www.youtube.com/watch?v={vid}" if vid else None,
            })
        return {"playlist_id": playlist_id, "count": len(rows), "videos": rows}

    return await cached("youtube", conn.id, "playlist_items", TTL_MEDIUM, _loader, args={"pid": playlist_id, "n": max_results})


async def video_comments(conn: Connection, db, args: dict) -> dict:
    """Top-level comments on a video (newest or most relevant first)."""
    video_id = _ids(args, "video_id", "id", label="video_id")
    order = (args.get("order") or "relevance").strip()
    max_results = _max(args)

    async def _loader():
        data = await _api(conn, "commentThreads", {
            "part": "snippet", "videoId": video_id, "order": order, "maxResults": max_results, "textFormat": "plainText",
        })
        rows = []
        for it in data.get("items", []):
            top = (((it.get("snippet") or {}).get("topLevelComment") or {}).get("snippet")) or {}
            rows.append({
                "author": top.get("authorDisplayName"), "text": top.get("textDisplay"),
                "likes": top.get("likeCount"), "published": top.get("publishedAt"),
                "replies": (it.get("snippet") or {}).get("totalReplyCount"),
            })
        return {"video_id": video_id, "count": len(rows), "comments": rows}

    return await cached("youtube", conn.id, "video_comments", TTL_SHORT, _loader, args={"vid": video_id, "order": order, "n": max_results})


async def trending(conn: Connection, db, args: dict) -> dict:
    """Most-popular videos for a region, optionally filtered by category id."""
    region = (args.get("region_code") or args.get("region") or "US").strip().upper()
    category = args.get("category_id")
    max_results = _max(args)

    async def _loader():
        params = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": region, "maxResults": max_results}
        if category:
            params["videoCategoryId"] = str(category)
        data = await _api(conn, "videos", params)
        rows = []
        for it in data.get("items", []):
            sn, st = it.get("snippet", {}), it.get("statistics", {})
            rows.append({
                "id": it.get("id"), "title": sn.get("title"), "channel": sn.get("channelTitle"),
                "published": sn.get("publishedAt"), "views": st.get("viewCount"), "likes": st.get("likeCount"),
                "url": f"https://www.youtube.com/watch?v={it.get('id')}",
            })
        return {"region": region, "count": len(rows), "videos": rows}

    return await cached("youtube", conn.id, "trending", TTL_SHORT, _loader, args={"region": region, "cat": category, "n": max_results})


async def video_categories(conn: Connection, db, args: dict) -> dict:
    """Assignable video categories for a region (use the ids with `trending`)."""
    region = (args.get("region_code") or args.get("region") or "US").strip().upper()

    async def _loader():
        data = await _api(conn, "videoCategories", {"part": "snippet", "regionCode": region})
        rows = [{"id": it.get("id"), "title": (it.get("snippet") or {}).get("title")} for it in data.get("items", [])]
        return {"region": region, "count": len(rows), "categories": rows}

    return await cached("youtube", conn.id, "video_categories", TTL_LONG, _loader, args={"region": region})


# ============================================================
# Catalog
# ============================================================

CATALOG = {
    "search": {
        "description": "Search YouTube for videos, channels or playlists. Returns title, channel, date, thumbnail and link.",
        "input": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text."},
                "type": {"type": "string", "enum": ["video", "channel", "playlist"], "default": "video"},
                "order": {"type": "string", "description": "relevance | date | rating | viewCount | title (default relevance)."},
                "max_results": {"type": "integer", "description": "1–50 (default 10)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "video_details": {
        "description": "Full details + statistics (views, likes, comments, duration, tags) for one or more video ids.",
        "input": {
            "type": "object",
            "properties": {"video_id": {"type": "string", "description": "Video id, or comma-separated ids."}},
            "required": ["video_id"],
            "additionalProperties": False,
        },
    },
    "channel_details": {
        "description": "Channel info + subscriber/view/video counts. Accepts channel_id, @handle, or username.",
        "input": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Channel id (UC...)."},
                "handle": {"type": "string", "description": "Channel handle, e.g. @MrBeast."},
                "username": {"type": "string", "description": "Legacy username."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "channel_videos": {
        "description": "A channel's most recent uploads. Accepts channel_id or @handle.",
        "input": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Channel id (UC...)."},
                "handle": {"type": "string", "description": "Channel handle, e.g. @MrBeast."},
                "max_results": {"type": "integer", "description": "1–50 (default 10)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "playlist_items": {
        "description": "List the videos inside a playlist.",
        "input": {
            "type": "object",
            "properties": {
                "playlist_id": {"type": "string", "description": "Playlist id (PL...)."},
                "max_results": {"type": "integer", "description": "1–50 (default 10)."},
            },
            "required": ["playlist_id"],
            "additionalProperties": False,
        },
    },
    "video_comments": {
        "description": "Top-level comments on a video, with author, text, likes and reply count.",
        "input": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string", "description": "Video id."},
                "order": {"type": "string", "description": "relevance | time (default relevance)."},
                "max_results": {"type": "integer", "description": "1–50 (default 10)."},
            },
            "required": ["video_id"],
            "additionalProperties": False,
        },
    },
    "trending": {
        "description": "Most-popular (trending) videos for a region, optionally filtered by category id.",
        "input": {
            "type": "object",
            "properties": {
                "region_code": {"type": "string", "description": "ISO region, e.g. US, IN, GB (default US)."},
                "category_id": {"type": "string", "description": "Optional category id from video_categories."},
                "max_results": {"type": "integer", "description": "1–50 (default 10)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "video_categories": {
        "description": "List assignable video categories for a region (ids feed into `trending`).",
        "input": {
            "type": "object",
            "properties": {"region_code": {"type": "string", "description": "ISO region, e.g. US, IN (default US)."}},
            "required": [],
            "additionalProperties": False,
        },
    },
}

HANDLERS = {
    "search": search,
    "video_details": video_details,
    "channel_details": channel_details,
    "channel_videos": channel_videos,
    "playlist_items": playlist_items,
    "video_comments": video_comments,
    "trending": trending,
    "video_categories": video_categories,
}

registry.register(
    Connector(
        slug="youtube",
        label="YouTube",
        auth="api_key",
        cred_fields=["api_key"],
        description="Reads public YouTube data through the Data API v3 - search, video and channel details and statistics, channel uploads, playlist items, comments and trending videos.",
        category="Content",
        catalog=CATALOG,
        handlers=HANDLERS,
    )
)
