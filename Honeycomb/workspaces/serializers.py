from rest_framework import serializers

from .models import Workspace


class WorkspaceSerializer(serializers.ModelSerializer):
    counts = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = [
            'id',
            'name',
            'slug',
            'description',
            'counts',
            'created_at',
            'updated_at',
        ]
        # slug is derived from the name, and it is stable on purpose: it ends up
        # in URLs, so renaming a workspace must not break links that already
        # point at it.
        read_only_fields = ['id', 'slug', 'counts', 'created_at', 'updated_at']

    def get_counts(self, workspace):
        """What the workspace holds, for the dashboard tile.

        Read off prefetched counts when the view annotated them, so a list of
        N workspaces stays one query instead of 3N.
        """
        return {
            'data_sources': getattr(workspace, 'data_source_count', None)
            if getattr(workspace, 'data_source_count', None) is not None
            else workspace.data_sources.count(),
            'queries': getattr(workspace, 'query_count', None)
            if getattr(workspace, 'query_count', None) is not None
            else workspace.saved_queries.count(),
            'pipelines': getattr(workspace, 'pipeline_count', None)
            if getattr(workspace, 'pipeline_count', None) is not None
            else workspace.pipelines.count(),
        }

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('Give the workspace a name.')
        return name
