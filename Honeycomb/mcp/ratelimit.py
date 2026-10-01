"""Per-credential call budget for the MCP data plane.

/mcp/** sits outside DRF, so none of its throttles apply there. Without a cap,
one key could fire unlimited parallel tools/call requests: each one can hold an
upstream connection for the whole tool timeout, spend the tenant's quota at the
provider (and get their account throttled or banned there), and write an
activity row.

A fixed one-minute window per credential, counted in the shared cache (Redis
in production, so every worker sees the same counter). It FAILS OPEN: a cache
outage must never stop a legitimate call, so any cache error means "allowed".
"""
import time

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache

DEFAULT_CALLS_PER_MINUTE = 120


def calls_per_minute():
    return int(getattr(settings, 'HONEYCOMB_MCP_CALLS_PER_MINUTE', DEFAULT_CALLS_PER_MINUTE))


def _count(key):
    # add() is a no-op when the key exists, so the window starts at 0 exactly
    # once; incr() is atomic on Redis and on LocMemCache.
    cache.add(key, 0, 90)
    return cache.incr(key)


async def allow(credential):
    """True if ``credential`` (an McpKey or OAuthToken row) may make another call."""
    limit = calls_per_minute()
    if limit <= 0:
        return True
    window = int(time.time() // 60)
    key = 'mcp:rl:{0}:{1}:{2}'.format(type(credential).__name__, credential.pk, window)
    try:
        used = await sync_to_async(_count, thread_sensitive=False)(key)
    except Exception:  # noqa: BLE001 - fail open, see the module docstring
        return True
    return used <= limit
