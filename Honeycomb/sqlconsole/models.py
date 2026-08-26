from django.conf import settings
from django.db import models

from accounts.models import TenantOwnedModel
from datasources.models import DataSource
from workspaces.models import Workspace


class SavedQuery(TenantOwnedModel):
    """
    A named SQL statement someone wants to keep.

    The statement *is* executed -- by datasources.connectors, over a connection
    opened to the row's DataSource with that source's own credentials. It is
    never executed through Django's database connection, and that distinction
    is the whole security model: a saved query is untrusted text written by a
    user, and the connection that owns this table can read every tenant's data.
    Anything that runs SQL from this column must go through the connector.
    """

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name='saved_queries'
    )
    data_source = models.ForeignKey(
        DataSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='saved_queries',
        help_text='Where this query runs. Cleared, not cascaded, so removing a '
                  'source does not silently delete the work written against it.',
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    sql = models.TextField(
        help_text='Run through the source\'s own read-only connection, never '
                  'through Honeycomb\'s. See the class docstring.'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='queries_created',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'saved query'
        verbose_name_plural = 'saved queries'
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'name'], name='unique_query_name_per_workspace'
            ),
        ]

    def __str__(self):
        return self.name


class QueryRun(TenantOwnedModel):
    """
    One execution: what was sent, what came back, and how long it took.

    Why keep it at all -- an IDE without history is a tool people are afraid to
    use. "What did I run before lunch that worked" is the most common question
    a console gets asked, and re-deriving it from a warehouse's own logs is not
    something a product should ask of anyone.

    **This table holds customer data.** `result_rows` is a slice of whatever the
    query returned, copied out of the customer's warehouse into Honeycomb's own
    database. Three consequences, all deliberate:

    * It is capped. The connector stops at HONEYCOMB_SQL_MAX_ROWS and a byte
      ceiling, so a row here is bounded by design rather than by luck.
    * It is trimmed. `trim` keeps the most recent runs per workspace and
      deletes the rest, so this does not become an unbounded shadow copy of a
      warehouse that nobody remembers agreeing to store.
    * At real scale the rows belong in object storage with a lifecycle policy,
      and this column holds a key. A JSON column is the honest development
      answer, not the production one.
    """

    class State(models.TextChoices):
        SUCCEEDED = 'SUCCEEDED', 'Succeeded'
        FAILED = 'FAILED', 'Failed'

    # How many runs to keep per workspace. Enough that yesterday's work is
    # still there, small enough that the table stays a log and not an archive.
    KEEP_PER_WORKSPACE = 100

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name='query_runs'
    )
    data_source = models.ForeignKey(
        DataSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='query_runs',
    )
    saved_query = models.ForeignKey(
        SavedQuery,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='runs',
        help_text='Set when the run came from a saved query. Cleared if that '
                  'query is later deleted -- the history of what ran stays true.',
    )

    sql = models.TextField(help_text='Exactly what was sent, not what was saved.')
    state = models.CharField(max_length=16, choices=State.choices)

    result_columns = models.JSONField(default=list, blank=True)
    result_rows = models.JSONField(default=list, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    truncated = models.BooleanField(
        default=False,
        help_text='True when the result hit a cap, so the rows here are a '
                  'prefix rather than the answer.',
    )
    notice = models.CharField(max_length=255, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='query_runs',
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'query run'
        verbose_name_plural = 'query runs'
        indexes = [
            # The console asks one question of this table -- "the recent runs
            # in this workspace" -- and asks it on every page load.
            models.Index(fields=['workspace', '-created_at'], name='queryrun_ws_recent'),
        ]

    def __str__(self):
        return '{0} · {1}'.format(self.get_state_display(), self.created_at)

    @property
    def preview(self):
        """The first line of the statement, for a history list.

        Whitespace is collapsed because a query pasted from a formatter starts
        with a blank line as often as not, and a history entry that reads as
        empty is useless.
        """
        text = ' '.join((self.sql or '').split())
        return text[:120] + ('…' if len(text) > 120 else '')

    @classmethod
    def trim(cls, workspace):
        """Delete this workspace's runs beyond KEEP_PER_WORKSPACE.

        Called after each run rather than by a scheduled job: there is no
        worker in this project yet, and a retention rule that only holds when
        someone remembers to run a command is not a retention rule.

        Two statements rather than a subquery-delete because MySQL cannot
        delete from a table it also selects from, and there is no reason to
        write this so it only works on one engine.
        """
        keep = list(
            cls.objects.filter(workspace=workspace)
            .order_by('-created_at')
            .values_list('pk', flat=True)[: cls.KEEP_PER_WORKSPACE]
        )
        cls.objects.filter(workspace=workspace).exclude(pk__in=keep).delete()
