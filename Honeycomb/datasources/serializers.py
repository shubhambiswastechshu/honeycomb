from rest_framework import serializers

from accounts.relations import TenantScopedPrimaryKeyRelatedField
from workspaces.models import Workspace

from .models import DataSource
from .secrets import env_var_for, has_secret


class DataSourceSerializer(serializers.ModelSerializer):
    # Not a plain PrimaryKeyRelatedField: without the tenant filter a caller
    # could file a source inside another organization's workspace by sending
    # its id. See accounts/relations.py.
    workspace = TenantScopedPrimaryKeyRelatedField(queryset=Workspace.objects.all())
    workspace_name = serializers.CharField(source='workspace.name', read_only=True)
    kind_display = serializers.CharField(source='get_kind_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    secret_configured = serializers.SerializerMethodField()
    secret_env_var = serializers.SerializerMethodField()

    def get_secret_configured(self, source):
        """Whether a password is actually reachable under `secret_name`.

        A boolean, never the secret. Without it the only way to learn that
        nobody stored the password is to press Test and read a failure, and
        the form is the right place to find out.
        """
        return has_secret(source.secret_name)

    def get_secret_env_var(self, source):
        """The variable the default backend looks in, so the fix is copyable.

        The name of a variable is not sensitive; not knowing it is what
        turns a two-second fix into a support ticket.
        """
        return env_var_for(source.secret_name) if source.secret_name else ''

    class Meta:
        model = DataSource
        fields = [
            'id',
            'workspace',
            'workspace_name',
            'name',
            'kind',
            'kind_display',
            'host',
            'port',
            'database',
            'username',
            'secret_name',
            'secret_configured',
            'secret_env_var',
            'status',
            'status_display',
            'last_checked_at',
            'last_error',
            'created_at',
            'updated_at',
        ]
        # status is the result of a connection test, not something a client
        # gets to assert about itself.
        read_only_fields = [
            'id',
            'workspace_name',
            'kind_display',
            'secret_configured',
            'secret_env_var',
            'status',
            'status_display',
            'last_checked_at',
            'last_error',
            'created_at',
            'updated_at',
        ]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('Give the data source a name.')
        return name

    def validate(self, attrs):
        """Reject a connection that could never be opened.

        Catching this here rather than at connect time means the operator finds
        out while the form is still in front of them.
        """
        kind = attrs.get('kind', getattr(self.instance, 'kind', None))
        host = attrs.get('host', getattr(self.instance, 'host', ''))
        needs_host = kind in (
            DataSource.Kind.POSTGRES,
            DataSource.Kind.MYSQL,
            DataSource.Kind.HTTP,
        )
        if needs_host and not (host or '').strip():
            raise serializers.ValidationError(
                {'host': ['A host is required for this kind of source.']}
            )
        return attrs
