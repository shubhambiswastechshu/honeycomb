"""MCP portal models: hashed bearer keys and the per-call activity log.

Both tables are tenant-scoped through accounts.models.TenantOwnedModel, which
supplies `tenant` and `created_at`. The tenant is always copied from the
connection the row belongs to -- never from anything a client sends.
"""
import hashlib
import secrets

from django.conf import settings
from django.db import models

from accounts.models import TenantOwnedModel


class McpKey(TenantOwnedModel):
    """A bearer token the user pastes into Claude / ChatGPT.

    Only the SHA-256 hash is ever stored. The plaintext exists exactly once, in
    the response to the POST that minted it; there is deliberately no way to
    read it back, so a lost key is re-minted rather than recovered.
    """

    PREFIX = 'hc_'

    connection = models.ForeignKey(
        'connections.Connection',
        on_delete=models.CASCADE,
        related_name='keys',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='mcp_keys',
    )
    label = models.CharField(max_length=80, blank=True)
    # The first few characters of the plaintext, kept so the UI can tell two
    # keys apart in a list. It is not a credential and never authenticates.
    key_prefix = models.CharField(max_length=16, db_index=True)
    key_hash = models.CharField(max_length=64, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # "Mcp key" next to McpActivity's "MCP activity" in the same admin
        # section; the acronym is spelled one way everywhere else in the product.
        verbose_name = 'MCP key'
        verbose_name_plural = 'MCP keys'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['connection', 'revoked_at'], name='mcpkey_conn_revoked_idx'),
        ]

    def __str__(self):
        return self.label or self.key_prefix

    @property
    def is_active(self):
        return self.revoked_at is None

    @staticmethod
    def hash_token(plain):
        """SHA-256 hex of a plaintext token. The only form ever persisted."""
        return hashlib.sha256(plain.encode()).hexdigest()

    @classmethod
    def mint(cls, connection, created_by, label=''):
        """Create and save a key for `connection`, returning (row, plaintext).

        The plaintext is returned, not stored: the caller shows it once and
        drops it. The tenant is taken from the connection so a key can never be
        filed against an organization other than the one that owns the data it
        unlocks.
        """
        plain = cls.PREFIX + secrets.token_urlsafe(32)
        row = cls.objects.create(
            tenant=connection.tenant,
            connection=connection,
            created_by=created_by,
            label=label[:80],
            key_prefix=plain[:len(cls.PREFIX) + 4],
            key_hash=cls.hash_token(plain),
        )
        return row, plain


class McpActivity(TenantOwnedModel):
    """One row per MCP tool call: what ran, how long it took, and how it ended.

    The connection is nullable and SET_NULL because this is an append-only
    audit trail -- disconnecting a connector must not erase the history of what
    was done through it.
    """

    STATUS_OK = 'ok'
    STATUS_ERROR = 'error'
    STATUS_CHOICES = [
        (STATUS_OK, 'OK'),
        (STATUS_ERROR, 'Error'),
    ]

    connection = models.ForeignKey(
        'connections.Connection',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity',
    )
    connector = models.CharField(max_length=48, db_index=True)
    tool_name = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=STATUS_OK)
    duration_ms = models.IntegerField(null=True, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    # Already passed through redact_text() by the caller: an upstream error can
    # carry a live access token in a query string.
    error_message = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name_plural = 'MCP activity'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'connector', '-created_at'], name='mcpact_tenant_conn_idx'),
            models.Index(fields=['connection', '-created_at'], name='mcpact_conn_time_idx'),
        ]

    def __str__(self):
        return '{0}.{1} {2}'.format(self.connector, self.tool_name, self.status)


class OAuthClient(models.Model):
    """An AI client that registered itself to reach this server's MCP endpoints.

    claude.ai has no account here and no pre-shared credentials, so it registers
    at run time (RFC 7591) the first time someone adds a connector. That is the
    whole reason this table exists: there is nobody to hand a client id to in
    advance.

    Public clients only. Every registrant is a browser-driven app that cannot
    keep a secret, so there is no client_secret column to leak -- PKCE is what
    binds the authorization code to the client that asked for it.
    """

    client_id = models.CharField(max_length=64, unique=True)
    client_name = models.CharField(max_length=160, blank=True)
    # Exact-match allow-list. An attacker who guesses a client_id still cannot
    # redirect a code anywhere the registrant did not name up front.
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'OAuth client'
        verbose_name_plural = 'OAuth clients'
        ordering = ['-created_at']

    def __str__(self):
        return self.client_name or self.client_id

    def allows(self, redirect_uri):
        return redirect_uri in (self.redirect_uris or [])

    @classmethod
    def new_client_id(cls):
        return 'hcc_' + secrets.token_urlsafe(24)


class OAuthGrant(models.Model):
    """One authorization code: the few seconds between consent and the token.

    Single use. `consumed_at` is set inside the same transaction that mints the
    token, so replaying a stolen code finds it already spent.
    """

    LIFETIME_SECONDS = 300

    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name='grants')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='mcp_oauth_grants')
    connection = models.ForeignKey('connections.Connection', on_delete=models.CASCADE,
                                   related_name='oauth_grants')
    code_hash = models.CharField(max_length=64, db_index=True)
    redirect_uri = models.TextField()
    # PKCE. Stored as the challenge, never the verifier -- the verifier only
    # ever exists in the client and in the body of the token request.
    code_challenge = models.CharField(max_length=128)
    code_challenge_method = models.CharField(max_length=8, default='S256')
    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'OAuth grant'
        verbose_name_plural = 'OAuth grants'
        ordering = ['-created_at']

    def __str__(self):
        return 'grant for {0}'.format(self.connection_id)


class OAuthToken(models.Model):
    """An access token issued to an AI client, scoped to ONE connection.

    Same rule as McpKey: only the hash is stored. The token is scoped to a
    single connection rather than to the user, so a token minted for a GA4
    connection cannot be spent against that tenant's Google Ads one -- the same
    boundary McpKey enforces, reached by a different door.
    """

    PREFIX = 'hco_'
    LIFETIME_SECONDS = 60 * 60 * 24 * 30

    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name='tokens')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='mcp_oauth_tokens')
    connection = models.ForeignKey('connections.Connection', on_delete=models.CASCADE,
                                   related_name='oauth_tokens')
    token_hash = models.CharField(max_length=64, db_index=True)
    refresh_hash = models.CharField(max_length=64, db_index=True, blank=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'OAuth token'
        verbose_name_plural = 'OAuth tokens'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['connection', 'revoked_at'], name='mcpoauth_conn_revoked_idx'),
        ]

    def __str__(self):
        return 'token for {0}'.format(self.connection_id)

    @staticmethod
    def hash_token(plain):
        return hashlib.sha256(plain.encode('utf-8')).hexdigest()
