from django.contrib import admin

from .models import DataSource


@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'kind', 'workspace', 'tenant', 'status', 'last_checked_at')
    list_filter = ('kind', 'status', 'tenant')
    search_fields = ('name', 'host', 'database', 'username')
    readonly_fields = ('created_at', 'updated_at', 'last_checked_at', 'last_error')
    raw_id_fields = ('tenant', 'workspace')
    fieldsets = (
        (None, {'fields': ('tenant', 'workspace', 'name', 'kind')}),
        (
            'Connection',
            {
                'fields': ('host', 'port', 'database', 'username', 'secret_name'),
                'description': 'Coordinates only. The password lives in the '
                               'secret manager under <em>secret name</em> and '
                               'is never stored on this row.',
            },
        ),
        ('State', {'fields': ('status', 'last_checked_at', 'last_error')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('tenant', 'workspace')
