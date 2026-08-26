from django.db import IntegrityError
from django.db.models import Count
from rest_framework import serializers, viewsets

from accounts.mixins import TenantScopedQuerysetMixin

from .models import Workspace
from .serializers import WorkspaceSerializer


class WorkspaceViewSet(TenantScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    CRUD for the caller's own workspaces.

    TenantScopedQuerysetMixin does the tenancy: it filters every read to
    request.user.tenant and stamps the same tenant on create, so a detail route
    cannot reach a neighbouring organization's row even with a guessed id.
    """

    queryset = Workspace.objects.all()
    serializer_class = WorkspaceSerializer
    throttle_scope = 'workspaces'

    def get_queryset(self):
        # The tile shows what each workspace holds. Annotating here keeps a
        # list of N workspaces at one query rather than 3N.
        return (
            super()
            .get_queryset()
            .annotate(
                data_source_count=Count('data_sources', distinct=True),
                query_count=Count('saved_queries', distinct=True),
                pipeline_count=Count('pipelines', distinct=True),
            )
        )

    def perform_create(self, serializer):
        # The mixin stamps the tenant; the author is this view's business.
        try:
            serializer.save(
                tenant=self.get_tenant(), created_by=self.request.user
            )
        except IntegrityError:
            # The unique constraint is per tenant, so this is always "you
            # already have one by that name" and never a cross-tenant clash.
            raise serializers.ValidationError(
                {'name': ['A workspace with this name already exists.']}
            )

    def perform_update(self, serializer):
        try:
            serializer.save()
        except IntegrityError:
            raise serializers.ValidationError(
                {'name': ['A workspace with this name already exists.']}
            )
