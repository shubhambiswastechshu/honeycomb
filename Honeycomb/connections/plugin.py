"""Serve the TechShu SEO Bridge plugin as a downloadable zip.

Connecting WordPress asks for a site URL and a token "from the TechShu SEO
Bridge plugin settings" -- and the product offered no way to obtain the plugin.
The source lived in a different repository, so the first step of the flow was a
dead end unless you already knew where to look.

The zip is built from `wordpress_plugin/falcon-seo/` on demand rather than
committed as a binary. A checked-in archive is unreviewable in a diff and drifts
from the code beside it the first time either is edited alone; building it here
means the bytes a user installs are exactly the bytes in this tree.

Deliberately unauthenticated. The plugin is GPLv2 and headed for the
WordPress.org directory, so there is nothing here to protect -- and gating it on
a session would mean a download that silently fails whenever the cookie does not
travel, which is precisely when someone is mid-setup and least able to diagnose
it.
"""
import io
import re
import zipfile
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.urls import reverse

#: Where the vendored plugin lives, and the folder name it unpacks into. The
#: directory name is the plugin's slug on disk, so WordPress activates it at
#: falcon-seo/falcon-seo.php.
PLUGIN_DIR = Path(__file__).resolve().parent.parent / 'wordpress_plugin' / 'falcon-seo'
PLUGIN_SLUG = 'falcon-seo'

#: Files that make up the distributed plugin. An explicit list rather than a
#: glob: a stray .bak or an editor swapfile in that directory must not end up
#: inside somebody's wp-content/plugins.
PLUGIN_FILES = ('falcon-seo.php', 'readme.txt')

#: Built once per process. The archive is ~70KB compressed and identical for
#: every caller, so rebuilding it per request is pure waste.
_CACHE = {}


def _header(field, default=''):
    """Read one ``Name: value`` line from the plugin's header block.

    The header is the single source of truth for the plugin's own metadata --
    the same block WordPress reads when it lists the plugin -- so the update
    manifest quotes it rather than restating a version that could drift.
    """
    php = PLUGIN_DIR / 'falcon-seo.php'
    if not php.exists():
        return default
    head = php.read_text(encoding='utf-8', errors='replace')[:2000]
    found = re.search(r'^\s*\*\s*{0}:\s*(.+?)\s*$'.format(re.escape(field)), head, re.MULTILINE)
    return found.group(1) if found else default


def plugin_version():
    """The version from the plugin header, so nothing here can claim a stale one."""
    return _header('Version')


def _readme_field(field, default=''):
    """One ``Field: value`` line from the readme header (e.g. Tested up to)."""
    readme = PLUGIN_DIR / 'readme.txt'
    if not readme.exists():
        return default
    text = readme.read_text(encoding='utf-8', errors='replace')[:2000]
    found = re.search(r'^{0}:\s*(.+?)\s*$'.format(re.escape(field)), text, re.MULTILINE)
    return found.group(1) if found else default


def _changelog_html():
    """The readme's Changelog section as the HTML the "View details" popup wants.

    A light conversion of the readme's ``= x.y.z =`` / ``* item`` shorthand: the
    version lines become headings and the bullets become a list, so the person
    deciding whether to update sees what changed without leaving WP-admin.
    """
    readme = PLUGIN_DIR / 'readme.txt'
    if not readme.exists():
        return ''
    text = readme.read_text(encoding='utf-8', errors='replace')
    section = re.search(r'==\s*Changelog\s*==\s*(.*?)(?:\n==\s|\Z)', text, re.DOTALL)
    if not section:
        return ''
    html, in_list = [], False
    for line in section.group(1).splitlines():
        line = line.strip()
        version = re.match(r'^=\s*(.+?)\s*=$', line)
        if version:
            if in_list:
                html.append('</ul>')
                in_list = False
            html.append('<h4>{0}</h4>'.format(_esc(version.group(1))))
        elif line.startswith('*'):
            if not in_list:
                html.append('<ul>')
                in_list = True
            html.append('<li>{0}</li>'.format(_esc(line.lstrip('* ').strip())))
    if in_list:
        html.append('</ul>')
    return ''.join(html)


def _esc(text):
    return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _public_base(request):
    """This API's public origin, for the absolute URLs the manifest must carry.

    Prefers the configured public base -- what an installed plugin was told to
    talk to -- and falls back to the request's own origin so a manifest fetched
    directly still points at reachable URLs.
    """
    base = (getattr(settings, 'HONEYCOMB_PUBLIC_BASE', '') or '').rstrip('/')
    if base:
        return base
    return '{0}://{1}'.format(request.scheme, request.get_host())


def _archive():
    version = plugin_version()
    cached = _CACHE.get('zip')
    if cached is not None and _CACHE.get('version') == version:
        return cached, version

    missing = [name for name in PLUGIN_FILES if not (PLUGIN_DIR / name).exists()]
    if missing:
        raise Http404('Plugin source is incomplete: ' + ', '.join(missing))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in PLUGIN_FILES:
            # A fixed date_time so the same source always produces byte-identical
            # output -- a zip whose checksum changes every build is one nobody
            # can verify they already have.
            info = zipfile.ZipInfo('{0}/{1}'.format(PLUGIN_SLUG, name), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (PLUGIN_DIR / name).read_bytes())

    data = buffer.getvalue()
    _CACHE['zip'] = data
    _CACHE['version'] = version
    return data, version


def download(request):
    """GET the plugin zip, named with its version."""
    data, version = _archive()
    name = 'techshu-seo-bridge{0}.zip'.format('-' + version if version else '')
    response = FileResponse(io.BytesIO(data), as_attachment=True, filename=name)
    response['Content-Length'] = str(len(data))
    # The bytes only change when the source in this repo changes, and that
    # arrives with a deploy -- but a stale plugin is a support problem, so this
    # is short enough that a fix is picked up the same day.
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def update_manifest(request):
    """The version check an installed plugin polls to keep itself up to date.

    The plugin has no update mechanism of its own until it carries the updater
    added in 2.2.0, and it is not on WordPress.org, so this is its update
    server: it answers "what is the current version, and where is the zip", and
    the plugin compares that against its own header to decide whether to offer
    the update. Everything here is derived from the same source the zip is built
    from, so the manifest can never advertise a version the download would not
    deliver.

    Unauthenticated for the same reason the download is: the plugin is GPL and
    public, a version number is not a secret, and gating it on anything would
    turn a routine background check into a silent failure on some sites.
    """
    version = plugin_version()
    base = _public_base(request)
    download_url = base + reverse('connections:plugin-wordpress')
    manifest = {
        'name': _header('Plugin Name', 'TechShu SEO Bridge'),
        'slug': PLUGIN_SLUG,
        'plugin': '{0}/{0}.php'.format(PLUGIN_SLUG),
        'version': version,
        'author': _header('Author', 'TechShu'),
        'homepage': _header('Plugin URI', ''),
        'requires': _header('Requires at least', '5.6'),
        'requires_php': _header('Requires PHP', '7.4'),
        'tested': _readme_field('Tested up to', ''),
        'download_url': download_url,
        # WordPress reads both keys in different places; send the same URL for
        # each so neither the list-table update nor the details popup is blank.
        'package': download_url,
        'sections': {
            'changelog': _changelog_html(),
        },
    }
    response = JsonResponse(manifest)
    # Short, matching the zip: a site polling this must see a new release the
    # same day it deploys, not up to a stale cache later.
    response['Cache-Control'] = 'public, max-age=3600'
    response['Access-Control-Allow-Origin'] = '*'
    return response
