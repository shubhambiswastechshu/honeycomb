from django.urls import path

from .views import (
    ChangeEmailView,
    ChangePasswordView,
    CookieTokenRefreshView,
    CsrfView,
    LogoutView,
    MeView,
    SignInView,
    SignUpCheckView,
    SignUpView,
    TenantUpdateView,
)

app_name = 'accounts'

urlpatterns = [
    path('csrf/', CsrfView.as_view(), name='csrf'),
    path('signup/', SignUpView.as_view(), name='signup'),
    path('signup/check/', SignUpCheckView.as_view(), name='signup-check'),
    path('signin/', SignInView.as_view(), name='signin'),
    path('refresh/', CookieTokenRefreshView.as_view(), name='refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('me/', MeView.as_view(), name='me'),
    path('change-email/', ChangeEmailView.as_view(), name='change-email'),
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),
]

#: Mounted at /api/ rather than /api/auth/, so it is a separate list the root
#: URLconf includes on its own prefix.
tenant_urlpatterns = [
    path('tenant/', TenantUpdateView.as_view(), name='tenant'),
]
