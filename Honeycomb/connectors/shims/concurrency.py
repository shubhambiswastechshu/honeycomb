"""Per-host concurrency limiter so a slow upstream can't drown the event loop.

An asyncio.Semaphore per hostname caps how many requests are in flight against
one upstream at a time. Keyed by HOST rather than by connector or connection
because the resource being protected is the remote server: ten tenants pointed at
the same Graph API share its rate limit and its latency, and without this a
single stalled host could hold every slot in the shared httpx pool.

The semaphores are module-level, so they are bound to the process's one event
loop — the same assumption ``shims.http`` documents at length.
"""
import asyncio
from urllib.parse import urlparse

_DEFAULT_SLOTS = 8
_semaphores: dict[str, asyncio.Semaphore] = {}


def host_of(url: str) -> str:
    return urlparse(url).hostname or 'unknown'


def limit_for(url_or_host: str, slots: int = _DEFAULT_SLOTS) -> asyncio.Semaphore:
    host = url_or_host if '/' not in url_or_host else host_of(url_or_host)
    sem = _semaphores.get(host)
    if sem is None:
        sem = asyncio.Semaphore(slots)
        _semaphores[host] = sem
    return sem
