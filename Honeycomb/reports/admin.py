"""
Admin for reports: to find one and see who owns it, not to edit it.

The layout is deliberately read-only. Every save through the API is validated
(reports/layout.py) -- a closed set of keys, a bounded size, connections that
belong to the report's own organization -- and a hand-edited JSON blob here
would skip all of it.
"""

from django.contrib import admin

from .models import Report


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'client', 'tenant', 'created_by', 'version', 'updated_at')
    list_filter = ('tenant',)
    list_select_related = ('tenant', 'created_by')
    search_fields = ('name', 'client', 'tenant__name')
    # name/description/client are model CharFields, not layout JSON, so a hand
    # edit here would not corrupt anything -- but it also would not go through
    # ReportViewSet.update(), so it would not bump `version`. A later ordinary
    # save from the app, still holding the old version, would then pass the
    # optimistic-concurrency check and silently overwrite the admin's edit with
    # no 409. Read-only here, same as everything else, closes that gap.
    readonly_fields = (
        'tenant', 'created_by', 'updated_by', 'name', 'description', 'client',
        'layout', 'filters', 'schema_version', 'version', 'created_at', 'updated_at',
    )

    def has_add_permission(self, request):
        # A report is made in the app: it needs an organization and a validated
        # layout, and this form supplies neither.
        return False
