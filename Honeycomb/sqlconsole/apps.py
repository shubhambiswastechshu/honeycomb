from django.apps import AppConfig


class SqlConsoleConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sqlconsole'
    verbose_name = 'SQL'
