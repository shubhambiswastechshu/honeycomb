from django.db import IntegrityError
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.mixins import TenantScopedQuerysetMixin

from . import connectors
from .models import DataSource
from .serializers import DataSourceSerializer


class DataSourceViewSet(TenantScopedQuerysetMixin, viewsets.ModelViewSet):
    """CRUD for the caller's data sources, plus a connection test and schema."""

    queryset = DataSource.objects.select_related('workspace')
    serializer_class = DataSourceSerializer
    throttle_scope = 'workspaces'

    def get_queryset(self):
        queryset = super().get_queryset()
        # The dashboard lists sources one workspace at a time. Filtering here
        # rather than client-side keeps a large tenant's payload small.
        workspace = self.request.query_params.get('workspace')
        if workspace:
            queryset = queryset.filter(workspace_id=workspace)
        return queryset

    def perform_create(self, serializer):
        try:
            serializer.save(tenant=self.get_tenant())
        except IntegrityError:
            raise serializers.ValidationError(
                {'name': ['A source with this name already exists in that workspace.']}
            )

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """Actually dial the source and record what came back.

        The result is written to the row whether it worked or not, so the
        status column means "this was true at `last_checked_at`" rather than
        "someone once pressed a button".
        """
        source = self.get_object()
        try:
            detail = connectors.test_connection(source)
        except connectors.ConnectorError as error:
            source.status = DataSource.Status.FAILED
            source.last_error = str(error)
        else:
            source.status = DataSource.Status.CONNECTED
            source.last_error = ''
        source.last_checked_at = timezone.now()
        source.save(update_fields=['status', 'last_checked_at', 'last_error'])

        body = self.get_serializer(source).data
        body['detail'] = (
            detail if source.status == DataSource.Status.CONNECTED else source.last_error
        )
        return Response(body, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'])
    def schema(self, request, pk=None):
        """Tables and columns this source's role can see.

        Feeds the editor's autocomplete. It is a live query rather than
        something cached on the row because a stale schema is worse than none:
        completing a column that was dropped last week sends someone hunting
        for a bug in their own SQL.
        """
        source = self.get_object()
        try:
            tables = connectors.introspect(source)
        except connectors.ConnectorError as error:
            # 400, not 500: nothing here is broken. The source is misconfigured
            # or unreachable, and the message says which.
            raise serializers.ValidationError({'detail': str(error)})
        return Response({'tables': tables}, status=status.HTTP_200_OK)
