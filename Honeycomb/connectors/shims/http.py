"""Async HTTP client with pooled connections, timeouts, and retry on 5xx/429/network.

A single shared httpx.AsyncClient is reused across the process for connection
pooling. All upstream connector calls go through `request()`.

Why a MODULE-LEVEL client is safe here, and would not be elsewhere: an
httpx.AsyncClient binds its connection pool to the event loop that first used it,
so a client cached at module scope is a latent bug under any design that spins up
a loop per request (``asyncio.run`` inside a sync view, ``async_to_sync`` per
call) — the second loop inherits sockets belonging to a loop that is closed, and
you get sporadic "Event loop is closed" / "attached to a different loop" errors.
Honeycomb's data plane is a FastAPI app mounted inside Django's ASGI app, served
by a single long-lived uvicorn/gunicorn-worker loop per process, so there is
exactly one loop for the client's whole life. Keep it that way: if a connector is
ever called from a per-request loop, this module must switch to a per-loop client
registry first.
"""
import asyncio
import logging

import httpx

from connectors.shims.errors import redact_text, redact_url

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
RETRY_STATUSES = {429, 500, 502, 503, 504}


# Identify ourselves with a branded, browser-compatible User-Agent. The default
# httpx UA ("python-httpx/x.y") is blocked outright (403) by common WordPress
# security layers (Wordfence, Cloudflare bot-fight, mod_security), which made
# every connector request to a hardened site look like a rejected auth token.
# "Mozilla/5.0 (compatible; …)" is the standard well-behaved-bot form: it passes
# naive UA filters while still honestly identifying Honeycomb.
USER_AGENT = 'Mozilla/5.0 (compatible; Honeycomb-MCP/1.0; +https://honeycomb.a.techshu.in)'


class UpstreamError(Exception):
    """Upstream returned a non-retryable error status."""


class UpstreamUnavailable(Exception):
    """Network failure or exhausted retries."""


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=True,
            headers={'User-Agent': USER_AGENT},
        )
    return _client


async def close_client() -> None:
    """Close the shared client. Called from the ASGI app's shutdown hook."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def request(
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff: float = 0.5,
    **kwargs,
) -> httpx.Response:
    """Issue an async request, retrying transient failures with exponential backoff."""
    client = get_client()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                await asyncio.sleep(backoff * (2 ** attempt))
                continue
            return resp
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(backoff * (2 ** attempt))
                continue
    # NEVER put the raw URL in this message: connectors call us with the credential
    # in the query string (Meta/Graph `access_token=`, AWR export links, Graph
    # `paging.next` URLs), and this exception text is surfaced to the AI client and
    # written to the activity log. redact_url keeps the endpoint, drops the secret.
    raise UpstreamUnavailable(
        f'{method} {redact_url(url)} failed after {retries + 1} attempts: '
        f'{redact_text(last_exc)}')


async def get(url: str, **kwargs) -> httpx.Response:
    return await request('GET', url, **kwargs)


async def post(url: str, **kwargs) -> httpx.Response:
    return await request('POST', url, **kwargs)
