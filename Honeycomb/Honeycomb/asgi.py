"""
ASGI config for Honeycomb project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/

----

This file is one architectural decision written out. Honeycomb serves two
different things over one port:

  /mcp/**  the data plane. JSON-RPC spoken by AI clients, authenticated by a
           bearer token, implemented in FastAPI, and almost entirely I/O-bound
           -- a tools/call is mostly waiting on somebody else's API.
  /**      the portal. Django: the control-plane REST API, the admin, the
           static files WhiteNoise serves.

They are composed side by side rather than one inside the other, and the
dispatch below is a plain prefix check, not a Django URL route. Three reasons,
all of which have bitten somebody:

1. Django's request path is built for sync views, and its middleware chain
   (WhiteNoise, CommonMiddleware, CSRF, sessions) is a stack of adapters around
   them. A sync view reached over ASGI is handed to a thread from the pool, so
   routing MCP traffic through Django would take a request that is pure async
   I/O, park a whole thread on it for the length of an upstream API call, and
   give back nothing for it. The prefix check means an MCP request never enters
   MIDDLEWARE at all.

2. CSRF. /mcp/** carries no cookie and is authenticated purely by
   `Authorization: Bearer hc_<token>`; see the "Two auth planes" block in
   settings.py and HANDOFF invariant 2. Keeping it outside Django means that is
   true by construction rather than by a csrf_exempt somebody could delete.

3. APPEND_SLASH. Django answers a slashless POST with a 301, and a browser --
   or an MCP client -- reissues that as a GET with no body. Under /mcp/ that
   surfaces to a user as "Session terminated" with no clue why. Outside
   Django's URL resolver, CommonMiddleware never gets the chance.

Everything else, including /admin/ and /api/, is untouched: it reaches Django
exactly as it did before this file grew past four lines.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Honeycomb.settings')

# Must come first, and must not be moved below the import that follows.
# get_asgi_application() is what runs django.setup(), and mcp.endpoint reaches
# for models (McpKey, Connection) and for the connector catalog at import time.
# Import it before the app registry is populated and Django raises
# AppRegistryNotReady -- a failure that only shows up under a real ASGI server,
# never under `manage.py`.
django_application = get_asgi_application()

from mcp.endpoint import app as mcp_application  # noqa: E402

MCP_PREFIX = '/mcp'

# Whether mcp/endpoint.py declares its routes with the /mcp prefix already in
# them ('/mcp/{connector}/{slug}/') or relative to the mount point
# ('/{connector}/{slug}/'). Both are reasonable ways to write that file, and
# this composite has no business dictating which -- so it looks once, at import,
# and strips the prefix only when the app expects it stripped. Deciding here
# instead of per request keeps the hot path a single string comparison.
_MCP_APP_IS_ABSOLUTE = any(
    getattr(route, 'path', '').startswith(MCP_PREFIX + '/')
    for route in getattr(mcp_application, 'routes', ())
)


async def application(scope, receive, send):
    """Dispatch by path prefix: /mcp/** to FastAPI, everything else to Django."""
    if scope['type'] == 'lifespan':
        # Django's ASGIHandler does not implement the lifespan protocol, and a
        # server that gets no reply either logs a warning and carries on or, on
        # some servers, refuses to start. FastAPI does implement it, and it is
        # the half with startup/shutdown work to do -- closing the shared httpx
        # client in connectors/shims/http.py, above all. So lifespan is the
        # data plane's.
        await mcp_application(scope, receive, send)
        return

    path = scope.get('path', '')
    if path == MCP_PREFIX or path.startswith(MCP_PREFIX + '/'):
        if _MCP_APP_IS_ABSOLUTE:
            await mcp_application(scope, receive, send)
        else:
            # Starlette resolves against scope['path'], and root_path is what
            # it uses to rebuild absolute URLs, so both have to move together.
            mounted = dict(scope)
            mounted['path'] = path[len(MCP_PREFIX):] or '/'
            mounted['root_path'] = scope.get('root_path', '') + MCP_PREFIX
            await mcp_application(mounted, receive, send)
        return

    await django_application(scope, receive, send)
