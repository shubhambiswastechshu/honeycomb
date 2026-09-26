"""
LinkedIn sign-in for ``auth='linkedin_oauth'`` connectors.

The pieces the LinkedIn flow does not share with Google's: its URLs, its token
exchange, its token renewal and how a member is named. The routes themselves
live in connections/oauth.py, which runs both providers through the same
start view and the same never-render-an-error callback.

Two differences from Google shape this file.

*The access token is the credential.* A Google access token lasts an hour, so
Honeycomb stores only the refresh token and mints a fresh one per use. A
LinkedIn access token lasts sixty days, so it is stored and used until it is
nearly spent, and only then renewed with the refresh token (valid a year).

*A refresh token is not guaranteed.* LinkedIn issues them only to apps it has
approved for programmatic refresh. Without one the connection still works for
sixty days, then asks to be reconnected, so its absence is not an error here.

Plain synchronous httpx, as in connections/google.py: these run inside Django
views, not on the MCP event loop.
"""

import logging
import time
from urllib.parse import urlencode

import httpx
from django.conf import settings
from rest_framework.exceptions import ValidationError

from connectors.shims.errors import redact_text

from .models import ConnectorOAuthState

logger = logging.getLogger(__name__)

#: The registry's value for a connector that is connected through LinkedIn.
LINKEDIN_AUTH = 'linkedin_oauth'

#: Legacy profile endpoint. Needs r_basicprofile, which the Advertising API
#: product grants; the OpenID userinfo endpoint needs a product this app may not
#: have, and asking for a scope the app lacks fails the whole sign-in.
PROFILE_URL = 'https://api.linkedin.com/v2/me'

#: The user is watching a blank redirect while these run.
TOKEN_TIMEOUT_SECONDS = 15.0
PROFILE_TIMEOUT_SECONDS = 8.0

STALE_STATE_MESSAGE = (
    'That LinkedIn sign-in link has expired or was already used. '
    'Please click Continue with LinkedIn again.'
)


def _setting(name: str) -> str:
    return str(getattr(settings, name, '') or '')


def redirect_uri() -> str:
    """The callback URL, byte for byte what the LinkedIn app must list.

    THE one builder, for the reason google_redirect_uri gives: the authorize
    request, the token exchange and the app's registered list must agree
    exactly, and the misconfiguration message quotes this same value.
    """
    base = _setting('HONEYCOMB_PUBLIC_BASE').rstrip('/')
    return '{0}/api/connectors/oauth/linkedin/callback/'.format(base)


def require_config() -> None:
    """Refuse to start, naming exactly what an operator has to set."""
    if not _setting('HONEYCOMB_PUBLIC_BASE'):
        raise ValidationError(
            'HONEYCOMB_PUBLIC_BASE is not set, so this server cannot tell '
            'LinkedIn where to send users back.'
        )
    if not _setting('LINKEDIN_CLIENT_ID') or not _setting('LINKEDIN_CLIENT_SECRET'):
        raise ValidationError(
            'LinkedIn sign-in is not configured on this server. Set '
            'LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in the environment, '
            'and add this exact URL under Authorized redirect URLs in the '
            'LinkedIn app\'s Auth tab: {0}'.format(redirect_uri())
        )


def authorize_url(tenant, user, slug: str) -> str:
    """Mint a one-time nonce and return LinkedIn's consent URL for it."""
    require_config()
    state = ConnectorOAuthState.objects.create(tenant=tenant, user=user, connector=slug)
    params = {
        'response_type': 'code',
        'client_id': _setting('LINKEDIN_CLIENT_ID'),
        'redirect_uri': redirect_uri(),
        'scope': _setting('LINKEDIN_OAUTH_SCOPES'),
        'state': state.state,
    }
    return '{0}?{1}'.format(_setting('LINKEDIN_OAUTH_AUTH_URI'), urlencode(params))


def _token_request(payload: dict):
    """POST to LinkedIn's token endpoint. -> (json, error_message)."""
    payload = dict(payload, client_id=_setting('LINKEDIN_CLIENT_ID'),
                   client_secret=_setting('LINKEDIN_CLIENT_SECRET'))
    try:
        response = httpx.post(
            _setting('LINKEDIN_OAUTH_TOKEN_URI'),
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=TOKEN_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return {}, 'Could not reach LinkedIn ({0}).'.format(redact_text(exc)[:200])
    if response.status_code != 200:
        # Redacted before truncating, for the reason oauth.py gives: an error
        # body can quote the request, and the request carried client_secret.
        return {}, 'LinkedIn rejected the sign-in ({0}): {1}'.format(
            response.status_code, redact_text(response.text[:1000])[:300]
        )
    try:
        return response.json(), ''
    except ValueError:
        return {}, 'LinkedIn answered the sign-in with something that is not JSON.'


def exchange_code(code: str):
    """Trade an authorization code for tokens. -> (json, error_message)."""
    return _token_request({
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri(),
    })


def creds_from_token(token: dict, previous: dict | None = None) -> dict:
    """The credential object to store, from a token response.

    Expiry is stored as an absolute epoch so the connector can tell a spent
    token from a live one without asking LinkedIn. A renewal response may omit
    the refresh token; the one already held is kept rather than dropped.
    """
    previous = previous or {}
    now = time.time()
    creds = dict(previous)
    creds['access_token'] = str(token.get('access_token') or '')
    creds['expires_at'] = now + _seconds(token.get('expires_in'), 60 * 24 * 3600)
    refresh = str(token.get('refresh_token') or '')
    if refresh:
        creds['refresh_token'] = refresh
        if token.get('refresh_token_expires_in') is not None:
            creds['refresh_expires_at'] = now + _seconds(token.get('refresh_token_expires_in'), 0)
    if token.get('scope'):
        creds['scope'] = str(token.get('scope'))
    return creds


def _seconds(value, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def fetch_member(access_token: str):
    """(member_id, display_name) for this token, or ('', '') on any failure.

    Never raises: naming a connection is cosmetic, and a failure here must not
    lose tokens the user has just consented to.
    """
    try:
        response = httpx.get(
            PROFILE_URL,
            headers={'Authorization': 'Bearer {0}'.format(access_token)},
            timeout=PROFILE_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            logger.warning('LinkedIn profile lookup returned %s', response.status_code)
            return '', ''
        body = response.json()
        name = ' '.join(
            part for part in (
                str(body.get('localizedFirstName') or '').strip(),
                str(body.get('localizedLastName') or '').strip(),
            ) if part
        )
        return str(body.get('id') or '')[:254], name[:60]
    except Exception:
        # The exception text is not logged: it can carry the bearer token.
        logger.warning('LinkedIn profile lookup failed', exc_info=False)
        return '', ''
