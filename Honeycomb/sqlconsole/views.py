from django.db import IntegrityError
from rest_framework import serializers, status, viewsets
from rest_framework.response import Response

from accounts.mixins import TenantScopedQuerysetMixin
from datasources import connectors

from .models import QueryRun, SavedQuery
from .serializers import (
    QueryRunListSerializer,
    QueryRunSerializer,
    RunRequestSerializer,
    SavedQuerySerializer,
)


class SavedQueryViewSet(TenantScopedQuerysetMixin, viewsets.ModelViewSet):
    """CRUD for saved SQL. Execution lives on QueryRunViewSet."""

    queryset = SavedQuery.objects.select_related(
        'workspace', 'data_source', 'created_by'
    )
    serializer_class = SavedQuerySerializer
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
                {'name': ['A query with this name already exists in that workspace.']}
            )

    def perform_update(self, serializer):
        try:
            serializer.save()
        except IntegrityError:
            raise serializers.ValidationError(
                {'name': ['A query with this name already exists in that workspace.']}
            )


class QueryRunViewSet(TenantScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    """
    Execute a statement, and read what previous executions produced.

    Create runs the query *on the request thread*. That is a real limitation
    and it is chosen rather than overlooked: this project has no Celery, no
    Redis and no worker, and a fake asynchronous API -- returning a run id that
    a synchronous handler has already finished -- would be a lie the frontend
    then has to poll. A person watching a spinner can wait for
    `statement_timeout`; nothing scheduled should ever call this.

    The path off it is the one the plan names: move `connectors.run_query` into
    a task, create the row in PENDING, and let this endpoint return it
    immediately. The model already carries the columns that needs.
    """

    queryset = QueryRun.objects.select_related(
        'workspace', 'data_source', 'saved_query', 'created_by'
    )
    serializer_class = QueryRunSerializer
    # Its own bucket, an order of magnitude below the CRUD scope: every call
    # here opens a socket to somebody's warehouse and can hold a worker for
    # thirty seconds. The CRUD limit is sized for a person clicking; this one
    # is sized for a person thinking between queries.
    throttle_scope = 'sql_run'

    def get_serializer_class(self):
        # The list omits rows -- see QueryRunListSerializer.
        if self.action == 'list':
            return QueryRunListSerializer
        return QueryRunSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        workspace = self.request.query_params.get('workspace')
        if workspace:
            queryset = queryset.filter(workspace_id=workspace)
        if self.action == 'list':
            # Bounded only on the list. Slicing the detail queryset too would
            # break get_object(), which filters it again -- Django refuses to
            # filter a query once a slice has been taken.
            return queryset[: QueryRun.KEEP_PER_WORKSPACE]
        return queryset

    def create(self, request, *args, **kwargs):
        form = RunRequestSerializer(data=request.data, context={'request': request})
        form.is_valid(raise_exception=True)
        data = form.validated_data

        workspace = data['workspace']
        source = data['data_source']
        sql = data['sql']

        try:
            result = connectors.run_query(source, sql, limit=data.get('limit'))
        except connectors.ConnectorError as error:
            # A failed run is still a run. Recording it is the point: "it said
            # relation does not exist" is what someone comes back to the
            # history for, and a failure that leaves no trace teaches nothing.
            run = QueryRun.objects.create(
                tenant=self.get_tenant(),
                workspace=workspace,
                data_source=source,
                saved_query=data.get('saved_query'),
                sql=sql,
                state=QueryRun.State.FAILED,
                error=str(error),
                created_by=request.user,
            )
            QueryRun.trim(workspace)
            # 200, not 500. The request was handled correctly; the *query* is
            # what failed, and the body says so. A 500 here would show the
            # frontend's generic "something went wrong" instead of the database
            # error the person needs to read.
            return Response(
                self.get_serializer(run).data, status=status.HTTP_200_OK
            )

        run = QueryRun.objects.create(
            tenant=self.get_tenant(),
            workspace=workspace,
            data_source=source,
            saved_query=data.get('saved_query'),
            sql=sql,
            state=QueryRun.State.SUCCEEDED,
            result_columns=result.columns,
            result_rows=result.rows,
            row_count=result.row_count,
            truncated=result.truncated,
            notice=result.notice[:255],
            duration_ms=result.duration_ms,
            created_by=request.user,
        )
        QueryRun.trim(workspace)
        return Response(self.get_serializer(run).data, status=status.HTTP_201_CREATED)
