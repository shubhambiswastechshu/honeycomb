from rest_framework import serializers

from accounts.relations import TenantScopedPrimaryKeyRelatedField
from workspaces.models import Workspace

from .models import PythonScript


class PythonScriptSerializer(serializers.ModelSerializer):
    workspace = TenantScopedPrimaryKeyRelatedField(queryset=Workspace.objects.all())
    workspace_name = serializers.CharField(source='workspace.name', read_only=True)
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=None
    )

    class Meta:
        model = PythonScript
        fields = [
            'id',
            'workspace',
            'workspace_name',
            'name',
            'description',
            'code',
            'created_by_name',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'workspace_name',
            'created_by_name',
            'created_at',
            'updated_at',
        ]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('Give the script a name.')
        return name

    def validate_code(self, value):
        # A ceiling, not a review. Nothing here inspects what the code does --
        # it never runs on this machine (see the model), so there is nothing to
        # inspect it for. This only stops the field being an unbounded blob.
        if len(value or '') > 400000:
            raise serializers.ValidationError('That script is too long to save.')
        return value
