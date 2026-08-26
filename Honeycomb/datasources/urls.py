from rest_framework.routers import SimpleRouter

from .views import DataSourceViewSet

app_name = 'datasources'

router = SimpleRouter()
router.register('sources', DataSourceViewSet, basename='datasource')

urlpatterns = router.urls
