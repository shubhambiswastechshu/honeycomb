"""Connector definitions and the runtime shims they are allowed to import.

This app deliberately has no models. A connector is pure metadata plus async
handler functions: everything persistent (the credentials, the per-tenant
connection rows, the MCP keys, the activity log) belongs to the ``connections``
and ``mcp`` apps. Keeping this app model-free means a connector module can never
grow its own table, so dropping a new file into ``connectors/catalog/`` can never
require a migration.
"""
