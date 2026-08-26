from django.db import models

from accounts.models import TenantOwnedModel
from workspaces.models import Workspace


class DataSource(TenantOwnedModel):
    """
    Somewhere this workspace reads data from.

    **No credential is stored on this row, and that is deliberate.** The table
    holds only the coordinates needed to *find* a system -- host, port,
    database, user. The password or key lives in a secret manager and this row
    keeps `secret_name`, the handle to look it up by.

    Storing the secret here would put every customer's production database
    password in one table, reachable by anything with a Django admin session, a
    SQL injection, or a database backup. A handle is worth nothing on its own.
    Resist the shortcut when wiring the first real connector: the moment a
    `password` column exists, a breach of this table becomes a breach of every
    customer's warehouse.
    """

    class Kind(models.TextChoices):
        POSTGRES = 'POSTGRES', 'PostgreSQL'
        MYSQL = 'MYSQL', 'MySQL'
        BIGQUERY = 'BIGQUERY', 'BigQuery'
        SNOWFLAKE = 'SNOWFLAKE', 'Snowflake'
        S3 = 'S3', 'Amazon S3'
        HTTP = 'HTTP', 'HTTP endpoint'

    class Status(models.TextChoices):
        # A source starts unproven. Nothing should read from it until a
        # connection test has actually succeeded.
        PENDING = 'PENDING', 'Not tested'
        CONNECTED = 'CONNECTED', 'Connected'
        FAILED = 'FAILED', 'Failed'

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name='data_sources'
    )
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.POSTGRES)

    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(null=True, blank=True)
    database = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True)
    secret_name = models.CharField(
        max_length=255,
        blank=True,
        help_text='Key this connection\'s password is stored under in the '
                  'secret manager. Never the password itself.',
    )

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'name'], name='unique_source_name_per_workspace'
            ),
        ]

    def __str__(self):
        return '{0} ({1})'.format(self.name, self.get_kind_display())
