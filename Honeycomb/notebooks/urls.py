from rest_framework.routers import SimpleRouter

from .views import PythonScriptViewSet

app_name = 'notebooks'

router = SimpleRouter()
router.register('scripts', PythonScriptViewSet, basename='pythonscript')

urlpatterns = router.urls
