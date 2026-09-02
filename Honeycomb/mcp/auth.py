"""Bearer-key resolution for the MCP data plane -- the whole tenant boundary.

There is exactly ONE way to authenticate against /mcp/: an `Authorization:
Bearer hc_...` key that hashes to a live McpKey row whose connection matches
BOTH the connector and the endpoint slug in the URL.

The falcon original this is ported from also had a slug-only mode, where a bare
request with no header authenticated as the connection's creator. It is not
reproduced here, not even behind a setting: the slug travels in a URL, so it
ends up in browser history, shared chats and proxy logs, and a leaked URL would
then be full tenant access. The slug *identifies* a connection; it does not
authorize one.

Every ORM call below is awaited (afirst/aupdate) because this module runs inside
FastAPI's event loop -- a plain queryset evaluation there raises
SynchronousOnlyOperation.
"""
from django.utils import timezone

from .models import McpKey


class AuthError(Exception):
    """Authentication failed. `message` is safe to show the MCP client."""

    def __init__(self, message, reason=''):
        super().__init__(message)
        self.message = message
        # A short machine string for logs; never contains any part of the token.
        self.reason = reason or 'unauthorized'


_BEARER = 'Bearer '

# Deliberately identical for every failure mode below. A caller holding a key
# should not be able to learn, by comparing messages, whether a given slug
# exists or which connector a key is scoped to.
_DENIED = (
    'This MCP key is not valid for this connection. Mint a key on the '
    'connection you are calling, in the Honeycomb dashboard, and paste it '
    'into your AI client as an Authorization: Bearer header.'
)


async def resolve_bearer(authorization, connector, slug):
    """Resolve `Authorization: Bearer hc_...` to (connection, key).

    Raises AuthError with a readable message on any failure. On success the
    key's last_used_at is refreshed with a bare UPDATE -- a full save() would
    rewrite every column of a row several requests may touch at once, for a
    field nothing reads transactionally.
    """
    header = (authorization or '').strip()
    if not header:
        raise AuthError(
            'This Honeycomb MCP endpoint requires a key. Add an '
            'Authorization: Bearer hc_... header in your AI client.',
            'missing_authorization',
        )
    if not header.startswith(_BEARER):
        raise AuthError('Authorization header must be of the form: Bearer hc_...',
                        'malformed_authorization')

    plain = header[len(_BEARER):].strip()
    # A cheap early reject only -- authentication is by hash, never by prefix.
    if not plain.startswith(McpKey.PREFIX):
        raise AuthError(_DENIED, 'invalid_token')

    # select_related pulls the connection (and its tenant) in the same query, so
    # nothing downstream touches a lazy FK descriptor from async code.
    key = await (
        McpKey.objects
        .select_related('connection', 'connection__tenant')
        .filter(key_hash=McpKey.hash_token(plain), revoked_at__isnull=True)
        .afirst()
    )
    if key is None:
        raise AuthError(_DENIED, 'invalid_key')

    connection = key.connection
    if connection is None:
        raise AuthError(_DENIED, 'orphan_key')
    # THE tenant boundary. The URL slug says which connection is being called;
    # the key says which connection the caller may call. They must be the same
    # row, and the connector in the path must match it too, so a key minted for
    # one connector can never be spent on another.
    if connection.connector != connector or connection.endpoint_slug != slug:
        raise AuthError(_DENIED, 'connection_mismatch')

    await McpKey.objects.filter(pk=key.pk).aupdate(last_used_at=timezone.now())
    return connection, key
