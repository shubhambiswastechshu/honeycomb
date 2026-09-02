"""
Control-plane routes, mounted by the root URLconf at /api/.

SimpleRouter rather than DefaultRouter: DefaultRouter adds an API-root view at
the empty path, which under an /api/ prefix would claim /api/ itself and shadow
whatever else the project mounts there.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from .oauth import GoogleOAuthCallbackView, GoogleOAuthStartView
from .views import ConnectionViewSet, ConnectorCatalogView

app_name = 'connections'

router = SimpleRouter(trailing_slash=True)
router.register('connections', ConnectionViewSet, basename='connection')

urlpatterns = [
    path('connectors/', ConnectorCatalogView.as_view(), name='connector-list'),
    # Declared above connector-detail. <str:> cannot swallow a '/', so the
    # detail route could not match these anyway -- but the OAuth pair is the
    # more specific pattern and reads better where nothing has to be reasoned
    # about to see that it wins.
    #
    # The callback path is registered in Google Cloud Console, so it is as
    # permanent as an endpoint_slug: changing it breaks every OAuth client
    # already configured. connections.oauth.google_redirect_uri builds this
    # same string -- keep the two in step.
    path(
        'connectors/<str:slug>/oauth/start/',
        GoogleOAuthStartView.as_view(),
        name='connector-oauth-start',
    ),
    path(
        # No <slug>: ONE registered redirect URI serves every connector, and
        # which connector is being connected comes off the nonce. Declared
        # before 'connectors/<str:slug>/' so the literal wins over the pattern.
        'connectors/oauth/callback/',
        GoogleOAuthCallbackView.as_view(),
        name='connector-oauth-callback',
    ),
    # <str:> and not <slug:>: a registry slug is ours to choose and has always
    # been slug-shaped, but routing must not be the thing that decides that.
    path('connectors/<str:slug>/', ConnectorCatalogView.as_view(), name='connector-detail'),
] + router.urls
