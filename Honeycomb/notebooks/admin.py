from django.contrib import admin

from .models import PythonScript


@admin.register(PythonScript)
class PythonScriptAdmin(admin.ModelAdmin):
    list_display = ('name', 'workspace', 'tenant', 'created_by', 'updated_at')
    list_filter = ('tenant',)
    search_fields = ('name', 'description', 'code')
    readonly_fields = ('created_at', 'updated_at')
    raw_id_fields = ('tenant', 'workspace', 'created_by')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'tenant', 'workspace', 'created_by'
        )
