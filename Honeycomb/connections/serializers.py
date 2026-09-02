"""
Serialisation and validation for the connections control plane.

Everything the API accepts is checked here, so the views stay thin: the
connector slug is resolved against the registry, the credential object is
checked field by field against that connector's declared cred_fields, and the
credential itself is write-only in every direction.
"""

from django.conf import settings
from rest_framework import serializers

from connectors import registry

from .models import Connection


def catalog_tools(connector) -> list:
    """Normalise a connector's catalog into [{name, description, write}].

    The registry pins the *contents* of a catalog entry but not the container:
    ported connector modules declare theirs as a list of dicts or as a mapping
    of name -> spec, both of which register cleanly. Normalising in one place
    means tool_count, the catalog endpoint and the per-connection tool list can
    never disagree about what the catalog holds.
    """
    catalog = getattr(connector, 'catalog', None) or ()
    if isinstance(catalog, dict):
        entries = [dict(spec or {}, name=name) for name, spec in catalog.items()]
    else:
        entries = [dict(spec or {}) for spec in catalog]
    tools = []
    for entry in entries:
        name = entry.get('name') or ''
        if not name:
            continue
        tools.append({
            'name': name,
            'description': entry.get('description') or '',
            'write': bool(entry.get('write', False)),
        })
    return tools


def connector_spec(connector, connected_count: int = 0) -> dict:
    """The marketplace-card shape for one connector."""
    return {
        'slug': connector.slug,
        'label': connector.label,
        'auth': connector.auth,
        'description': getattr(connector, 'description', '') or '',
        'category': getattr(connector, 'category', '') or '',
        'cred_fields': list(getattr(connector, 'cred_fields', None) or []),
        'tool_count': len(catalog_tools(connector)),
        'connected_count': connected_count,
    }


def connector_detail(connector, connected_count: int = 0) -> dict:
    """The card shape plus the full tool list."""
    payload = connector_spec(connector, connected_count)
    payload['tools'] = catalog_tools(connector)
    return payload


class ConnectionSerializer(serializers.ModelSerializer):
    """Read and write one connected account.

    `creds` is write-only and has no read counterpart at all -- not a masked
    one, not a bullet-string placeholder. A masked field still tells a reader
    which keys are set and how long each value is, and the dashboard has no
    reason to know either; re-entering a credential is the only way to change
    it.
    """

    connector_label = serializers.SerializerMethodField()
    mcp_url = serializers.SerializerMethodField()
    tool_count = serializers.SerializerMethodField()
    key_count = serializers.SerializerMethodField()
    creds = serializers.DictField(
        child=serializers.CharField(allow_blank=False, trim_whitespace=False),
        write_only=True,
        required=False,
        help_text=(
            'The connector declared cred_fields, complete. Replaces whatever '
            'is stored; never merged into it.'
        ),
    )

    class Meta:
        model = Connection
        fields = (
            'id',
            'connector',
            'connector_label',
            'name',
            'status',
            'last_error',
            'endpoint_slug',
            'mcp_url',
            'disabled_tools',
            'tool_count',
            'key_count',
            'created_at',
            'updated_at',
            'creds',
        )
        read_only_fields = (
            'id',
            'status',
            'last_error',
            'endpoint_slug',
            # Toggled one tool at a time through /connections/<id>/tools/, which
            # validates the name against the catalog. Writable here, a client
            # could park arbitrary strings in the deny-list.
            'disabled_tools',
            'created_at',
            'updated_at',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance is not None:
            # The endpoint slug is fixed and the live URL embeds the connector,
            # so re-pointing an existing row at a different connector would
            # change what an already-configured MCP URL means without changing
            # the URL itself.
            self.fields['connector'].read_only = True

    def get_connector_label(self, obj) -> str:
        connector = registry.get(obj.connector)
        # A connector can be dropped from the catalog while rows still name it;
        # falling back to the slug keeps those rows renderable instead of 500.
        return connector.label if connector is not None else obj.connector

    def get_tool_count(self, obj) -> int:
        connector = registry.get(obj.connector)
        return len(catalog_tools(connector)) if connector is not None else 0

    def get_key_count(self, obj) -> int:
        """Live (unrevoked) keys for this connection.

        Prefers the annotation the viewset adds, so a list of N connections
        stays one query. The manager fallback covers this serializer being used
        outside that viewset, and the 0 covers the mcp app not being installed.
        """
        annotated = getattr(obj, 'active_key_count', None)
        if annotated is not None:
            return annotated
        manager = getattr(obj, 'keys', None)
        if manager is None:
            return 0
        return manager.filter(revoked_at__isnull=True).count()

    def get_mcp_url(self, obj) -> str:
        base = (getattr(settings, 'HONEYCOMB_PUBLIC_BASE', '') or '').rstrip('/')
        return '{0}/mcp/{1}/{2}/'.format(base, obj.connector, obj.endpoint_slug)

    def validate_connector(self, value):
        if registry.get(value) is None:
            raise serializers.ValidationError('Unknown connector.')
        return value

    def validate(self, attrs):
        slug = attrs.get('connector') or getattr(self.instance, 'connector', '')
        connector = registry.get(slug)
        if connector is None:
            raise serializers.ValidationError({'connector': 'Unknown connector.'})
        declared = list(getattr(connector, 'cred_fields', None) or [])
        if 'creds' in attrs:
            attrs['creds'] = self._check_creds(attrs['creds'], declared)
        elif self.instance is None and declared:
            raise serializers.ValidationError(
                {'creds': 'This connector needs credentials to be created.'}
            )
        return attrs

    def _check_creds(self, creds: dict, declared: list) -> dict:
        """Reject unknown keys and demand every declared one.

        Both halves matter. A missing field surfaces here as a form error
        instead of as an opaque 401 from the upstream API on the first tool
        call. An unknown field is rejected rather than dropped, because
        silently discarding a key the user typed is how a credential ends up
        looking saved when it never was.
        """
        unknown = sorted(set(creds) - set(declared))
        if unknown:
            raise serializers.ValidationError({
                'creds': 'Unexpected field{0}: {1}.'.format(
                    '' if len(unknown) == 1 else 's', ', '.join(unknown)
                ),
            })
        missing = [field for field in declared if not (creds.get(field) or '').strip()]
        if missing:
            raise serializers.ValidationError({
                'creds': 'Missing required field{0}: {1}.'.format(
                    '' if len(missing) == 1 else 's', ', '.join(missing)
                ),
            })
        return {field: creds[field].strip() for field in declared}

    def create(self, validated_data):
        creds = validated_data.pop('creds', None)
        connection = Connection(**validated_data)
        connection.set_creds(creds or {})
        connection.save()
        return connection

    def update(self, instance, validated_data):
        creds = validated_data.pop('creds', None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if creds is not None:
            instance.set_creds(creds)
            # Re-entering credentials is how a user answers an expired token,
            # so the stale verdict has to be cleared along with them -- else the
            # connection keeps reading "error" until something happens to call
            # it again, and the fix looks like it did not work.
            instance.status = Connection.Status.ACTIVE
            instance.last_error = ''
        instance.save()
        return instance


class ToolToggleSerializer(serializers.Serializer):
    """Body of POST /api/connections/<id>/tools/.

    Needs `connection` in the serializer context: a tool name is only valid
    against the catalog of the connector that connection belongs to.
    """

    tool = serializers.CharField(max_length=64)
    enabled = serializers.BooleanField()

    def validate_tool(self, value):
        connection = self.context['connection']
        connector = registry.get(connection.connector)
        if connector is None:
            raise serializers.ValidationError(
                'This connection points at a connector that is no longer installed.'
            )
        names = {tool['name'] for tool in catalog_tools(connector)}
        if value not in names:
            # Names the connector rather than listing its tools; the catalog is
            # already readable at /api/connectors/<slug>/.
            raise serializers.ValidationError(
                'No tool named "{0}" on {1}.'.format(value, connector.label)
            )
        return value
