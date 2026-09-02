"""Generic JSON-RPC MCP endpoint for every registered connector.

Route: /mcp/{connector}/{slug}/  -- `connector` selects the registry entry and
`slug` identifies the caller's Connection row. The paths are absolute on
purpose: the ASGI entrypoint hands this app requests whose path still begins
with /mcp/, it does not strip a mount prefix.

Transport: MCP **Streamable HTTP**. AI clients (Claude.ai, ChatGPT) speak this
over plain HTTP -- POST JSON-RPC requests, GET to probe for a server->client SSE
stream, DELETE to end a session. This server is **stateless** (request/response
only, no in-memory sessions), so the rules that keep clients happy are:

  * A JSON-RPC *notification* (no ``id``) MUST NOT get a response body -- ack 202.
  * ``ping`` is always answerable with an empty result.
  * A GET MUST get 405 (we offer no stream), never a 200 JSON body the client
    cannot parse.
  * NEVER answer anything under /mcp/ with a bare HTTP 404 -- Streamable-HTTP
    clients surface that as the dreaded **"Session terminated"**. A missing
    connection, a bad key and an unknown path all come back as a readable
    JSON-RPC error with HTTP 200 instead.

Every route is registered twice, with and without the trailing slash. Django's
APPEND_SLASH would answer a slash-less POST with a 301, and a redirected POST
loses its body -- the request would arrive here empty and the session would die.
"""
import asyncio
import json
import logging
import time

from django.conf import settings
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from connectors import registry
from connectors.shims.errors import ConnectorError, redact_exc, redact_text

from .auth import AuthError, resolve_bearer
from .models import McpActivity

logger = logging.getLogger(__name__)

# Default protocol version we advertise. We also accept (and echo back) any of
# the versions a current client may negotiate during initialize.
PROTOCOL_VERSION = '2025-03-26'
SUPPORTED_PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')

# One slow upstream must not pin a worker forever.
DEFAULT_TOOL_TIMEOUT = 45.0

_ALLOW = {'Allow': 'POST, DELETE'}


def _tool_timeout():
    return float(getattr(settings, 'HONEYCOMB_MCP_TOOL_TIMEOUT', DEFAULT_TOOL_TIMEOUT))


def _rpc(rid, result=None, error=None):
    payload = {'jsonrpc': '2.0', 'id': rid}
    if error is not None:
        payload['error'] = error
    else:
        payload['result'] = result
    return JSONResponse(payload)


def _err(rid, code, message, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return _rpc(rid, error=error)


def _tool_result(rid, text, is_error):
    return _rpc(rid, {'content': [{'type': 'text', 'text': text}], 'isError': is_error})


def _negotiate_protocol(params):
    """Echo the client's requested protocolVersion when we support it, else default."""
    requested = (params or {}).get('protocolVersion')
    return requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION


def _elapsed_ms(started):
    """Milliseconds since a time.monotonic() reading, as an int."""
    return int((time.monotonic() - started) * 1000)


async def _log_activity(connection, connector, tool, status, duration_ms,
                        error_message='', detail=None):
    """Write the audit row for one tools/call. Never raises.

    A failure to record history is not a reason to fail the call the user is
    watching, so this swallows and logs instead of propagating.
    """
    try:
        await McpActivity.objects.acreate(
            tenant_id=connection.tenant_id,
            connection=connection,
            connector=connector[:48],
            tool_name=str(tool)[:64],
            status=status,
            duration_ms=duration_ms,
            detail=detail or {},
            error_message=str(error_message)[:300],
        )
    except Exception:  # noqa: BLE001 -- the audit trail is best-effort, the call is not
        logger.exception('Failed to record McpActivity for %s.%s', connector, tool)


def build_app():
    """Build the FastAPI data-plane app. Mounted by the project's asgi.py."""
    application = FastAPI(
        title='Honeycomb MCP',
        version='0.1.0',
        # Nothing here is a browsable API; a schema endpoint under /mcp/ would
        # only be one more surface a client can trip over.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get('/mcp/{connector}/{slug}/')
    @application.get('/mcp/{connector}/{slug}')
    async def probe(connector: str, slug: str):
        # Clients open a GET (Accept: text/event-stream, sometimes */*) to probe
        # for a server->client stream; we offer none, and we hold no session, so
        # the honest answer is 405 with the methods we do serve. A 404 here is
        # read as a dead session, and a 200 JSON body is unparseable to a client
        # that asked for an event stream.
        return Response(status_code=405, headers=_ALLOW)

    @application.delete('/mcp/{connector}/{slug}/')
    @application.delete('/mcp/{connector}/{slug}')
    async def terminate(connector: str, slug: str):
        # Clients DELETE the endpoint to end a session on disconnect. We hold no
        # session state, so just acknowledge -- never error.
        return Response(status_code=200)

    @application.post('/mcp/{connector}/{slug}/')
    @application.post('/mcp/{connector}/{slug}')
    async def endpoint(connector: str, slug: str, request: Request):
        try:
            body = json.loads(await request.body() or b'{}')
        except json.JSONDecodeError:
            return _err(None, -32700, 'Parse error')
        if not isinstance(body, dict):
            # JSON-RPC batches (arrays) are not used by these clients; reject
            # cleanly rather than 500 on body.get(...).
            return _err(None, -32600, 'Invalid Request: expected a single JSON-RPC object.')

        rid = body.get('id')
        method = body.get('method')
        params = body.get('params') or {}

        # No reply body is owed for: a notification (no "id"), an explicit
        # notifications/*, or a client->server response object (has "id" but no
        # "method"). Replying to any of these with an error body is exactly what
        # tears the session down. Ack 202 with no body.
        if 'id' not in body or method is None or (
                isinstance(method, str) and method.startswith('notifications/')):
            return Response(status_code=202)

        # ping: a liveness check either peer may send at any time. Always
        # answerable, cheaply, without touching the database or auth.
        if method == 'ping':
            return _rpc(rid, {})

        spec = registry.get(connector)
        if spec is None:
            return _err(rid, -32601, "Unknown connector '{0}'.".format(connector))

        # Authenticate BEFORE resolving anything about the connection. The falcon
        # original looked the connection up first so it could say "this one was
        # disconnected"; that answers slug-existence questions for a caller who
        # holds no key at all, so here the key is the only thing that can turn a
        # slug into a connection. Failures are JSON-RPC errors at HTTP 200: a 401
        # without the WWW-Authenticate OAuth flow we do not implement only makes
        # clients drop the session.
        try:
            connection, _key = await resolve_bearer(
                request.headers.get('authorization'), connector, slug)
        except AuthError as exc:
            return _err(rid, -32001, exc.message, {'reason': exc.reason})

        if method == 'initialize':
            # Deliberately STATELESS: we issue NO Mcp-Session-Id. The client then
            # never echoes one, so we can never 404 on an unknown or expired
            # session after a restart -- the classic "Session terminated" trap. Do
            # not start validating a session id here without a durable store.
            # Only `tools` is declared: nothing here implements resources/*, and
            # advertising a capability we do not serve invites calls that fail.
            return _rpc(rid, {
                'protocolVersion': _negotiate_protocol(params),
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'honeycomb-{0}'.format(connector), 'version': '0.1.0'},
            })

        if method == 'tools/list':
            disabled = set(connection.disabled_tools or [])
            tools = []
            for name, entry in spec.catalog.items():
                if name in disabled:
                    continue
                # The [WRITE] marker is the only warning the user gets inside the
                # AI client about a tool that changes their upstream data.
                description = ('[WRITE] ' if entry.get('write') else '') + entry['description']
                tools.append({
                    'name': name,
                    'description': description,
                    'inputSchema': entry['input'],
                })
            return _rpc(rid, {'tools': tools})

        if method == 'tools/call':
            name = params.get('name')
            args = params.get('arguments') or {}
            entry = spec.catalog.get(name)
            started = time.monotonic()

            # An unknown or switched-off tool comes back as an isError *result*,
            # not a JSON-RPC error: a model that gets a protocol error usually
            # abandons the session, while an isError result it can read and
            # recover from by picking a different tool.
            #
            # A refusal is logged like any other call. The audit trail exists to
            # answer "what did this key do", and a key working through every
            # tool name it can think of is the shape of a stolen one -- which is
            # invisible if only the calls that reached a handler are recorded.
            if entry is None:
                await _log_activity(connection, connector, name or '', McpActivity.STATUS_ERROR,
                                    _elapsed_ms(started), 'Unknown tool')
                return _tool_result(rid, 'Unknown tool: {0}'.format(name), True)
            if name in set(connection.disabled_tools or []):
                await _log_activity(connection, connector, name, McpActivity.STATUS_ERROR,
                                    _elapsed_ms(started), 'Tool switched off')
                return _tool_result(
                    rid,
                    "Tool '{0}' is switched off for this connection in the Honeycomb "
                    'dashboard.'.format(name),
                    True,
                )
            handler = spec.handlers.get(name)
            if handler is None:
                await _log_activity(connection, connector, name, McpActivity.STATUS_ERROR,
                                    _elapsed_ms(started), 'Tool has no handler')
                return _tool_result(rid, "Tool '{0}' has no handler.".format(name), True)

            try:
                # The second argument is the falcon handlers' `db` session, which
                # no ported handler ever dereferences -- they read everything they
                # need off the connection. None is correct here, not a placeholder.
                payload = await asyncio.wait_for(
                    handler(connection, None, args), timeout=_tool_timeout())
            # Never echo a raw upstream error: it carries the credential (some
            # providers pass the access token as a query parameter, and we follow
            # paging URLs that embed it). The redacted string is produced ONCE and
            # used for both the client-visible text and the activity log.
            except asyncio.TimeoutError:
                message = "Tool '{0}' timed out after {1:.0f}s.".format(name, _tool_timeout())
                await _log_activity(connection, connector, name, McpActivity.STATUS_ERROR,
                                    _elapsed_ms(started), message)
                return _tool_result(rid, message, True)
            except ConnectorError as exc:
                message = redact_text(str(exc))
                await _log_activity(connection, connector, name, McpActivity.STATUS_ERROR,
                                    _elapsed_ms(started), message)
                return _tool_result(rid, message, True)
            except Exception as exc:  # noqa: BLE001 -- a clean MCP error, never a raw 500
                message = redact_exc(exc)
                logger.exception('MCP tool %s.%s failed', connector, name)
                await _log_activity(connection, connector, name, McpActivity.STATUS_ERROR,
                                    _elapsed_ms(started), message)
                return _tool_result(rid, message, True)

            await _log_activity(connection, connector, name, McpActivity.STATUS_OK,
                                _elapsed_ms(started), detail={'via': 'mcp'})
            return _tool_result(rid, json.dumps(payload, default=str, indent=2), False)

        return _err(rid, -32601, 'Method not found: {0}'.format(method))

    @application.api_route(
        '/{path:path}',
        methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
        include_in_schema=False,
    )
    async def catch_all(path: str, request: Request):
        # Registered last, so it only sees paths the real routes did not match --
        # a truncated URL, a stray /messages, a client appending a segment of its
        # own. Anything but a readable answer here reads as "Session terminated".
        if request.method != 'POST':
            return Response(status_code=405, headers=_ALLOW)
        rid = None
        try:
            body = json.loads(await request.body() or b'{}')
            if isinstance(body, dict):
                rid = body.get('id')
        except json.JSONDecodeError:
            pass
        return _err(rid, -32001,
                    'No Honeycomb MCP endpoint at this URL. Copy the connection URL again '
                    'from the Honeycomb dashboard.')

    @application.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException):
        # Belt and braces for the one thing that must never happen: a bare 404
        # reaching an MCP client.
        if exc.status_code == 404:
            return _err(None, -32001, 'No Honeycomb MCP endpoint at this URL.')
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, 'headers', None))

    return application


app = build_app()
