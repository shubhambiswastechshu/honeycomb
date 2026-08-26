from rest_framework.routers import SimpleRouter

from .views import PipelineViewSet

app_name = 'pipelines'

router = SimpleRouter()
router.register('pipelines', PipelineViewSet, basename='pipeline')

urlpatterns = router.urls
