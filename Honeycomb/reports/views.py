"""
Saved reports: CRUD, and the run route that fills a report with data.

Everything is confined to the caller's own organization by
TenantScopedQuerysetMixin, so a detail route cannot reach a neighbouring
organization's report even with a guessed id. The data itself never comes from
here: a run goes through connections.runner, the same gate the Google Ads report
uses -- read-only tools, switched-off tools respected, errors redacted.
"""

import logging
import time

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response

from accounts.mixins import TenantScopedQuerysetMixin
from accounts.models import Tenant

from . import access
from .execution import execute_plan
from .layout import LAYOUT_SCHEMA_VERSION
from .models import Report
from .runplan import build_plan, resolve_window
from .serializers import (
    ReportListSerializer,
    ReportSerializer,
    RunRequestSerializer,
    VersionConflict,
)

logger = logging.getLogger(__name__)

#: The most rows the list returns. It is one page, never paged, so the ceiling
#: is what keeps a workspace with an unusual number of reports from making it slow.
MAX_LIST = 500


class ReportViewSet(TenantScopedQuerysetMixin, viewsets.ModelViewSet):
    """CRUD over the caller's organization's reports, plus running one."""

    queryset = Report.objects.all()
    serializer_class = ReportSerializer
    permission_classes = [IsAuthenticated]
    throttle_scope = 'reports_write'

    def initial(self, request, *args, **kwargs):
        super(ReportViewSet, self).initial(request, *args, **kwargs)
        # A platform-level account with no organization is refused up front, on
        # every route, rather than wherever a queryset first happens to need one.
        self.get_tenant()

    def get_throttles(self):
        """Three ceilings, because the three kinds of request cost different things.

        Reading is a SELECT and happens on every page. Writing is the builder's
        autosave: debounced to one every second or two, so the ceiling sits above
        what a person editing produces and below a loop. A run fans out to the
        provider's API like the Google Ads report does, and the builder re-runs a
        widget as its settings change, so it gets a little more headroom than
        that report's own 12 a minute.
        """
        if getattr(self, 'action', None) == 'run':
            self.throttle_scope = 'report_run'
        elif self.request.method in SAFE_METHODS:
            self.throttle_scope = 'reports_read'
        else:
            self.throttle_scope = 'reports_write'
        return super(ReportViewSet, self).get_throttles()

    def get_serializer_class(self):
        if self.action == 'list':
            return ReportListSerializer
        return ReportSerializer

    def get_queryset(self):
        queryset = super(ReportViewSet, self).get_queryset()
        if self.action == 'list':
            return self._listing(queryset)
        if self.action in ('update', 'partial_update'):
            # Locks the row (on databases that can) for the version check and the
            # write after it, so two saves cannot both pass the check. No
            # select_related here: a nullable join under FOR UPDATE is an error
            # on PostgreSQL.
            return queryset.select_for_update()
        return queryset.select_related('created_by', 'updated_by')

    def _listing(self, queryset):
        params = self.request.query_params
        client = params.get('client', '').strip()
        if client:
            queryset = queryset.filter(client__iexact=client)
        text = params.get('q', '').strip()
        if text:
            queryset = queryset.filter(
                Q(name__icontains=text) | Q(client__icontains=text) | Q(description__icontains=text))
        return (
            queryset.select_related('created_by', 'updated_by')
            .defer('layout', 'filters')[:MAX_LIST]
        )

    def create(self, request, *args, **kwargs):
        with transaction.atomic():
            # Locks the tenant row so two requests creating a report for the
            # same organization at the same instant cannot both read the same
            # under-the-cap count in ReportSerializer.validate and both insert:
            # the second waits for the lock and then counts the first one's row.
            Tenant.objects.select_for_update().get(pk=self.get_tenant().pk)
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        # The tenant and the author come from the session and nowhere else, and
        # the version is the server's: none of them is ever read from the body.
        user = self.request.user
        serializer.save(
            tenant=self.get_tenant(), created_by=user, updated_by=user,
            version=1, schema_version=LAYOUT_SCHEMA_VERSION,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        try:
            with transaction.atomic():
                report = self.get_object()
                serializer = self.get_serializer(report, data=request.data, partial=partial)
                serializer.is_valid(raise_exception=True)
                extra = {'updated_by': request.user, 'version': report.version + 1}
                # Only bumped when this save actually writes a layout: the
                # contract is "schema_version is set ... whenever layout is
                # written", not on every rename. An update that tightens the
                # layout schema in a later release must not silently mark an
                # untouched old report as already migrated to it.
                if 'layout' in serializer.validated_data:
                    extra['schema_version'] = LAYOUT_SCHEMA_VERSION
                serializer.save(**extra)
        except VersionConflict as conflict:
            return Response(
                {
                    'detail': 'This report was changed somewhere else. Reload it to get '
                              'the latest version before saving again.',
                    'current_version': conflict.current,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(serializer.data)

    def perform_destroy(self, instance):
        if not access.can_delete(self.request.user, instance):
            raise PermissionDenied(
                'Only the person who made this report, or an owner or admin, can delete it.')
        instance.delete()

    @action(detail=True, methods=['post'], url_path='run')
    def run(self, request, pk=None):
        """Run a report's widgets against their providers, one answer per distinct call.

        Widgets that ask for the same tool with the same arguments share one
        call and one entry under ``runs``; ``widgets`` says which run each one
        reads. A widget that cannot be answered -- the connection is gone, the
        provider errored, the tool is switched off -- is a failed run under its
        own key, and the rest of the report still comes back.

        Like the Google Ads report this writes no McpActivity rows: opening a
        dashboard is a person looking at their data, not an AI client calling a
        tool, and a dozen rows per page view would bury the real ones.

        The body may carry a draft ``layout`` and/or ``filters`` to run instead
        of the saved ones. Nothing in the body is saved.
        """
        report = self.get_object()
        body = RunRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        layout = body.validated_data.get('layout')
        if layout is None:
            layout = report.layout or []
        filters = body.validated_data.get('filters')
        if filters is None:
            filters = report.filters or {}

        # USE_TZ is on and TIME_ZONE is UTC, so now() is a UTC instant and its
        # date is the UTC date. Read through the module so a test can freeze it.
        now = timezone.now()
        try:
            window = resolve_window(filters.get('date'), now.date())
            plan = build_plan(layout, window, filters.get('compare') is True)
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
            # What is saved was checked on the way in, but is deliberately NOT
            # checked again here: a rule tightened in a later release must not
            # turn every older report into a refusal to run. This only catches a
            # row too malformed to plan at all -- one that reached the table
            # some other way -- and says so instead of returning a 500.
            logger.exception('Report %s could not be planned', report.pk)
            raise ValidationError({
                'layout': ['This report could not be read. Open it in the editor and '
                           'save it again.'],
            })

        started = time.monotonic()
        try:
            results = execute_plan(self.get_tenant(), plan)
        except OverflowError:
            # A stored connection id wider than any real id can be (see
            # layout.MAX_CONNECTION_ID) reaching the database lookup in
            # execution.py -- only possible for a row written before that bound
            # existed. Same refusal as an unplannable layout, not a 500.
            logger.exception('Report %s could not be run', report.pk)
            raise ValidationError({
                'layout': ['This report could not be read. Open it in the editor and '
                           'save it again.'],
            })
        return Response({
            'generated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'duration_ms': int((time.monotonic() - started) * 1000),
            'window': window._asdict(),
            'runs': {spec.key: results[spec.key] for spec in plan.runs},
            'widgets': plan.widgets,
        }, status=status.HTTP_200_OK)
