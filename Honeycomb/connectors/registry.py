"""Connector registry — every connector registers its metadata + tools here.

A connector is a thin module that calls `register()` with:
  - slug:        URL/identifier, e.g. 'ga4'
  - label:       human name
  - auth:        'api_key' (user pastes credentials) or 'google_oauth' (OAuth dance)
  - scopes:      Google OAuth scopes (google_oauth only)
  - catalog:     {tool_name: {'description', 'input', 'write'?}}
  - handlers:    {tool_name: async (conn, db, args) -> dict}
  - cred_fields: for api_key connectors, the credential keys the UI should collect

The generic MCP endpoint + the control-plane API are driven entirely off this
registry, so adding a connector never touches routing code.
"""
from dataclasses import dataclass, field
from typing import Awaitable, Callable

Handler = Callable[..., Awaitable[dict]]


@dataclass
class Connector:
    slug: str
    label: str
    auth: str  # 'api_key' | 'google_oauth'
    catalog: dict[str, dict]
    handlers: dict[str, Handler]
    scopes: list[str] = field(default_factory=list)
    cred_fields: list[str] = field(default_factory=list)
    # Non-secret resource identifiers collected AFTER an OAuth connect
    # (e.g. ga4 property_id, gsc site_url, merchant_id, ads customer_id).
    # Stored in the connection's creds and editable via the settings endpoint.
    setup_fields: list[str] = field(default_factory=list)
    write_tools: tuple[str, ...] = ()
    # Marketplace copy. Kept on the connector rather than in a parallel table so
    # a single file is the whole truth about a connector.
    description: str = ''
    category: str = ''


REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> None:
    """Add ``connector`` to the process-wide registry, deriving its write tools.

    ``write_tools`` is computed here rather than hand-maintained by each
    connector so the catalog entry stays the single place a tool is declared
    mutating — the two can never drift apart.
    """
    write = tuple(n for n, e in connector.catalog.items() if e.get('write'))
    connector.write_tools = write
    REGISTRY[connector.slug] = connector


def get(slug: str) -> Connector | None:
    return REGISTRY.get(slug)


def all_connectors() -> list[Connector]:
    return list(REGISTRY.values())


def tool_enabled(connection, name: str, connector: Connector | None = None) -> bool:
    """Whether ``connection`` may run tool ``name``. The single rule, used by the
    MCP endpoint, the dashboard's tool list and the portal runner alike.

    Read tools are on unless switched off (disabled_tools is a deny-list, so a
    read tool a connector gains later appears everywhere). Write tools are off
    unless switched on: they must also be listed in enabled_write_tools, so a
    write tool shipped in a later release starts off on every existing
    connection instead of quietly arriving switched on.
    """
    if name in (connection.disabled_tools or []):
        return False
    connector = connector or get(connection.connector)
    entry = connector.catalog.get(name) if connector is not None else None
    if entry is not None and entry.get('write'):
        return name in (getattr(connection, 'enabled_write_tools', None) or [])
    return True
