"""Async response cache with per-tool TTLs, backed by ``django.core.cache``.

Falcon's original talked to Redis directly; this one goes through Django's cache
framework so the deployment decides the backend (LocMemCache in development, a
shared Redis in production) without a connector ever knowing. The async signature
is kept identical to falcon's so ported connector code needs no edits — the sync
cache API is bridged with ``sync_to_async(thread_sensitive=False)`` so cache I/O
never blocks the single event loop the data plane runs on, and never queues
behind Django's sync thread.

Keys are namespaced by connector/instance/tool so one tenant's connection can
never read another's data.

The cache FAILS OPEN, always. A tool call that would have succeeded must not fail
because a cache node is unreachable, so every cache error is swallowed and the
loader runs. After a failure the cache is skipped process-wide for a cooldown
window rather than retried on every call — a dead Redis would otherwise add its
full connect timeout to every single tool call.
"""
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable

from asgiref.sync import sync_to_async
from django.core.cache import cache

logger = logging.getLogger(__name__)

TTL_SHORT = 60          # 1 min — fast-changing perf data
TTL_MEDIUM = 5 * 60     # 5 min — standard reports
TTL_LONG = 6 * 60 * 60  # 6 h  — account hierarchy, rarely changes

# How long the cache stays switched off after an error, in seconds.
_COOLDOWN = 60.0
_disabled_until = 0.0

# Cache I/O is pure network/dict work with no ORM and no thread-local state, so
# it is safe off the main sync thread — and running it there would serialise
# every connector's cache reads behind whatever Django is doing. The wrappers are
# built per call rather than at import: ``django.core.cache.cache`` is a lazy
# proxy, and touching an attribute on it resolves the configured backend, which
# must not happen while this module is being imported.
async def _cache_get(key: str) -> Any:
    return await sync_to_async(cache.get, thread_sensitive=False)(key)


async def _cache_set(key: str, value: Any, timeout: int | None) -> None:
    await sync_to_async(cache.set, thread_sensitive=False)(key, value, timeout)


def _version_key(connector: str, instance_id: Any) -> str:
    return f'mcpc:ver:{connector}:{instance_id}'


def _key(connector: str, instance_id: Any, tool: str, args: dict | None, version: int) -> str:
    """Build ``mcpc:{connector}:{instance_id}:{tool}:{hash of args}``.

    The instance's version counter is hashed in alongside the arguments. This is
    how ``invalidate()`` works on a backend with no key-scan support (memcached
    has none, and Django's cache API exposes none even on Redis): rather than
    enumerating and deleting keys, bumping the counter changes the digest of
    every key that instance will compute from then on, so the whole previous
    generation becomes unreachable at once and expires on its own TTL.
    """
    raw = json.dumps({'v': version, 'args': args or {}}, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f'mcpc:{connector}:{instance_id}:{tool}:{digest}'


def _enabled() -> bool:
    return time.monotonic() >= _disabled_until


def _disable(exc: BaseException, what: str) -> None:
    global _disabled_until
    _disabled_until = time.monotonic() + _COOLDOWN
    logger.warning('cache %s skipped, disabling for %ss: %s', what, int(_COOLDOWN), exc)


async def _version(connector: str, instance_id: Any) -> int:
    """Current generation number for one connection. Absent counter means 1.

    Stored with no expiry. If the backend evicts it anyway the counter restarts
    at 1, which can briefly resurrect entries written before the last
    invalidation — bounded by those entries' own TTL, and preferable to failing
    a tool call over a cache detail.
    """
    version = await _cache_get(_version_key(connector, instance_id))
    try:
        return int(version)
    except (TypeError, ValueError):
        return 1


async def cached(
    connector: str,
    instance_id: Any,
    tool: str,
    ttl: int,
    loader: Callable[[], Awaitable[Any]],
    args: dict | None = None,
) -> Any:
    """Return the cached value, or run ``loader()`` and cache its JSON-able result.

    Fails open: any cache error at all just runs the loader.
    """
    if not _enabled():
        return await loader()
    try:
        version = await _version(connector, instance_id)
        key = _key(connector, instance_id, tool, args, version)
        hit = await _cache_get(key)
    except Exception as exc:  # noqa: BLE001 — cache must never break a tool call
        _disable(exc, 'read')
        return await loader()
    if hit is not None:
        try:
            return json.loads(hit)
        except (TypeError, ValueError):
            pass  # A corrupt entry is a miss, not an error.
    # The loader runs OUTSIDE the try on purpose. Wrapping it would mean a failing
    # upstream got mistaken for a failing cache — the tool error would be
    # swallowed, caching switched off for a minute, and the loader (possibly a
    # write tool) run a second time.
    value = await loader()
    try:
        # Serialised on the way in so the value round-trips identically on every
        # backend — LocMemCache would otherwise hand back a live reference to the
        # same object a later caller could mutate.
        await _cache_set(key, json.dumps(value, default=str), ttl)
    except Exception as exc:  # noqa: BLE001
        _disable(exc, 'write')
    return value


async def invalidate(connector: str, instance_id: Any) -> int:
    """Drop every cached entry for one connection (e.g. after a write). Fails open.

    Returns the new generation number, or 0 if the cache was unavailable. See
    ``_key`` for why bumping a counter is the invalidation mechanism instead of
    a key scan.
    """
    if not _enabled():
        return 0
    try:
        version = await _version(connector, instance_id) + 1
        await _cache_set(_version_key(connector, instance_id), version, None)
        return version
    except Exception as exc:  # noqa: BLE001
        _disable(exc, 'invalidate')
        return 0
