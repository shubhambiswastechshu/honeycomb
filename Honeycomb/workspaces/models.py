from django.conf import settings
from django.db import models
from django.utils.text import slugify

from accounts.models import TenantOwnedModel


class Workspace(TenantOwnedModel):
    """
    A named container inside an organization.

    The layering matters and is easy to get wrong: a Tenant is the *customer*
    and the tenancy boundary; a Workspace is a folder that customer organises
    work into. Data sources, saved queries and pipelines all hang off a
    workspace, so a team can keep "Analytics" and "Marketing" apart without
    needing a second account.

    Inherits TenantOwnedModel, so every row carries the tenant foreign key that
    TenantScopedQuerysetMixin filters on. Nothing below re-checks the tenant --
    it is the base class's job, and duplicating it invites the two checks to
    drift.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, blank=True)
    description = models.TextField(
        blank=True,
        help_text='What this workspace is for. Shown on the dashboard tile.',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workspaces_created',
        help_text='Cleared rather than cascading: losing an author must not '
                  'delete the team\'s workspace.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            # Scoped to the tenant, not global: two organizations naming a
            # workspace "Analytics" is normal and must not collide.
            models.UniqueConstraint(
                fields=['tenant', 'slug'], name='unique_workspace_slug_per_tenant'
            ),
            models.UniqueConstraint(
                fields=['tenant', 'name'], name='unique_workspace_name_per_tenant'
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._build_unique_slug()
        return super().save(*args, **kwargs)

    def _build_unique_slug(self):
        """Mirrors Tenant._build_unique_slug, but only unique within a tenant."""
        base = slugify(self.name)[:120] or 'workspace'
        slug = base
        suffix = 2
        queryset = Workspace.objects.filter(tenant=self.tenant_id)
        if self.pk:
            queryset = queryset.exclude(pk=self.pk)
        while queryset.filter(slug=slug).exists():
            slug = '{0}-{1}'.format(base, suffix)
            suffix += 1
        return slug
