"""Root-level routes for the MCP authorization server.

Included at the project root, not under /api/, because RFC 9728 and RFC 8414
fix these paths absolutely: a client derives `/.well-known/...` from the origin
and will not look anywhere else.

Every route is registered twice, with and without a trailing slash. Clients
build these URLs by string concatenation and differ over the trailing slash;
APPEND_SLASH would answer the other spelling with a 301, and enough clients
follow a redirect badly -- or not at all, for a POST -- that it is cheaper to
just serve both.
"""
from django.urls import path, re_path

from . import oauth

# The path-inserted forms (RFC 9728 section 3.1): the resource's own path is
# appended after the well-known segment, so one server can describe many
# protected resources. <slug> is matched loosely because it is generated
# elsewhere; the view resolves it against the database anyway.
_RESOURCE = 'mcp/<str:connector>/<str:slug>'
# A client that addressed the endpoint as .../<slug>/mcp derives its metadata
# URL from that same path, so the well-known route has to accept the suffix too
# -- otherwise the challenge points at a document that 404s and discovery stops
# one step short of working.
_RESOURCE_TAIL = 'mcp/<str:connector>/<str:slug>/<str:tail>'

urlpatterns = [
    # --- protected resource metadata (RFC 9728) ---
    path('.well-known/oauth-protected-resource/{0}/'.format(_RESOURCE_TAIL),
         oauth.protected_resource_metadata),
    path('.well-known/oauth-protected-resource/{0}'.format(_RESOURCE_TAIL),
         oauth.protected_resource_metadata),
    path('.well-known/oauth-protected-resource/{0}/'.format(_RESOURCE),
         oauth.protected_resource_metadata, name='mcp-oauth-prm'),
    path('.well-known/oauth-protected-resource/{0}'.format(_RESOURCE),
         oauth.protected_resource_metadata),
    re_path(r'^\.well-known/oauth-protected-resource/?$',
            oauth.protected_resource_metadata),

    # --- authorization server metadata (RFC 8414) ---
    # Also served under the resource path: some clients ask for the AS metadata
    # at the path-inserted location before trying the bare one.
    path('.well-known/oauth-authorization-server/{0}/'.format(_RESOURCE_TAIL),
         oauth.authorization_server_metadata),
    path('.well-known/oauth-authorization-server/{0}'.format(_RESOURCE_TAIL),
         oauth.authorization_server_metadata),
    path('.well-known/oauth-authorization-server/{0}/'.format(_RESOURCE),
         oauth.authorization_server_metadata),
    path('.well-known/oauth-authorization-server/{0}'.format(_RESOURCE),
         oauth.authorization_server_metadata),
    re_path(r'^\.well-known/oauth-authorization-server/?$',
            oauth.authorization_server_metadata, name='mcp-oauth-asm'),
    # OpenID discovery, for clients that try it first out of habit.
    re_path(r'^\.well-known/openid-configuration/?$',
            oauth.authorization_server_metadata),

    # --- the flow itself ---
    re_path(r'^oauth/register/?$', oauth.register, name='mcp-oauth-register'),
    re_path(r'^oauth/authorize/?$', oauth.authorize, name='mcp-oauth-authorize'),
    re_path(r'^oauth/token/?$', oauth.token, name='mcp-oauth-token'),
]
