from django.contrib import admin

from .models import Workspace


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ('name', 'tenant', 'slug', 'created_by', 'created_at')
    list_filter = ('tenant',)
    search_fields = ('name', 'slug', 'description')
    readonly_fields = ('created_at', 'updated_at')
    # The slug is derived on save, but an admin editing an existing workspace
    # should see what it will become rather than a blank box.
    prepopulated_fields = {'slug': ('name',)}
    # Straight selects here would load every tenant's users and workspaces into
    # the page; raw ids keep the form bounded as the tables grow.
    raw_id_fields = ('tenant', 'created_by')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('tenant', 'created_by')
