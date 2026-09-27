"""
A Report is a saved dashboard: a grid of widgets, each reading one connector tool.

It belongs to the organization, not to whoever made it -- a marketer leaves, the
client's report stays. ``layout`` is JSON rather than a widget table because a
widget's settings are the frontend's business and change with every visual; the
rules for what may be stored live in reports/layout.py and are applied by the
serializer, so nothing reaches this column unchecked.

Widgets point at connections by id inside that JSON, not by foreign key. The id
is therefore checked against the caller's tenant on every save AND again on
every run: a connection deleted since, or an id a client made up, becomes one
failed widget instead of a route into someone else's data.
"""

from django.conf import settings
from django.db import models

from accounts.models import TenantOwnedModel

from .layout import LAYOUT_SCHEMA_VERSION, default_filters


class Report(TenantOwnedModel):
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True)
    # A label, not a table: agencies name their clients however they like, and a
    # free-text field lets a marketer file a report under one without first
    # setting anything up. It can grow into a real Client model later; the
    # distinct values are already the list of clients.
    client = models.CharField(
        max_length=120,
        blank=True,
        help_text='The client this report is for. Used to group and filter reports.',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reports_created',
        help_text='NULL once the person who made this report is deleted. The '
                  'report belongs to the organization, so it outlives their account.',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reports_edited',
    )
    layout = models.JSONField(default=list, blank=True)
    filters = models.JSONField(default=default_filters, blank=True)
    schema_version = models.PositiveSmallIntegerField(default=LAYOUT_SCHEMA_VERSION)
    # Optimistic concurrency. A report is edited by autosave, often from more
    # than one tab or person, and the last write silently winning would throw
    # away someone's work. Every update bumps this; a client that sends the
    # version it last saw is refused (409) if it is no longer current.
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            # The list page: one tenant's reports, newest first.
            models.Index(fields=['tenant', '-updated_at'], name='report_tenant_updated_idx'),
        ]

    def __str__(self):
        return self.name
