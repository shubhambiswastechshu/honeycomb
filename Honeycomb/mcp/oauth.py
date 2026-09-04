"""The OAuth 2.1 authorization server that lets claude.ai add a connection.

WHY THIS EXISTS
claude.ai's custom-connector dialog takes a URL and nothing else. There is no
field for a static token, so the `hc_...` keys this product mints -- which work
fine in Claude Code and Claude Desktop, where a header can be configured -- can
never be handed to it. A browser client can only obtain a credential by being
walked through an authorization flow, so the server has to run one.

The discovery chain a client follows, and which function serves each step:

  1. POST /mcp/<connector>/<slug>/ with no token
       -> 401 + WWW-Authenticate: ... resource_metadata="<the URL in step 2>"
          (mcp/endpoint.py, not this file)
  2. GET /.well-known/oauth-protected-resource/mcp/<connector>/<slug>/
       -> protected_resource_metadata()          [RFC 9728]
  3. GET /.well-known/oauth-authorization-server
       -> authorization_server_metadata()        [RFC 8414]
  4. POST /oauth/register
       -> register()                             [RFC 7591]
  5. GET  /oauth/authorize  -> authorize()       [user signs in, approves ONE connection]
  6. POST /oauth/token      -> token()           [PKCE code exchange, then refresh]

Break any link and the client falls back to "Couldn't determine the server
settings" -- which is the exact symptom this file was written to fix.

These are plain Django views on purpose. Honeycomb/asgi.py routes /mcp/** to
FastAPI and EVERYTHING ELSE to Django, so /.well-known/** and /oauth/** arrive
here; putting them in the FastAPI app would make them unreachable.
"""
import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_deny
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from connections.models import Connection

from .models import OAuthClient, OAuthGrant, OAuthToken

# Scope names are advertised in the metadata and echoed back in token responses.
# One scope, because a token's authority is decided by WHICH connection it is
# bound to, not by a scope string.
SCOPES = ['mcp']


def public_base():
    """The origin this server is reached at, exactly as clients will see it.

    Must match what the client used, character for character: the `resource`
    in the metadata is compared against the URL the client called, and an
    http/https or host mismatch invalidates the whole chain.
    """
    return (getattr(settings, 'HONEYCOMB_PUBLIC_BASE', '') or '').rstrip('/')


def resource_url(connector, slug):
    return '{0}/mcp/{1}/{2}/'.format(public_base(), connector, slug)


def resource_metadata_url(connector, slug):
    """RFC 9728 inserts the resource's PATH after the well-known segment.

    So the document for https://host/mcp/ga4/abc/ lives at
    https://host/.well-known/oauth-protected-resource/mcp/ga4/abc/ -- not at the
    bare well-known path. Clients construct this themselves and will not find a
    document served anywhere else.
    """
    return '{0}/.well-known/oauth-protected-resource/mcp/{1}/{2}/'.format(
        public_base(), connector, slug)


def _json(payload, status=200):
    response = JsonResponse(payload, status=status)
    # Discovery documents are fetched cross-origin by the client's own backend
    # and, in some clients, from the browser. They contain no secrets.
    response['Access-Control-Allow-Origin'] = '*'
    response['Cache-Control'] = 'public, max-age=300'
    return response


@require_http_methods(['GET', 'OPTIONS'])
def protected_resource_metadata(request, connector=None, slug=None):
    """RFC 9728. Tells the client which authorization server guards this URL."""
    if connector and slug:
        resource = resource_url(connector, slug)
    else:
        # The bare well-known path. Some clients probe it before trying the
        # path-inserted one; answering keeps them from giving up early.
        resource = '{0}/mcp/'.format(public_base())
    return _json({
        'resource': resource,
        'authorization_servers': [public_base()],
        'scopes_supported': SCOPES,
        'bearer_methods_supported': ['header'],
        'resource_documentation': '{0}/dashboard/connectors'.format(
            (getattr(settings, 'HONEYCOMB_FRONTEND_BASE', '') or '').rstrip('/')),
    })


@require_http_methods(['GET', 'OPTIONS'])
def authorization_server_metadata(request, connector=None, slug=None):
    """RFC 8414. The endpoint map, plus the two facts that gate a modern client.

    `registration_endpoint` is what makes dynamic registration possible; without
    it claude.ai has no client_id and stops. `code_challenge_methods_supported`
    must list S256, or an OAuth 2.1 client refuses to start a flow it cannot
    protect.
    """
    base = public_base()
    return _json({
        'issuer': base,
        'authorization_endpoint': '{0}/oauth/authorize'.format(base),
        'token_endpoint': '{0}/oauth/token'.format(base),
        'registration_endpoint': '{0}/oauth/register'.format(base),
        'scopes_supported': SCOPES,
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        'token_endpoint_auth_methods_supported': ['none'],
        'service_documentation': '{0}/dashboard/connectors'.format(
            (getattr(settings, 'HONEYCOMB_FRONTEND_BASE', '') or '').rstrip('/')),
    })


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def register(request):
    """RFC 7591 dynamic client registration.

    Deliberately open: any client may register, and registration grants nothing
    on its own. A client_id is not a credential here -- it names a redirect
    allow-list, and every actual authority still comes from a human approving a
    named connection at /oauth/authorize.
    """
    if request.method == 'OPTIONS':
        return _cors_preflight()
    try:
        payload = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return _json({'error': 'invalid_client_metadata',
                      'error_description': 'Body must be JSON.'}, status=400)

    redirect_uris = payload.get('redirect_uris') or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _json({'error': 'invalid_redirect_uri',
                      'error_description': 'redirect_uris is required.'}, status=400)
    for uri in redirect_uris:
        if not isinstance(uri, str) or not _redirect_uri_is_sane(uri):
            return _json({'error': 'invalid_redirect_uri',
                          'error_description': 'Unsupported redirect_uri: {0}'.format(uri)},
                         status=400)

    client = OAuthClient.objects.create(
        client_id=OAuthClient.new_client_id(),
        client_name=str(payload.get('client_name') or '')[:160],
        redirect_uris=redirect_uris,
    )
    # No client_secret: token_endpoint_auth_method is "none" and PKCE carries
    # the proof. Returning one would invite clients to store it in a browser.
    return _json({
        'client_id': client.client_id,
        'client_name': client.client_name,
        'redirect_uris': client.redirect_uris,
        'token_endpoint_auth_method': 'none',
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'client_id_issued_at': int(client.created_at.timestamp()),
    }, status=201)


def _redirect_uri_is_sane(uri):
    """Allow https anywhere, and http only on loopback (for desktop clients)."""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if parsed.scheme == 'https':
        return bool(parsed.netloc)
    if parsed.scheme == 'http':
        return parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    # Custom schemes (claude://, cursor://) are how native clients come back.
    return bool(parsed.scheme) and '://' in uri


def _cors_preflight():
    response = JsonResponse({}, status=204)
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response['Access-Control-Allow-Headers'] = 'authorization, content-type'
    return response


def _error_redirect(redirect_uri, state, code, description):
    params = {'error': code, 'error_description': description}
    if state:
        params['state'] = state
    joiner = '&' if '?' in redirect_uri else '?'
    return HttpResponseRedirect(redirect_uri + joiner + urlencode(params))


@xframe_options_deny
@require_http_methods(['GET', 'POST'])
def authorize(request):
    """The consent screen. The only place a human is in this loop.

    Two failure styles, and the difference matters for security: anything wrong
    with `client_id` or `redirect_uri` is rendered as a PAGE, because redirecting
    an error to an unverified URI is how open redirectors are built. Everything
    after those two are verified is reported by redirecting, as OAuth requires.
    """
    params = request.POST if request.method == 'POST' else request.GET
    client_id = params.get('client_id', '')
    redirect_uri = params.get('redirect_uri', '')
    state = params.get('state', '')
    challenge = params.get('code_challenge', '')
    method = params.get('code_challenge_method', '')
    resource = params.get('resource', '')

    client = OAuthClient.objects.filter(client_id=client_id).first()
    if client is None:
        return _deny(request, 'Unknown client. Add the connector again from your AI client.')
    if not redirect_uri or not client.allows(redirect_uri):
        return _deny(request, 'This client asked to be sent back to an address it '
                              'did not register. Nothing was approved.')

    if params.get('response_type', 'code') != 'code':
        return _error_redirect(redirect_uri, state, 'unsupported_response_type',
                               'Only the authorization code flow is supported.')
    # PKCE is mandatory, not negotiated. These clients are public: without a
    # verifier, anyone who intercepts the code can redeem it.
    if not challenge or method != 'S256':
        return _error_redirect(redirect_uri, state, 'invalid_request',
                               'PKCE with code_challenge_method=S256 is required.')

    user = _signed_in_user(request)
    if user is None:
        # Send them to the product's own sign-in, then straight back here with
        # every parameter intact, so the flow resumes where it paused.
        frontend = (getattr(settings, 'HONEYCOMB_FRONTEND_BASE', '') or '').rstrip('/')
        back = '{0}/oauth/authorize?{1}'.format(public_base(), urlencode(dict(params.items())))
        return HttpResponseRedirect('{0}/signin?{1}'.format(
            frontend, urlencode({'next': back})))

    connection = _connection_for(user, resource)
    choices = list(_connections_for(user))

    if request.method == 'GET':
        # Three states, and they are shown differently because they need
        # different actions from the person reading the page:
        #   - the resource named one connection  -> confirm that one
        #   - several to choose from             -> pick one
        #   - none at all                        -> nothing to approve, and the
        #     signed-in account is named, because the usual cause is being
        #     signed in as the wrong one (the platform admin owns no tenant).
        return render(request, 'mcp/authorize.html', {
            'client': client,
            'connection': connection,
            'choices': choices,
            'params': params.items(),
            'needs_choice': connection is None,
            'account': getattr(user, 'email', ''),
            'frontend': (getattr(settings, 'HONEYCOMB_FRONTEND_BASE', '') or '').rstrip('/'),
        })

    # POST: the user pressed a button.
    if params.get('decision') != 'allow':
        return _error_redirect(redirect_uri, state, 'access_denied',
                               'The account holder declined.')
    if connection is None:
        chosen = params.get('connection')
        connection = next((c for c in choices if str(c.pk) == str(chosen)), None)
    if connection is None:
        return _error_redirect(redirect_uri, state, 'invalid_request',
                               'No connection was selected.')

    code = secrets.token_urlsafe(32)
    OAuthGrant.objects.create(
        client=client, user=user, connection=connection,
        code_hash=OAuthToken.hash_token(code), redirect_uri=redirect_uri,
        code_challenge=challenge, code_challenge_method='S256',
    )
    out = {'code': code}
    if state:
        out['state'] = state
    joiner = '&' if '?' in redirect_uri else '?'
    return HttpResponseRedirect(redirect_uri + joiner + urlencode(out))


def _deny(request, message):
    return render(request, 'mcp/authorize_error.html', {'message': message}, status=400)


def _signed_in_user(request):
    """Who is at the keyboard, preferring the PRODUCT identity over the admin's.

    Two sessions can exist in one browser at once on this host: the portal's
    httpOnly JWT cookie, and a Django admin session. The order below is the
    whole point of this function.

    The portal cookie wins. Checking request.user first -- the obvious way to
    write this -- reads the ADMIN session, and the platform admin deliberately
    belongs to no tenant, so the consent screen offered a user with two live
    connections an empty list and the message "You have no connected data
    sources yet". The account that owns connections is the one that must be
    asked to share them.

    The Django session remains a fallback so that a staff user who only ever
    signs in at /admin/ can still complete a flow.
    """
    try:
        from accounts.authentication import access_cookie_name
        from rest_framework_simplejwt.authentication import JWTAuthentication
    except ImportError:
        access_cookie_name = None
    if access_cookie_name is not None:
        raw = request.COOKIES.get(access_cookie_name())
        if raw:
            try:
                backend = JWTAuthentication()
                return backend.get_user(backend.get_validated_token(raw))
            except Exception:
                # An expired or tampered cookie is simply "not signed in" --
                # fall through to the session, then to the /signin redirect.
                pass
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return user
    return None


def _connections_for(user):
    tenant_id = getattr(user, 'tenant_id', None)
    if not tenant_id:
        return Connection.objects.none()
    return Connection.objects.filter(tenant_id=tenant_id).order_by('connector', 'name')


def _connection_for(user, resource):
    """Resolve RFC 8707's `resource` to one of the user's own connections.

    Scoped to the user's tenant by construction, so naming another tenant's
    endpoint URL resolves to nothing rather than to their data.
    """
    if not resource:
        return None
    path = urlparse(resource).path.strip('/')
    parts = path.split('/')
    if len(parts) < 3 or parts[0] != 'mcp':
        return None
    connector, slug = parts[1], parts[2]
    return _connections_for(user).filter(connector=connector, endpoint_slug=slug).first()


@csrf_exempt
@require_http_methods(['POST', 'OPTIONS'])
def token(request):
    """Code -> access token, and refresh -> access token."""
    if request.method == 'OPTIONS':
        return _cors_preflight()
    grant_type = request.POST.get('grant_type', '')
    if grant_type == 'authorization_code':
        return _token_from_code(request)
    if grant_type == 'refresh_token':
        return _token_from_refresh(request)
    return _json({'error': 'unsupported_grant_type'}, status=400)


def _token_from_code(request):
    code = request.POST.get('code', '')
    verifier = request.POST.get('code_verifier', '')
    client_id = request.POST.get('client_id', '')
    redirect_uri = request.POST.get('redirect_uri', '')

    grant = (OAuthGrant.objects
             .select_related('client', 'connection', 'user')
             .filter(code_hash=OAuthToken.hash_token(code), consumed_at__isnull=True)
             .first())
    if grant is None:
        return _json({'error': 'invalid_grant',
                      'error_description': 'Unknown, expired or already-used code.'}, status=400)
    age = (timezone.now() - grant.created_at).total_seconds()
    if age > OAuthGrant.LIFETIME_SECONDS:
        return _json({'error': 'invalid_grant', 'error_description': 'Code expired.'}, status=400)
    if grant.client.client_id != client_id:
        return _json({'error': 'invalid_grant',
                      'error_description': 'Code was issued to a different client.'}, status=400)
    if redirect_uri and redirect_uri != grant.redirect_uri:
        return _json({'error': 'invalid_grant',
                      'error_description': 'redirect_uri does not match the authorization.'},
                     status=400)
    if not _pkce_ok(verifier, grant.code_challenge):
        return _json({'error': 'invalid_grant',
                      'error_description': 'PKCE verification failed.'}, status=400)

    # One transaction: the code is burned and the token is born together, so a
    # racing second redemption finds consumed_at already set.
    with transaction.atomic():
        spent = (OAuthGrant.objects
                 .filter(pk=grant.pk, consumed_at__isnull=True)
                 .update(consumed_at=timezone.now()))
        if not spent:
            return _json({'error': 'invalid_grant',
                          'error_description': 'Code already used.'}, status=400)
        return _issue(grant.client, grant.user, grant.connection)


def _token_from_refresh(request):
    presented = request.POST.get('refresh_token', '')
    client_id = request.POST.get('client_id', '')
    existing = (OAuthToken.objects
                .select_related('client', 'connection', 'user')
                .filter(refresh_hash=OAuthToken.hash_token(presented), revoked_at__isnull=True)
                .first())
    if existing is None or (client_id and existing.client.client_id != client_id):
        return _json({'error': 'invalid_grant',
                      'error_description': 'Unknown or revoked refresh token.'}, status=400)
    with transaction.atomic():
        # Rotate: the presented refresh token dies with the access token it
        # renewed, so a captured one is useful only until its owner next refreshes.
        OAuthToken.objects.filter(pk=existing.pk).update(revoked_at=timezone.now())
        return _issue(existing.client, existing.user, existing.connection)


def _issue(client, user, connection):
    access = OAuthToken.PREFIX + secrets.token_urlsafe(32)
    refresh = OAuthToken.PREFIX + 'r_' + secrets.token_urlsafe(32)
    expires_at = timezone.now() + timezone.timedelta(seconds=OAuthToken.LIFETIME_SECONDS)
    OAuthToken.objects.create(
        client=client, user=user, connection=connection,
        token_hash=OAuthToken.hash_token(access),
        refresh_hash=OAuthToken.hash_token(refresh),
        expires_at=expires_at,
    )
    OAuthClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
    response = _json({
        'access_token': access,
        'token_type': 'Bearer',
        'expires_in': OAuthToken.LIFETIME_SECONDS,
        'refresh_token': refresh,
        'scope': ' '.join(SCOPES),
    })
    # A token response must never be cached, whatever _json's default says.
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response


def _pkce_ok(verifier, challenge):
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return secrets.compare_digest(expected, challenge)
