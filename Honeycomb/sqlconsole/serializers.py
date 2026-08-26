from rest_framework import serializers

from accounts.relations import TenantScopedPrimaryKeyRelatedField
from datasources.models import DataSource
from workspaces.models import Workspace

from .models import QueryRun, SavedQuery


class SavedQuerySerializer(serializers.ModelSerializer):
    workspace = TenantScopedPrimaryKeyRelatedField(queryset=Workspace.objects.all())
    data_source = TenantScopedPrimaryKeyRelatedField(
        queryset=DataSource.objects.all(), required=False, allow_null=True
    )
    workspace_name = serializers.CharField(source='workspace.name', read_only=True)
    data_source_name = serializers.CharField(
        source='data_source.name', read_only=True, default=None
    )
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=None
    )

    class Meta:
        model = SavedQuery
        fields = [
            'id',
            'workspace',
            'workspace_name',
            'data_source',
            'data_source_name',
            'name',
            'description',
            'sql',
            'created_by_name',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'workspace_name',
            'data_source_name',
            'created_by_name',
            'created_at',
            'updated_at',
        ]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('Give the query a name.')
        return name

    def validate_sql(self, value):
        sql = value.strip()
        if not sql:
            raise serializers.ValidationError('The query is empty.')
        return sql

    def validate(self, attrs):
        """A query must run against a source in its own workspace.

        The related fields already confine both ids to the caller's tenant, so
        this is the narrower check: source and query agreeing on a workspace.
        Without it a query filed under "Marketing" could point at "Finance"'s
        warehouse -- inside one tenant, but still not what anyone meant.
        """
        workspace = attrs.get('workspace', getattr(self.instance, 'workspace', None))
        source = attrs.get('data_source', getattr(self.instance, 'data_source', None))
        if source is not None and workspace is not None:
            if source.workspace_id != workspace.pk:
                raise serializers.ValidationError(
                    {'data_source': ['That source belongs to a different workspace.']}
                )
        return attrs


class QueryRunSerializer(serializers.ModelSerializer):
    """A finished run, as the history list and the results grid read it."""

    workspace_name = serializers.CharField(source='workspace.name', read_only=True)
    data_source_name = serializers.CharField(
        source='data_source.name', read_only=True, default=None
    )
    saved_query_name = serializers.CharField(
        source='saved_query.name', read_only=True, default=None
    )
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=None
    )
    state_display = serializers.CharField(source='get_state_display', read_only=True)
    preview = serializers.CharField(read_only=True)

    class Meta:
        model = QueryRun
        fields = [
            'id',
            'workspace',
            'workspace_name',
            'data_source',
            'data_source_name',
            'saved_query',
            'saved_query_name',
            'sql',
            'preview',
            'state',
            'state_display',
            'result_columns',
            'result_rows',
            'row_count',
            'truncated',
            'notice',
            'duration_ms',
            'error',
            'created_by_name',
            'created_at',
        ]
        # Every field is a record of something that already happened. A client
        # asserting its own row_count would be a client writing history.
        read_only_fields = fields


class QueryRunListSerializer(QueryRunSerializer):
    """The same run without its rows.

    History is fetched on every page load and a hundred entries each carrying a
    thousand rows is megabytes of payload nobody looks at. The rows arrive when
    an entry is opened.
    """

    class Meta(QueryRunSerializer.Meta):
        fields = [
            field
            for field in QueryRunSerializer.Meta.fields
            if field not in ('result_columns', 'result_rows', 'sql')
        ]
        read_only_fields = fields


class RunRequestSerializer(serializers.Serializer):
    """What the console posts to execute something.

    `sql` is sent as text rather than as a saved query id even when a saved
    query is open, because the editor's buffer is what the person is looking
    at. Running the stored version of a query someone has just edited is the
    kind of surprise that costs trust in a tool immediately.
    """

    workspace = TenantScopedPrimaryKeyRelatedField(queryset=Workspace.objects.all())
    data_source = TenantScopedPrimaryKeyRelatedField(queryset=DataSource.objects.all())
    saved_query = TenantScopedPrimaryKeyRelatedField(
        queryset=SavedQuery.objects.all(), required=False, allow_null=True
    )
    sql = serializers.CharField(trim_whitespace=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100000)

    def validate_sql(self, value):
        statement = (value or '').strip()
        if not statement:
            raise serializers.ValidationError('There is no statement to run.')
        # A ceiling on the text itself, separate from the row cap. Nothing a
        # person types is this long, and an unbounded field on a POST that ends
        # in a database connection is an easy thing to abuse.
        if len(statement) > 200000:
            raise serializers.ValidationError('That statement is too long to run.')
        return statement

    def validate(self, attrs):
        """The source has to belong to the workspace the run is filed under.

        Both ids are already confined to the caller's tenant by the related
        fields. This is the narrower rule: a run recorded against "Marketing"
        that actually read Finance's warehouse would make the history lie about
        who touched what.
        """
        workspace = attrs['workspace']
        source = attrs['data_source']
        if source.workspace_id != workspace.pk:
            raise serializers.ValidationError(
                {'data_source': ['That source belongs to a different workspace.']}
            )
        saved = attrs.get('saved_query')
        if saved is not None and saved.workspace_id != workspace.pk:
            raise serializers.ValidationError(
                {'saved_query': ['That query belongs to a different workspace.']}
            )
        return attrs
