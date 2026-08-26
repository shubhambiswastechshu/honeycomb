from rest_framework.routers import SimpleRouter

from .views import WorkspaceViewSet

app_name = 'workspaces'

# SimpleRouter, not DefaultRouter: the latter adds a browsable API root at "/"
# that would collide with the other apps mounted alongside this one, and this
# API is consumed by the Next.js client rather than browsed.
router = SimpleRouter()
router.register('workspaces', WorkspaceViewSet, basename='workspace')

urlpatterns = router.urls
