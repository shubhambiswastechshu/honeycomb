from django.conf import settings
from django.db import IntegrityError
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .authentication import (
    SAFE_METHODS,
    CookieJWTAuthentication,
    clear_auth_cookies,
    enforce_csrf,
    set_access_cookie,
    set_auth_cookies,
)
from .exceptions import (
    AMBIGUOUS_ORGANIZATION_DETAIL,
    TENANTLESS_DETAIL,
    AmbiguousOrganizationError,
)
from .models import User
from .serializers import (
    ChangeEmailSerializer,
    ChangePasswordSerializer,
    ProfileUpdateSerializer,
    SignInSerializer,
    SignUpCheckSerializer,
    SignUpSerializer,
    TenantSerializer,
    TenantUpdateSerializer,
    UserSerializer,
)


def identity_payload(user):
    """
    The {"user", "tenant"} body of every auth response.

    The contract types "tenant" as an object and never as null, so callers must
    reject tenant-less (platform superuser) accounts before getting here --
    SignInSerializer.validate() and tenant_guard() both do.
    """
    return {
        'user': UserSerializer(user).data,
        'tenant': TenantSerializer(user.tenant).data,
    }


def tenant_guard(user):
    """
    403 for a tenant-less account, or None when the account is usable.

    Mirrors TenantScopedQuerysetMixin.get_tenant(): a platform-level superuser
    is not a tenant identity and must not be handed a null tenant the contract
    does not allow.
    """
    if user.tenant_id is None:
        return Response(
            {'detail': TENANTLESS_DETAIL}, status=status.HTTP_403_FORBIDDEN
        )
    return None


class PublicAPIView(APIView):
    """
    Base for the endpoints that establish a session rather than consume one.

    authentication_classes is emptied on purpose. With the default
    CookieJWTAuthentication in place, a *stale* hc_access cookie makes
    authentication raise before the view runs, so a caller whose access token
    had expired would get 401 from /auth/signin/ and /auth/refresh/ -- exactly
    the two endpoints that exist to fix that state. These views read the
    cookies they care about directly.

    Emptying it also removes the only CSRF check that would have run, because
    DRF views are csrf_exempt at the middleware level -- so ``initial()`` puts
    the check back explicitly. Skipping it is not safe here: a forged signin
    plants *the attacker's* hc_access/hc_refresh cookies in the victim's
    browser (login CSRF), after which the victim types their own data into an
    account the attacker controls. The attacker never needs to read the
    response for that to work, so CORS is no defence, and SameSite=Lax does not
    help when the API and the app are same-site. A forged refresh or logout is
    less severe but has no reason to be allowed either.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def initial(self, request, *args, **kwargs):
        super(PublicAPIView, self).initial(request, *args, **kwargs)
        # Safe methods are exempt, which is what keeps GET /auth/csrf/ -- the
        # endpoint that hands out the token -- reachable without one.
        if request.method not in SAFE_METHODS:
            enforce_csrf(request)

    def get_authenticate_header(self, request):
        # DRF rewrites a 401 into a 403 whenever the view has no authenticator
        # to name in the WWW-Authenticate header. These views have none by
        # design, but bad credentials must still answer 401 the way they did
        # under header auth, so the scheme is named explicitly.
        return 'Bearer realm="api"'


@method_decorator(ensure_csrf_cookie, name='dispatch')
class CsrfView(PublicAPIView):
    """
    Primes the csrftoken cookie so the client can echo it back as a header.

    The frontend calls this once on boot. The cookie is intentionally not
    httpOnly -- the whole double-submit scheme depends on JavaScript being able
    to read it -- which is safe because knowing the token grants nothing on its
    own; it only proves the request came from a page that could read a
    same-site cookie.
    """

    def get(self, request):
        return Response({'ok': True}, status=status.HTTP_200_OK)


class SignUpView(PublicAPIView):
    # Unauthenticated row creation: cap it per client IP.
    throttle_scope = 'signup'

    def post(self, request):
        serializer = SignUpSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        response = Response(identity_payload(user), status=status.HTTP_201_CREATED)
        return set_auth_cookies(response, user)


class SignUpCheckView(PublicAPIView):
    """
    Validates one step of the signup wizard without creating anything.

    Returns 200 {"ok": true} when the supplied fields are acceptable, or the
    usual 400 field-error map when they are not, so the client can show the
    problem on the step that produced it.
    """

    # Creates nothing, so it is far cheaper than signup -- but it does probe
    # for address existence, so it still needs a ceiling.
    throttle_scope = 'signup_check'

    def post(self, request):
        serializer = SignUpCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({'ok': True}, status=status.HTTP_200_OK)


class SignInView(PublicAPIView):
    # Slows credential stuffing and makes the remaining timing signal
    # impractical to sample.
    throttle_scope = 'signin'

    def post(self, request):
        serializer = SignInSerializer(data=request.data, context={'request': request})
        try:
            serializer.is_valid(raise_exception=True)
        except AmbiguousOrganizationError as exc:
            # Additive to the contract: the 200 shape is unchanged, but a
            # password that is valid in several organizations gets a 409 and
            # the caller re-POSTs with the chosen organization_slug.
            return Response(
                {
                    'detail': AMBIGUOUS_ORGANIZATION_DETAIL,
                    'organizations': TenantSerializer(exc.tenants, many=True).data,
                },
                status=status.HTTP_409_CONFLICT,
            )
        user = serializer.validated_data['user']
        response = Response(identity_payload(user), status=status.HTTP_200_OK)
        return set_auth_cookies(response, user)


class CookieTokenRefreshView(PublicAPIView):
    """
    Exchanges the hc_refresh cookie for a fresh hc_access cookie.

    Neither token is ever in the body: the refresh token arrives as a cookie
    and the new access token leaves as one, so no part of the pair is exposed
    to page JavaScript.
    """

    throttle_scope = 'refresh'

    def post(self, request):
        raw_token = request.COOKIES.get(settings.HC_REFRESH_COOKIE)
        if not raw_token:
            return Response(
                {'detail': 'Refresh cookie is missing.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        try:
            refresh = RefreshToken(raw_token)
        except TokenError:
            # Never echo the library's reason back: it distinguishes expired
            # from malformed from wrong-signature, which is free reconnaissance.
            response = Response(
                {'detail': 'Refresh token is invalid or expired.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            # The cookie is dead weight from here on and would keep the client
            # retrying a refresh that cannot succeed.
            return clear_auth_cookies(response)
        # access_token copies the custom claims (tenant_id) off the refresh.
        access = refresh.access_token
        # RefreshToken() only proves the signature and expiry: the subject can
        # have been deactivated or deleted since it was minted, and minting a
        # fresh access cookie for them would let a dead account renew itself
        # indefinitely -- and, because the Next.js middleware keys on cookie
        # presence, trap the browser bouncing between /dashboard and /signin.
        # get_user() is the same lookup CookieJWTAuthentication performs, so
        # refresh accepts exactly the subjects /auth/me/ would.
        try:
            CookieJWTAuthentication().get_user(access)
        except AuthenticationFailed:
            response = Response(
                {'detail': 'Refresh token is invalid or expired.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            return clear_auth_cookies(response)
        response = Response({'ok': True}, status=status.HTTP_200_OK)
        return set_access_cookie(response, access)


class LogoutView(PublicAPIView):
    """
    Drops both auth cookies. Idempotent, and safe to call while signed out.

    Deleting the cookies ends the session for this browser but does not revoke
    the tokens themselves -- see the note in ChangePasswordView.
    """

    def post(self, request):
        response = Response({'ok': True}, status=status.HTTP_200_OK)
        return clear_auth_cookies(response)


class MeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = 'profile'

    def get(self, request):
        denied = tenant_guard(request.user)
        if denied is not None:
            return denied
        return Response(identity_payload(request.user), status=status.HTTP_200_OK)

    def patch(self, request):
        denied = tenant_guard(request.user)
        if denied is not None:
            return denied
        serializer = ProfileUpdateSerializer(
            instance=request.user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(identity_payload(user), status=status.HTTP_200_OK)


class ChangeEmailView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = 'change_email'

    def post(self, request):
        denied = tenant_guard(request.user)
        if denied is not None:
            return denied
        serializer = ChangeEmailSerializer(
            data=request.data, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.email = serializer.validated_data['new_email']
        try:
            user.save(update_fields=['email'])
        except IntegrityError:
            # Race backstop, same shape as SignUpSerializer.create(): two
            # concurrent changes can both pass validation and then collide.
            return Response(
                {'new_email': ['A user with this email already exists.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # The outstanding JWTs stay valid: they carry user_id, not the address.
        return Response(identity_payload(user), status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = 'change_password'

    def post(self, request):
        denied = tenant_guard(request.user)
        if denied is not None:
            return denied
        serializer = ChangePasswordSerializer(
            data=request.data, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.save(update_fields=['password'])
        response = Response({'ok': True}, status=status.HTTP_200_OK)
        # Reissue rather than clear: changing your own password should not sign
        # you out of the tab you did it in.
        #
        # Known limitation: a JWT is self-contained, so every access and
        # refresh token minted before this call stays cryptographically valid
        # until it expires (one hour and seven days respectively). A password
        # change therefore does not evict a session an attacker already holds.
        # Closing that gap means enabling ROTATE_REFRESH_TOKENS with
        # BLACKLIST_AFTER_ROTATION and installing
        # 'rest_framework_simplejwt.token_blacklist' so refresh tokens can be
        # revoked server-side. Deliberately not added here.
        return set_auth_cookies(response, user)


class TenantUpdateView(APIView):
    """Renames the caller's own organization. Never reachable across tenants."""

    permission_classes = [IsAuthenticated]
    throttle_scope = 'profile'

    def patch(self, request):
        denied = tenant_guard(request.user)
        if denied is not None:
            return denied
        if request.user.role not in (User.Role.OWNER, User.Role.ADMIN):
            return Response(
                {'detail': 'Only an owner or admin can rename the organization.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = TenantUpdateSerializer(
            instance=request.user.tenant, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        return Response(TenantSerializer(tenant).data, status=status.HTTP_200_OK)
