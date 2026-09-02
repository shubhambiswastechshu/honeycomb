"""App config whose ready() populates the connector registry.

Every module under ``connectors/catalog/`` self-registers on import, so the
registry is built by importing the package's contents rather than by keeping a
hand-maintained list somewhere else — dropping a file in is the whole install
step. Discovery uses pkgutil rather than a literal list (falcon's loader kept a
list and it drifted) so a new connector cannot be forgotten.

Import errors are logged and swallowed per module: a connector is third-party-
shaped code, and one typo or one missing optional dependency must not take the
whole site down with it. The failed connector simply does not appear in the
marketplace; everything else boots.
"""
import importlib
import logging
import pkgutil

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class ConnectorsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'connectors'
    verbose_name = 'Connectors'

    def ready(self):
        self.load_catalog()

    @staticmethod
    def load_catalog() -> list[str]:
        """Import every module in ``connectors.catalog``; return the slugs loaded."""
        from connectors import catalog

        loaded: list[str] = []
        for module in pkgutil.iter_modules(catalog.__path__):
            # A leading underscore marks a shared helper module, not a connector.
            if module.name.startswith('_'):
                continue
            try:
                importlib.import_module(f'{catalog.__name__}.{module.name}')
            except Exception as exc:  # noqa: BLE001 - one bad connector must not stop boot
                logger.error('Failed to load connector %s: %s', module.name, exc)
                continue
            loaded.append(module.name)
        return loaded
