"""No admin registrations: this app owns no models, by design.

The file exists so ``django.contrib.admin``'s autodiscovery finds something
importable and nobody wonders whether it was forgotten. What an operator would
want to inspect — connections, keys, activity — lives in the ``connections`` and
``mcp`` apps and is registered there.
"""
