"""
Cookie-transported JWT authentication.

The access token moves in an httpOnly cookie rather than an Authorization
header so that the Next.js middleware -- which runs on the server, before a
page is rendered, and therefore cannot read localStorage -- can decide whether
a request is authenticated. httpOnly also removes the token from the reach of
any script that manages to run on the page, which is the main reason
localStorage is a poor place to keep credentials.

That transport swap has one consequence that must not be skipped: a cookie is
sent *ambiently*. The browser attaches it to any request the origin receives,
including one forged by a third-party page, so a cookie-authenticated endpoint
that mutates state is cross-site request forgeable by construction. A bearer
header is immune only because a foreign page cannot set it. Restoring that
protection is the job of :meth:`CookieJWTAuthentication.enforce_csrf`, which
runs Django's CSRF machinery exactly the way
``rest_framework.authentication.SessionAuthentication`` does -- DRF views are
``csrf_exempt`` at the middleware level, so the check has to happen here.
"""
from django.conf import settings
from rest_framework import exceptions
from rest_framework.authentication import CSRFCheck
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken

#: Methods RFC 9110 defines as safe, plus TRACE -- the same set Django's
#: CsrfViewMiddleware exempts. Kept here so the intent is readable at the call
#: site even though the middleware re-derives it.
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS', 'TRACE')


def access_cookie_name():
    return getattr(settings, 'HC_ACCESS_COOKIE', 'hc_access')


def refresh_cookie_name():
    return getattr(settings, 'HC_REFRESH_COOKIE', 'hc_refresh')


def _cookie_security():
    return {
        'secure': getattr(settings, 'HC_COOKIE_SECURE', not settings.DEBUG),
        'samesite': getattr(settings, 'HC_COOKIE_SAMESITE', 'Lax'),
        'path': '/',
    }


def _lifetime_seconds(key, fallback):
    lifetime = settings.SIMPLE_JWT.get(key) if hasattr(settings, 'SIMPLE_JWT') else None
    return int(lifetime.total_seconds()) if lifetime is not None else fallback


def issue_tokens(user):
    """Mint the token pair for ``user`` with the tenant claim attached."""
    refresh = RefreshToken.for_user(user)
    # The tenant travels inside the JWT so a request can be scoped without a
    # database round-trip. RefreshToken.access_token copies every custom claim,
    # and so does the access token minted from a refresh at /auth/refresh/.
    refresh['tenant_id'] = user.tenant_id
    return refresh


def session_seconds():
    """How long a browser should keep the auth cookies.

    The *session* lasts as long as a refresh token can renew it, which is not
    the same as how long an access token is valid. Both cookies therefore carry
    the refresh lifetime.
    """
    return _lifetime_seconds('REFRESH_TOKEN_LIFETIME', 604800)


def set_access_cookie(response, access_token):
    """Write the access cookie.

    `max_age` is the *session* length, deliberately not the access token's own
    60 minutes. Those are different clocks and conflating them was a bug: the
    browser deleted this cookie an hour after sign-in, and because the Next.js
    middleware gates /dashboard on the cookie being present, the visitor was
    bounced to /signin -- while a refresh token good for another six days sat
    untouched in the next cookie along. The redirect happens at the edge, so
    the client never got as far as the 401-then-refresh path that would have
    renewed it silently.

    The cookie outliving the token it carries is safe: presence is not
    authority. Django verifies the JWT's signature and expiry on every request,
    so a stale cookie buys exactly one 401, which the client answers with a
    refresh.
    """
    kwargs = _cookie_security()
    response.set_cookie(
        access_cookie_name(),
        str(access_token),
        max_age=session_seconds(),
        httponly=True,
        **kwargs
    )
    return response


def set_auth_cookies(response, user):
    """Attach a freshly minted access/refresh pair for ``user`` to ``response``."""
    refresh = issue_tokens(user)
    set_access_cookie(response, refresh.access_token)
    set_refresh_cookie(response, refresh)
    return response


def set_refresh_cookie(response, refresh_token):
    kwargs = _cookie_security()
    response.set_cookie(
        refresh_cookie_name(),
        str(refresh_token),
        max_age=session_seconds(),
        httponly=True,
        **kwargs
    )
    return response


def clear_auth_cookies(response):
    kwargs = _cookie_security()
    for name in (access_cookie_name(), refresh_cookie_name()):
        response.delete_cookie(
            name, path=kwargs['path'], samesite=kwargs['samesite']
        )
    return response


def enforce_csrf(request):
    """
    Run Django's CSRF check against ``request`` and raise on failure.

    Lifted from ``SessionAuthentication.enforce_csrf``: build a CSRFCheck with
    a throwaway get_response, let ``process_request`` populate the expected
    token from the csrftoken cookie, then ask ``process_view`` to compare it
    with the ``X-CSRFToken`` header (or form field). ``CSRFCheck`` overrides
    ``_reject`` to return the reason instead of an HttpResponse, so a non-None
    return means the check failed.
    """

    def dummy_get_response(request):  # pragma: no cover
        return None

    check = CSRFCheck(dummy_get_response)
    check.process_request(request)
    reason = check.process_view(request, None, (), {})
    if reason:
        raise exceptions.PermissionDenied('CSRF Failed: %s' % reason)


class CookieJWTAuthentication(JWTAuthentication):
    """
    Read the access token from the ``hc_access`` cookie, then enforce CSRF.

    The Authorization header remains a secondary path: it costs one call to
    ``super().authenticate()`` and keeps curl, the browsable API and any
    server-to-server caller working. Header-borne tokens are deliberately *not*
    CSRF-checked -- a cross-site page cannot set an Authorization header, so
    there is nothing ambient to protect against, and demanding a csrftoken from
    a non-browser client would be noise.

    Cookie-borne tokens are the opposite case and always pay the CSRF cost on
    unsafe methods. Without it, ``evil.example`` could POST a form at
    /api/auth/change-email/ and the browser would attach hc_access for it.
    """

    def authenticate(self, request):
        raw_token = request.COOKIES.get(access_cookie_name())
        if raw_token:
            validated_token = self.get_validated_token(raw_token)
            user = self.get_user(validated_token)
            # Only after the token proves out: an unauthenticated request has
            # no ambient authority to forge, and answering 403 CSRF to an
            # anonymous caller would just be confusing.
            if request.method not in SAFE_METHODS:
                enforce_csrf(request)
            return (user, validated_token)
        return super(CookieJWTAuthentication, self).authenticate(request)
