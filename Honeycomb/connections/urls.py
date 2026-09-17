"""
Control-plane routes, mounted by the root URLconf at /api/.

SimpleRouter rather than DefaultRouter: DefaultRouter adds an API-root view at
the empty path, which under an /api/ prefix would claim /api/ itself and shadow
whatever else the project mounts there.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from .oauth import GoogleOAuthCallbackView, GoogleOAuthStartView, LinkedInOAuthCallbackView
from .plugin import download as plugin_download
from .plugin import update_manifest as plugin_update_manifest
from .views import ConnectionViewSet, ConnectorCatalogView

app_name = 'connections'

router = SimpleRouter(trailing_slash=True)
router.register('connections', ConnectionViewSet, basename='connection')

urlpatterns = [
    path('connectors/', ConnectorCatalogView.as_view(), name='connector-list'),
    # The WordPress connector cannot be set up without this file, so it is
    # served from here rather than from a repository the user has no access to.
    # Declared above the <slug> patterns, which would otherwise swallow it.
    path('plugins/wordpress/', plugin_download, name='plugin-wordpress'),
    # The update server an installed plugin polls (see connections.plugin.
    # update_manifest). Declared next to the download it points at.
    path('plugins/wordpress/update.json', plugin_update_manifest,
         name='plugin-wordpress-update'),
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
    # LinkedIn's landing route. Registered in the LinkedIn app's Auth tab, so
    # as permanent as the Google one; connections.linkedin.redirect_uri builds it.
    path(
        'connectors/oauth/linkedin/callback/',
        LinkedInOAuthCallbackView.as_view(),
        name='connector-oauth-linkedin-callback',
    ),
    # <str:> and not <slug:>: a registry slug is ours to choose and has always
    # been slug-shaped, but routing must not be the thing that decides that.
    path('connectors/<str:slug>/', ConnectorCatalogView.as_view(), name='connector-detail'),
] + router.urls
