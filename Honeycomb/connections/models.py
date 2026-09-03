"""
A Connection is one tenant's configured instance of one connector.

It carries the credential blob (see connections/crypto.py for why that lives in
the row at all), the per-instance tool allow-list, and the endpoint slug that
its live MCP URL is built from.
"""

import json
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from accounts.models import Tenant, TenantOwnedModel

from . import crypto


def generate_endpoint_slug() -> str:
    """A fresh, unguessable path segment for this connection's MCP URL.

    token_urlsafe(16) already yields 22 characters; the slice is a belt-and-
    braces cap so the value can never overflow the 24-char column if the
    encoding ever changes.

    Module-level (not a lambda or a closure) because Django serialises the
    default into the migration by import path.
    """
    return secrets.token_urlsafe(16)[:22]


def generate_oauth_state() -> str:
    """A fresh nonce for one outbound OAuth round trip.

    43 characters from token_urlsafe(32), well inside the 64-char column.
    Module-level for the same reason as generate_endpoint_slug: Django
    serialises a field default into the migration by import path, so a lambda
    or a closure cannot be used here.
    """
    return secrets.token_urlsafe(32)


class Connection(TenantOwnedModel):
    """One connected account: a connector plus the credentials to reach it."""

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        ERROR = 'error', 'Error'

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='connections',
        help_text='NULL once the person who created this connection is deleted. '
                  'The connection itself belongs to the tenant, not to them, so '
                  'it must outlive their account.',
    )
    connector = models.CharField(
        max_length=48,
        db_index=True,
        help_text='Registry slug, e.g. "github". Not a foreign key: the '
                  'connector catalog is code, not rows.',
    )
    name = models.CharField(max_length=80, blank=True)
    # Which remote account this connection points at -- a Google address, for
    # instance. Deliberately NOT a secret and deliberately outside creds_enc:
    # it has to be queryable, because it is what stops a second trip through
    # the consent screen creating a duplicate of a connection that already
    # exists. Empty for api_key connectors, where the person names the
    # connection themselves and two against the same account are legitimate.
    account_key = models.CharField(
        max_length=190,
        blank=True,
        db_index=True,
        help_text='Remote account this connection is bound to. Never a secret.',
    )
    creds_enc = models.TextField(
        blank=True,
        help_text='Fernet ciphertext of a JSON credential object. Never read '
                  'this field directly -- use creds().',
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    last_error = models.TextField(blank=True)
    disabled_tools = models.JSONField(
        default=list,
        blank=True,
        help_text='Tool names switched off for this instance. Stored as the '
                  'deny-list rather than the allow-list so a connector that '
                  'gains a tool exposes it by default instead of silently '
                  'hiding it from every existing connection.',
    )
    # Live MCP URLs embed this slug: an AI client is configured once with
    # /mcp/<connector>/<endpoint_slug>/ and never asks again. Regenerating it
    # therefore breaks every client already pointed at this connection, with no
    # error the user can act on -- the endpoint simply stops existing. It is
    # generated once, at insert, and must never be rewritten. Rotating access
    # is done by revoking the McpKey, which is what auth actually turns on.
    endpoint_slug = models.CharField(
        max_length=24,
        unique=True,
        db_index=True,
        default=generate_endpoint_slug,
        editable=False,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            # One connection per remote account, enforced where it cannot be
            # raced. The condition excludes api_key connections, which carry an
            # empty account_key and may legitimately repeat.
            models.UniqueConstraint(
                fields=['tenant', 'connector', 'account_key'],
                condition=~models.Q(account_key=''),
                name='uniq_connection_per_account',
            ),
        ]
        indexes = [
            # Every list read is "this tenant's instances of this connector",
            # and the tenant FK index alone leaves the connector filter to a
            # scan once a workspace has more than a handful of connections.
            models.Index(
                fields=['tenant', 'connector'], name='connection_tenant_conn_idx'
            ),
        ]

    def __str__(self) -> str:
        return '{0} ({1})'.format(self.name or self.connector, self.endpoint_slug)

    def set_creds(self, values: dict) -> None:
        """Replace the stored credential object. An empty mapping clears it."""
        if not values:
            self.creds_enc = ''
            return
        # Compact separators: the blob is ciphertext either way, but there is no
        # reason to pay for whitespace in every row.
        self.creds_enc = crypto.encrypt(json.dumps(values, separators=(',', ':')))

    def creds(self) -> dict:
        """Decrypt and return the credential object, or {} when none is set."""
        if not self.creds_enc:
            return {}
        return json.loads(crypto.decrypt(self.creds_enc))

    def mark_error(self, message: str) -> None:
        """Park the connection in 'error' with a redacted, length-capped reason.

        Callers pass an already-redacted string; the cap here is the second
        line of defence, because last_error is rendered in the dashboard and an
        upstream stack trace is neither useful nor safe at full length.
        """
        self.status = self.Status.ERROR
        self.last_error = (message or '')[:2000]
        self.save(update_fields=['status', 'last_error', 'updated_at'])


class ConnectorOAuthState(models.Model):
    """One in-flight outbound OAuth round trip.

    Deliberately NOT a TenantOwnedModel. This is not customer data: it is a
    short-lived nonce that exists only between the moment a user clicks
    "Connect with Google" and the moment Google bounces their browser back. It
    carries a tenant FK all the same, because the callback arrives with no
    session -- the nonce is the only thing that says which organization the
    resulting Connection belongs to, and that answer must come from the row we
    wrote at start time rather than from anything the callback sends.

    The row IS the credential for the callback, which is why it is single-use
    (used_at) and short-lived (is_fresh). Nothing else guards that route.
    """

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='connector_oauth_states',
        help_text='The organization the resulting connection will land in, '
                  'captured when the flow started. The callback is '
                  'unauthenticated and cannot be asked.',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='connector_oauth_states',
        help_text='Who started the flow; becomes the connection\'s created_by.',
    )
    connector = models.CharField(
        max_length=48,
        help_text='Registry slug the flow was started for. Re-checked at the '
                  'callback so a nonce minted for one connector cannot be '
                  'redeemed against another.',
    )
    # unique=True already builds the index this is looked up by, so there is no
    # separate db_index: a second index on the same single column would be
    # written on every insert and read by nothing.
    state = models.CharField(
        max_length=64,
        unique=True,
        default=generate_oauth_state,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Stamped the instant the nonce is redeemed, before the token '
                  'exchange runs, so a replayed callback cannot mint a second '
                  'connection from the same code.',
    )

    class Meta:
        # Django would title-case the class name into "Connector o auth state",
        # which is what the admin rail, the breadcrumbs and the index all show.
        verbose_name = 'OAuth handshake'
        verbose_name_plural = 'OAuth handshakes'
        ordering = ('-created_at',)

    def __str__(self) -> str:
        return '{0} oauth state for {1}'.format(self.connector, self.tenant_id)

    def is_fresh(self, max_age_seconds: int = 600) -> bool:
        """True only while this nonce is still redeemable.

        Ten minutes is the whole budget for a consent screen. Anything longer
        is a window in which a leaked state value -- out of a browser history,
        a referrer header, a shoulder-surfed URL -- is still worth something.
        """
        if self.used_at is not None:
            return False
        if self.created_at is None:
            # Unsaved instance: it has never been issued, so it is not valid.
            return False
        return (timezone.now() - self.created_at).total_seconds() <= max_age_seconds
