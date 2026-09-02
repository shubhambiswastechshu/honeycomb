"""Key and activity routes, mounted under /api/ next to the connections app's
own routes."""
from django.urls import path

from .views import (
    ActivityListView,
    ActivitySummaryView,
    McpKeyDetailView,
    McpKeyListCreateView,
)

app_name = 'mcp'

urlpatterns = [
    # Tenant-wide, unlike connections/<id>/activity/ which is one connection's
    # slice. Declared before the bare list so the literal wins over nothing --
    # they cannot collide, but the specific route reads first.
    path('activity/summary/', ActivitySummaryView.as_view(), name='activity-summary'),
    path('activity/', ActivityListView.as_view(), name='activity'),
    path('connections/<int:connection_id>/keys/',
         McpKeyListCreateView.as_view(), name='keys'),
    path('connections/<int:connection_id>/keys/<int:key_id>/',
         McpKeyDetailView.as_view(), name='key-detail'),
]
