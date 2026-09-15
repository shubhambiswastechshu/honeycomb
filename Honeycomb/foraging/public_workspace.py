"""The crawler workspace: Screaming Frog's tabs, filters, issues and reports.

Everything here reads one public crawl's page rows. It adds no data the worker
does not already send -- it turns the ~45 columns on each CrawlPage into the
views a technical SEO expects: a tab per concern, filters with live counts, an
issues list by priority, a per-URL detail pane with a search-result preview,
site structure and response-time distributions, the report catalogue, an XML
sitemap and a crawl-to-crawl comparison.

Scoped exactly like public.py: only crawls started from the public page, only
for the organization configured as HONEYCOMB_PUBLIC_CRAWL_TENANT, no login, and
under the public read rate limit. A private crawl's id returns 404 here.

What is deliberately not here, because Honeycomb does not store it: individual
links with anchor text, per-image rows, response headers, rendered HTML,
spelling and grammar, and third-party data (PageSpeed, Analytics, Search
Console). Those live on the worker, or nowhere yet.

The thresholds come from the Forager connector (connectors/catalog/forager.py)
rather than being restated, so the workspace and the AI-client reports can never
disagree about what "a long title" means.
"""
import csv
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from django.db.models import Count, F, Q
from django.db.models.functions import Length
from django.http import HttpResponse
from rest_framework import status as http
from rest_framework.response import Response

from connectors.catalog import forager as fg

from .models import CrawlJob, CrawlPage
from .public import SOURCE, PublicView, _job_payload, _public_job, _queue_position

#: Screaming Frog's defaults for the checks the connector has no constant for.
META_MIN_CHARS = 70
URL_MAX_CHARS = 115
LARGE_PAGE_BYTES = 1_000_000
DEEP_DEPTH = 4
MANY_OUTLINKS = 100

GRID_MAX_LIMIT = 200
REPORT_MAX_LIMIT = 500
COMPARE_LIST_LIMIT = 200

RESPONSE_BUCKETS = (
    ('Under 250 ms', Q(response_time_ms__lt=250)),
    ('250 – 500 ms', Q(response_time_ms__gte=250, response_time_ms__lt=500)),
    ('500 ms – 1 s', Q(response_time_ms__gte=500, response_time_ms__lt=1000)),
    ('1 – 2 s', Q(response_time_ms__gte=1000, response_time_ms__lt=2000)),
    ('2 s and over', Q(response_time_ms__gte=2000)),
)


# --------------------------------------------------------------------------- #
# Issues: code -> (label, severity)
# --------------------------------------------------------------------------- #
# Severities mirror the crawler's own SEVERITY map (crawler/forager/parse/
# onpage.py), so an issue reads the same priority in the worker's CLI and here.
ISSUES = {
    'title_missing': ('Page title is missing', 'high'),
    'title_multiple': ('More than one page title', 'high'),
    'title_over_length': ('Page title over {0} px (truncated in search)'.format(fg.TITLE_MAX_PX), 'medium'),
    'title_below_length': ('Page title under {0} characters'.format(fg.TITLE_MIN_CHARS), 'low'),
    'meta_description_missing': ('Meta description is missing', 'medium'),
    'meta_description_multiple': ('More than one meta description', 'medium'),
    'meta_description_over_length': ('Meta description over {0} px'.format(fg.META_MAX_PX), 'low'),
    'meta_description_below_length': ('Meta description too short', 'low'),
    'h1_missing': ('H1 is missing', 'medium'),
    'h1_multiple': ('More than one H1', 'low'),
    'h1_over_length': ('H1 over {0} characters'.format(fg.H1_MAX_CHARS), 'low'),
    'h2_missing': ('H2 is missing', 'low'),
    'canonical_missing': ('Canonical is missing', 'medium'),
    'meta_refresh_present': ('Uses a meta refresh', 'medium'),
    'images_missing_alt': ('Images missing alt text', 'medium'),
    'thin_content': ('Low content (under {0} words)'.format(fg.LOW_CONTENT_WORDS), 'medium'),
    'low_text_ratio': ('Low text-to-HTML ratio', 'low'),
    'lang_missing': ('Missing lang attribute', 'low'),
    'hreflang_invalid_code': ('Hreflang: invalid language code', 'medium'),
    'hreflang_missing_return_link': ('Hreflang: missing return link', 'medium'),
    'hreflang_missing_x_default': ('Hreflang: missing x-default', 'low'),
    'spelling_errors': ('Spelling errors', 'low'),
    'grammar_errors': ('Grammar errors', 'low'),
}


def issue_info(code):
    code = str(code)
    if code in ISSUES:
        label, severity = ISSUES[code]
        return {'code': code, 'label': label, 'severity': severity}
    if code.startswith('non_indexable_'):
        reason = code[len('non_indexable_'):].replace('_', ' ')
        return {'code': code, 'label': 'Non-indexable: ' + reason, 'severity': 'high'}
    return {'code': code, 'label': code.replace('_', ' ').capitalize(), 'severity': 'low'}


SEVERITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}


def _issue_q(code):
    """Pages whose issue list contains `code`.

    A JSON `contains` lookup would be the natural query, and SQLite does not
    support it. Matching the quoted code inside the list's text form works on
    both databases this app runs on, and the quotes stop `h1_missing` from also
    matching some future `h1_missing_x`.
    """
    return Q(issues__icontains='"{0}"'.format(code))


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
def col(key, label, kind='text'):
    return {'key': key, 'label': label, 'type': kind}


ADDRESS = col('url', 'Address', 'url')
INDEXABILITY = col('indexability', 'Indexability', 'index')
INDEXABILITY_STATUS = col('indexability_status', 'Indexability status')


def _base(job):
    """The page rows for a crawl, with the two computed lengths some tabs sort by."""
    return CrawlPage.objects.filter(job=job).annotate(
        url_len=Length('url'), h1_len=Length('h1_1'))


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
    own pages.
    """
    html = Q(content_type__icontains='html')
    ok = Q(status_code=200)
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
                        col('response_time_ms', 'Response time', 'ms'), col('error', 'Error')],
            'filters': [
                ('all', 'All', Q()),
                ('success', 'Success (2xx)', Q(status_code__gte=200, status_code__lt=300)),
                ('redirection', 'Redirection (3xx)', Q(status_code__gte=300, status_code__lt=400)),
                ('client_error', 'Client error (4xx)', Q(status_code__gte=400, status_code__lt=500)),
                ('server_error', 'Server error (5xx)', Q(status_code__gte=500, status_code__lt=600)),
                ('no_response', 'No response', Q(status_code__isnull=True)),
                ('chains', 'Redirect chains', Q(redirect_count__gt=1)),
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
            'columns': [ADDRESS, col('meta_robots', 'Meta robots'), INDEXABILITY, INDEXABILITY_STATUS],
            'filters': [
                ('all', 'All', html),
                ('indexable', 'Indexable', Q(indexability='Indexable')),
                ('non_indexable', 'Non-indexable', Q(indexability='Non-Indexable')),
                ('noindex', 'Noindex', Q(meta_robots__icontains='noindex') | Q(indexability_status='Noindex')),
                ('nofollow', 'Nofollow', Q(meta_robots__icontains='nofollow')),
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

    rows = _base(job)
    issue = (request.GET.get('issue') or '').strip()[:80]
    if issue:
        # An issue from the sidebar overrides the tab filter: the question being
        # asked is "which pages have this", whatever tab happens to be open.
        rows = rows.filter(_issue_q(issue))
    else:
        rows = rows.filter(filters[filter_key])

    query = (request.GET.get('q') or '').strip()[:200]
    if query:
        rows = rows.filter(url__icontains=query)
    return tab, filter_key, issue, rows


def _sorted(tab, rows, request):
    allowed = {c['key'] for c in tab['columns']} | {'id'}
    raw = (request.GET.get('sort') or tab['sort']).strip()
    key = raw.lstrip('-')
    if key not in allowed:
        raw, key = tab['sort'], tab['sort'].lstrip('-')
    direction = request.GET.get('dir')
    if direction in ('asc', 'desc'):
        descending = direction == 'desc'
    else:
        descending = raw.startswith('-')
    order = ('-' if descending else '') + key
    # A stable tiebreak, or pages with equal values shuffle between polls.
    return rows.order_by(order, 'id'), key, 'desc' if descending else 'asc'


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
            for key, _label, q in tab['filters']:
                aggregates['{0}__{1}'.format(tab['key'], key)] = Count('id', filter=q)
        for index, (_label, q) in enumerate(RESPONSE_BUCKETS):
            aggregates['rt__{0}'.format(index)] = Count('id', filter=q)
        aggregates['rt__none'] = Count('id', filter=Q(response_time_ms__isnull=True))
        aggregates['pages_total'] = Count('id')
        # One query for every count on the page: sixty-odd filters as conditional
        # aggregates, rather than sixty round trips on every poll.
        counts = _base(job).aggregate(**aggregates)

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
        rows, sort_key, direction = _sorted(tab, rows, request)
        try:
            offset = max(0, int(request.GET.get('offset', 0)))
            limit = max(1, min(GRID_MAX_LIMIT, int(request.GET.get('limit', 100))))
        except ValueError:
            offset, limit = 0, 100

        columns = list(tab['columns'])
        if issue:
            columns = [ADDRESS, col('status_code', 'Status', 'code'), INDEXABILITY,
                       col('title', 'Title 1'), col('issues', 'Issues', 'list')]
        keys = [c['key'] for c in columns]
        total = rows.count()
        return Response({
            'tab': tab['key'],
            'filter': filter_key,
            'issue': issue,
            'issue_label': issue_info(issue)['label'] if issue else '',
            'columns': columns,
            'sort': sort_key,
            'dir': direction,
            'total': total,
            'offset': offset,
            'limit': limit,
            'rows': list(rows.values(*keys)[offset:offset + limit]),
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
        rows, _key, _dir = _sorted(tab, rows, request)
        columns = tab['columns']
        if issue:
            columns = [ADDRESS, col('status_code', 'Status', 'code'), INDEXABILITY,
                       col('title', 'Title 1'), col('issues', 'Issues', 'list')]
        keys = [c['key'] for c in columns]

        host = (urlsplit(job.seed_url).hostname or 'crawl').replace('"', '')
        name = '{0}-{1}-{2}.csv'.format(host, tab['key'], issue or filter_key)
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="{0}"'.format(name)
        writer = csv.writer(response)
        writer.writerow([c['label'] for c in columns])
        for row in rows.values_list(*keys).iterator(chunk_size=2000):
            writer.writerow([
                _neutralise(', '.join(map(str, v)) if isinstance(v, list) else v) for v in row
            ])
        return response


#: Fields shown in the URL detail pane, in reading order.
DETAIL_FIELDS = (
    ('url', 'Address', 'url'), ('status_code', 'Status code', 'code'),
    ('content_type', 'Content type', 'text'), ('indexability', 'Indexability', 'index'),
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
    ('redirect_count', 'Redirects', 'int'), ('final_url', 'Redirect URL', 'url'),
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
            return Response({'detail': 'That URL is not in this crawl.'},
                            status=http.HTTP_404_NOT_FOUND)

        issues = sorted((issue_info(code) for code in set(page.issues or [])),
                        key=lambda i: (SEVERITY_ORDER.get(i['severity'], 3), i['code']))
        return Response({
            'url': page.url,
            'fields': [{'key': key, 'label': label, 'type': kind, 'value': getattr(page, key)}
                       for key, label, kind in DETAIL_FIELDS],
            'issues': issues,
            'content_hash': fg._hex(page.content_hash),
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
