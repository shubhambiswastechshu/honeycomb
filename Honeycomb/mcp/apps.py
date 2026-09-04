from django.apps import AppConfig
from django.core.checks import register


class McpConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mcp'
    verbose_name = 'MCP'

    def ready(self):
        # See mcp/checks.py: a multi-line {# #} is not a comment, it is text on
        # the page, and it has shipped twice. manage.py runs system checks
        # before every command, so this catches it at build and at boot.
        from .checks import check_template_comments
        register(check_template_comments)
