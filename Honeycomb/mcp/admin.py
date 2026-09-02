"""Admin registrations for the MCP tables.

Both are effectively read-only. A key's hash is shown but never editable: it is
the credential's only stored form, and a typo in it would silently break a live
integration while an edited one would be a way to plant a token nobody minted.
Activity is an append-only audit trail, so it is not editable at all.
"""
from django.contrib import admin
from django.utils import timezone

from .models import McpActivity, McpKey


@admin.register(McpKey)
class McpKeyAdmin(admin.ModelAdmin):
    list_display = ('id', 'label', 'key_prefix', 'tenant', 'connection', 'created_by',
                    'created_at', 'last_used_at', 'revoked_at')
    list_filter = ('tenant', 'revoked_at')
    list_select_related = ('tenant', 'connection', 'created_by')
    search_fields = ('label', 'key_prefix')
    ordering = ('-created_at',)
    readonly_fields = ('tenant', 'connection', 'created_by', 'label', 'key_prefix',
                       'key_hash', 'created_at', 'last_used_at', 'revoked_at')
    actions = ('revoke_selected',)

    def has_add_permission(self, request):
        # Keys exist only as a (row, plaintext) pair. Adding one here would
        # create a row whose token nobody holds.
        return False

    @admin.action(description='Revoke selected keys')
    def revoke_selected(self, request, queryset):
        updated = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, '{0} key(s) revoked.'.format(updated))


@admin.register(McpActivity)
class McpActivityAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at', 'tenant', 'connector', 'tool_name', 'status',
                    'duration_ms', 'error_message')
    list_filter = ('status', 'connector', 'tenant')
    list_select_related = ('tenant', 'connection')
    search_fields = ('tool_name', 'error_message')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = ('tenant', 'connection', 'connector', 'tool_name', 'status',
                       'duration_ms', 'detail', 'error_message', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
