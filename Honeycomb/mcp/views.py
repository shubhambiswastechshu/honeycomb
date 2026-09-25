"""Control plane for MCP keys (mint, list, revoke) and the tenant-wide
activity feed the Overview dashboard reads.

Cookie-authenticated and tenant-scoped like the rest of /api/. The connection id
in the URL is never trusted on its own -- it is resolved inside the caller's own
tenant, so a guessed id from a neighbouring organization simply does not exist.
"""
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.mixins import TenantScopedQuerysetMixin
from accounts.models import User
from connections.models import Connection
from connectors import registry

from .models import McpActivity, McpKey
from .serializers import (
    ActivityQuerySerializer,
    ActivitySummaryQuerySerializer,
    McpActivityRowSerializer,
    McpKeyCreateSerializer,
    McpKeySerializer,
)

# Minting hands out a long-lived credential to the tenant's live upstream data,
# so it is an administrative act. Members may still use connections; they just
# cannot issue new keys for them.
KEY_ADMIN_ROLES = (User.Role.OWNER, User.Role.ADMIN)


class ConnectionScopedView(TenantScopedQuerysetMixin, APIView):
    """Base for the key routes: resolves <connection_id> inside the caller's tenant.

    TenantScopedQuerysetMixin is mixed in for get_tenant(), which is the single
    definition of "which tenant is this caller" (and which refuses a
    platform-level superuser rather than falling through unfiltered).
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = 'keys'

    def get_connection(self, connection_id):
        connection = Connection.objects.filter(
            tenant=self.get_tenant(), pk=connection_id
        ).first()
        if connection is None:
            # 404 rather than 403: whether a row exists in another organization
            # is not something this tenant gets to learn.
            raise NotFound('Connection not found.')
        return connection

    def require_key_admin(self):
        if self.request.user.role not in KEY_ADMIN_ROLES:
            raise PermissionDenied('Only an owner or admin can manage MCP keys.')


class McpKeyListCreateView(ConnectionScopedView):
    """GET  /api/connections/<id>/keys/  -> the connection's keys
    POST /api/connections/<id>/keys/  -> mint one; the token is returned ONCE."""

    def get(self, request, connection_id):
        connection = self.get_connection(connection_id)
        keys = McpKey.objects.filter(tenant=connection.tenant, connection=connection)
        return Response(McpKeySerializer(keys, many=True).data)

    def post(self, request, connection_id):
        connection = self.get_connection(connection_id)
        self.require_key_admin()
        serializer = McpKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        key, plain = McpKey.mint(
            connection, created_by=request.user, label=serializer.validated_data['label']
        )
        payload = McpKeySerializer(key).data
        # The only time the plaintext ever leaves the process. It is not stored,
        # so a client that loses it must mint a new key.
        payload['token'] = plain
        return Response(payload, status=status.HTTP_201_CREATED)


class McpKeyDetailView(ConnectionScopedView):
    """DELETE /api/connections/<id>/keys/<key_id>/ -> revoke it."""

    def delete(self, request, connection_id, key_id):
        connection = self.get_connection(connection_id)
        key = McpKey.objects.filter(
            tenant=connection.tenant, connection=connection, pk=key_id
        ).first()
        if key is None:
            raise NotFound('Key not found.')
        # Anyone who could mint may revoke, and so may whoever minted this one --
        # taking a leaked credential out of service should never be blocked on
        # finding an admin.
        if request.user.role not in KEY_ADMIN_ROLES and key.created_by_id != request.user.pk:
            raise PermissionDenied('Only an owner, an admin, or the key owner can revoke it.')
        if key.revoked_at is None:
            # Revoked, not deleted: the activity log refers to work this key did,
            # and the dashboard shows a revoked key so its disappearance from an
            # AI client is explainable. Re-revoking is a no-op, not an error.
            key.revoked_at = timezone.now()
            key.save(update_fields=['revoked_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class TenantActivityBaseView(TenantScopedQuerysetMixin, APIView):
    """Base for the tenant-wide activity reads.

    The filter is `tenant=self.get_tenant()` and nothing else. It deliberately
    does NOT go through the connection FK: that FK is nullable, so rows whose
    connection has been deleted would silently vanish from a join -- and those
    rows still carry a tenant, so joining would also be a second, weaker place
    for the isolation rule to live.
    """

    permission_classes = [IsAuthenticated]
    # Read-only and cheap, but polled by an open dashboard tab, so it needs a
    # ceiling of its own rather than borrowing the write-shaped 'connect' one.
    throttle_scope = 'activity'

    def tenant_activity(self):
        return McpActivity.objects.filter(tenant=self.get_tenant())


class ActivityListView(TenantActivityBaseView):
    """GET /api/activity/?limit=N -> the tenant's most recent tool calls."""

    def get(self, request):
        query = ActivityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = (
            self.tenant_activity()
            # select_related, not a per-row fetch: the serializer reads
            # connection.name, which would otherwise be one query per row.
            .select_related('connection')
            .order_by('-created_at')[:query.validated_data['limit']]
        )
        return Response(McpActivityRowSerializer(rows, many=True).data)


class ActivitySummaryView(TenantActivityBaseView):
    """GET /api/activity/summary/?days=N -> per-day ok/error counts.

    Every day in the window is present, including the silent ones. A sparkline
    that omits empty days draws an x-axis that lies about when the calls
    happened.

    Days are UTC days. TIME_ZONE is UTC, and we hold no per-tenant timezone --
    bucketing by an invented local midnight would be a claim about data we do
    not have, so the boundary is stated rather than guessed.
    """

    def get(self, request):
        query = ActivitySummaryQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        days = query.validated_data['days']

        today = timezone.now().astimezone(dt_timezone.utc).date()
        first_day = today - timedelta(days=days - 1)
        window_start = datetime.combine(first_day, time.min, tzinfo=dt_timezone.utc)

        # Grouped in the database. Pulling the window into Python to count it
        # would read every row of a table that only ever grows.
        grouped = (
            self.tenant_activity()
            .filter(created_at__gte=window_start)
            .annotate(day=TruncDate('created_at', tzinfo=dt_timezone.utc))
            .values('day')
            .annotate(
                ok=Count('id', filter=Q(status=McpActivity.STATUS_OK)),
                error=Count('id', filter=Q(status=McpActivity.STATUS_ERROR)),
            )
        )
        counts = {row['day']: row for row in grouped}

        payload = []
        total = 0
        errors = 0
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            row = counts.get(day)
            ok_count = row['ok'] if row else 0
            error_count = row['error'] if row else 0
            total += ok_count + error_count
            errors += error_count
            payload.append({
                'date': day.isoformat(),
                'ok': ok_count,
                'error': error_count,
            })
        # Totals are summed from the same rows the chart draws, so the headline
        # number can never disagree with the bars under it.
        return Response({'days': payload, 'total': total, 'errors': errors})


#: The live panel's shape. A clock hour is the unit, so the window is the
#: current partial hour plus the 23 before it -- between 23 and 24 hours of
#: history, and `since` says exactly where it starts rather than leaving the
#: label to round.
LIVE_HOURS = 24
LIVE_RECENT_LIMIT = 30
LIVE_TOP_N = 8


def _average_ms(duration_sum, duration_n):
    """Mean call time, or None when nothing in the window was timed.

    A weighted mean over the sums and counts, never a mean of per-group
    means: an hour with one 900 ms call must not weigh the same as an hour
    with a thousand 40 ms ones.
    """
    if not duration_n:
        return None
    return int(round(float(duration_sum) / duration_n))


class ActivityLiveView(TenantActivityBaseView):
    """GET /api/activity/live/ -> the last 24 hours in one response, for the
    live panel that stays open beside every dashboard page.

    One request answers the whole panel -- hourly buckets, the totals behind
    them, who is being called and the newest calls -- because the panel polls
    this every few seconds and five separate endpoints would multiply that by
    five against the same throttle.

    Every figure is counted from McpActivity rows in the caller's tenant, and
    the totals are summed from the same hourly rows the chart draws, so the
    number over a chart can never disagree with the bars under it. Nothing is
    estimated and nothing is sampled: an empty tenant gets zeros and an empty
    list, not invented traffic.
    """

    def get(self, request):
        now = timezone.now().astimezone(dt_timezone.utc)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        since = current_hour - timedelta(hours=LIVE_HOURS - 1)
        window = self.tenant_activity().filter(created_at__gte=since)

        is_ok = Q(status=McpActivity.STATUS_OK)
        is_error = Q(status=McpActivity.STATUS_ERROR)

        # Grouped in the database, like the daily summary: the window can hold
        # a lot of rows and the answer is 24 numbers.
        grouped = (
            window
            .annotate(hour=TruncHour('created_at', tzinfo=dt_timezone.utc))
            .values('hour')
            .annotate(
                ok=Count('id', filter=is_ok),
                error=Count('id', filter=is_error),
                duration_sum=Sum('duration_ms'),
                duration_n=Count('duration_ms'),
            )
        )
        by_hour = {row['hour']: row for row in grouped}

        hours = []
        ok_total = 0
        error_total = 0
        duration_sum = 0
        duration_n = 0
        for offset in range(LIVE_HOURS):
            start = since + timedelta(hours=offset)
            row = by_hour.get(start)
            ok_count = row['ok'] if row else 0
            error_count = row['error'] if row else 0
            ok_total += ok_count
            error_total += error_count
            if row and row['duration_n']:
                duration_sum += row['duration_sum']
                duration_n += row['duration_n']
            hours.append({'start': start.isoformat(), 'ok': ok_count, 'error': error_count})

        def shares(rows):
            payload = []
            for row in rows:
                entry = {
                    'connector': row['connector'],
                    'calls': row['calls'],
                    'ok': row['ok'],
                    'error': row['error'],
                    'avg_ms': _average_ms(row['duration_sum'], row['duration_n']),
                }
                connector = registry.get(row['connector'])
                entry['connector_label'] = connector.label if connector is not None else row['connector']
                if 'tool_name' in row:
                    entry['tool_name'] = row['tool_name']
                payload.append(entry)
            return payload

        measures = {
            'calls': Count('id'),
            'ok': Count('id', filter=is_ok),
            'error': Count('id', filter=is_error),
            'duration_sum': Sum('duration_ms'),
            'duration_n': Count('duration_ms'),
        }
        connectors = (
            window.values('connector')
            .annotate(**measures)
            .order_by('-calls', 'connector')[:LIVE_TOP_N]
        )
        tools = (
            window.values('connector', 'tool_name')
            .annotate(**measures)
            .order_by('-calls', 'connector', 'tool_name')[:LIVE_TOP_N]
        )

        last_call = (
            self.tenant_activity()
            .order_by('-created_at')
            .values_list('created_at', flat=True)
            .first()
        )
        recent = (
            self.tenant_activity()
            .select_related('connection')
            .order_by('-created_at', '-id')[:LIVE_RECENT_LIMIT]
        )

        return Response({
            'generated_at': now.isoformat(),
            'since': since.isoformat(),
            'calls': ok_total + error_total,
            'errors': error_total,
            'avg_ms': _average_ms(duration_sum, duration_n),
            'last_call_at': last_call.isoformat() if last_call else None,
            'hours': hours,
            'connectors': shares(connectors),
            'tools': shares(tools),
            'recent': McpActivityRowSerializer(recent, many=True).data,
        })
