"""
Admin for Connection and for the short-lived OAuth nonces beside it.

Read-mostly on purpose. Support staff need to see *that* a connection exists,
which tenant owns it and whether it is erroring; they never need its
credentials, and the admin is exactly the kind of broad session that turns one
compromised staff account into every customer's tokens. So creds_enc is
displayed as ciphertext and never decrypted here, and there is no admin action
anywhere in this file that could decrypt it.
"""

from django.contrib import admin

from .models import Connection, ConnectorOAuthState


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'name',
        'connector',
        'tenant',
        'status',
        'endpoint_slug',
        'has_credentials',
        'created_at',
    )
    list_filter = ('connector', 'status', 'tenant')
    list_select_related = ('tenant', 'created_by')
    search_fields = ('name', 'connector', 'endpoint_slug')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    # endpoint_slug is read-only because live MCP URLs embed it: editing it here
    # would silently break every AI client already configured against the row.
    # creds_enc is read-only because a writable field is an editable credential,
    # and it is shown as ciphertext because there is no reason for a staff
    # session to ever hold the plaintext.
    readonly_fields = (
        'endpoint_slug',
        'creds_enc',
        'has_credentials',
        'created_at',
        'updated_at',
    )
    fieldsets = (
        (None, {'fields': ('tenant', 'created_by', 'connector', 'name')}),
        ('Status', {'fields': ('status', 'last_error', 'disabled_tools')}),
        ('Endpoint', {'fields': ('endpoint_slug',)}),
        ('Credentials', {
            'fields': ('has_credentials', 'creds_enc'),
            'description': 'Encrypted blob. It is never decrypted in the admin. '
                           'To change a credential, re-enter it in the product.',
        }),
        ('Dates', {'fields': ('created_at', 'updated_at')}),
    )

    @admin.display(boolean=True, description='Credentials stored')
    def has_credentials(self, obj) -> bool:
        return bool(obj.creds_enc)


@admin.register(ConnectorOAuthState)
class ConnectorOAuthStateAdmin(admin.ModelAdmin):
    """Read-only view of in-flight OAuth handshakes.

    Registered because "the Connect button does nothing" is nearly always
    answered here -- were nonces even created, and were they redeemed? -- and
    that question is otherwise invisible.

    Nothing is editable, and the nonce itself is never listed or searchable. It
    is a bearer credential for the unauthenticated callback route: an admin
    screen that printed it, or let it be looked up, would hand a staff session
    (or anyone reading over a shoulder) the ability to attach a Google account
    to somebody else's organization. The row is identified by its id and its
    timestamps instead, which is all a support question needs.
    """

    list_display = ('id', 'connector', 'tenant', 'user', 'created_at', 'used_at', 'is_live')
    list_filter = ('connector', 'tenant')
    list_select_related = ('tenant', 'user')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    # Every field, including the FKs: these rows are written by the flow and
    # read by nobody else. Editing one by hand could only ever mean re-pointing
    # a live handshake at a different tenant.
    readonly_fields = ('tenant', 'user', 'connector', 'created_at', 'used_at', 'is_live')
    fields = readonly_fields
    # A stale row is spent and worthless, but deleting one is still the right
    # cleanup action, so deletion stays available while creation does not.

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(boolean=True, description='Still redeemable')
    def is_live(self, obj) -> bool:
        return obj.is_fresh()
