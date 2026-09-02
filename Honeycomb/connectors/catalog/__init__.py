"""Connector modules. Every module here self-registers on import.

``ConnectorsConfig.ready()`` walks this package with pkgutil and imports each
module, so installing a connector is dropping a file in — there is no list to
update. A module whose name starts with an underscore is treated as a shared
helper and skipped.
"""
