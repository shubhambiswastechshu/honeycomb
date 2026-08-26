from rest_framework.routers import SimpleRouter

from .views import QueryRunViewSet, SavedQueryViewSet

app_name = 'sqlconsole'

router = SimpleRouter()
router.register('queries', SavedQueryViewSet, basename='savedquery')
# "runs" rather than "queries/run": a run is a record with its own lifetime and
# its own list, not an action on a saved query -- most runs are never saved.
router.register('runs', QueryRunViewSet, basename='queryrun')

urlpatterns = router.urls
