"""The crawler workspace: Screaming Frog's tabs, filters, issues and reports.

Everything here reads one public crawl's rows. It adds no data the worker does
not already send -- it turns the columns on each CrawlPage, and the link graph
in CrawlLink, into the views a technical SEO expects: a tab per concern, filters
with live counts, an issues list by priority with what each one means and how
to fix it, a per-URL detail pane (search-result preview, inlinks, outlinks,
images, response headers, what JavaScript changed), the site as a folder tree,
the report catalogue, an XML sitemap and a crawl-to-crawl comparison.

Scoped exactly like public.py: only crawls started from the public page, only
for the organization configured as HONEYCOMB_PUBLIC_CRAWL_TENANT, no login, and
under the public read rate limit. A private crawl's id returns 404 here.

What is deliberately not here, because Honeycomb does not store it: spelling
and grammar, image file sizes, and third-party data (PageSpeed, Analytics,
Search Console).

The thresholds come from the Forager connector (connectors/catalog/forager.py)
rather than being restated, so the workspace and the AI-client reports can never
disagree about what "a long title" means.
"""
import csv
import hashlib
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from django.db.models import Count, F, Max, OuterRef, Q, Subquery
from django.db.models.fields.json import KT
from django.db.models.functions import Length
from django.http import HttpResponse
from rest_framework import status as http
from rest_framework.response import Response

from connectors.catalog import forager as fg

from . import sitemap_audit
from .models import CrawlLink, CrawlPage
from .public import PublicView, _job_payload, _public_job, _queue_position

#: Screaming Frog's defaults for the checks the connector has no constant for.
META_MIN_CHARS = 70
URL_MAX_CHARS = 115
ALT_MAX_CHARS = 100
LARGE_PAGE_BYTES = 1_000_000
DEEP_DEPTH = 4
MANY_OUTLINKS = 100

GRID_MAX_LIMIT = 200
REPORT_MAX_LIMIT = 500
COMPARE_LIST_LIMIT = 200
LINKS_MAX_LIMIT = 200

#: Site tree bounds: enough to read any real site's structure, small enough to
#: send in one response.
TREE_MAX_URLS = 50_000
TREE_MAX_DEPTH = 6
TREE_MAX_CHILDREN = 100

RESPONSE_BUCKETS = (
    ('Under 250 ms', Q(response_time_ms__lt=250)),
    ('250 – 500 ms', Q(response_time_ms__gte=250, response_time_ms__lt=500)),
    ('500 ms – 1 s', Q(response_time_ms__gte=500, response_time_ms__lt=1000)),
    ('1 – 2 s', Q(response_time_ms__gte=1000, response_time_ms__lt=2000)),
    ('2 s and over', Q(response_time_ms__gte=2000)),
)


# --------------------------------------------------------------------------- #
# Issues: code -> label, severity, why it matters, how to fix it
# --------------------------------------------------------------------------- #
# Severities mirror the crawler's own SEVERITY map (crawler/forager/parse/
# onpage.py), so an issue reads the same priority in the worker's CLI and here.
ISSUES = {
    'title_missing': (
        'Page title is missing', 'high',
        'The title is the headline of the search result. Without one, Google '
        'writes its own from the page, usually badly.',
        'Add a unique <title> in the <head> that says what this page is, '
        'about 50–60 characters.'),
    'title_multiple': (
        'More than one page title', 'high',
        'Browsers and search engines use only one title, and which one they '
        'pick is not something you control.',
        'Keep exactly one <title>. This usually comes from a theme and an SEO '
        'plugin both printing one.'),
    'title_over_length': (
        'Page title over {0} px (truncated in search)'.format(fg.TITLE_MAX_PX), 'medium',
        'Google cuts titles at about {0} pixels, so the end of this one is '
        'replaced by "…" in results.'.format(fg.TITLE_MAX_PX),
        'Shorten the title, and put the words that matter most at the start.'),
    'title_below_length': (
        'Page title under {0} characters'.format(fg.TITLE_MIN_CHARS), 'low',
        'A very short title wastes the most visible line of the search result.',
        'Describe the page more fully: its topic plus a detail such as a '
        'place, product type or brand.'),
    'meta_description_missing': (
        'Meta description is missing', 'medium',
        'The description is the grey text under a search result. Without one, '
        'Google picks a snippet from the page.',
        'Add a <meta name="description"> summarising the page in 120–155 '
        'characters, written to earn the click.'),
    'meta_description_multiple': (
        'More than one meta description', 'medium',
        'Search engines use only one description, and conflicting ones make '
        'the snippet unpredictable.',
        'Keep one meta description. Check for a theme and a plugin both '
        'adding it.'),
    'meta_description_over_length': (
        'Meta description over {0} px'.format(fg.META_MAX_PX), 'low',
        'Long descriptions are cut off in results, so the end of the message '
        'is never seen.',
        'Trim it to about 155 characters and lead with the main point.'),
    'meta_description_below_length': (
        'Meta description too short', 'low',
        'A short description leaves most of the snippet space unused, and '
        'Google often replaces it.',
        'Expand it to 120–155 characters with what the visitor gets here.'),
    'h1_missing': (
        'H1 is missing', 'medium',
        'The H1 is the visible headline of the page and a strong signal of its '
        'topic, for readers and for search engines.',
        'Add one <h1> near the top of the content that matches the page topic.'),
    'h1_multiple': (
        'More than one H1', 'low',
        'Several H1s blur which heading is the page\'s main subject.',
        'Keep one H1 for the main headline and turn the rest into H2s.'),
    'h1_over_length': (
        'H1 over {0} characters'.format(fg.H1_MAX_CHARS), 'low',
        'A very long headline is hard to scan and dilutes the main topic.',
        'Shorten the H1 to a clear headline and move detail into the text.'),
    'h2_missing': (
        'H2 is missing', 'low',
        'Subheadings break a page into sections readers can scan and search '
        'engines can understand.',
        'Structure longer content with H2 subheadings.'),
    'canonical_missing': (
        'Canonical is missing', 'medium',
        'Without a canonical, copies of this page (with tracking parameters, '
        'different sorting, http vs https) compete with each other.',
        'Add <link rel="canonical" href="…"> pointing at the preferred URL, '
        'usually the page itself.'),
    'meta_refresh_present': (
        'Uses a meta refresh', 'medium',
        'A meta refresh is a slow, client-side redirect that passes signals '
        'less reliably than a server redirect.',
        'Replace it with a 301 redirect on the server.'),
    'images_missing_alt': (
        'Images missing alt text', 'medium',
        'Alt text is what screen readers announce and what search engines use '
        'to understand an image.',
        'Add a short alt description to meaningful images, and alt="" to '
        'purely decorative ones.'),
    'thin_content': (
        'Low content (under {0} words)'.format(fg.LOW_CONTENT_WORDS), 'medium',
        'Pages with very little text rarely rank, and many of them can drag a '
        'site\'s quality down.',
        'Add useful, original content, merge it with a related page, or keep '
        'it out of the index if it is not meant to rank.'),
    'low_text_ratio': (
        'Low text-to-HTML ratio', 'low',
        'Most of this page is markup, scripts or styles rather than readable '
        'text, which often means slow and thin.',
        'Remove unused code and inline scripts, and make sure the main content '
        'is in the HTML.'),
    'lang_missing': (
        'Missing lang attribute', 'low',
        'Without <html lang>, screen readers and translation tools have to '
        'guess the language.',
        'Add the page language to the html tag, e.g. <html lang="en">.'),
    'hreflang_invalid_code': (
        'Hreflang: invalid language code', 'medium',
        'Search engines ignore hreflang annotations with codes they do not '
        'recognise.',
        'Use ISO 639-1 language codes, optionally with an ISO 3166-1 region '
        '(e.g. en-GB).'),
    'hreflang_missing_return_link': (
        'Hreflang: missing return link', 'medium',
        'Hreflang must be confirmed from both sides. A one-way annotation is '
        'ignored.',
        'Make every alternate page link back to this one with its own hreflang.'),
    'hreflang_missing_x_default': (
        'Hreflang: missing x-default', 'low',
        'Visitors whose language matches no version get no guided fallback.',
        'Add an x-default alternate, usually the language chooser or main page.'),
    'spelling_errors': (
        'Spelling errors', 'low',
        'Typos cost credibility with readers.',
        'Proofread the page copy.'),
    'grammar_errors': (
        'Grammar errors', 'low',
        'Awkward or incorrect sentences cost credibility with readers.',
        'Proofread the page copy.'),
}

#: Why a page is not indexable, by the reason the crawler records.
NON_INDEXABLE_HELP = {
    'redirected': (
        'This URL redirects somewhere else, so the destination is what gets '
        'indexed.',
        'Update internal links to point straight at the final URL so visitors '
        'and crawlers skip the redirect.'),
    'canonicalised': (
        'This page names a different URL as its canonical, asking search '
        'engines to index that one instead.',
        'If this page should rank, make its canonical point to itself. If not, '
        'link to the canonical URL instead.'),
    'noindex': (
        'A noindex directive tells search engines to keep this page out of '
        'results.',
        'Remove the noindex (meta robots or X-Robots-Tag) if the page should '
        'appear in search.'),
    'client_error': (
        'This URL returns a 4xx error, so there is nothing to index and '
        'visitors hit a dead end.',
        'Restore the page, 301-redirect it to the closest live page, or remove '
        'the links pointing at it.'),
    'server_error': (
        'The server failed to answer this URL. Repeated errors make search '
        'engines crawl the site less.',
        'Check the server logs for this URL and fix the error.'),
    'no_response': (
        'The server did not respond at all (timeout, DNS or connection '
        'failure).',
        'Check the site is reachable and not blocking crawlers.'),
}

SEVERITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}


def issue_info(code):
    code = str(code)
    if code in ISSUES:
        label, severity, why, fix = ISSUES[code]
        return {'code': code, 'label': label, 'severity': severity, 'why': why, 'fix': fix}
    if code.startswith('non_indexable_'):
        reason = code[len('non_indexable_'):]
        why, fix = NON_INDEXABLE_HELP.get(reason, (
            'Search engines will not index this page as it stands.',
            'Check its status code, robots directives and canonical.'))
        return {'code': code, 'label': 'Non-indexable: ' + reason.replace('_', ' '),
                'severity': 'high', 'why': why, 'fix': fix}
    return {'code': code, 'label': code.replace('_', ' ').capitalize(),
            'severity': 'low', 'why': '', 'fix': ''}


def _issue_labels(codes):
    return [issue_info(c)['label'] for c in (codes or [])]


def _issue_q(code):
    """Pages whose issue list contains `code`.

    A JSON `contains` lookup would be the natural query, and SQLite does not
    support it. Matching the quoted code inside the list's text form works on
    both databases this app runs on, and the quotes stop `h1_missing` from also
    matching some future `h1_missing_x`.
    """
    return Q(issues__icontains='"{0}"'.format(code))


def url_hash(url):
    """The worker's key for a normalized URL (crawler/forager/urls.py url_hash)."""
    return hashlib.blake2b(url.encode('utf-8'), digest_size=16).digest()


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
def col(key, label, kind='text'):
    return {'key': key, 'label': label, 'type': kind}


ADDRESS = col('url', 'Address', 'url')
INDEXABILITY = col('indexability', 'Indexability', 'index')
INDEXABILITY_STATUS = col('indexability_status', 'Indexability status')
ISSUE_COLUMNS = [ADDRESS, col('status_code', 'Status', 'code'), INDEXABILITY,
                 col('title', 'Title 1'), col('issues', 'Issues', 'list')]


def _base(job):
    """The page rows for a crawl, with the computed values some tabs read.

    The header values are pulled out of the JSON column here so the Security
    tab can filter and sort on them like any other column.
    """
    return CrawlPage.objects.filter(job=job).annotate(
        url_len=Length('url'),
        h1_len=Length('h1_1'),
        hsts=KT('response_headers__strict-transport-security'),
        csp=KT('response_headers__content-security-policy'),
        xfo=KT('response_headers__x-frame-options'),
        xcto=KT('response_headers__x-content-type-options'),
        referrer_policy=KT('response_headers__referrer-policy'),
        x_robots=KT('response_headers__x-robots-tag'),
        powered_by=KT('response_headers__x-powered-by'),
    )


def _image_rows(job):
    """One row per image file used on the site, from the link graph."""
    return (
        CrawlLink.objects.filter(job=job, kind='img')
        .values('to_url')
        .annotate(
            pages=Count('from_hash', distinct=True),
            missing_alt=Count('id', filter=Q(anchor__isnull=True)),
            empty_alt=Count('id', filter=Q(anchor='')),
            alt=Max('anchor'),
            alt_len=Max(Length('anchor')),
        )
    )


def _dupes(job, field):
    raw = CrawlPage.objects.filter(job=job)
    if field == 'content_hash':
        raw = raw.exclude(content_hash=None)
    else:
        raw = raw.exclude(**{field: ''})
    return raw.values(field).annotate(n=Count('id')).filter(n__gt=1).values(field)


def tabs_for(job):
    """Every tab: its columns, default sort, and filters as (key, label, Q).

    Built per job because the duplicate filters are subqueries over that job's
    own pages. A tab with 'source': 'images' reads the link graph instead of
    the page rows.
    """
    html = Q(content_type__icontains='html')
    ok = Q(status_code=200)
    with_headers = html & ~Q(response_headers={})
    https = Q(url__startswith='https://')
    return [
        {
            'key': 'internal', 'label': 'Internal', 'sort': 'id',
            'columns': [ADDRESS, col('content_type', 'Content type'),
                        col('status_code', 'Status', 'code'), INDEXABILITY, INDEXABILITY_STATUS,
                        col('title', 'Title 1'), col('word_count', 'Word count', 'int'),
                        col('response_time_ms', 'Response time', 'ms'),
                        col('size_bytes', 'Size', 'bytes'), col('depth', 'Crawl depth', 'int'),
                        col('inlinks', 'Inlinks', 'int'), col('outlinks', 'Outlinks', 'int'),
                        col('link_score', 'Link score', 'float')],
            'filters': [('all', 'All', Q()), ('html', 'HTML', html), ('other', 'Non-HTML', ~html)],
        },
        {
            'key': 'response_codes', 'label': 'Response Codes', 'sort': 'id',
            'columns': [ADDRESS, col('status_code', 'Status code', 'code'), INDEXABILITY_STATUS,
                        col('redirect_count', 'Redirects', 'int'), col('final_url', 'Redirect URL', 'url'),
                        col('final_status_code', 'Final status', 'code'),
                        col('response_time_ms', 'Response time', 'ms'), col('error', 'Error')],
            'filters': [
                ('all', 'All', Q()),
                ('success', 'Success (2xx)', Q(status_code__gte=200, status_code__lt=300)),
                ('redirection', 'Redirection (3xx)', Q(status_code__gte=300, status_code__lt=400)),
                ('client_error', 'Client error (4xx)', Q(status_code__gte=400, status_code__lt=500)),
                ('server_error', 'Server error (5xx)', Q(status_code__gte=500, status_code__lt=600)),
                ('no_response', 'No response', Q(status_code__isnull=True)),
                ('chains', 'Redirect chains', Q(redirect_count__gt=1)),
                ('redirect_to_error', 'Redirects to an error', Q(final_status_code__gte=400)),
            ],
        },
        {
            'key': 'url', 'label': 'URL', 'sort': 'id',
            'columns': [ADDRESS, col('url_len', 'Length', 'int'), col('depth', 'Crawl depth', 'int'),
                        col('status_code', 'Status', 'code')],
            'filters': [
                ('all', 'All', Q()),
                ('over_length', 'Over {0} characters'.format(URL_MAX_CHARS), Q(url_len__gt=URL_MAX_CHARS)),
                ('uppercase', 'Uppercase', Q(url__regex=r'[A-Z]')),
                ('underscores', 'Underscores', Q(url__contains='_')),
                ('parameters', 'Parameters', Q(url__contains='?')),
                ('multiple_slashes', 'Multiple slashes', Q(url__regex=r'^https?://[^/]+.*//')),
                ('non_ascii', 'Non-ASCII characters', Q(url__regex=r'[^\x00-\x7F]')),
            ],
        },
        {
            'key': 'titles', 'label': 'Page Titles', 'sort': 'id',
            'columns': [ADDRESS, col('title', 'Title 1'), col('title_length', 'Length', 'int'),
                        col('title_pixel_width', 'Pixel width', 'int'), INDEXABILITY],
            'filters': [
                ('all', 'All', html),
                ('missing', 'Missing', html & Q(title='')),
                ('duplicate', 'Duplicate', Q(title__in=_dupes(job, 'title'))),
                ('over_px', 'Over {0} pixels'.format(fg.TITLE_MAX_PX), Q(title_pixel_width__gt=fg.TITLE_MAX_PX)),
                ('below_chars', 'Below {0} characters'.format(fg.TITLE_MIN_CHARS),
                 Q(title_length__gt=0, title_length__lt=fg.TITLE_MIN_CHARS)),
                ('same_as_h1', 'Same as H1', ~Q(title='') & Q(title=F('h1_1'))),
                ('multiple', 'Multiple', _issue_q('title_multiple')),
            ],
        },
        {
            'key': 'meta', 'label': 'Meta Description', 'sort': 'id',
            'columns': [ADDRESS, col('meta_description', 'Meta description 1'),
                        col('meta_description_length', 'Length', 'int'),
                        col('meta_description_pixel_width', 'Pixel width', 'int'), INDEXABILITY],
            'filters': [
                ('all', 'All', html),
                ('missing', 'Missing', html & Q(meta_description='')),
                ('duplicate', 'Duplicate', Q(meta_description__in=_dupes(job, 'meta_description'))),
                ('over_px', 'Over {0} pixels'.format(fg.META_MAX_PX),
                 Q(meta_description_pixel_width__gt=fg.META_MAX_PX)),
                ('below_chars', 'Below {0} characters'.format(META_MIN_CHARS),
                 Q(meta_description_length__gt=0, meta_description_length__lt=META_MIN_CHARS)),
                ('multiple', 'Multiple', _issue_q('meta_description_multiple')),
            ],
        },
        {
            'key': 'h1', 'label': 'H1', 'sort': 'id',
            'columns': [ADDRESS, col('h1_1', 'H1-1'), col('h1_len', 'Length', 'int'),
                        col('h1_count', 'Occurrences', 'int'), INDEXABILITY],
            'filters': [
                ('all', 'All', html),
                ('missing', 'Missing', html & Q(h1_count=0)),
                ('duplicate', 'Duplicate', Q(h1_1__in=_dupes(job, 'h1_1'))),
                ('over_chars', 'Over {0} characters'.format(fg.H1_MAX_CHARS), Q(h1_len__gt=fg.H1_MAX_CHARS)),
                ('multiple', 'Multiple', Q(h1_count__gt=1)),
            ],
        },
        {
            'key': 'h2', 'label': 'H2', 'sort': 'id',
            'columns': [ADDRESS, col('h2_count', 'Occurrences', 'int'), INDEXABILITY],
            'filters': [
                ('all', 'All', html),
                ('missing', 'Missing', html & Q(h2_count=0)),
                ('multiple', 'Multiple', Q(h2_count__gt=1)),
            ],
        },
        {
            'key': 'content', 'label': 'Content', 'sort': 'id',
            'columns': [ADDRESS, col('word_count', 'Word count', 'int'),
                        col('text_ratio', 'Text ratio %', 'float'),
                        col('flesch_reading_ease', 'Flesch reading ease', 'float'),
                        col('readability', 'Readability'), col('language', 'Language'),
                        col('near_duplicates', 'Near duplicates', 'int'),
                        col('closest_similarity', 'Closest similarity', 'float')],
            'filters': [
                ('all', 'All', html),
                ('exact_duplicates', 'Exact duplicates', Q(content_hash__in=_dupes(job, 'content_hash'))),
                ('near_duplicates', 'Near duplicates', Q(near_duplicates__gt=0)),
                ('low_content', 'Low content pages', Q(word_count__gt=0, word_count__lt=fg.LOW_CONTENT_WORDS)),
                ('low_text_ratio', 'Low text ratio', Q(text_ratio__gt=0, text_ratio__lt=10)),
                ('lang_missing', 'Missing lang', html & ok & Q(language='')),
            ],
        },
        {
            'key': 'images', 'label': 'Images', 'sort': 'id',
            'columns': [ADDRESS, col('images', 'Images', 'int'),
                        col('images_missing_alt', 'Missing alt text', 'int')],
            'filters': [
                ('all', 'Pages with images', Q(images__gt=0)),
                ('missing_alt', 'Missing alt text', Q(images_missing_alt__gt=0)),
            ],
        },
        {
            'key': 'image_files', 'label': 'Image Files', 'sort': '-pages', 'source': 'images',
            'columns': [ADDRESS, col('alt', 'Alt text'), col('alt_len', 'Alt length', 'int'),
                        col('pages', 'Used on pages', 'int'),
                        col('missing_alt', 'Uses without alt', 'int')],
            'filters': [
                ('all', 'All images', Q()),
                ('missing_alt', 'Missing alt attribute', Q(missing_alt__gt=0)),
                ('empty_alt', 'Empty alt (decorative)', Q(empty_alt__gt=0)),
                ('long_alt', 'Alt over {0} characters'.format(ALT_MAX_CHARS), Q(alt_len__gt=ALT_MAX_CHARS)),
                ('external', 'Hosted on another site', Q(internal=False)),
            ],
        },
        {
            'key': 'canonicals', 'label': 'Canonicals', 'sort': 'id',
            'columns': [ADDRESS, col('canonical', 'Canonical link element', 'url'),
                        INDEXABILITY, INDEXABILITY_STATUS],
            'filters': [
                ('all', 'All', html),
                ('contains', 'Contains canonical', ~Q(canonical='')),
                ('self_referencing', 'Self referencing', Q(canonical=F('url'))),
                ('canonicalised', 'Canonicalised', Q(indexability_status='Canonicalised')),
                ('missing', 'Missing', html & ok & Q(canonical='')),
            ],
        },
        {
            'key': 'directives', 'label': 'Directives', 'sort': 'id',
            'columns': [ADDRESS, col('meta_robots', 'Meta robots'), col('x_robots', 'X-Robots-Tag'),
                        INDEXABILITY, INDEXABILITY_STATUS],
            'filters': [
                ('all', 'All', html),
                ('indexable', 'Indexable', Q(indexability='Indexable')),
                ('non_indexable', 'Non-indexable', Q(indexability='Non-Indexable')),
                ('noindex', 'Noindex', Q(meta_robots__icontains='noindex') | Q(indexability_status='Noindex')),
                ('nofollow', 'Nofollow', Q(meta_robots__icontains='nofollow')),
                ('x_robots', 'X-Robots-Tag header', Q(x_robots__isnull=False)),
                ('refresh', 'Meta refresh', _issue_q('meta_refresh_present')),
            ],
        },
        {
            'key': 'hreflang', 'label': 'Hreflang', 'sort': 'id',
            'columns': [ADDRESS, col('hreflang_count', 'Annotations', 'int'),
                        col('hreflang_issues', 'Issues', 'list')],
            'filters': [
                ('all', 'Contains hreflang', Q(hreflang_count__gt=0)),
                ('invalid_code', 'Invalid language code', _issue_q('hreflang_invalid_code')),
                ('missing_return', 'Missing return links', _issue_q('hreflang_missing_return_link')),
                ('missing_x_default', 'Missing x-default', _issue_q('hreflang_missing_x_default')),
            ],
        },
        {
            'key': 'structured', 'label': 'Structured Data', 'sort': 'id',
            'columns': [ADDRESS, col('structured_data_types', 'Types'),
                        col('structured_data_errors', 'Errors', 'int'),
                        col('structured_data_warnings', 'Warnings', 'int')],
            'filters': [
                ('all', 'Contains structured data', ~Q(structured_data_types='')),
                ('errors', 'Validation errors', Q(structured_data_errors__gt=0)),
                ('warnings', 'Validation warnings', Q(structured_data_warnings__gt=0)),
                ('missing', 'Missing', html & ok & Q(structured_data_types='')),
            ],
        },
        {
            'key': 'javascript', 'label': 'JavaScript', 'sort': '-js_added_words',
            'columns': [ADDRESS, col('js_dependent', 'Changed by JS', 'bool'),
                        col('word_count', 'Raw words', 'int'),
                        col('rendered_word_count', 'Rendered words', 'int'),
                        col('js_added_words', 'Words added by JS', 'int'),
                        col('js_added_links', 'Links added by JS', 'int'),
                        col('js_console_errors', 'Console errors', 'int')],
            'filters': [
                ('all', 'Rendered pages', Q(js_rendered=True)),
                ('dependent', 'Content changed by JavaScript', Q(js_dependent=True)),
                ('links', 'Links only after rendering', Q(js_added_links__gt=0)),
                ('words', 'Text only after rendering', Q(js_added_words__gt=0)),
                ('errors', 'JavaScript console errors', Q(js_console_errors__gt=0)),
            ],
        },
        {
            'key': 'security', 'label': 'Security', 'sort': 'id',
            'columns': [ADDRESS, col('status_code', 'Status', 'code'), col('http_version', 'HTTP'),
                        col('hsts', 'Strict-Transport-Security'),
                        col('csp', 'Content-Security-Policy'), col('xfo', 'X-Frame-Options'),
                        col('xcto', 'X-Content-Type-Options'),
                        col('referrer_policy', 'Referrer-Policy')],
            'filters': [
                ('all', 'All', with_headers),
                ('http', 'Not served over HTTPS', Q(url__startswith='http://')),
                ('no_hsts', 'Missing HSTS', with_headers & https & Q(hsts__isnull=True)),
                ('no_csp', 'Missing Content-Security-Policy', with_headers & Q(csp__isnull=True)),
                ('no_xcto', 'Missing X-Content-Type-Options', with_headers & Q(xcto__isnull=True)),
                ('no_frame', 'Missing clickjacking protection',
                 with_headers & Q(xfo__isnull=True)
                 & (Q(csp__isnull=True) | ~Q(csp__icontains='frame-ancestors'))),
                ('no_referrer', 'Missing Referrer-Policy', with_headers & Q(referrer_policy__isnull=True)),
                ('powered_by', 'Exposes X-Powered-By', Q(powered_by__isnull=False)),
            ],
        },
        {
            'key': 'links', 'label': 'Link Metrics', 'sort': '-link_score',
            'columns': [ADDRESS, col('link_score', 'Link score', 'float'), col('inlinks', 'Inlinks', 'int'),
                        col('outlinks', 'Outlinks', 'int'), col('depth', 'Crawl depth', 'int'), INDEXABILITY],
            'filters': [
                ('all', 'All', Q()),
                ('no_inlinks', 'No internal inlinks', Q(inlinks=0, depth__gt=0)),
                ('deep', 'Crawl depth {0}+'.format(DEEP_DEPTH), Q(depth__gte=DEEP_DEPTH)),
                ('many_outlinks', 'Over {0} outlinks'.format(MANY_OUTLINKS), Q(outlinks__gt=MANY_OUTLINKS)),
            ],
        },
        {
            'key': 'performance', 'label': 'Performance', 'sort': '-response_time_ms',
            'columns': [ADDRESS, col('response_time_ms', 'Response time', 'ms'),
                        col('size_bytes', 'Size', 'bytes'), col('content_type', 'Content type'),
                        col('status_code', 'Status', 'code')],
            'filters': [
                ('all', 'All', Q()),
                ('slow', 'Slow (over {0} ms)'.format(fg.SLOW_MS), Q(response_time_ms__gte=fg.SLOW_MS)),
                ('large', 'Large (over 1 MB)', Q(size_bytes__gte=LARGE_PAGE_BYTES)),
            ],
        },
    ]


def _find_tab(tabs, key):
    for tab in tabs:
        if tab['key'] == key:
            return tab
    return tabs[0]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _count_issues(job):
    """Issue code -> pages, over every page. One pass, no cap."""
    counts = {}
    rows = CrawlPage.objects.filter(job=job).exclude(issues=[]).values_list('issues', flat=True)
    for codes in rows.iterator(chunk_size=2000):
        for code in set(codes or []):
            counts[str(code)] = counts.get(str(code), 0) + 1
    return counts


def _grid_queryset(job, request):
    """(tab, filter_key, issue, queryset) for the grid and its CSV twin."""
    tabs = tabs_for(job)
    tab = _find_tab(tabs, request.GET.get('tab', 'internal'))
    filters = {key: q for key, _label, q in tab['filters']}
    filter_key = request.GET.get('filter', 'all')
    if filter_key not in filters:
        filter_key = tab['filters'][0][0]
    query = (request.GET.get('q') or '').strip()[:200]

    if tab.get('source') == 'images':
        rows = _image_rows(job).filter(filters[filter_key])
        if query:
            rows = rows.filter(to_url__icontains=query)
        return tab, filter_key, '', rows

    rows = _base(job)
    issue = (request.GET.get('issue') or '').strip()[:80]
    if issue:
        # An issue from the sidebar overrides the tab filter: the question being
        # asked is "which pages have this", whatever tab happens to be open.
        rows = rows.filter(_issue_q(issue))
    else:
        rows = rows.filter(filters[filter_key])
    if query:
        rows = rows.filter(url__icontains=query)
    return tab, filter_key, issue, rows


def _columns(tab, issue):
    return ISSUE_COLUMNS if issue else list(tab['columns'])


def _sorted(tab, rows, request, issue=''):
    allowed = {c['key'] for c in _columns(tab, issue)}
    images = tab.get('source') == 'images'
    fallback = tab['sort'] if not issue else 'id'
    raw = (request.GET.get('sort') or fallback).strip()
    key = raw.lstrip('-')
    if key not in allowed and not (key == 'id' and not images):
        raw, key = fallback, fallback.lstrip('-')
    direction = request.GET.get('dir')
    if direction in ('asc', 'desc'):
        descending = direction == 'desc'
    else:
        descending = raw.startswith('-')
    field = 'to_url' if images and key == 'url' else key
    tiebreak = 'to_url' if images else 'id'
    # A stable tiebreak, or rows with equal values shuffle between polls.
    return rows.order_by(('-' if descending else '') + field, tiebreak), key, (
        'desc' if descending else 'asc')


def _values(tab, rows, keys):
    """Rows as dicts with the tab's column keys, whatever the source."""
    if tab.get('source') == 'images':
        fields = ['to_url' if k == 'url' else k for k in keys]
        out = []
        for row in rows.values(*fields):
            row['url'] = row.pop('to_url')
            out.append(row)
        return out
    out = list(rows.values(*keys))
    if 'issues' in keys:
        for row in out:
            row['issues'] = _issue_labels(row['issues'])
    return out


def _neutralise(value):
    """Crawled text is attacker-controlled; stop spreadsheets running it as a formula."""
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value


def _not_found():
    return Response({'detail': 'No such public crawl.'}, status=http.HTTP_404_NOT_FOUND)


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
class PublicWorkspace(PublicView):
    """Everything the sidebar and tab strip need, in one request."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()

        tabs = tabs_for(job)
        aggregates = {}
        for tab in tabs:
            if tab.get('source') == 'images':
                continue
            for key, _label, q in tab['filters']:
                aggregates['{0}__{1}'.format(tab['key'], key)] = Count('id', filter=q)
        for index, (_label, q) in enumerate(RESPONSE_BUCKETS):
            aggregates['rt__{0}'.format(index)] = Count('id', filter=q)
        aggregates['rt__none'] = Count('id', filter=Q(response_time_ms__isnull=True))
        aggregates['pages_total'] = Count('id')
        # One query for every page count on the screen: seventy-odd filters as
        # conditional aggregates, rather than seventy round trips on every poll.
        counts = _base(job).aggregate(**aggregates)
        for tab in tabs:
            if tab.get('source') == 'images':
                for key, _label, q in tab['filters']:
                    counts['{0}__{1}'.format(tab['key'], key)] = _image_rows(job).filter(q).count()

        pages = CrawlPage.objects.filter(job=job)
        issue_counts = _count_issues(job)
        issues = sorted(
            (dict(issue_info(code), pages=n) for code, n in issue_counts.items()),
            key=lambda i: (SEVERITY_ORDER.get(i['severity'], 3), -i['pages'], i['code']),
        )
        totals = {'high': 0, 'medium': 0, 'low': 0}
        for item in issues:
            totals[item['severity']] = totals.get(item['severity'], 0) + 1

        return Response({
            'job': _job_payload(job, _queue_position(self.tenant, job)),
            'pages_total': counts['pages_total'],
            'links_total': CrawlLink.objects.filter(job=job).count(),
            'tabs': [{
                'key': tab['key'],
                'label': tab['label'],
                'filters': [{'key': key, 'label': label,
                             'count': counts['{0}__{1}'.format(tab['key'], key)]}
                            for key, label, _q in tab['filters']],
            } for tab in tabs],
            'issues': issues,
            'issue_totals': totals,
            'structure': [{'depth': r['depth'], 'pages': r['n']} for r in
                          pages.values('depth').annotate(n=Count('id')).order_by('depth')[:50]],
            'response_times': [{'bucket': label, 'pages': counts['rt__{0}'.format(i)]}
                               for i, (label, _q) in enumerate(RESPONSE_BUCKETS)]
                              + [{'bucket': 'No response', 'pages': counts['rt__none']}],
            'status_codes': [{'code': r['status_code'], 'pages': r['n']} for r in
                             pages.values('status_code').annotate(n=Count('id')).order_by('-n')[:20]],
            'indexability': [{'reason': (r['indexability_status'] or r['indexability'] or 'Not analysed yet'),
                              'indexable': r['indexability'] == 'Indexable', 'pages': r['n']}
                             for r in pages.values('indexability', 'indexability_status')
                             .annotate(n=Count('id')).order_by('-n')[:20]],
            'content_types': [{'type': r['content_type'] or 'unknown', 'pages': r['n']} for r in
                              pages.values('content_type').annotate(n=Count('id')).order_by('-n')[:10]],
            'thresholds': {
                'title_max_px': fg.TITLE_MAX_PX, 'title_min_chars': fg.TITLE_MIN_CHARS,
                'meta_max_px': fg.META_MAX_PX, 'meta_min_chars': META_MIN_CHARS,
                'h1_max_chars': fg.H1_MAX_CHARS, 'low_content_words': fg.LOW_CONTENT_WORDS,
                'slow_ms': fg.SLOW_MS, 'url_max_chars': URL_MAX_CHARS,
            },
        })


class PublicGrid(PublicView):
    """One tab's rows under one filter (or one issue), sorted and paged."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()

        tab, filter_key, issue, rows = _grid_queryset(job, request)
        rows, sort_key, direction = _sorted(tab, rows, request, issue)
        try:
            offset = max(0, int(request.GET.get('offset', 0)))
            limit = max(1, min(GRID_MAX_LIMIT, int(request.GET.get('limit', 100))))
        except ValueError:
            offset, limit = 0, 100

        columns = _columns(tab, issue)
        keys = [c['key'] for c in columns]
        total = rows.count()
        return Response({
            'tab': tab['key'],
            'source': tab.get('source', 'pages'),
            'filter': filter_key,
            'issue': issue,
            'issue_label': issue_info(issue)['label'] if issue else '',
            'columns': columns,
            'sort': sort_key,
            'dir': direction,
            'total': total,
            'offset': offset,
            'limit': limit,
            'rows': _values(tab, rows[offset:offset + limit], keys),
        })


class PublicGridExport(PublicView):
    """The grid as it is filtered right now, as CSV."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()

        tab, filter_key, issue, rows = _grid_queryset(job, request)
        rows, _key, _dir = _sorted(tab, rows, request, issue)
        columns = _columns(tab, issue)
        keys = [c['key'] for c in columns]

        host = (urlsplit(job.seed_url).hostname or 'crawl').replace('"', '')
        name = '{0}-{1}-{2}.csv'.format(host, tab['key'], issue or filter_key)
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="{0}"'.format(name)
        writer = csv.writer(response)
        writer.writerow([c['label'] for c in columns])
        start = 0
        while True:
            chunk = _values(tab, rows[start:start + 2000], keys)
            if not chunk:
                break
            for row in chunk:
                writer.writerow([
                    _neutralise(', '.join(map(str, row[k])) if isinstance(row[k], list) else row[k])
                    for k in keys
                ])
            start += 2000
        return response


#: Fields shown in the URL detail pane, in reading order.
DETAIL_FIELDS = (
    ('url', 'Address', 'url'), ('status_code', 'Status code', 'code'),
    ('final_url', 'Redirect URL', 'url'), ('final_status_code', 'Final status', 'code'),
    ('content_type', 'Content type', 'text'), ('http_version', 'HTTP version', 'text'),
    ('indexability', 'Indexability', 'index'),
    ('indexability_status', 'Indexability status', 'text'),
    ('title', 'Title 1', 'text'), ('title_length', 'Title length', 'int'),
    ('title_pixel_width', 'Title pixel width', 'int'),
    ('meta_description', 'Meta description 1', 'text'),
    ('meta_description_length', 'Meta description length', 'int'),
    ('meta_description_pixel_width', 'Meta description pixel width', 'int'),
    ('h1_1', 'H1-1', 'text'), ('h1_count', 'H1 occurrences', 'int'),
    ('h2_count', 'H2 occurrences', 'int'), ('canonical', 'Canonical link element', 'url'),
    ('meta_robots', 'Meta robots', 'text'), ('word_count', 'Word count', 'int'),
    ('text_ratio', 'Text ratio %', 'float'), ('flesch_reading_ease', 'Flesch reading ease', 'float'),
    ('readability', 'Readability', 'text'), ('language', 'Language', 'text'),
    ('response_time_ms', 'Response time', 'ms'), ('size_bytes', 'Size', 'bytes'),
    ('redirect_count', 'Redirects', 'int'),
    ('depth', 'Crawl depth', 'int'), ('discovered_via', 'Discovered via', 'text'),
    ('inlinks', 'Inlinks', 'int'), ('outlinks', 'Outlinks', 'int'),
    ('link_score', 'Link score', 'float'), ('images', 'Images', 'int'),
    ('images_missing_alt', 'Images missing alt text', 'int'),
    ('structured_data_types', 'Structured data types', 'text'),
    ('structured_data_errors', 'Structured data errors', 'int'),
    ('structured_data_warnings', 'Structured data warnings', 'int'),
    ('hreflang_count', 'Hreflang annotations', 'int'),
    ('near_duplicates', 'Near duplicates', 'int'),
    ('closest_similarity', 'Closest similarity', 'float'), ('error', 'Error', 'text'),
)

JS_FIELDS = (
    ('js_dependent', 'Changed by JavaScript', 'bool'),
    ('word_count', 'Words in raw HTML', 'int'),
    ('rendered_word_count', 'Words after rendering', 'int'),
    ('js_added_words', 'Words added by JavaScript', 'int'),
    ('js_added_links', 'Links added by JavaScript', 'int'),
    ('js_console_errors', 'Console errors', 'int'),
)


class PublicUrlDetail(PublicView):
    """Every stored fact about one URL, its issues, and a search-result preview."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()
        address = (request.GET.get('u') or '')[:2000]
        page = CrawlPage.objects.filter(job=job, url=address).first()
        if page is None:
            return Response({'detail': 'That URL is not a crawled page in this crawl.'},
                            status=http.HTTP_404_NOT_FOUND)

        issues = sorted((issue_info(code) for code in set(page.issues or [])),
                        key=lambda i: (SEVERITY_ORDER.get(i['severity'], 3), i['code']))
        return Response({
            'url': page.url,
            'fields': [{'key': key, 'label': label, 'type': kind, 'value': getattr(page, key)}
                       for key, label, kind in DETAIL_FIELDS],
            'issues': issues,
            'content_hash': fg._hex(page.content_hash),
            'headers': page.response_headers or {},
            'javascript': ([{'key': key, 'label': label, 'type': kind, 'value': getattr(page, key)}
                            for key, label, kind in JS_FIELDS] if page.js_rendered else None),
            'serp': {
                'url': page.url,
                'title': page.title,
                'title_pixel_width': page.title_pixel_width,
                'title_truncated': page.title_pixel_width > fg.TITLE_MAX_PX,
                'meta': page.meta_description,
                'meta_pixel_width': page.meta_description_pixel_width,
                'meta_truncated': page.meta_description_pixel_width > fg.META_MAX_PX,
                'title_max_px': fg.TITLE_MAX_PX,
                'meta_max_px': fg.META_MAX_PX,
            },
        })


class PublicUrlLinks(PublicView):
    """What links to a URL, or what it links to, from the stored link graph.

    ?u=<url>&dir=in|out&kind=a|img|... Works for any URL in the graph, not only
    crawled pages, so "which pages use this image" is the same question as
    "which pages link here".
    """

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()

        address = (request.GET.get('u') or '')[:2000]
        direction = 'out' if request.GET.get('dir') == 'out' else 'in'
        kind = (request.GET.get('kind') or '').strip()[:16]
        try:
            offset = max(0, int(request.GET.get('offset', 0)))
            limit = max(1, min(LINKS_MAX_LIMIT, int(request.GET.get('limit', 100))))
        except ValueError:
            offset, limit = 0, 100

        key = url_hash(address)
        edges = CrawlLink.objects.filter(job=job)
        edges = edges.filter(to_hash=key) if direction == 'in' else edges.filter(from_hash=key)
        kinds = {r['kind']: r['n'] for r in edges.values('kind').annotate(n=Count('id'))}
        if kind:
            edges = edges.filter(kind=kind)

        pages = CrawlPage.objects.filter(job=job)
        if direction == 'in':
            other = pages.filter(url_hash=OuterRef('from_hash'))
            edges = edges.annotate(other_url=Subquery(other.values('url')[:1]),
                                   other_status=Subquery(other.values('status_code')[:1]))
        else:
            other = pages.filter(url_hash=OuterRef('to_hash'))
            edges = edges.annotate(other_url=F('to_url'),
                                   other_status=Subquery(other.values('status_code')[:1]))

        total = edges.count()
        return Response({
            'url': address,
            'dir': direction,
            'kind': kind,
            'kinds': kinds,
            'total': total,
            'offset': offset,
            'limit': limit,
            'links_stored': CrawlLink.objects.filter(job=job).exists(),
            'rows': [{
                'url': r['other_url'] or '',
                'status_code': r['other_status'],
                'anchor': r['anchor'],
                'rel': r['rel'],
                'kind': r['kind'],
                'internal': r['internal'],
                'position': r['position'],
            } for r in edges.order_by('id').values(
                'other_url', 'other_status', 'anchor', 'rel', 'kind', 'internal', 'position',
            )[offset:offset + limit]],
        })


class PublicSiteTree(PublicView):
    """The crawl as folders: every path segment with how many URLs sit under it.

    Built in Python from the URL list rather than in SQL, because splitting
    paths is not something either database does well, and a public crawl is
    capped at a size where one pass over its URLs is cheap.
    """

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()

        def node(name, path):
            return {'name': name, 'path': path, 'pages': 0, 'errors': 0,
                    'redirects': 0, 'noindex': 0, 'status': None, 'children': {}}

        host = urlsplit(job.seed_url).hostname or ''
        root = node(host, '/')
        rows = (CrawlPage.objects.filter(job=job).order_by('id')
                .values_list('url', 'status_code', 'indexability')[:TREE_MAX_URLS])
        for url, code, indexability in rows:
            parts = urlsplit(url)
            segments = [s for s in parts.path.split('/') if s][:TREE_MAX_DEPTH]
            here = root
            trail = [root]
            for index, segment in enumerate(segments):
                path = '/' + '/'.join(segments[:index + 1])
                child = here['children'].get(segment)
                if child is None:
                    child = here['children'][segment] = node(segment, path)
                here = child
                trail.append(here)
            if not parts.query:
                here['status'] = code
            for item in trail:
                item['pages'] += 1
                if code is None or code >= 400:
                    item['errors'] += 1
                elif 300 <= code < 400:
                    item['redirects'] += 1
                if indexability == 'Non-Indexable':
                    item['noindex'] += 1

        def shape(item):
            children = sorted(item['children'].values(), key=lambda c: (-c['pages'], c['name']))
            return {
                'name': item['name'], 'path': item['path'], 'pages': item['pages'],
                'errors': item['errors'], 'redirects': item['redirects'],
                'noindex': item['noindex'], 'status': item['status'],
                'more': max(0, len(children) - TREE_MAX_CHILDREN),
                'children': [shape(c) for c in children[:TREE_MAX_CHILDREN]],
            }

        return Response({'tree': shape(root), 'truncated': job.pages_crawled > TREE_MAX_URLS})


class PublicSitemapAudit(PublicView):
    """The site's live sitemaps, checked against what the crawl found.

    Fetched at request time rather than read from the crawl: a URL the crawler
    never reached leaves no row behind, and those are exactly the ones worth
    knowing about. Cached for an hour per crawl, with ?refresh=1 to look again.
    """

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()
        return Response(sitemap_audit.audit(job, refresh=request.GET.get('refresh') == '1'))


class PublicReports(PublicView):
    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        if _public_job(self.tenant, job_id) is None:
            return _not_found()
        return Response({'reports': [
            {'name': name, 'title': title, 'description': description}
            for name, (title, description, _run, _pending) in sorted(
                fg.REPORTS.items(), key=lambda kv: kv[1][0])
        ]})


class PublicReport(PublicView):
    def get(self, request, job_id, name):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()
        if name not in fg.REPORTS:
            return Response({'detail': 'No such report.'}, status=http.HTTP_404_NOT_FOUND)
        try:
            limit = max(1, min(REPORT_MAX_LIMIT, int(request.GET.get('limit', 100))))
        except ValueError:
            limit = 100
        title, description, run, _pending = fg.REPORTS[name]
        columns, rows = run(CrawlPage.objects.filter(job=job), limit)
        return Response({
            'name': name, 'title': title, 'description': description,
            'columns': list(columns),
            'rows': [[fg._hex(v) if isinstance(v, (bytes, memoryview)) else v for v in row]
                     for row in rows],
            'count': len(rows),
            'truncated': len(rows) >= limit,
        })


class PublicSitemap(PublicView):
    """XML sitemap of the indexable 200s -- what a search engine should be told about."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return _not_found()
        urls = (CrawlPage.objects.filter(job=job, status_code=200, indexability='Indexable')
                .order_by('depth', 'url').values_list('url', flat=True)[:fg.SITEMAP_URLS_PER_FILE])
        body = ''.join('  <url><loc>{0}</loc></url>\n'.format(escape(u)) for u in urls)
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               '{0}</urlset>\n'.format(body))
        response = HttpResponse(xml, content_type='application/xml; charset=utf-8')
        host = (urlsplit(job.seed_url).hostname or 'crawl').replace('"', '')
        response['Content-Disposition'] = 'attachment; filename="{0}-sitemap.xml"'.format(host)
        return response


class PublicCompare(PublicView):
    """Two public crawls, diffed by URL: added, removed, status and title changes."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        current = _public_job(self.tenant, job_id)
        try:
            against_id = int(request.GET.get('against', ''))
        except ValueError:
            against_id = None
        previous = _public_job(self.tenant, against_id) if against_id else None
        if current is None or previous is None:
            return _not_found()

        def snapshot(job):
            rows = (CrawlPage.objects.filter(job=job)
                    .values_list('url', 'status_code', 'title')[:fg.DIFF_MAX_PAGES])
            return {r[0]: (r[1], r[2]) for r in rows}

        old, new = snapshot(previous), snapshot(current)
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        status_changed, title_changed = [], []
        for url in sorted(set(old) & set(new)):
            if old[url][0] != new[url][0]:
                status_changed.append({'url': url, 'was': old[url][0], 'now': new[url][0]})
            if old[url][1] != new[url][1]:
                title_changed.append({'url': url, 'was': old[url][1], 'now': new[url][1]})

        return Response({
            'current': _job_payload(current),
            'previous': _job_payload(previous),
            'summary': {'added': len(added), 'removed': len(removed),
                        'status_changed': len(status_changed),
                        'title_changed': len(title_changed),
                        'in_both': len(set(old) & set(new))},
            'added': added[:COMPARE_LIST_LIMIT],
            'removed': removed[:COMPARE_LIST_LIMIT],
            'status_changed': status_changed[:COMPARE_LIST_LIMIT],
            'title_changed': title_changed[:COMPARE_LIST_LIMIT],
            'truncated': max(len(old), len(new)) >= fg.DIFF_MAX_PAGES,
        })
