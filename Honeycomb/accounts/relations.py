from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied


class TenantScopedPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    """
    A related field that can only ever point at a row in the caller's tenant.

    TenantScopedQuerysetMixin guards the *rows a view returns*. It does nothing
    about the ids a client sends in, and a plain PrimaryKeyRelatedField will
    happily resolve any primary key in the table -- so

        POST /api/sql/queries/  {"workspace": 41, "name": "..."}

    would file the caller's query inside workspace 41 even when that workspace
    belongs to another organization. The row is then unreadable to its creator
    (the read filter excludes it) and quietly present in someone else's data.
    That is a write across a tenant boundary, which is the one thing
    shared-schema tenancy has to make impossible.

    Filtering the field's queryset closes it at validation time: an id outside
    the tenant is simply not a valid choice, and DRF reports it as one more
    invalid field rather than as an error that leaks whether the row exists.

    Usage::

        class SavedQuerySerializer(serializers.ModelSerializer):
            workspace = TenantScopedPrimaryKeyRelatedField(
                queryset=Workspace.objects.all()
            )

    The model behind the field must inherit TenantOwnedModel, or carry whatever
    field name is passed as `tenant_field`.
    """

    def __init__(self, *args, **kwargs):
        self.tenant_field = kwargs.pop('tenant_field', 'tenant')
        super().__init__(*args, **kwargs)

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(**{self.tenant_field: self._tenant()})

    def _tenant(self):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        tenant = getattr(user, 'tenant', None)
        if tenant is None:
            # Platform superusers have no tenant. Refusing is the only safe
            # answer: an unfiltered queryset here would hand them every
            # organization's rows as valid choices.
            raise PermissionDenied('This account is not attached to an organization.')
        return tenant
