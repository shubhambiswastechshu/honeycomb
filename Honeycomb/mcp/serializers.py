"""Serializers for the MCP key control plane.

A key is write-once from the client's point of view: it posts a label and gets
the plaintext token back in that one response. There is deliberately no
serializer field that can read a token or a hash out of the database.
"""
from rest_framework import serializers

from connectors import registry

from .models import McpActivity, McpKey

#: Ceilings on the tenant-wide activity query string. The Overview asks for 20
#: rows and 7 days; the caps stop a client turning one page load into an
#: unbounded scan of an append-only table.
DEFAULT_ACTIVITY_LIMIT = 20
MAX_ACTIVITY_LIMIT = 100
DEFAULT_SUMMARY_DAYS = 7
#: The Overview's activity field asks for a quarter. The response is one small
#: row per day whatever the window -- 90 days is ~4KB of JSON, and the query
#: behind it is a single grouped aggregate over an indexed timestamp, so the
#: cost of the larger window is bounded by the number of DAYS, not by how much
#: traffic those days hold.
MAX_SUMMARY_DAYS = 90


class McpKeySerializer(serializers.ModelSerializer):
    """Everything the dashboard may know about a key. No hash, no plaintext."""

    class Meta:
        model = McpKey
        fields = ('id', 'label', 'key_prefix', 'created_at', 'last_used_at', 'revoked_at')
        read_only_fields = fields


class McpKeyCreateSerializer(serializers.Serializer):
    """Input for minting. The label is the only thing a client gets to choose --
    the token, its prefix and its tenant are all derived server-side."""

    label = serializers.CharField(max_length=80, required=False, allow_blank=True, default='')

    def validate_label(self, value):
        return (value or '').strip()


class McpActivitySerializer(serializers.ModelSerializer):
    """Read-only view of the audit trail, shaped to the frontend's ActivityRow."""

    class Meta:
        model = McpActivity
        fields = ('id', 'connector', 'tool_name', 'status', 'duration_ms',
                  'error_message', 'created_at')
        read_only_fields = fields


class McpActivityRowSerializer(serializers.ModelSerializer):
    """One row of the tenant-wide feed, shaped for the Overview page.

    Two of the fields are resolved rather than stored, because the log is
    append-only and outlives the things it refers to:

    * ``connector_label`` comes off the registry, which is code -- a connector
      that ships under a new label renames itself everywhere at once. When the
      slug is no longer registered the raw slug is returned, so removing a
      connector cannot blank out the history of what it did.
    * ``connection_name`` is null once the connection has been deleted. That is
      exactly why McpActivity.connection is SET_NULL: the call still happened.
    """

    connector_label = serializers.SerializerMethodField()
    connection_name = serializers.SerializerMethodField()

    class Meta:
        model = McpActivity
        fields = ('id', 'connector', 'connector_label', 'connection', 'connection_name',
                  'tool_name', 'status', 'duration_ms', 'error_message', 'created_at')
        read_only_fields = fields

    def get_connector_label(self, row) -> str:
        connector = registry.get(row.connector)
        return connector.label if connector is not None else row.connector

    def get_connection_name(self, row):
        # Relies on the view's select_related('connection'): without it this
        # method is one query per row.
        return row.connection.name if row.connection_id else None


class ActivityQuerySerializer(serializers.Serializer):
    """?limit= for the feed.

    Out of range clamps instead of failing: a client asking for more rows than
    we serve should get the maximum, not a broken dashboard. A non-numeric
    value is a real client bug and is allowed to 400.
    """

    limit = serializers.IntegerField(required=False, default=DEFAULT_ACTIVITY_LIMIT)

    def validate_limit(self, value) -> int:
        return max(1, min(value, MAX_ACTIVITY_LIMIT))


class ActivitySummaryQuerySerializer(serializers.Serializer):
    """?days= for the sparkline window, clamped for the same reason as limit."""

    days = serializers.IntegerField(required=False, default=DEFAULT_SUMMARY_DAYS)

    def validate_days(self, value) -> int:
        return max(1, min(value, MAX_SUMMARY_DAYS))
