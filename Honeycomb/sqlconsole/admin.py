from django.contrib import admin

from .models import QueryRun, SavedQuery


@admin.register(SavedQuery)
class SavedQueryAdmin(admin.ModelAdmin):
    list_display = ('name', 'workspace', 'data_source', 'tenant', 'created_by', 'updated_at')
    list_filter = ('tenant',)
    search_fields = ('name', 'description', 'sql')
    readonly_fields = ('created_at', 'updated_at')
    raw_id_fields = ('tenant', 'workspace', 'data_source', 'created_by')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'tenant', 'workspace', 'data_source', 'created_by'
        )


@admin.register(QueryRun)
class QueryRunAdmin(admin.ModelAdmin):
    """Read-only. A run is a record of something that happened.

    Nothing here is editable, including by a superuser: an audit row someone
    can retype is not an audit row. Deleting is left on so a support request to
    remove a result set holding customer data can be honoured.
    """

    list_display = ('created_at', 'state', 'workspace', 'data_source', 'row_count',
                    'duration_ms', 'created_by')
    list_filter = ('state', 'tenant')
    search_fields = ('sql', 'error')
    date_hierarchy = 'created_at'
    raw_id_fields = ('tenant', 'workspace', 'data_source', 'saved_query', 'created_by')

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'tenant', 'workspace', 'data_source', 'created_by'
        )
