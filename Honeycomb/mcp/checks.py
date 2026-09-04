"""A system check for the one template mistake that has now shipped twice.

Django's `{# ... #}` comment is matched by a regex with no DOTALL, so it only
works when the whole comment sits on ONE line. Spread it over two and it stops
being a comment: the text renders into the page. It has no error, no warning and
no visual clue in an editor -- the first sighting was a paragraph of prose down
the left edge of every admin page, the second was a note about POST parameters
sitting in the middle of the OAuth consent screen, where users saw it.

`manage.py check` runs before every management command, so this fires locally,
in the Docker build's collectstatic, and at container start before migrate.

Registered as an ERROR rather than a warning on purpose. It is precise -- an
opening `{#` with no `#}` on the same line is never intentional -- and the fix
is to write `{% comment %}`, which has no such limit.
"""
import io
import os

from django.conf import settings
from django.core.checks import Error


def _template_dirs():
    """Every directory Django would load a template from, app dirs included."""
    seen = []
    for engine in getattr(settings, 'TEMPLATES', []):
        for directory in engine.get('DIRS', []):
            seen.append(str(directory))
        if engine.get('APP_DIRS'):
            from django.apps import apps
            for config in apps.get_app_configs():
                candidate = os.path.join(config.path, 'templates')
                if os.path.isdir(candidate):
                    seen.append(candidate)
    return seen


def check_template_comments(app_configs, **kwargs):
    errors = []
    for root in _template_dirs():
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(('.html', '.txt')):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    with io.open(path, encoding='utf-8') as handle:
                        lines = handle.readlines()
                except (OSError, UnicodeDecodeError):
                    continue
                for number, line in enumerate(lines, 1):
                    if '{#' in line and '#}' not in line.split('{#', 1)[1]:
                        errors.append(Error(
                            'Multi-line {# #} comment renders as visible text.',
                            hint=('Django only strips {# #} when it is on one line. '
                                  'Use {% comment %} ... {% endcomment %} instead.'),
                            obj='{0}:{1}'.format(path, number),
                            id='mcp.E001',
                        ))
    return errors
