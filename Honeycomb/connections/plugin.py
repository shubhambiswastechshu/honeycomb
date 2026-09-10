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

from django.http import FileResponse, Http404

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


def plugin_version():
    """The version from the plugin header, so nothing here can claim a stale one."""
    php = PLUGIN_DIR / 'falcon-seo.php'
    if not php.exists():
        return ''
    # Only the header block: the constant further down says the same thing, and
    # reading 4,000 lines to find it would be silly.
    head = php.read_text(encoding='utf-8', errors='replace')[:2000]
    found = re.search(r'^\s*\*\s*Version:\s*(\S+)', head, re.MULTILINE)
    return found.group(1) if found else ''


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
