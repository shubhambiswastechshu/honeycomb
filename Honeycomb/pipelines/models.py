from django.conf import settings
from django.db import models

from accounts.models import TenantOwnedModel
from datasources.models import DataSource
from workspaces.models import Workspace


class Pipeline(TenantOwnedModel):
    """
    A scheduled move of data from a source into somewhere useful.

    Nothing runs a pipeline yet: there is no scheduler and no worker, so
    `status` records intent and `last_run_at` stays null until one exists. A
    pipeline that claimed to be ACTIVE while nothing executed it would be worse
    than one that admits it is a draft.
    """

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        ACTIVE = 'ACTIVE', 'Active'
        PAUSED = 'PAUSED', 'Paused'

    class Outcome(models.TextChoices):
        NEVER_RUN = 'NEVER_RUN', 'Never run'
        SUCCEEDED = 'SUCCEEDED', 'Succeeded'
        FAILED = 'FAILED', 'Failed'

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name='pipelines'
    )
    source = models.ForeignKey(
        DataSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pipelines',
        help_text='Where this pipeline reads from.',
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    destination = models.CharField(
        max_length=255,
        blank=True,
        help_text='Table or path the run writes into.',
    )
    schedule = models.CharField(
        max_length=120,
        blank=True,
        help_text='Cron expression, e.g. "0 3 * * *". Blank means manual only.',
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    last_outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.NEVER_RUN
    )
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pipelines_created',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'name'], name='unique_pipeline_name_per_workspace'
            ),
        ]

    def __str__(self):
        return self.name
