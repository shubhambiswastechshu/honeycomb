from rest_framework.exceptions import NotAuthenticated, PermissionDenied


class TenantScopedQuerysetMixin(object):
    """
    Confine a DRF view to the tenant of the authenticated user.

    This is the guard-rail that makes shared-schema multi-tenancy real: rather
    than trusting every view author to remember `.filter(tenant=...)`, mix this
    in and the filter (plus the tenant stamp on create) happens automatically.

    Usage::

        from accounts.mixins import TenantScopedQuerysetMixin

        class ProjectViewSet(TenantScopedQuerysetMixin, viewsets.ModelViewSet):
            queryset = Project.objects.all()          # Project(TenantOwnedModel)
            serializer_class = ProjectSerializer

    Reads become ``Project.objects.filter(tenant=request.user.tenant)`` and
    writes are stamped with the same tenant, so a detail route cannot reach a
    neighbouring organization's row even if its primary key is guessed. Set
    ``tenant_field`` if the foreign key is named something other than
    ``tenant``.
    """

    tenant_field = 'tenant'

    def get_tenant(self):
        user = getattr(self.request, 'user', None)
        if user is None or not user.is_authenticated:
            raise NotAuthenticated()
        tenant = getattr(user, 'tenant', None)
        if tenant is None:
            # Platform-level superusers have no tenant; they must not silently
            # fall through to an unfiltered queryset.
            raise PermissionDenied('This account is not attached to an organization.')
        return tenant

    def get_queryset(self):
        queryset = super(TenantScopedQuerysetMixin, self).get_queryset()
        return queryset.filter(**{self.tenant_field: self.get_tenant()})

    def perform_create(self, serializer):
        serializer.save(**{self.tenant_field: self.get_tenant()})
