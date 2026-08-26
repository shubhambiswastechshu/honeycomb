from rest_framework import serializers

from accounts.relations import TenantScopedPrimaryKeyRelatedField
from datasources.models import DataSource
from workspaces.models import Workspace

from .models import Pipeline


class PipelineSerializer(serializers.ModelSerializer):
    workspace = TenantScopedPrimaryKeyRelatedField(queryset=Workspace.objects.all())
    source = TenantScopedPrimaryKeyRelatedField(
        queryset=DataSource.objects.all(), required=False, allow_null=True
    )
    workspace_name = serializers.CharField(source='workspace.name', read_only=True)
    source_name = serializers.CharField(
        source='source.name', read_only=True, default=None
    )
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    last_outcome_display = serializers.CharField(
        source='get_last_outcome_display', read_only=True
    )

    class Meta:
        model = Pipeline
        fields = [
            'id',
            'workspace',
            'workspace_name',
            'source',
            'source_name',
            'name',
            'description',
            'destination',
            'schedule',
            'status',
            'status_display',
            'last_outcome',
            'last_outcome_display',
            'last_run_at',
            'created_at',
            'updated_at',
        ]
        # The outcome and the timestamp are facts about a run. Only whatever
        # executes pipelines gets to write them, and nothing does yet.
        read_only_fields = [
            'id',
            'workspace_name',
            'source_name',
            'status_display',
            'last_outcome',
            'last_outcome_display',
            'last_run_at',
            'created_at',
            'updated_at',
        ]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('Give the pipeline a name.')
        return name

    def validate(self, attrs):
        workspace = attrs.get('workspace', getattr(self.instance, 'workspace', None))
        source = attrs.get('source', getattr(self.instance, 'source', None))
        if source is not None and workspace is not None:
            if source.workspace_id != workspace.pk:
                raise serializers.ValidationError(
                    {'source': ['That source belongs to a different workspace.']}
                )

        # A pipeline with no source has nothing to read; letting it go ACTIVE
        # would put a row in the scheduler's queue that can only ever fail.
        status = attrs.get('status', getattr(self.instance, 'status', None))
        if status == Pipeline.Status.ACTIVE and source is None:
            raise serializers.ValidationError(
                {'source': ['Choose a data source before activating this pipeline.']}
            )
        return attrs
