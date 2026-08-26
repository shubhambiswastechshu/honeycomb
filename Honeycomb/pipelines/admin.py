from django.contrib import admin

from .models import Pipeline


@admin.register(Pipeline)
class PipelineAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'workspace',
        'tenant',
        'status',
        'schedule',
        'last_outcome',
        'last_run_at',
    )
    list_filter = ('status', 'last_outcome', 'tenant')
    search_fields = ('name', 'description', 'destination', 'schedule')
    readonly_fields = ('created_at', 'updated_at', 'last_run_at', 'last_outcome')
    raw_id_fields = ('tenant', 'workspace', 'source', 'created_by')
    fieldsets = (
        (None, {'fields': ('tenant', 'workspace', 'name', 'description')}),
        ('Move', {'fields': ('source', 'destination', 'schedule')}),
        (
            'State',
            {
                'fields': ('status', 'last_outcome', 'last_run_at'),
                'description': 'No scheduler runs these yet, so the outcome '
                               'and timestamp stay empty by design.',
            },
        ),
        ('Timestamps', {'fields': ('created_by', 'created_at', 'updated_at')}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'tenant', 'workspace', 'source'
        )
