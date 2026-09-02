"""
Google account labelling for the outbound OAuth flow.

Two small things live here, both used only by connections/oauth.py:

  * ``with_id_scopes`` -- append the OpenID email scopes to whatever a connector
    asked for, so the grant we obtain can be read back for a label.
  * ``fetch_google_email`` -- best effort lookup of which Google account the user
    actually picked, so a tenant with three Google Ads logins can tell its three
    connections apart.

Why plain httpx and not connectors.shims.http: that shim is async and caches a
single AsyncClient at module scope, which is only safe on the one long-lived
event loop that serves /mcp/**. These calls run inside a synchronous Django
view, so reaching for it would mean spinning up a per-request loop -- exactly
the pattern its module docstring says will inherit sockets from a closed loop.
A one-shot synchronous request has no such problem.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'

#: Appended to every Google authorize request so the grant can be read back for
#: a human-readable label. Nothing else in the product depends on them.
GOOGLE_ID_SCOPES = ('openid', 'https://www.googleapis.com/auth/userinfo.email')

#: Short on purpose. Labelling is a nicety; the user is sitting on a redirect
#: waiting for the connection to appear, and a slow userinfo call must not be
#: what makes that feel broken.
USERINFO_TIMEOUT_SECONDS = 8.0


def with_id_scopes(scopes) -> str:
    """The space-joined scope string to request: ``scopes`` plus the ID scopes.

    De-duplicated and order-preserving, so a connector that already asks for
    ``openid`` does not send it twice -- Google echoes the scope string back
    into the grant, and a duplicated entry there is noise in every later
    comparison.
    """
    merged = list(dict.fromkeys([*(scopes or ()), *GOOGLE_ID_SCOPES]))
    return ' '.join(merged)


def fetch_google_email(access_token: str) -> str:
    """The Google account email behind this access token, or '' on any failure.

    Never raises. Labelling a connection is cosmetic, and a failure here must
    not be able to lose a refresh token the user has already consented to --
    the connection is created either way, just unlabelled.
    """
    if not access_token:
        return ''
    try:
        response = httpx.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': 'Bearer {0}'.format(access_token)},
            timeout=USERINFO_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            # Capped at the length of an EmailField; a hostile or malformed
            # payload does not get to decide how long the name column is.
            return str(response.json().get('email') or '')[:254]
        logger.warning('Google userinfo returned %s', response.status_code)
    except Exception:
        # Bare except, deliberately: httpx errors, JSON errors and anything the
        # response object throws all mean the same thing here -- no label.
        # The exception text is NOT logged, because a failing request can carry
        # the bearer token in its message.
        logger.warning('Google userinfo lookup failed', exc_info=False)
    return ''
