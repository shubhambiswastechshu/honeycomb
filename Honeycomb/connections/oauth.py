"""
The outbound Google OAuth flow for ``auth='google_oauth'`` connectors.

Two routes, both GET:

  ``/api/connectors/<slug>/oauth/start/``     names the connector
  ``/api/connectors/oauth/callback/``         shared by every connector

The callback is deliberately not per-connector; see ``google_redirect_uri``.

  ``start/``     Cookie-authenticated, owner/admin only. Mints a one-time nonce
                 and hands the browser Google's authorize URL.
  ``callback/``  Unauthenticated, because Google is the one sending the browser
                 back and the session cookie may not survive the round trip
                 (a cross-site redirect, a different browser profile, a user who
                 signed into Google in another window). The nonce is therefore
                 the credential for this route, which is exactly why it is
                 single-use, expires in ten minutes, and is burned before the
                 authorization code is spent.

Two rules shape everything below.

*Never render an error.* The callback belongs to Google, and a 500 or a DRF
error page there is a dead end the user cannot act on. Every path -- including
an unexpected exception -- ends in a redirect back to the connector's page in
the dashboard with ``?connected=1`` or ``?error=<message>``.

*Never echo an upstream body raw.* A failed token exchange answers with a body
that quotes the request back, and that request contained ``client_secret``.
Google's own ``?error=`` string is likewise attacker-influenceable text on its
way into a URL. Both go through ``redact_text`` first.
"""

import logging
from urllib.parse import quote, urlencode

import httpx
from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.mixins import TenantScopedQuerysetMixin
from accounts.models import User
from connectors import registry
from connectors.shims.errors import redact_text

from .google import fetch_google_email, with_id_scopes
from .models import Connection, ConnectorOAuthState

logger = logging.getLogger(__name__)

#: Connecting an account writes a long-lived refresh token for the whole
#: organization's data. Members may use the connections that result; they do
#: not get to create them. Mirrors mcp.views.KEY_ADMIN_ROLES.
CONNECT_ADMIN_ROLES = (User.Role.OWNER, User.Role.ADMIN)

#: Google's published endpoints, used when settings does not override them. The
#: settings names exist so a test or a staging tenant can point elsewhere; the
#: defaults exist so a missing setting is not a 500.
DEFAULT_AUTH_URI = 'https://accounts.google.com/o/oauth2/v2/auth'
DEFAULT_TOKEN_URI = 'https://oauth2.googleapis.com/token'

#: The user is watching a blank redirect while this runs, so it is bounded.
TOKEN_TIMEOUT_SECONDS = 15.0

#: One message for every "this nonce is no good" case. Distinguishing expired
#: from already-used from never-existed would tell a probe which of its guesses
#: was closest, and the user's next action is the same in all three.
STALE_STATE_MESSAGE = (
    'That Google sign-in link has expired or was already used. '
    'Please click Connect with Google again.'
)


def google_redirect_uri() -> str:
    """The callback URL Google must be told, and must be registered with.

    THE one builder for this value. Google compares the ``redirect_uri`` on the
    authorize request, the one on the token exchange and the one registered in
    Cloud Console byte for byte; three hand-written copies of the same f-string
    is how a flow ends up failing with ``redirect_uri_mismatch`` that nobody can
    reproduce. The error message shown when the credentials are unset is built
    from this function too, so what we tell the user to register is literally
    what we will send.

    ONE URI for every connector, not one each. Google matches this value
    byte for byte against the list registered on the OAuth client, so a
    per-connector callback would mean registering a new line in Cloud Console
    for every connector ever added -- a deployment step that is easy to forget
    and fails with a redirect_uri_mismatch nobody can reproduce. The connector
    is already recorded on the nonce at start time, so the URL does not need to
    carry it, and the callback reads it back from there.

    Trailing slash included: the route is declared with one, and Django's
    APPEND_SLASH redirect would arrive at Google's comparison too late to help.
    """
    base = (getattr(settings, 'HONEYCOMB_PUBLIC_BASE', '') or '').rstrip('/')
    return '{0}/api/connectors/oauth/callback/'.format(base)


def frontend_connector_url(connector_slug: str) -> str:
    """Where the callback sends the browser when it is done, success or not.

    Falls back to a site-relative path when HONEYCOMB_FRONTEND_BASE is unset,
    which is correct for the single-origin proxy deployment and is in any case
    better than redirecting to ``/dashboard/...`` prefixed with nothing.
    """
    base = (getattr(settings, 'HONEYCOMB_FRONTEND_BASE', '') or '').rstrip('/')
    return '{0}/dashboard/connectors/{1}'.format(base, connector_slug)


def _setting(name: str, default: str = '') -> str:
    return str(getattr(settings, name, '') or default)


def _redirect_with(destination: str, **params) -> HttpResponseRedirect:
    """Bounce to ``destination`` carrying exactly one query parameter.

    quote_via=quote so a message's spaces become %20 rather than '+': the value
    is read back by a frontend that will render it as prose, not parse it as a
    form body.
    """
    query = urlencode(params, quote_via=quote)
    return HttpResponseRedirect('{0}?{1}'.format(destination, query))


class GoogleOAuthStartView(TenantScopedQuerysetMixin, APIView):
    """GET /api/connectors/<slug>/oauth/start/ -> {"authorize_url": ...}.

    TenantScopedQuerysetMixin is mixed in for get_tenant() alone -- it is the
    single definition of "which organization is this caller", and it refuses a
    platform-level superuser rather than letting one fall through to a
    tenant-less write.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = 'connect'

    def get(self, request, slug):
        tenant = self.get_tenant()
        if request.user.role not in CONNECT_ADMIN_ROLES:
            raise PermissionDenied(
                'Only an owner or admin can connect a Google account.'
            )
        connector = self._google_connector(slug)
        redirect_uri = google_redirect_uri()
        self._require_google_config(redirect_uri)

        state = ConnectorOAuthState.objects.create(
            tenant=tenant, user=request.user, connector=slug
        )
        params = {
            'client_id': _setting('GOOGLE_CLIENT_ID'),
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            # openid/email is appended so the connection can be labelled with
            # the account that was chosen.
            'scope': with_id_scopes(getattr(connector, 'scopes', ()) or ()),
            # offline is what makes Google issue a refresh token at all; without
            # it the grant dies in an hour and every tool call after that fails.
            'access_type': 'offline',
            # select_account lets someone add a SECOND instance under a
            # different Google login instead of silently re-using the session
            # they already have; consent is what makes Google re-issue the
            # refresh token on a repeat authorization.
            'prompt': 'select_account consent',
            'state': state.state,
        }
        authorize_url = '{0}?{1}'.format(
            _setting('GOOGLE_OAUTH_AUTH_URI', DEFAULT_AUTH_URI), urlencode(params)
        )
        return Response({'authorize_url': authorize_url})

    def _google_connector(self, slug):
        connector = registry.get(slug)
        if connector is None:
            raise NotFound('No such connector.')
        auth = getattr(connector, 'auth', '')
        if auth != 'google_oauth':
            raise ValidationError(
                '{0} authenticates with {1}, not Google sign-in. Create it by '
                'POSTing its credentials to /api/connections/ instead.'.format(
                    getattr(connector, 'label', slug), auth or 'an unknown method'
                )
            )
        return connector

    def _require_google_config(self, redirect_uri: str) -> None:
        """Refuse early, with the exact thing an operator has to go and do.

        "Google OAuth is not configured" on its own costs whoever reads it an
        hour in the Cloud Console guessing at the redirect URI. So the message
        names the environment variables AND the URI to register, taken from the
        same builder the flow will use.
        """
        if not (getattr(settings, 'HONEYCOMB_PUBLIC_BASE', '') or ''):
            raise ValidationError(
                'HONEYCOMB_PUBLIC_BASE is not set, so this server cannot tell '
                'Google where to send users back. Set it to this API\'s public '
                'origin (for example https://honeycomb.example.com) and retry.'
            )
        if not _setting('GOOGLE_CLIENT_ID') or not _setting('GOOGLE_CLIENT_SECRET'):
            raise ValidationError(
                'Google sign-in is not configured on this server. Set '
                'GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the environment, '
                'and register this exact redirect URI on the OAuth client in '
                'Google Cloud Console: {0}'.format(redirect_uri)
            )


class GoogleOAuthCallbackView(APIView):
    """GET /api/connectors/oauth/callback/ -- Google's landing route.

    One route for every connector. Which connector this callback is completing
    comes off the nonce, not the URL, so the caller cannot name it at all.

    Unauthenticated by construction: the browser arriving here has come from
    accounts.google.com, and requiring a session would fail people whose cookie
    did not survive the cross-site hop. Authentication is replaced by the
    nonce, which is single-use and expires in ten minutes.

    Throttled all the same. This route creates rows and calls an upstream, and
    it is reachable by anyone -- the ceiling is what stops a stranger turning it
    into free traffic against Google.
    """

    # Empty, not "the defaults minus session": the route must not 403 a caller
    # for a stale CSRF cookie or a half-valid credential it does not use.
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = 'connect'

    def handle_exception(self, exc):
        """Even a framework-level refusal leaves this route as a redirect.

        get() already catches everything it can raise, but throttling, content
        negotiation and permission checks run in DRF's dispatch, *before* the
        handler -- and their default rendering is a JSON error body, which is
        the raw error page this route promises never to show. dispatch() has
        There is no connector slug in the URL to send the user back to, and
        no nonce has been read yet, so this lands on the connector list rather
        than on one connector's page.
        """
        slug = ''
        if isinstance(exc, Throttled):
            message = (
                'Too many sign-in attempts from this network. '
                'Wait a minute and click Connect with Google again.'
            )
        else:
            logger.exception('Google OAuth callback rejected before dispatch')
            message = 'Could not complete Google sign-in. Please try again.'
        return _redirect_with(frontend_connector_url(slug), error=message)

    def get(self, request):
        # Provisional: until the nonce is read we do not know which connector
        # this is, so a failure before that point lands on the list page.
        # Bound BEFORE the try, not inside it -- an exception raised before
        # _complete() returns would otherwise leave the name unbound and the
        # handler below would die on it, turning the one path that promises a
        # redirect into the 500 it exists to prevent.
        slug = ''
        destination = frontend_connector_url('')
        try:
            error, slug = self._complete(request)
            if slug:
                destination = frontend_connector_url(slug)
        except Exception as exc:
            # Broad on purpose, and the last line of defence: whatever went
            # wrong, the user gets their connector page back with a reason
            # rather than a stack trace, and Google gets a 302 rather than a
            # 500. The redacted type/message is enough to correlate with the
            # traceback that goes to the log.
            logger.exception('Google OAuth callback failed for connector %r', slug)
            error = 'Could not complete Google sign-in ({0}).'.format(
                redact_text('{0}: {1}'.format(type(exc).__name__, exc))[:200]
            )
        if error:
            return _redirect_with(destination, error=error)
        return _redirect_with(destination, connected='1')

    def _complete(self, request):
        """Do the exchange.

        Returns ``(error_message, slug)``: an empty message means success, and
        the slug -- which is only known once the nonce has been read -- is
        returned alongside so the caller can send the browser back to the right
        connector's page even when the exchange failed.
        """

        # The user pressed Cancel, or Google refused the grant. Redacted before
        # it goes anywhere near the redirect: it is upstream text landing in a
        # URL that a browser will render.
        denied = request.query_params.get('error')

        code = request.query_params.get('code') or ''
        state_value = request.query_params.get('state') or ''

        # The nonce is read first even when Google reported a refusal: it is
        # what tells us which connector page to send the browser back to.
        state = self._claim_state(state_value) if state_value else None
        if state is None:
            return STALE_STATE_MESSAGE, ''
        slug = state.connector

        if denied:
            return 'Google did not grant access ({0}).'.format(
                redact_text(denied)[:120]
            ), slug

        connector = registry.get(slug)
        if connector is None or getattr(connector, 'auth', '') != 'google_oauth':
            return 'This connector does not use Google sign-in.', slug

        if not code:
            return 'Google did not return an authorization code. Please try again.', slug

        token, error = self._exchange(code)
        if error:
            return error, slug

        refresh_token = str(token.get('refresh_token') or '')
        if not refresh_token:
            # access_type=offline + prompt=consent should always yield one; when
            # it does not, the account has a stale grant that Google will not
            # re-issue against. Say what fixes it.
            return (
                'Google did not return a refresh token, so this connection '
                'would stop working within the hour. Remove Honeycomb under '
                'your Google Account > Security > Third-party access, then '
                'connect again.'
            ), slug

        email = fetch_google_email(str(token.get('access_token') or ''))
        label = getattr(connector, 'label', slug)
        name = '{0} - {1}'.format(label, email) if email else label
        # Reconnecting an account that is already connected UPDATES it. It
        # does not add a second one.
        #
        # Two reasons, and the second is the important one. Consenting again is
        # how somebody fixes a connection whose refresh token was revoked, so
        # it has to be a repair rather than a way to accumulate duplicates of
        # one account. And a connection's endpoint_slug is embedded in every
        # MCP URL already pasted into an AI client -- replacing the row would
        # silently break all of them, while updating it in place means the
        # repair is invisible to the client, which is the whole point.
        existing = None
        if email:
            existing = (
                Connection.objects
                .filter(tenant=state.tenant, connector=slug, account_key=email)
                .first()
            )

        if existing is not None:
            connection = existing
            # The name is refreshed but disabled_tools is NOT: whichever tools
            # this person switched off, they switched off deliberately, and a
            # token refresh is no reason to turn them back on.
            connection.name = name[:80]
            connection.status = Connection.STATUS_ACTIVE
            connection.last_error = ''
        else:
            connection = Connection(
                # From the nonce row, never from the request: this route has no
                # session, so the tenant recorded at start time is the only
                # trustworthy answer to "whose connection is this".
                tenant=state.tenant,
                created_by=state.user,
                connector=slug,
                name=name[:80],
                account_key=email,
                # As falcon does: a connection that was created by clicking a
                # consent screen starts read-only. Turning a write tool on is
                # then a deliberate, separate act in the dashboard.
                disabled_tools=list(getattr(connector, 'write_tools', ()) or ()),
            )

        # set_creds, never a plain column: the refresh token is the whole
        # credential, and it is Fernet-encrypted at rest like every other one.
        connection.set_creds({
            'refresh_token': refresh_token,
            'scope': str(token.get('scope') or ''),
            'email': email,
        })
        connection.save()
        return '', slug

    def _claim_state(self, state_value: str):
        """Burn the nonce and return it, or None if it was not redeemable.

        The claim is a conditional UPDATE rather than a read-then-save: two
        callbacks arriving together would both pass an in-Python
        ``used_at is None`` check and both mint a connection. ``filter(
        used_at__isnull=True).update(...)`` makes exactly one of them win, in
        the database.

        It happens BEFORE the authorization code is spent, deliberately. A code
        replayed after a slow or failed exchange finds a nonce that is already
        used, which costs the user one more click and costs an attacker the
        whole attack.
        """
        state = (
            ConnectorOAuthState.objects
            # The nonce is the only thing looked up, and the connector is read
            # back off it. Nothing the caller sends names a connector, so a
            # nonce cannot be redeemed against one it was not minted for.
            .filter(state=state_value)
            .select_related('tenant', 'user')
            .first()
        )
        if state is None or not state.is_fresh():
            return None
        claimed = ConnectorOAuthState.objects.filter(
            pk=state.pk, used_at__isnull=True
        ).update(used_at=timezone.now())
        if not claimed:
            return None
        return state

    def _exchange(self, code: str):
        """Trade the authorization code for tokens. -> (payload, error_message)."""
        payload = {
            'client_id': _setting('GOOGLE_CLIENT_ID'),
            'client_secret': _setting('GOOGLE_CLIENT_SECRET'),
            'code': code,
            'grant_type': 'authorization_code',
            # Google re-checks this against the authorize request. Same builder,
            # so the two cannot drift.
            'redirect_uri': google_redirect_uri(),
        }
        try:
            response = httpx.post(
                _setting('GOOGLE_OAUTH_TOKEN_URI', DEFAULT_TOKEN_URI),
                data=payload,
                timeout=TOKEN_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            return {}, 'Could not reach Google to finish signing in ({0}).'.format(
                redact_text(exc)[:200]
            )
        if response.status_code != 200:
            # THE reason redact_text is not optional here: Google's 400 body
            # quotes the request it rejected, and the request carried
            # client_secret. Redact first, then truncate -- truncating first
            # can cut a secret in half and leave the half that still matters.
            return {}, 'Google rejected the sign-in ({0}): {1}'.format(
                response.status_code, redact_text(response.text[:1000])[:300]
            )
        return response.json(), ''
