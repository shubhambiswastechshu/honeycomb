"""
Serializers for saved reports.

They own validation and keep the views thin: the rules for what a layout may hold
are in reports/layout.py, and what is left for this file is the part that needs a
tenant -- that every connection a widget reads belongs to the caller's own
organization.
"""

from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from connections.models import Connection

from . import access
from .layout import LayoutError, check_filters, check_layout, connection_ids, has_control_chars
from .models import Report

#: A workspace's reports are listed on one page and never paged, so the number
#: is bounded here rather than left to grow until the list is slow.
MAX_REPORTS_PER_TENANT = 500

DEFAULT_NAME = 'Untitled report'


class VersionConflict(Exception):
    """The copy the client edited is no longer the current one.

    A plain exception and not a DRF APIException on purpose: APIException coerces
    every leaf of its detail to a string, which would send ``"current_version":
    "8"`` where the client needs the integer. ReportViewSet.update catches this
    and renders the 409 itself.
    """

    def __init__(self, current):
        self.current = current
        super(VersionConflict, self).__init__('This report was changed somewhere else.')


def display_name(user):
    """A person's name for a byline: their name if they have one, else their email."""
    if user is None:
        return ''
    return user.full_name or user.email


class ReportSerializer(serializers.ModelSerializer):
    # Writable, but only as a claim: "this is the version I was editing". The
    # view refuses a stale one; what is stored is always the server's own count.
    version = serializers.IntegerField(required=False, min_value=1)
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()
    can_delete = serializers.SerializerMethodField()

    class Meta:
        model = Report
        fields = (
            'id', 'name', 'description', 'client', 'layout', 'filters',
            'schema_version', 'version', 'created_by', 'created_by_name',
            'updated_by_name', 'created_at', 'updated_at', 'can_delete',
        )
        read_only_fields = ('id', 'schema_version', 'created_by', 'created_at', 'updated_at')

    def get_fields(self):
        fields = super(ReportSerializer, self).get_fields()
        # A marketer starting a report should not have to name it first; a full
        # update (PUT) of an existing one still has to say what it is called.
        if self.instance is None:
            fields['name'].required = False
        return fields

    def _tenant(self):
        request = self.context.get('request')
        tenant = getattr(getattr(request, 'user', None), 'tenant', None)
        if tenant is None:
            raise PermissionDenied('This account is not attached to an organization.')
        return tenant

    def get_created_by_name(self, obj):
        return display_name(obj.created_by)

    def get_updated_by_name(self, obj):
        return display_name(obj.updated_by)

    def get_can_delete(self, obj):
        return access.can_delete(self.context['request'].user, obj)

    def validate_version(self, value):
        if self.instance is not None and value != self.instance.version:
            raise VersionConflict(self.instance.version)
        return value

    def _no_control_chars(self, value):
        # name/description/client are plain model CharFields, not layout JSON,
        # so they never pass through layout.py's checks. PostgreSQL's text
        # storage cannot hold a NUL byte at all, so this is refused as an
        # ordinary 400 here rather than reaching the database as a 500.
        if has_control_chars(value):
            raise serializers.ValidationError('This cannot contain a control character.')
        return value

    def validate_name(self, value):
        return self._no_control_chars(value)

    def validate_description(self, value):
        return self._no_control_chars(value)

    def validate_client(self, value):
        return self._no_control_chars(value)

    def validate_layout(self, value):
        try:
            layout = check_layout(value)
        except LayoutError as exc:
            raise serializers.ValidationError(str(exc))
        ids = connection_ids(layout)
        if ids:
            # One query for the whole layout, not one per widget. A connection id
            # is only ever accepted if it belongs to the caller's organization:
            # the layout is JSON, so there is no foreign key to enforce it.
            owned = set(
                Connection.objects.filter(tenant=self._tenant(), id__in=ids)
                .values_list('id', flat=True)
            )
            for index, widget in enumerate(layout):
                if widget['source']['connection_id'] not in owned:
                    raise serializers.ValidationError(
                        'Widget {0} reads a connection that is not part of this workspace.'.format(
                            index + 1))
        return layout

    def validate_filters(self, value):
        try:
            return check_filters(value)
        except LayoutError as exc:
            raise serializers.ValidationError(str(exc))

    def validate(self, attrs):
        if self.instance is None:
            attrs.setdefault('name', DEFAULT_NAME)
            if Report.objects.filter(tenant=self._tenant()).count() >= MAX_REPORTS_PER_TENANT:
                raise serializers.ValidationError(
                    'Your workspace has reached the limit of {0} reports.'.format(
                        MAX_REPORTS_PER_TENANT))
        return attrs


class ReportListSerializer(ReportSerializer):
    """The row of the reports page: enough to find a report, not to draw it.

    Leaves out ``layout`` and ``filters``, which are the bulk of a row -- the
    list view defers them, so listing a workspace never reads them at all.
    """

    class Meta(ReportSerializer.Meta):
        fields = (
            'id', 'name', 'description', 'client', 'version', 'created_by',
            'created_by_name', 'updated_by_name', 'created_at', 'updated_at', 'can_delete',
        )


class RunRequestSerializer(serializers.Serializer):
    """Body of POST /api/reports/<id>/run/ -- both keys optional.

    ``layout`` runs a draft instead of the saved widgets (the builder previews
    what is being edited before it is saved) and ``filters`` replaces the saved
    filters for this run only. They are checked by the same rules as a save, but
    connection ids are not, here: a run finds them through the caller's tenant
    and reports the ones it cannot as failed widgets.
    """

    layout = serializers.JSONField(required=False)
    filters = serializers.JSONField(required=False)

    def validate_layout(self, value):
        try:
            return check_layout(value)
        except LayoutError as exc:
            raise serializers.ValidationError(str(exc))

    def validate_filters(self, value):
        try:
            return check_filters(value)
        except LayoutError as exc:
            raise serializers.ValidationError(str(exc))
