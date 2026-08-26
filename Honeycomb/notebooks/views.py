from django.db import IntegrityError
from rest_framework import serializers, viewsets

from accounts.mixins import TenantScopedQuerysetMixin

from .models import PythonScript
from .serializers import PythonScriptSerializer


class PythonScriptViewSet(TenantScopedQuerysetMixin, viewsets.ModelViewSet):
    """CRUD for saved Python. The code runs in the browser -- see the model."""

    queryset = PythonScript.objects.select_related('workspace', 'created_by')
    serializer_class = PythonScriptSerializer
    throttle_scope = 'workspaces'

    def get_queryset(self):
        queryset = super().get_queryset()
        workspace = self.request.query_params.get('workspace')
        if workspace:
            queryset = queryset.filter(workspace_id=workspace)
        return queryset

    def perform_create(self, serializer):
        try:
            serializer.save(tenant=self.get_tenant(), created_by=self.request.user)
        except IntegrityError:
            raise serializers.ValidationError(
                {'name': ['A script with this name already exists in that workspace.']}
            )

    def perform_update(self, serializer):
        try:
            serializer.save()
        except IntegrityError:
            raise serializers.ValidationError(
                {'name': ['A script with this name already exists in that workspace.']}
            )
