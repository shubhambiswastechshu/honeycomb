"""Admin registrations for the MCP tables.

Both are effectively read-only. A key's hash is shown but never editable: it is
the credential's only stored form, and a typo in it would silently break a live
integration while an edited one would be a way to plant a token nobody minted.
Activity is an append-only audit trail, so it is not editable at all.
"""
from django.contrib import admin
from django.utils import timezone

from .models import McpActivity, McpKey, OAuthClient, OAuthToken


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


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):
    list_display = ('id', 'client_name', 'client_id', 'created_at', 'last_used_at')
    search_fields = ('client_name', 'client_id')
    ordering = ('-created_at',)
    readonly_fields = ('client_id', 'client_name', 'redirect_uris', 'created_at', 'last_used_at')

    def has_add_permission(self, request):
        return False


@admin.register(OAuthToken)
class OAuthTokenAdmin(admin.ModelAdmin):
    """Read-only like McpKey: only the hashes are stored, and editing one would
    either break a live client or plant a token nobody was issued."""

    list_display = ('id', 'client', 'connection', 'user', 'created_at', 'expires_at',
                    'last_used_at', 'revoked_at')
    list_filter = ('revoked_at',)
    list_select_related = ('client', 'connection', 'user')
    ordering = ('-created_at',)
    readonly_fields = ('client', 'connection', 'user', 'token_hash', 'refresh_hash',
                       'expires_at', 'last_used_at', 'revoked_at', 'created_at',
                       'family_started_at')
    actions = ('revoke_selected',)

    def has_add_permission(self, request):
        return False

    @admin.action(description='Revoke selected tokens')
    def revoke_selected(self, request, queryset):
        updated = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, '{0} token(s) revoked.'.format(updated))
