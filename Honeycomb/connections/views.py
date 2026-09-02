"""
The connections control plane.

Two surfaces live here. The connector catalog is read-only and comes straight
off the registry, which is code rather than rows -- the only per-tenant fact on
it is how many connections the caller's own organization already has. The
connection viewset is ordinary tenant-scoped CRUD, guarded by
TenantScopedQuerysetMixin so a detail route cannot reach a neighbouring
organization's row even with a guessed primary key.
"""

from django.apps import apps
from django.db.models import Count, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.mixins import TenantScopedQuerysetMixin
from connectors import registry

from .models import Connection
from .serializers import (
    ConnectionSerializer,
    ToolToggleSerializer,
    catalog_tools,
    connector_detail,
    connector_spec,
)

#: Ceiling on ?limit= for the activity feed. The dashboard asks for 50; the cap
#: stops a client turning one request into an unbounded table scan.
MAX_ACTIVITY_LIMIT = 200
DEFAULT_ACTIVITY_LIMIT = 50


def tenant_of(request):
    """The caller's tenant, or an error -- never a fall-through to unscoped.

    Mirrors TenantScopedQuerysetMixin.get_tenant() for the plain APIView below,
    which has no queryset for the mixin to filter. A platform-level superuser
    has no tenant and must be refused rather than handed every organization's
    counts.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        raise NotAuthenticated()
    tenant = getattr(user, 'tenant', None)
    if tenant is None:
        raise PermissionDenied('This account is not attached to an organization.')
    return tenant


def connected_counts(tenant) -> dict:
    """{connector_slug: count} for one tenant, in a single query."""
    rows = (
        Connection.objects.filter(tenant=tenant)
        .values('connector')
        .annotate(total=Count('id'))
    )
    return {row['connector']: row['total'] for row in rows}


def with_key_counts(queryset):
    """Annotate active_key_count when the mcp app is installed.

    Counting keys per row inside the serializer would be one extra query per
    connection on the list endpoint. The reverse accessor belongs to the mcp
    app's McpKey, so it only exists once that app is installed -- hence the
    guard rather than an unconditional annotate.
    """
    if not apps.is_installed('mcp'):
        return queryset
    return queryset.annotate(
        active_key_count=Count('keys', filter=Q(keys__revoked_at__isnull=True))
    )


class ConnectorCatalogView(APIView):
    """GET /api/connectors/ and /api/connectors/<slug>/.

    The catalog itself is identical for every tenant -- it is the registry, and
    the registry is code. Only connected_count is tenant-specific, and it is
    computed from request.user.tenant, never from anything the client sends.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = 'connectors'

    def get(self, request, slug=None):
        tenant = tenant_of(request)
        counts = connected_counts(tenant)
        if slug is None:
            payload = [
                connector_spec(connector, counts.get(connector.slug, 0))
                for connector in registry.all_connectors()
            ]
            return Response(payload, status=status.HTTP_200_OK)
        connector = registry.get(slug)
        if connector is None:
            return Response(
                {'detail': 'No such connector.'}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(
            connector_detail(connector, counts.get(connector.slug, 0)),
            status=status.HTTP_200_OK,
        )


class ConnectionViewSet(TenantScopedQuerysetMixin, viewsets.ModelViewSet):
    """CRUD over the caller's own connected accounts."""

    queryset = Connection.objects.all()
    serializer_class = ConnectionSerializer
    permission_classes = [IsAuthenticated]
    # Creating a connection writes an encrypted credential and, downstream, a
    # live MCP endpoint. Cheap to read, but not something a browser has any
    # reason to do in bulk.
    throttle_scope = 'connect'

    def get_queryset(self):
        queryset = with_key_counts(super().get_queryset())
        connector = self.request.query_params.get('connector')
        if connector:
            queryset = queryset.filter(connector=connector)
        return queryset

    def perform_create(self, serializer):
        # The mixin stamps the tenant; created_by is stamped from the session
        # for the same reason -- neither is ever read from the request body.
        serializer.save(tenant=self.get_tenant(), created_by=self.request.user)

    @action(detail=True, methods=['get', 'post'], url_path='tools')
    def tools(self, request, pk=None):
        """List this connection's tools, or switch one on or off.

        `enabled` is derived, not stored: the row holds disabled_tools, so a
        connector that gains a tool in a later release exposes it on every
        existing connection instead of silently hiding it.
        """
        connection = self.get_object()
        if request.method == 'POST':
            serializer = ToolToggleSerializer(
                data=request.data, context={'connection': connection, 'request': request}
            )
            serializer.is_valid(raise_exception=True)
            tool = serializer.validated_data['tool']
            enabled = serializer.validated_data['enabled']
            disabled = [name for name in (connection.disabled_tools or []) if name != tool]
            if not enabled:
                disabled.append(tool)
            connection.disabled_tools = disabled
            connection.save(update_fields=['disabled_tools', 'updated_at'])
        return Response(self._tool_rows(connection), status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='activity')
    def activity(self, request, pk=None):
        """The most recent tool calls made through this connection."""
        connection = self.get_object()
        # Imported inside the method on purpose: mcp.models imports
        # connections.models, so a module-level import here closes the loop and
        # breaks app loading.
        from mcp.models import McpActivity

        rows = (
            McpActivity.objects.filter(tenant=connection.tenant, connection=connection)
            .order_by('-created_at')[:self._activity_limit(request)]
        )
        payload = [
            {
                'id': row.id,
                'connector': row.connector,
                'tool_name': row.tool_name,
                'status': row.status,
                'duration_ms': row.duration_ms,
                'error_message': row.error_message,
                'created_at': row.created_at,
            }
            for row in rows
        ]
        return Response(payload, status=status.HTTP_200_OK)

    def _tool_rows(self, connection) -> list:
        connector = registry.get(connection.connector)
        if connector is None:
            return []
        disabled = set(connection.disabled_tools or [])
        return [
            dict(tool, enabled=tool['name'] not in disabled)
            for tool in catalog_tools(connector)
        ]

    def _activity_limit(self, request) -> int:
        try:
            limit = int(request.query_params.get('limit', DEFAULT_ACTIVITY_LIMIT))
        except (TypeError, ValueError):
            # A junk limit is a client bug, not a reason to fail the page.
            return DEFAULT_ACTIVITY_LIMIT
        return max(1, min(limit, MAX_ACTIVITY_LIMIT))
