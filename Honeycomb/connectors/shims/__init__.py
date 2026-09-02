"""The only project modules a ported connector is allowed to import.

Each shim keeps the async signature the falcon originals had but re-homes the
implementation on Django infrastructure (``django.core.cache`` instead of a raw
Redis client, project settings instead of falcon's config object). Connector
modules therefore port across unchanged, and any future move off Django is a
change to four files rather than to every connector.
"""
