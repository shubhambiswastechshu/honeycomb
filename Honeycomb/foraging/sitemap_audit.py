"""Sitemap audit: what the sitemap claims, against what the crawl found.

Three questions people actually ask, and none of them can be answered by the
crawl alone:

* Which URLs are in the sitemap but were never crawled? Those are pages the
  site is asking Google to index that its own links do not reach.
* Which crawled, indexable pages are missing from the sitemap?
* Which sitemap URLs are broken, redirected, canonicalised elsewhere or
  noindexed? A sitemap is a set of promises, and those four are broken ones.

The sitemap is fetched here, now, rather than read out of the crawl, because a
URL the crawler skipped leaves no row to read. That means this is a live view:
it can disagree with a week-old crawl, and when it does, the sitemap is the
half that changed.

Bounded on every axis -- files, URLs, bytes and wall clock -- because this runs
inside a web request on a public page. Cached per crawl, so re-opening the tab
costs nothing.
"""
import gzip
import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx
from django.core.cache import cache

from .models import CrawlPage

logger = logging.getLogger(__name__)

#: Bounds. A sitemap index may legitimately point at fifty files; twenty is
#: enough to describe any site this crawler is allowed to crawl.
MAX_FILES = 20
MAX_URLS = 50_000
MAX_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT = 8.0
#: Total wall clock for the whole audit, whatever it has managed by then.
TIME_BUDGET = 20.0
CACHE_TTL = 3600
#: How many URLs each list in the answer carries. The counts are exact; the
#: lists are a sample to act on.
LIST_LIMIT = 200

#: The two things worth pulling out of a sitemap, without an XML parser: no
#: parser means no entity expansion and no external entities to worry about.
LOC_RE = re.compile(r'<loc>\s*([^<\s][^<]*?)\s*</loc>', re.IGNORECASE)
SITEMAP_INDEX_RE = re.compile(r'<sitemapindex', re.IGNORECASE)
ROBOTS_SITEMAP_RE = re.compile(r'^\s*sitemap:\s*(\S+)', re.IGNORECASE | re.MULTILINE)

USER_AGENT = 'Mozilla/5.0 (compatible; Honeycomb-Sitemap/1.0; +https://honeycomb.a.techshu.in)'


def _is_public(host):
    """Whether a hostname resolves only to globally routable addresses.

    The same check public.py makes before a crawl starts, repeated because
    this fetches from inside the server rather than from the worker: DNS can
    change between the two, and a name that now points at 10.0.0.1 must not
    turn this endpoint into a way to read the private network.
    """
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return False
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ipaddress.ip_address(address.split('%', 1)[0]).is_global:
                return False
        except ValueError:
            return False
    return True


def _normalise(url):
    """Drop the fragment and trailing whitespace so both sides compare equal."""
    url = str(url or '').strip()
    if not url:
        return ''
    return url.split('#', 1)[0]


def _fetch(client, url):
    """(text, error) for one sitemap file, gunzipped when it needs to be."""
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return None, '{0}: {1}'.format(type(exc).__name__, str(exc)[:120])
    if response.status_code >= 400:
        return None, 'HTTP {0}'.format(response.status_code)
    body = response.content[:MAX_BYTES]
    if url.endswith('.gz') or body[:2] == b'\x1f\x8b':
        try:
            body = gzip.decompress(body)[:MAX_BYTES]
        except (OSError, EOFError):
            return None, 'Could not decompress this file.'
    return body.decode('utf-8', errors='replace'), ''


def collect(seed_url, client=None):
    """Every URL the site's sitemaps list, plus which files were read.

    Starts at robots.txt -- the only place a site can *tell* you where its
    sitemaps are -- and falls back to the two conventional paths.
    """
    parts = urlsplit(seed_url)
    origin = '{0}://{1}'.format(parts.scheme, parts.netloc)
    if not _is_public(parts.hostname or ''):
        return {'urls': [], 'files': [], 'errors': ['That host could not be resolved to a public address.']}

    owned = client is None
    if owned:
        client = httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True,
                              headers={'User-Agent': USER_AGENT})
    started = time.monotonic()
    files, errors, urls = [], [], []
    seen_files, seen_urls = set(), set()

    try:
        robots, robots_error = _fetch(client, origin + '/robots.txt')
        queue = []
        if robots:
            queue.extend(ROBOTS_SITEMAP_RE.findall(robots))
        elif robots_error:
            errors.append('robots.txt: {0}'.format(robots_error))
        queue.extend([origin + path for path in ('/sitemap.xml', '/sitemap_index.xml')])

        while queue and len(files) < MAX_FILES and len(urls) < MAX_URLS:
            if time.monotonic() - started > TIME_BUDGET:
                errors.append('Stopped after {0:.0f} seconds; the sitemap is unusually large.'.format(TIME_BUDGET))
                break
            target = _normalise(urljoin(origin + '/', queue.pop(0)))
            if not target or target in seen_files:
                continue
            seen_files.add(target)
            # Same site only: a sitemap may not speak for another host, and
            # following one off-site would make this a fetch-anything endpoint.
            if urlsplit(target).hostname != parts.hostname:
                errors.append('Ignored a sitemap on another host: {0}'.format(target[:120]))
                continue

            body, error = _fetch(client, target)
            if body is None:
                # The conventional paths are guesses; only say so when the site
                # itself pointed at the file.
                if target not in (origin + '/sitemap.xml', origin + '/sitemap_index.xml'):
                    errors.append('{0}: {1}'.format(target, error))
                continue

            found = [_normalise(loc) for loc in LOC_RE.findall(body)]
            is_index = bool(SITEMAP_INDEX_RE.search(body))
            files.append({'url': target, 'kind': 'index' if is_index else 'urlset',
                          'entries': len(found)})
            if is_index:
                queue.extend(found)
                continue
            for loc in found:
                if loc and loc not in seen_urls and len(urls) < MAX_URLS:
                    seen_urls.add(loc)
                    urls.append(loc)
    finally:
        if owned:
            client.close()

    return {'urls': urls, 'files': files, 'errors': errors}


def audit(job, refresh=False):
    """Compare a crawl with its site's live sitemaps."""
    key = 'sitemap-audit:{0}'.format(job.id)
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    found = collect(job.seed_url)
    listed = found['urls']
    listed_set = set(listed)

    crawled = {}
    rows = (CrawlPage.objects.filter(job=job)
            .values_list('url', 'status_code', 'indexability', 'indexability_status',
                         'final_url', 'content_type')[:MAX_URLS])
    for url, status, indexability, reason, final_url, content_type in rows:
        crawled[_normalise(url)] = {
            'status_code': status,
            'indexability': indexability,
            'reason': reason,
            'final_url': final_url,
            'html': 'html' in (content_type or ''),
        }

    missing_from_crawl = [url for url in listed if url not in crawled]
    problems = []
    for url in listed:
        page = crawled.get(url)
        if page is None:
            continue
        status = page['status_code']
        if status is None or status >= 400:
            problems.append({'url': url, 'problem': 'Broken', 'detail': status or 'No response'})
        elif 300 <= status < 400:
            problems.append({'url': url, 'problem': 'Redirects',
                             'detail': page['final_url'] or status})
        elif page['indexability'] and page['indexability'].lower() != 'indexable':
            problems.append({'url': url, 'problem': page['reason'] or 'Non-indexable',
                             'detail': status})

    # Worth listing in a sitemap: a page that returned 200, is indexable, and is
    # a page rather than a PDF or an image.
    missing_from_sitemap = [
        url for url, page in crawled.items()
        if url not in listed_set
        and page['status_code'] == 200
        and (page['indexability'] or '').lower() == 'indexable'
        and page['html']
    ]

    result = {
        'checked_at': time.time(),
        'seed_url': job.seed_url,
        'files': found['files'],
        'errors': found['errors'],
        'truncated': len(listed) >= MAX_URLS,
        'counts': {
            'in_sitemap': len(listed),
            'crawled': len(crawled),
            'in_both': len(listed_set & set(crawled)),
            'missing_from_crawl': len(missing_from_crawl),
            'missing_from_sitemap': len(missing_from_sitemap),
            'problems': len(problems),
        },
        'missing_from_crawl': sorted(missing_from_crawl)[:LIST_LIMIT],
        'missing_from_sitemap': sorted(missing_from_sitemap)[:LIST_LIMIT],
        'problems': problems[:LIST_LIMIT],
        'list_limit': LIST_LIMIT,
    }
    cache.set(key, result, CACHE_TTL)
    return result
