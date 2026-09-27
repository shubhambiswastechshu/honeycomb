"""
Saved-report routes, mounted by the root URLconf at /api/.

SimpleRouter for the same reason as connections.urls: DefaultRouter would add an
API-root view at the empty path and shadow whatever else is mounted under /api/.
"""

from rest_framework.routers import SimpleRouter

from .views import ReportViewSet

app_name = 'reports'

router = SimpleRouter(trailing_slash=True)
router.register('reports', ReportViewSet, basename='report')

urlpatterns = router.urls
