"""Bearer resolution for the MCP data plane -- the whole tenant boundary.

Two credentials authenticate against /mcp/, and both are `Authorization: Bearer`
values that must hash to a live row whose connection matches BOTH the connector
and the endpoint slug in the URL:

  hc_...   an McpKey the user minted and pasted by hand.
  hco_...  an OAuthToken this server issued, for clients that cannot be given a
           header at all -- claude.ai's connector dialog takes a URL and nothing
           else, so the flow in mcp/oauth.py is the only way it can hold one.

The check that matters is identical for both, and lives in the last few lines of
each resolver: one credential, one connection.

The falcon original this is ported from also had a slug-only mode, where a bare
request with no header authenticated as the connection's creator. It is not
reproduced here, not even behind a setting: the slug travels in a URL, so it
ends up in browser history, shared chats and proxy logs, and a leaked URL would
then be full tenant access. The slug *identifies* a connection; it does not
authorize one.

The ORM work runs through mcp.db.run_db, which puts it on the thread pool
instead of asgiref's single shared thread -- see that module for why.
"""
from datetime import timedelta

from django.utils import timezone

from .db import run_db
from .models import McpKey, OAuthToken

# last_used_at is a "roughly when" for the dashboard, not an audit record (the
# activity log is that). Writing it on every request doubled the database
# work of tools/list; writing it at most this often costs nothing anyone reads.
LAST_USED_GRANULARITY = timedelta(seconds=60)


def _touch(model, row):
    now = timezone.now()
    if row.last_used_at is None or now - row.last_used_at > LAST_USED_GRANULARITY:
        model.objects.filter(pk=row.pk).update(last_used_at=now)


def _load_key(plain):
    key = (
        McpKey.objects
        .select_related('connection', 'connection__tenant')
        .filter(key_hash=McpKey.hash_token(plain), revoked_at__isnull=True)
        .first()
    )
    if key is not None and key.connection is not None:
        _touch(McpKey, key)
    return key


def _load_token(plain):
    token = (
        OAuthToken.objects
        .select_related('connection', 'connection__tenant', 'user')
        .filter(token_hash=OAuthToken.hash_token(plain), revoked_at__isnull=True)
        .first()
    )
    if token is not None and token.connection is not None:
        _touch(OAuthToken, token)
    return token


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

    # Two kinds of bearer reach this endpoint, and they are told apart by
    # prefix only to pick the right table -- both are verified by hash.
    #   hc_   a key the user minted and pasted, for clients that can send a
    #         header (Claude Code, Claude Desktop, curl).
    #   hco_  a token this server issued through the OAuth flow in mcp/oauth.py,
    #         for browser-only clients such as claude.ai that have nowhere to
    #         paste a key.
    # Both are scoped to exactly one connection, so the tenant boundary below is
    # identical whichever door the caller came through.
    if plain.startswith(OAuthToken.PREFIX):
        return await _resolve_oauth_token(plain, connector, slug)

    # A cheap early reject only -- authentication is by hash, never by prefix.
    if not plain.startswith(McpKey.PREFIX):
        raise AuthError(_DENIED, 'invalid_token')

    # select_related pulls the connection (and its tenant) in the same query, so
    # nothing downstream touches a lazy FK descriptor from async code.
    key = await run_db(_load_key, plain)
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
    return connection, key


async def _resolve_oauth_token(plain, connector, slug):
    """Resolve an `hco_...` token minted by the OAuth flow.

    Deliberately the same shape, and the same refusals, as the McpKey path
    above: one connection per credential, the connector and slug in the URL
    must match the row, and every failure says the same thing.
    """
    token = await run_db(_load_token, plain)
    if token is None:
        raise AuthError(_DENIED, 'invalid_key')
    # Expiry is enforced here rather than by a cleanup job: a row that outlives
    # its expires_at must not authenticate just because nothing has swept it.
    if token.expires_at and token.expires_at <= timezone.now():
        raise AuthError(
            'This authorization has expired. Reconnect the connector in your AI client.',
            'expired_token',
        )
    connection = token.connection
    if connection is None:
        raise AuthError(_DENIED, 'orphan_key')
    if connection.connector != connector or connection.endpoint_slug != slug:
        raise AuthError(_DENIED, 'connection_mismatch')
    # The person who approved this token must still be someone who could
    # approve it now: an account that was deactivated, or moved out of the
    # organization, stops working at once rather than when the token expires.
    user = token.user
    if user is None or not user.is_active or user.tenant_id != connection.tenant_id:
        raise AuthError(_DENIED, 'user_inactive')
    return connection, token
