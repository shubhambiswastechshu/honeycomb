from django.db import IntegrityError
from rest_framework import serializers, viewsets

from accounts.mixins import TenantScopedQuerysetMixin

from .models import Pipeline
from .serializers import PipelineSerializer


class PipelineViewSet(TenantScopedQuerysetMixin, viewsets.ModelViewSet):
    """CRUD for pipeline definitions. Nothing executes them yet."""

    queryset = Pipeline.objects.select_related('workspace', 'source')
    serializer_class = PipelineSerializer
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
                {'name': ['A pipeline with this name already exists in that workspace.']}
            )
