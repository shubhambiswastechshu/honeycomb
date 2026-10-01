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
# For a request that is not safe to replay: answers that mean "nothing was done".
_NOT_ACTED_STATUSES = {429, 503}
_TRANSIENT_ERRORS = (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)
# The request never reached the server, so sending it again cannot repeat work.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.PoolTimeout)


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
    """Issue an async request, retrying transient failures with exponential backoff.

    GET/HEAD (and any request carrying an Idempotency-Key) retry on every
    transient failure. Anything else retries only when the upstream certainly
    did NOT act: the connection never opened, or the answer was 429/503. A
    read timeout or a 500/502/504 on a POST may mean the work was done --
    retrying a refund or a "create post" there could do it twice.
    """
    client = get_client()
    headers = kwargs.get('headers') or {}
    replayable = method.upper() in ('GET', 'HEAD', 'OPTIONS') or any(
        str(k).lower() == 'idempotency-key' for k in headers)
    retry_statuses = RETRY_STATUSES if replayable else _NOT_ACTED_STATUSES
    retry_errors = _TRANSIENT_ERRORS if replayable else _NOT_SENT_ERRORS
    last_exc: Exception | None = None
    attempts = 0
    for attempt in range(retries + 1):
        attempts = attempt + 1
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code in retry_statuses and attempt < retries:
                await asyncio.sleep(backoff * (2 ** attempt))
                continue
            return resp
        except _TRANSIENT_ERRORS as e:
            last_exc = e
            if attempt < retries and isinstance(e, retry_errors):
                await asyncio.sleep(backoff * (2 ** attempt))
                continue
            break
    # NEVER put the raw URL in this message: connectors call us with the credential
    # in the query string (Meta/Graph `access_token=`, AWR export links, Graph
    # `paging.next` URLs), and this exception text is surfaced to the AI client and
    # written to the activity log. redact_url keeps the endpoint, drops the secret.
    raise UpstreamUnavailable(
        f'{method} {redact_url(url)} failed after {attempts} attempt(s): '
        f'{redact_text(last_exc)}')


async def get(url: str, **kwargs) -> httpx.Response:
    return await request('GET', url, **kwargs)


async def post(url: str, **kwargs) -> httpx.Response:
    return await request('POST', url, **kwargs)


# --------------------------------------------------------------------------- #
# Requests to tenant-chosen hosts
# --------------------------------------------------------------------------- #
# Most connectors talk to a fixed provider (googleapis.com, graph.facebook.com).
# A few talk to a host the TENANT typed in -- WordPress above all -- and that is
# a server-side request forgery surface: the request leaves from inside our
# network, so "http://169.254.169.254/..." or a Docker service name would reach
# cloud metadata or an internal service, and the error text would hand the
# body back to the caller. public_request() is the only way such a host may be
# fetched.
import ipaddress  # noqa: E402
import socket  # noqa: E402
from urllib.parse import urljoin  # noqa: E402

#: Largest response body read from a tenant-chosen host. A server that
#: streams gigabytes would otherwise exhaust a worker shared by every tenant.
PUBLIC_MAX_BYTES = 10 * 1024 * 1024
PUBLIC_MAX_REDIRECTS = 5
_REDIRECTS = {301, 302, 303, 307, 308}


class UnsafeUpstream(Exception):
    """The URL points somewhere a tenant-chosen request must not go."""


def _allow_private() -> bool:
    try:
        from django.conf import settings
        return bool(getattr(settings, 'HONEYCOMB_ALLOW_PRIVATE_UPSTREAMS', settings.DEBUG))
    except Exception:  # noqa: BLE001 - outside Django: be strict
        return False


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_multicast


async def _vet(url: str) -> tuple[httpx.URL, str]:
    """``(url, ip)`` once the URL is http(s) to a host that resolves only to public IPs."""
    try:
        parsed = httpx.URL(url)
    except Exception:  # noqa: BLE001
        raise UnsafeUpstream('That address is not a valid URL.')
    if parsed.scheme not in ('http', 'https') or not parsed.host:
        raise UnsafeUpstream('Only http and https addresses can be fetched.')
    if parsed.userinfo:
        raise UnsafeUpstream('Remove the username and password from the address.')
    host = parsed.host
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError):
            raise UnsafeUpstream('{0} could not be found.'.format(host))
        addresses = {ipaddress.ip_address(info[4][0].split('%', 1)[0]) for info in infos}
    if not addresses:
        raise UnsafeUpstream('{0} could not be found.'.format(host))
    if not _allow_private() and not all(_is_public_ip(ip) for ip in addresses):
        # Every address, not any: a name that resolves to one public and one
        # private address must not get a coin toss at the private one.
        raise UnsafeUpstream(
            '{0} resolves to a private or reserved address, which Honeycomb will not '
            'contact.'.format(host))
    return parsed, str(sorted(addresses, key=str)[0])


async def public_request(method: str, url: str, *, max_bytes: int = PUBLIC_MAX_BYTES,
                         headers: dict | None = None, **kwargs) -> httpx.Response:
    """Fetch a tenant-chosen URL safely.

    * every hop's host must resolve only to public addresses (redirects are
      followed by hand and re-checked, so a public site cannot bounce us
      inward);
    * the connection is pinned to the address that was checked, so DNS cannot
      answer differently between the check and the connect (rebinding) -- TLS
      still verifies the certificate against the real host name;
    * the body is read up to ``max_bytes`` and no further.

    Raises UnsafeUpstream for a refused address and UpstreamUnavailable for a
    network failure, like request().
    """
    client = get_client()
    current = url
    for _hop in range(PUBLIC_MAX_REDIRECTS + 1):
        parsed, ip = await _vet(current)
        pinned = parsed.copy_with(host=ip)
        hop_headers = dict(headers or {})
        hop_headers['Host'] = parsed.netloc.decode('ascii')
        extensions = {'sni_hostname': parsed.host} if parsed.scheme == 'https' else {}
        try:
            req = client.build_request(method, pinned, headers=hop_headers,
                                       extensions=extensions, **kwargs)
            resp = await client.send(req, stream=True, follow_redirects=False)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                httpx.PoolTimeout, httpx.RemoteProtocolError) as e:
            raise UpstreamUnavailable(
                '{0} {1} failed: {2}'.format(method, redact_url(current), redact_text(e)))
        try:
            if resp.status_code in _REDIRECTS and resp.headers.get('location'):
                current = urljoin(str(parsed), resp.headers['location'])
                if resp.status_code == 303 or (resp.status_code in (301, 302) and method != 'GET'):
                    method = 'GET'
                    kwargs.pop('json', None)
                    kwargs.pop('data', None)
                    kwargs.pop('content', None)
                continue
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise UpstreamUnavailable(
                        '{0} {1} returned more than {2} MB; refusing to read it.'.format(
                            method, redact_url(current), max_bytes // (1024 * 1024)))
            # aiter_bytes() already undid any Content-Encoding; leaving the header
            # on would make the new Response decode the bytes a second time.
            plain_headers = [(k, v) for k, v in resp.headers.multi_items()
                             if k.lower() not in ('content-encoding', 'content-length',
                                                  'transfer-encoding')]
            return httpx.Response(resp.status_code, headers=plain_headers,
                                  content=bytes(body), request=req)
        finally:
            await resp.aclose()
    raise UpstreamUnavailable('{0} {1} redirected too many times.'.format(method, redact_url(url)))
