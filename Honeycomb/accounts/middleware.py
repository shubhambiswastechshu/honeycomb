"""
Rate limiting for the Django admin's login form.

DRF's throttles cover /api/ only -- ``/admin/login/`` is a plain Django view and
answers an unlimited number of wrong passwords, one HTTP 200 re-render at a
time. That is an online password-guessing oracle against the most privileged
accounts in the system, so it needs a ceiling of its own.

Deliberately small and dependency-free: django-axes would bring a model, a
migration and an admin of its own for a project whose only staff account is the
platform superuser. The counter lives in the default cache -- see the
SECURITY WARNING on CACHES in settings.py about what that means once more than
one worker process is serving.
"""
from django.core.cache import cache
from django.http import HttpResponse
from django.urls import NoReverseMatch, reverse

#: Failed POSTs to the admin login form allowed per client address per window.
#: Generous for a human who mistyped, useless for a script.
ADMIN_LOGIN_MAX_ATTEMPTS = 10

#: Seconds. Also how long a blocked address stays blocked, since every further
#: attempt refreshes the counter's expiry.
ADMIN_LOGIN_WINDOW_SECONDS = 300

_UNRESOLVED = object()


def _client_ip(request):
    """
    The address to key on.

    REMOTE_ADDR only, never X-Forwarded-For: that header is whatever the caller
    types, so trusting it would hand an attacker an unlimited supply of fresh
    buckets -- the same mistake REST_FRAMEWORK['NUM_PROXIES'] exists to prevent
    on the API side. Behind a real proxy, have the proxy set REMOTE_ADDR.
    """
    return request.META.get('REMOTE_ADDR') or 'unknown'


class AdminLoginRateLimitMiddleware(object):
    """Caps failed admin sign-in attempts per client address."""

    def __init__(self, get_response):
        self.get_response = get_response
        # Resolved on first use rather than here: middleware is instantiated
        # while the handler loads, and forcing the URLconf to import that early
        # is a needless import-order hazard.
        self._login_path = _UNRESOLVED

    def login_path(self):
        if self._login_path is _UNRESOLVED:
            try:
                self._login_path = reverse('admin:login')
            except NoReverseMatch:
                # Admin not installed or not routed; nothing to guard.
                self._login_path = None
        return self._login_path

    def __call__(self, request):
        # Fast path: everything that is not a login submission is untouched.
        if request.method != 'POST' or request.path != self.login_path():
            return self.get_response(request)

        key = 'admin-login-attempts:' + _client_ip(request)
        attempts = cache.get(key, 0)
        if attempts >= ADMIN_LOGIN_MAX_ATTEMPTS:
            response = HttpResponse(
                'Too many sign-in attempts. Try again later.',
                content_type='text/plain',
                status=429,
            )
            response['Retry-After'] = str(ADMIN_LOGIN_WINDOW_SECONDS)
            return response

        response = self.get_response(request)

        # The admin redirects on success and re-renders the form on failure, so
        # a 302 is the only outcome that clears the count.
        if response.status_code == 302:
            cache.delete(key)
        else:
            cache.set(key, attempts + 1, ADMIN_LOGIN_WINDOW_SECONDS)
        return response
