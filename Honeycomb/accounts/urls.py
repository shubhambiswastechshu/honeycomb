from django.urls import path

from .team import (
    InvitationAcceptView,
    InvitationListView,
    InvitationLookupView,
    InvitationRevokeView,
    TeamView,
)
from .views import (
    ChangeEmailView,
    ChangePasswordView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
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
    path('password-reset/', PasswordResetRequestView.as_view(), name='password-reset'),
    path('password-reset/confirm/', PasswordResetConfirmView.as_view(),
         name='password-reset-confirm'),
    # Redeeming an invitation happens before there is an account, so it lives
    # with the other session-less endpoints rather than under /team/.
    path('invite/', InvitationLookupView.as_view(), name='invite-lookup'),
    path('invite/accept/', InvitationAcceptView.as_view(), name='invite-accept'),
]

#: Mounted at /api/ rather than /api/auth/, so it is a separate list the root
#: URLconf includes on its own prefix.
tenant_urlpatterns = [
    path('tenant/', TenantUpdateView.as_view(), name='tenant'),
    path('team/', TeamView.as_view(), name='team'),
    path('team/invites/', InvitationListView.as_view(), name='team-invites'),
    path('team/invites/<int:pk>/revoke/', InvitationRevokeView.as_view(),
         name='team-invite-revoke'),
]
