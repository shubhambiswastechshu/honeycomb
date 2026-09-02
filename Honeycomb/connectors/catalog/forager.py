"""Forager connector -- crawl a site using hardware that is not this server.

Every other connector in this catalog reaches out to somebody else's API. This
one reaches into Honeycomb's own queue: a tool call writes a CrawlJob row, and
an always-on machine elsewhere claims it, does the work, and streams rows back.

That shape is deliberate. Crawling is the one job in this product that would
flatten a small VPS -- it is unbounded in pages, memory and time -- so the
server's only role is to hold the queue and the results.

The consequence a caller has to understand: `start_crawl` returns immediately
with a job id, not a crawl. Nothing here blocks on a crawl finishing, because a
full-depth crawl can run for hours and an MCP tool call cannot.

Auth is `api_key` with no credential fields. A connection exists so the tenant
boundary and the MCP key model work exactly as they do for every other
connector; there is no third party to hold a secret for.
"""
from asgiref.sync import sync_to_async

from connectors import registry
from connectors.registry import Connector
from connectors.shims.errors import ConnectorError

# Imported lazily inside handlers: connectors/catalog/* is imported during
# AppConfig.ready(), and reaching for a model at that point risks touching the
# app registry before it is populated.


def _models():
    from foraging.models import CrawlEvent, CrawlJob, CrawlPage, Worker
    return CrawlJob, CrawlPage, CrawlEvent, Worker


def _int(args, key, default, maximum=None):
    """One numeric argument, or a ConnectorError that names it.

    An MCP caller is a language model, and a model sends "50" as readily as 50
    and "all" as readily as either. A bare int() on that raises ValueError,
    which the endpoint can only hand back as ``ValueError: invalid literal for
    int()`` -- a string that names neither the tool nor the argument, so the
    model has nothing to correct and retries the same call. Every numeric
    argument in this module goes through here instead.
    """
    raw = args.get(key)
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConnectorError(
            '{0} must be a whole number; got {1!r}.'.format(key, raw))
    if value <= 0:
        # Nought and negatives mean "unset" rather than "return nothing", which
        # is what the old `or default` did and what a caller passing 0 means.
        return default
    return min(value, maximum) if maximum is not None else value


def _job_id(value, key='job_id'):
    """A job id as an int, or None. Never lets a non-numeric id reach a query."""
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConnectorError(
            '{0} must be a crawl job id (a number); got {1!r}. Call '
            'list_crawls for the ids.'.format(key, value))


def _job_dict(job):
    """Serialise a job. **Sync context only.**

    ``job.worker`` is a lazy foreign key: on a row not fetched with
    ``select_related('worker')`` reading it issues a query, and a query inside
    the event loop is a SynchronousOnlyOperation, not merely a slow call. Every
    caller below therefore builds this inside its own ``sync_to_async`` block --
    never on the payload after awaiting one.
    """
    return {
        'job_id': job.id,
        'seed_url': job.seed_url,
        'status': job.status,
        'worker': job.worker.name if job.worker else None,
        'pages_crawled': job.pages_crawled,
        'pages_queued': job.pages_queued,
        'urls_discovered': job.urls_discovered,
        'links_found': job.links_found,
        'failures': job.failures,
        'bytes_downloaded': job.bytes_downloaded,
        'status_counts': job.status_counts,
        'pages_per_second': round(job.rate, 2),
        'elapsed_seconds': round(job.duration_seconds, 1),
        'created_at': job.created_at.isoformat(),
        'finished_at': job.finished_at.isoformat() if job.finished_at else None,
        'error': job.error or None,
    }


# --------------------------------------------------------------------------- #
# Handlers. Each runs in FastAPI's event loop, so every ORM call is wrapped --
# a plain queryset there raises SynchronousOnlyOperation.
# --------------------------------------------------------------------------- #
async def start_crawl(conn, db, args):
    CrawlJob, _, _, Worker = _models()

    url = (args.get('url') or '').strip()
    if not url.startswith(('http://', 'https://')):
        raise ConnectorError('url must be an http(s) address.')

    config = {}
    for key, cast in (('limit', int), ('depth', int), ('rps', float),
                      ('concurrency', int), ('per_host', int)):
        if args.get(key) is not None:
            try:
                config[key] = cast(args[key])
            except (TypeError, ValueError):
                raise ConnectorError('{0} must be a number.'.format(key))
    for key in ('include', 'exclude'):
        if args.get(key):
            config[key] = list(args[key]) if isinstance(args[key], list) else [args[key]]
    for key in ('store_html', 'strip_all_params', 'ignore_robots'):
        if args.get(key) is not None:
            config[key] = bool(args[key])

    @sync_to_async
    def create():
        online = [w for w in Worker.objects.filter(
            tenant=conn.tenant, revoked_at__isnull=True) if w.is_online]
        job = CrawlJob.objects.create(
            tenant=conn.tenant, seed_url=url[:500], config=config, source='mcp')
        payload = _job_dict(job)
        payload['workers_online'] = [w.name for w in online]
        return payload, online

    payload, online = await create()
    if not online:
        # Not an error: the job is queued and will run when the machine comes
        # back. Saying so is better than letting the caller wonder why nothing
        # happens.
        payload['note'] = ('No Forager worker is online right now. The job is '
                           'queued and will start as soon as one connects.')
    return payload


async def crawl_status(conn, db, args):
    CrawlJob, _, CrawlEvent, _ = _models()
    job_id = _job_id(args.get('job_id'))

    @sync_to_async
    def read():
        query = CrawlJob.objects.select_related('worker').filter(tenant=conn.tenant)
        job = query.filter(pk=job_id).first() if job_id else query.first()
        if job is None:
            return None
        tail = list(CrawlEvent.objects.filter(job=job).order_by('-seq')[:15])
        payload = _job_dict(job)
        payload['recent'] = [
            {'level': e.level, 'text': e.text, 'status': e.status_code}
            for e in tail[::-1]
        ]
        return payload

    payload = await read()
    if payload is None:
        raise ConnectorError('No such crawl job.')
    return payload


async def list_crawls(conn, db, args):
    CrawlJob, _, _, _ = _models()
    limit = _int(args, 'limit', 20, 100)

    @sync_to_async
    def read():
        rows = CrawlJob.objects.select_related('worker').filter(tenant=conn.tenant)
        if args.get('status'):
            rows = rows.filter(status=args['status'])
        return [_job_dict(j) for j in rows[:limit]]

    return {'crawls': await read()}


async def cancel_crawl(conn, db, args):
    CrawlJob, _, _, _ = _models()
    job_id = _job_id(args.get('job_id'))
    if not job_id:
        raise ConnectorError('job_id is required.')

    @sync_to_async
    def stop():
        from django.utils import timezone
        # select_related because _job_dict reads job.worker, and every job worth
        # cancelling has been claimed by one. Fetching it lazily afterwards is a
        # query in the event loop, which raises rather than being merely slow.
        job = (CrawlJob.objects.select_related('worker')
               .filter(tenant=conn.tenant, pk=job_id).first())
        if job is None:
            return None
        if job.status == CrawlJob.Status.QUEUED:
            job.status = CrawlJob.Status.CANCELLED
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'finished_at'])
        elif not job.is_terminal:
            # The worker owns the process; all this can do is raise the flag it
            # reads on its next progress post.
            job.cancel_requested = True
            job.save(update_fields=['cancel_requested'])
        payload = _job_dict(job)
        # A cancel of a running crawl is a request, not an act. Saying which of
        # the two happened is the difference between the caller polling
        # crawl_status and assuming the crawl has already stopped.
        payload['cancel_requested'] = job.cancel_requested
        return payload

    payload = await stop()
    if payload is None:
        raise ConnectorError('No such crawl job.')
    return payload


async def crawl_pages(conn, db, args):
    CrawlJob, CrawlPage, _, _ = _models()
    job_id = _job_id(args.get('job_id'))
    limit = _int(args, 'limit', 50, 500)
    status_code = _int(args, 'status_code', None)
    min_status = _int(args, 'min_status', None)

    @sync_to_async
    def read():
        job = CrawlJob.objects.filter(tenant=conn.tenant, pk=job_id).first()
        if job is None:
            return None
        rows = CrawlPage.objects.filter(job=job)
        if status_code is not None:
            rows = rows.filter(status_code=status_code)
        if args.get('contains'):
            rows = rows.filter(url__icontains=str(args['contains'])[:200])
        if min_status is not None:
            rows = rows.filter(status_code__gte=min_status)
        return [{
            'url': p.url, 'status': p.status_code, 'depth': p.depth,
            'ms': p.response_time_ms, 'bytes': p.size_bytes,
            'type': p.content_type, 'redirects': p.redirect_count,
            'final_url': p.final_url or None, 'via': p.discovered_via,
            'error': p.error or None,
        } for p in rows.order_by('id')[:limit]]

    rows = await read()
    if rows is None:
        raise ConnectorError('No such crawl job.')
    return {'pages': rows, 'count': len(rows)}


async def crawl_issues(conn, db, args):
    """The reports Phase 1's data supports, in one call."""
    CrawlJob, CrawlPage, _, _ = _models()
    job_id = _job_id(args.get('job_id'))

    @sync_to_async
    def read():
        from django.db.models import Count

        job = (CrawlJob.objects.select_related('worker')
               .filter(tenant=conn.tenant, pk=job_id).first())
        if job is None:
            return None
        pages = CrawlPage.objects.filter(job=job)
        broken = list(pages.filter(status_code__gte=400)
                      .values('url', 'status_code')[:100])
        redirects = list(pages.filter(redirect_count__gt=1)
                         .values('url', 'redirect_count', 'final_url')[:100])
        slow = list(pages.filter(response_time_ms__gte=1000)
                    .values('url', 'response_time_ms')
                    .order_by('-response_time_ms')[:50])
        failed = list(pages.exclude(error='').values('url', 'error')[:50])
        dupes = list(
            pages.exclude(content_hash=None)
            .values('content_hash').annotate(n=Count('id'))
            .filter(n__gt=1).order_by('-n')[:50]
        )
        return {
            'job': _job_dict(job),
            'broken_links': broken,
            'redirect_chains': redirects,
            'slow_pages': slow,
            'fetch_errors': failed,
            'duplicate_groups': len(dupes),
            'note': ('Phase 1 reports. Titles, meta descriptions, headings and '
                     'near-duplicate detection arrive with on-page extraction.'),
        }

    payload = await read()
    if payload is None:
        raise ConnectorError('No such crawl job.')
    return payload


async def list_workers(conn, db, args):
    _, _, _, Worker = _models()

    @sync_to_async
    def read():
        return [{
            'name': w.name,
            'status': w.status,
            'version': w.version or None,
            'platform': w.platform or None,
            'last_seen': w.last_seen_at.isoformat() if w.last_seen_at else None,
            'memory_mb': round(w.rss_mb, 1) if w.rss_mb else None,
            'active_jobs': w.active_jobs,
        } for w in Worker.objects.filter(tenant=conn.tenant, revoked_at__isnull=True)]

    rows = await read()
    return {
        'workers': rows,
        'online': sum(1 for w in rows if w['status'] == 'online'),
        'note': ('Crawls run on these machines, not on the Honeycomb server. '
                 'With none online, jobs queue until one connects.'),
    }


# --------------------------------------------------------------------------- #
# Phase 3 handlers: the analyses that only mean anything over a whole crawl.
#
# The split that matters here is which of them the worker actually ships. A
# page row carries the on-page columns and nothing else, so anything derived
# from the link graph, the shingle index or the structured-data validator has a
# column on CrawlPage waiting for it and zeros in it today. Each tool below
# says so in its own payload rather than returning an empty list and letting
# the caller conclude the site is clean, because "no data" and "no findings"
# are opposite answers and an AI client cannot tell them apart.
# --------------------------------------------------------------------------- #

# Thresholds copied from the crawler's parse.onpage rather than imported: this
# server does not have the crawler on its path, and a report here quietly using
# a different pixel bound from the worker's own report would be worse than a
# duplicated constant.
TITLE_MAX_PX = 561
TITLE_MIN_CHARS = 30
META_MAX_PX = 985
H1_MAX_CHARS = 70
LOW_CONTENT_WORDS = 200
SLOW_MS = 1000

# The sitemaps.org per-file cap. Not a preference: a consumer rejects a file
# that exceeds it whole rather than reading the part that fits.
SITEMAP_URLS_PER_FILE = 50000

# compare_crawls holds both crawls' URL sets in memory to diff them, so it is
# bounded by a number rather than by the size of the crawl. Twenty thousand a
# side is a few megabytes of strings and covers most sites; beyond it the tool
# says it truncated rather than diffing a prefix and calling the missing half a
# regression.
DIFF_MAX_PAGES = 20000

# What the tools below are waiting for. Written once because four of them have
# to say it, and a caller that reads two of them should read the same sentence.
PENDING = ('The Forager worker ships the on-page columns and nothing else. '
           'Link Score, near-duplicate similarity, hreflang findings and '
           'structured-data validation are computed on the worker and are not '
           'in its page payload yet, so those columns are zero for every crawl '
           'here. The columns exist and the ingest already reads them, so '
           'shipping the data is a change to the worker alone.')


def _job_for(conn, job_id):
    """The job, or None. Callers run this inside their own sync_to_async.

    The id is coerced here rather than at each call site, so a model that sends
    "the second one" gets a sentence naming the argument instead of a ValueError
    raised from inside the query compiler.
    """
    CrawlJob, _, _, _ = _models()
    job_id = _job_id(job_id)
    if not job_id:
        return None
    return (CrawlJob.objects.select_related('worker')
            .filter(tenant=conn.tenant, pk=job_id).first())


def _hex(value):
    """A BinaryField comes back as bytes or as a memoryview, driver depending."""
    return bytes(value).hex() if value else None


# --------------------------------------------------------------------------- #
# Segments
# --------------------------------------------------------------------------- #
# The columns a rule may name, and whether the column holds a number. Written
# out rather than derived from the model's _meta because a rule is user input
# that ends up in a queryset, and a field nobody chose to expose -- url_hash,
# the job foreign key -- must not become filterable because somebody added it
# to the table.
SEGMENT_FIELDS = {
    'url': False, 'final_url': False, 'content_type': False,
    'discovered_via': False, 'indexability': False, 'indexability_status': False,
    'title': False, 'meta_description': False, 'h1_1': False,
    'canonical': False, 'meta_robots': False, 'readability': False,
    'language': False, 'structured_data_types': False, 'error': False,
    'depth': True, 'status_code': True, 'response_time_ms': True,
    'size_bytes': True, 'redirect_count': True, 'inlinks': True,
    'outlinks': True, 'title_length': True, 'title_pixel_width': True,
    'meta_description_length': True, 'meta_description_pixel_width': True,
    'h1_count': True, 'h2_count': True, 'word_count': True, 'text_ratio': True,
    'flesch_reading_ease': True, 'images': True, 'images_missing_alt': True,
    'link_score': True, 'near_duplicates': True, 'closest_similarity': True,
    'structured_data_errors': True, 'structured_data_warnings': True,
}

# The same operator names the crawler's own segment rules use, so a segment
# written in one place reads the same in the other. Text comparison is
# case-insensitive throughout, which is what somebody typing "blog" into a box
# means, and one operator folding case while the next does not is the kind of
# inconsistency nobody discovers until a segment is missing a third of its pages.
SEGMENT_OPS = ('contains', 'not_contains', 'equals', 'not_equals',
               'starts_with', 'ends_with', 'matches_regex', 'greater_than',
               'less_than', 'is_empty', 'is_not_empty')
NULLARY_OPS = ('is_empty', 'is_not_empty')


def _rule_q(rule):
    """One rule as a Q object.

    Every branch ends in a keyword lookup rather than raw SQL, so the value is
    always a bound parameter and a rule carrying a regex cannot become an
    injection.
    """
    from django.db.models import Q

    if not isinstance(rule, dict):
        raise ConnectorError('Each rule must be an object with field, op and value.')
    field = str(rule.get('field') or '')
    op = str(rule.get('op') or '')
    value = rule.get('value', '')

    if field not in SEGMENT_FIELDS:
        raise ConnectorError('Unknown segment field {0!r}. Try one of: {1}.'.format(
            field, ', '.join(sorted(SEGMENT_FIELDS))))
    if op not in SEGMENT_OPS:
        raise ConnectorError('Unknown operator {0!r}. Try one of: {1}.'.format(
            op, ', '.join(SEGMENT_OPS)))

    numeric = SEGMENT_FIELDS[field]
    if op in NULLARY_OPS:
        # Missing and blank are the same answer to "is there a meta
        # description". Zero is not: a word count of 0 is a measurement, not an
        # absence, which is why the numeric branch compares against 0 rather
        # than treating it as empty.
        empty = Q(**{field: 0}) if numeric else Q(**{field: ''})
        return ~empty if op == 'is_not_empty' else empty

    if numeric:
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ConnectorError('{0} holds a number; {1!r} is not one.'.format(
                field, value))
    else:
        value = str(value)

    if op in ('greater_than', 'less_than'):
        if not numeric:
            raise ConnectorError('{0} is text, so {1} does not apply.'.format(field, op))
        suffix = 'gt' if op == 'greater_than' else 'lt'
        return Q(**{'{0}__{1}'.format(field, suffix): value})

    if op == 'matches_regex':
        import re
        try:
            re.compile(value)
        except re.error as exc:
            # Better caught here than at query time: a typo in a pattern should
            # blame the rule that has it, not the report that used the segment.
            raise ConnectorError('Bad regex {0!r}: {1}'.format(value, exc))
        return Q(**{'{0}__iregex'.format(field): value})

    lookup = {
        'contains': 'icontains', 'not_contains': 'icontains',
        'equals': 'exact' if numeric else 'iexact',
        'not_equals': 'exact' if numeric else 'iexact',
        'starts_with': 'istartswith', 'ends_with': 'iendswith',
    }[op]
    q = Q(**{'{0}__{1}'.format(field, lookup): value})
    return ~q if op in ('not_contains', 'not_equals') else q


def _segment_filter(pages, rules, match):
    if not rules:
        # An empty rule set compiles to no filter at all and would quietly
        # select the whole crawl, which is the most expensive way to be wrong.
        raise ConnectorError('A segment needs at least one rule.')
    combined = None
    for rule in rules:
        q = _rule_q(rule)
        if combined is None:
            combined = q
        elif match == 'any':
            combined = combined | q
        else:
            combined = combined & q
    return pages.filter(combined)


def _apply_named_segment(conn, pages, name):
    """Narrow `pages` by a stored segment. Runs inside a sync_to_async caller."""
    from foraging.models import CrawlSegment

    seg = CrawlSegment.objects.filter(tenant=conn.tenant, name__iexact=name).first()
    if seg is None:
        raise ConnectorError(
            'No segment named {0!r}. Create one with crawl_segment.'.format(name))
    return _segment_filter(pages, seg.rules, seg.match), seg


# --------------------------------------------------------------------------- #
# Reports
#
# A deliberately smaller registry than the crawler's sixty. The crawler runs
# its reports as SQL over the whole crawl database -- link rows, anchor text,
# hreflang pairs, response headers -- and that database never leaves the
# worker. What Honeycomb holds is one row per URL, so what can be rebuilt here
# is exactly the reports whose SQL only ever touched the page row. The rest are
# not reimplemented and not silently dropped either: list_reports names the
# ones that need data this server does not have, because a caller asking what
# it can run deserves to know what exists and why it cannot have it.
# --------------------------------------------------------------------------- #
def _rows(pages, fields, limit, order=None):
    query = pages.order_by(order) if order else pages
    return [tuple(r) for r in query.values_list(*fields)[:limit]]


def _tally(pages, field, limit):
    """Group by one column, biggest bucket first."""
    from django.db.models import Count

    rows = pages.values(field).annotate(n=Count('id')).order_by('-n')[:limit]
    return [(r[field], r['n']) for r in rows]


def _shared(pages, field, limit):
    """Values more than one page carries: the shape every duplicate report has."""
    from django.db.models import Count

    rows = (pages.exclude(**{field: ''}).values(field)
            .annotate(n=Count('id')).filter(n__gt=1).order_by('-n')[:limit])
    return [(r[field], r['n']) for r in rows]


def _indexable(pages):
    return pages.filter(indexability='Indexable')


def _r_codes(pages, limit):
    return ('status_code', 'urls'), _tally(pages, 'status_code', limit)


def _r_broken(pages, limit):
    return (('url', 'status_code'),
            _rows(pages.filter(status_code__gte=400), ('url', 'status_code'),
                  limit, '-status_code'))


def _r_redirects(pages, limit):
    return (('url', 'hops', 'final_url'),
            _rows(pages.filter(redirect_count__gt=1),
                  ('url', 'redirect_count', 'final_url'), limit, '-redirect_count'))


def _r_slow(pages, limit):
    return (('url', 'ms'),
            _rows(pages.filter(response_time_ms__gte=SLOW_MS),
                  ('url', 'response_time_ms'), limit, '-response_time_ms'))


def _r_heavy(pages, limit):
    return ('url', 'bytes'), _rows(pages, ('url', 'size_bytes'), limit, '-size_bytes')


def _r_depth(pages, limit):
    from django.db.models import Count

    rows = pages.values('depth').annotate(n=Count('id')).order_by('depth')[:limit]
    return ('depth', 'urls'), [(r['depth'], r['n']) for r in rows]


def _r_nonindexable(pages, limit):
    return (('reason', 'urls'),
            _tally(pages.filter(indexability='Non-Indexable'),
                   'indexability_status', limit))


def _r_canonicalised(pages, limit):
    return (('url', 'canonical'),
            _rows(pages.filter(indexability_status='Canonicalised'),
                  ('url', 'canonical'), limit, 'url'))


def _r_titles_missing(pages, limit):
    return ('url',), _rows(_indexable(pages).filter(title=''), ('url',), limit, 'url')


def _r_titles_dupe(pages, limit):
    return ('title', 'pages'), _shared(_indexable(pages), 'title', limit)


def _r_titles_long(pages, limit):
    return (('url', 'title', 'pixels'),
            _rows(pages.filter(title_pixel_width__gt=TITLE_MAX_PX),
                  ('url', 'title', 'title_pixel_width'), limit, '-title_pixel_width'))


def _r_titles_short(pages, limit):
    return (('url', 'title', 'chars'),
            _rows(pages.filter(title_length__gt=0, title_length__lt=TITLE_MIN_CHARS),
                  ('url', 'title', 'title_length'), limit, 'title_length'))


def _r_meta_missing(pages, limit):
    return ('url',), _rows(_indexable(pages).filter(meta_description=''),
                           ('url',), limit, 'url')


def _r_meta_dupe(pages, limit):
    return (('meta_description', 'pages'),
            _shared(_indexable(pages), 'meta_description', limit))


def _r_meta_long(pages, limit):
    return (('url', 'pixels'),
            _rows(pages.filter(meta_description_pixel_width__gt=META_MAX_PX),
                  ('url', 'meta_description_pixel_width'), limit,
                  '-meta_description_pixel_width'))


def _r_h1_missing(pages, limit):
    return ('url',), _rows(_indexable(pages).filter(h1_count=0), ('url',), limit, 'url')


def _r_h1_dupe(pages, limit):
    # The first h1 only, which is the one that behaves like a heading.
    return ('h1', 'pages'), _shared(_indexable(pages), 'h1_1', limit)


def _r_h1_multi(pages, limit):
    return (('url', 'h1_count'),
            _rows(pages.filter(h1_count__gt=1), ('url', 'h1_count'), limit, '-h1_count'))


def _r_h1_long(pages, limit):
    from django.db.models.functions import Length

    rows = (pages.annotate(h1_chars=Length('h1_1')).filter(h1_chars__gt=H1_MAX_CHARS)
            .order_by('-h1_chars').values_list('url', 'h1_1', 'h1_chars')[:limit])
    return ('url', 'h1', 'chars'), [tuple(r) for r in rows]


def _r_img_alt(pages, limit):
    return (('url', 'images', 'missing_alt'),
            _rows(pages.filter(images_missing_alt__gt=0),
                  ('url', 'images', 'images_missing_alt'), limit,
                  '-images_missing_alt'))


def _r_thin(pages, limit):
    return (('url', 'words'),
            _rows(_indexable(pages).filter(word_count__lt=LOW_CONTENT_WORDS),
                  ('url', 'word_count'), limit, 'word_count'))


def _r_structured(pages, limit):
    """What each page claims to be, counted by type name.

    Split in Python because the column is a comma-joined list and no portable
    SQL splits one. A side table would query better and would also mean a join
    on the hottest read in the console for a value that is only ever read whole.
    """
    counts = {}
    for row in pages.exclude(structured_data_types='').values_list(
            'structured_data_types', flat=True)[:DIFF_MAX_PAGES]:
        for name in str(row).split(','):
            name = name.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    return ('type', 'pages'), sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def _r_issues(pages, limit):
    """Issue-code frequency.

    Counted in Python over a capped scan rather than in a query, because the
    codes live in a JSON list and aggregating inside one is written differently
    on every backend this app is deployed against.
    """
    counts = {}
    for codes in pages.exclude(issues=[]).values_list(
            'issues', flat=True)[:DIFF_MAX_PAGES]:
        for code in (codes or []):
            counts[str(code)] = counts.get(str(code), 0) + 1
    return ('issue', 'pages'), sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def _r_dupes_exact(pages, limit):
    from django.db.models import Count

    rows = (pages.exclude(content_hash=None).values('content_hash')
            .annotate(n=Count('id')).filter(n__gt=1).order_by('-n')[:limit])
    return ('content_hash', 'pages'), [(_hex(r['content_hash']), r['n']) for r in rows]


def _r_link_score(pages, limit):
    return (('url', 'link_score', 'inlinks'),
            _rows(pages, ('url', 'link_score', 'inlinks'), limit, '-link_score'))


def _r_near_dupes(pages, limit):
    return (('url', 'near_duplicates', 'closest_similarity'),
            _rows(pages.filter(near_duplicates__gt=0),
                  ('url', 'near_duplicates', 'closest_similarity'), limit,
                  '-closest_similarity'))


def _r_hreflang(pages, limit):
    return (('url', 'annotations', 'issues'),
            _rows(pages.exclude(hreflang_issues=[]),
                  ('url', 'hreflang_count', 'hreflang_issues'), limit, 'url'))


def _r_sd_errors(pages, limit):
    return (('url', 'errors', 'warnings', 'findings'),
            _rows(pages.filter(structured_data_errors__gt=0),
                  ('url', 'structured_data_errors', 'structured_data_warnings',
                   'structured_data_findings'), limit, '-structured_data_errors'))


# name -> (title, description, run, pending). The pending flag marks a report
# whose columns no worker fills yet: it runs and returns nothing, and the flag
# is the only thing standing between "no rows" and a caller reading that as
# "no problems".
REPORTS = {
    'codes': ('Response codes', 'Status code breakdown', _r_codes, False),
    'broken': ('Broken pages', '4xx and 5xx responses', _r_broken, False),
    'redirects': ('Redirect chains', 'More than one hop', _r_redirects, False),
    'slow': ('Slow pages', 'Responses over {0}ms'.format(SLOW_MS), _r_slow, False),
    'heavy': ('Heaviest pages', 'Largest response bodies', _r_heavy, False),
    'depth': ('Crawl depth', 'URLs per click depth', _r_depth, False),
    'nonindexable': ('Non-indexable', 'Why each page cannot be indexed',
                     _r_nonindexable, False),
    'canonicalised': ('Canonicalised', 'Pages whose canonical names another URL',
                      _r_canonicalised, False),
    'titles-missing': ('Missing titles', 'Indexable pages with no title',
                       _r_titles_missing, False),
    'titles-dupe': ('Duplicate titles', 'Titles shared by more than one page',
                    _r_titles_dupe, False),
    'titles-long': ('Long titles',
                    'Wider than {0}px, where Google truncates'.format(TITLE_MAX_PX),
                    _r_titles_long, False),
    'titles-short': ('Short titles', 'Under {0} characters'.format(TITLE_MIN_CHARS),
                     _r_titles_short, False),
    'meta-missing': ('Missing meta descriptions', 'Indexable pages with none',
                     _r_meta_missing, False),
    'meta-dupe': ('Duplicate meta descriptions', 'Shared by more than one page',
                  _r_meta_dupe, False),
    'meta-long': ('Long meta descriptions', 'Wider than {0}px'.format(META_MAX_PX),
                  _r_meta_long, False),
    'h1-missing': ('Missing h1', 'Indexable pages with no h1', _r_h1_missing, False),
    'h1-dupe': ('Duplicate h1', 'First h1 shared by more than one page',
                _r_h1_dupe, False),
    'h1-multi': ('Multiple h1', 'Pages carrying more than one h1', _r_h1_multi, False),
    'h1-long': ('Long h1', 'Over {0} characters'.format(H1_MAX_CHARS),
                _r_h1_long, False),
    'img-alt': ('Images missing alt', 'Pages with unlabelled images',
                _r_img_alt, False),
    'thin': ('Thin content', 'Under {0} words'.format(LOW_CONTENT_WORDS),
             _r_thin, False),
    'structured': ('Structured data types', 'What pages claim to be',
                   _r_structured, False),
    'issues': ('Issue codes', 'Every issue the crawler flagged, counted',
               _r_issues, False),
    'dupes-exact': ('Exact duplicates', 'Identical bodies, grouped by hash',
                    _r_dupes_exact, False),
    'link-score': ('Link Score', 'Internal PageRank, highest first',
                   _r_link_score, True),
    'near-dupes': ('Near duplicates', 'Pages with a close but not identical twin',
                   _r_near_dupes, True),
    'hreflang': ('Hreflang findings', 'Pages with a broken annotation',
                 _r_hreflang, True),
    'sd-errors': ('Structured data errors', 'Rich-result validation failures',
                  _r_sd_errors, True),
}


async def list_reports(conn, db, args):
    rows = []
    for name in sorted(REPORTS):
        title, description, _run, pending = REPORTS[name]
        rows.append({'name': name, 'title': title, 'description': description,
                     'available': not pending})
    return {
        'reports': rows,
        'available': sum(1 for r in rows if r['available']),
        'note': ('Run one with crawl_report. A report marked available false '
                 'runs but returns nothing today; pending_note says why. The '
                 'crawler itself has around sixty reports, and the ones absent '
                 'here -- anchor text, inlinks, hreflang pairs, response '
                 'headers -- read tables that stay on the worker.'),
        'pending_note': PENDING,
    }


async def crawl_report(conn, db, args):
    _, CrawlPage, _, _ = _models()
    name = str(args.get('report') or '').strip()
    if name not in REPORTS:
        raise ConnectorError(
            'Unknown report {0!r}. Call list_reports for the names.'.format(name))
    limit = _int(args, 'limit', 50, 500)
    title, description, run, pending = REPORTS[name]

    @sync_to_async
    def read():
        job = _job_for(conn, args.get('job_id'))
        if job is None:
            return None
        pages = CrawlPage.objects.filter(job=job)
        segment = None
        if args.get('segment'):
            pages, seg = _apply_named_segment(conn, pages, str(args['segment']))
            segment = seg.name
        columns, rows = run(pages, limit)
        return job, segment, columns, rows

    found = await read()
    if found is None:
        raise ConnectorError('No such crawl job.')
    job, segment, columns, rows = found
    payload = {
        'report': name,
        'title': title,
        'description': description,
        'job_id': job.id,
        'segment': segment,
        'columns': list(columns),
        'rows': [list(r) for r in rows],
        'count': len(rows),
        'truncated': len(rows) >= limit,
    }
    if pending:
        payload['note'] = PENDING
    return payload


async def crawl_link_scores(conn, db, args):
    _, CrawlPage, _, _ = _models()
    limit = _int(args, 'limit', 25, 500)

    @sync_to_async
    def read():
        from django.db.models import Max

        job = _job_for(conn, args.get('job_id'))
        if job is None:
            return None
        pages = CrawlPage.objects.filter(job=job)
        if args.get('segment'):
            pages, _seg = _apply_named_segment(conn, pages, str(args['segment']))
        best = pages.aggregate(m=Max('link_score'))['m'] or 0
        rows = list(pages.order_by('-link_score', 'depth')
                    .values('url', 'link_score', 'inlinks', 'outlinks',
                            'depth', 'indexability')[:limit])
        return job, best, rows

    found = await read()
    if found is None:
        raise ConnectorError('No such crawl job.')
    job, best, rows = found
    payload = {
        'job_id': job.id,
        'pages': [{'url': r['url'], 'link_score': round(r['link_score'], 2),
                   'inlinks': r['inlinks'], 'outlinks': r['outlinks'],
                   'depth': r['depth'], 'indexability': r['indexability'] or None}
                  for r in rows],
        'count': len(rows),
        'computed': best > 0,
    }
    if best <= 0:
        # Zero everywhere is the signature of a column nobody filled, not of a
        # site whose pages have no authority: even an orphan scores above zero,
        # because the teleport term gives every node a floor.
        payload['note'] = PENDING
    return payload


async def crawl_duplicates(conn, db, args):
    """Exact clusters are real; near ones wait on the worker.

    The exact half needs nothing new because the worker already sends a content
    hash with every page, and identical bodies are a GROUP BY over it. The near
    half is a MinHash index the worker builds and keeps.
    """
    _, CrawlPage, _, _ = _models()
    limit = _int(args, 'limit', 25, 200)

    @sync_to_async
    def read():
        from django.db.models import Count

        job = _job_for(conn, args.get('job_id'))
        if job is None:
            return None
        pages = CrawlPage.objects.filter(job=job)
        groups = list(pages.exclude(content_hash=None).values('content_hash')
                      .annotate(n=Count('id')).filter(n__gt=1).order_by('-n')[:limit])
        members = {}
        if groups:
            # One query for every cluster's members rather than one per
            # cluster, which would be a round trip per group.
            rows = pages.filter(content_hash__in=[g['content_hash'] for g in groups])
            for row in rows.values('content_hash', 'url')[:limit * 20]:
                members.setdefault(_hex(row['content_hash']), []).append(row['url'])
        near = list(pages.filter(near_duplicates__gt=0)
                    .order_by('-closest_similarity')
                    .values('url', 'near_duplicates', 'closest_similarity')[:limit])
        return job, groups, members, near

    found = await read()
    if found is None:
        raise ConnectorError('No such crawl job.')
    job, groups, members, near = found
    return {
        'job_id': job.id,
        'exact': [{'content_hash': _hex(g['content_hash']), 'pages': g['n'],
                   'urls': members.get(_hex(g['content_hash']), [])[:20]}
                  for g in groups],
        'exact_clusters': len(groups),
        'near': [{'url': r['url'], 'near_duplicates': r['near_duplicates'],
                  'closest_similarity': round(r['closest_similarity'], 3)}
                 for r in near],
        'near_computed': bool(near),
        'note': ('Exact duplicates are real: they group the content hash the '
                 'worker sends with every page. The near-duplicate half is '
                 'empty. ' + PENDING),
    }


async def crawl_hreflang(conn, db, args):
    _, CrawlPage, _, _ = _models()
    limit = _int(args, 'limit', 50, 500)

    @sync_to_async
    def read():
        from django.db.models import Sum

        job = _job_for(conn, args.get('job_id'))
        if job is None:
            return None
        pages = CrawlPage.objects.filter(job=job)
        declared = pages.filter(hreflang_count__gt=0)
        rows = list(pages.exclude(hreflang_issues=[])
                    .values('url', 'hreflang_count', 'hreflang_issues')[:limit])
        annotations = pages.aggregate(n=Sum('hreflang_count'))['n'] or 0
        return job, declared.count(), annotations, rows

    found = await read()
    if found is None:
        raise ConnectorError('No such crawl job.')
    job, pages_with, annotations, rows = found

    counts = {}
    for row in rows:
        for code in (row['hreflang_issues'] or []):
            counts[str(code)] = counts.get(str(code), 0) + 1
    return {
        'job_id': job.id,
        'pages_with_hreflang': pages_with,
        'annotations': annotations,
        'findings': [{'url': r['url'], 'annotations': r['hreflang_count'],
                      'issues': r['hreflang_issues']} for r in rows],
        'by_issue': [{'issue': k, 'pages': v}
                     for k, v in sorted(counts.items(), key=lambda kv: -kv[1])],
        'audited': pages_with > 0,
        'note': ('Zero pages with hreflang does not mean the site declares '
                 'none. ' + PENDING),
    }


async def crawl_structured(conn, db, args):
    """The type breakdown is real; the validation half is not.

    onpage extraction already collects every page's @type names and the worker
    ships them, so what a site claims to be is answerable today. Whether those
    claims meet Google's rich-result requirements is a separate parse that runs
    on the worker and stays there.
    """
    _, CrawlPage, _, _ = _models()
    limit = _int(args, 'limit', 50, 500)

    @sync_to_async
    def read():
        from django.db.models import Sum

        job = _job_for(conn, args.get('job_id'))
        if job is None:
            return None
        pages = CrawlPage.objects.filter(job=job)
        with_data = pages.exclude(structured_data_types='').count()
        _columns, types = _r_structured(pages, limit)
        invalid = list(pages.filter(structured_data_errors__gt=0)
                       .order_by('-structured_data_errors')
                       .values('url', 'structured_data_errors',
                               'structured_data_warnings',
                               'structured_data_findings')[:limit])
        totals = pages.aggregate(e=Sum('structured_data_errors'),
                                 w=Sum('structured_data_warnings'))
        return job, with_data, types, invalid, totals

    found = await read()
    if found is None:
        raise ConnectorError('No such crawl job.')
    job, with_data, types, invalid, totals = found
    errors = totals.get('e') or 0
    return {
        'job_id': job.id,
        'pages_with_structured_data': with_data,
        'types': [{'type': name, 'pages': n} for name, n in types],
        'errors': errors,
        'warnings': totals.get('w') or 0,
        'invalid': [{'url': r['url'], 'errors': r['structured_data_errors'],
                     'warnings': r['structured_data_warnings'],
                     'findings': r['structured_data_findings']} for r in invalid],
        'validated': errors > 0,
        'note': ('The type breakdown is real. Validation against Google\'s '
                 'rich-result requirements is not in the page payload, so '
                 'errors and warnings read zero for every crawl. ' + PENDING),
    }


async def compare_crawls(conn, db, args):
    """Two crawls, diffed by URL.

    Both URL sets are pulled into memory to be compared, so this is capped by a
    number rather than by the size of the crawl -- a million rows a side is
    exactly the work this architecture exists to keep off the server. Past the
    cap the payload says it truncated, because a diff that quietly compared a
    prefix would report the missing half as deleted pages.
    """
    CrawlJob, CrawlPage, _, _ = _models()
    before_id = _job_id(args.get('job_id'))
    after_id = _job_id(args.get('against'), 'against')
    if not before_id or not after_id:
        raise ConnectorError('job_id and against are both required.')
    limit = _int(args, 'limit', 50, 500)

    @sync_to_async
    def read():
        jobs = {j.id: j for j in CrawlJob.objects.filter(
            tenant=conn.tenant, pk__in=[before_id, after_id])}
        before, after = jobs.get(before_id), jobs.get(after_id)
        if before is None or after is None:
            return None

        def snapshot(job):
            rows = (CrawlPage.objects.filter(job=job)
                    .values_list('url', 'status_code', 'title')[:DIFF_MAX_PAGES])
            return {r[0]: (r[1], r[2]) for r in rows}

        return before, after, snapshot(before), snapshot(after)

    found = await read()
    if found is None:
        raise ConnectorError('One of those crawl jobs does not exist.')
    before, after, old, new = found

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    status_changed, title_changed = [], []
    for url in sorted(set(old) & set(new)):
        was, now = old[url], new[url]
        if was[0] != now[0]:
            status_changed.append({'url': url, 'was': was[0], 'now': now[0]})
        if was[1] != now[1]:
            title_changed.append({'url': url, 'was': was[1], 'now': now[1]})

    def counters(job):
        return {'pages_crawled': job.pages_crawled, 'failures': job.failures,
                'urls_discovered': job.urls_discovered,
                'links_found': job.links_found,
                'bytes_downloaded': job.bytes_downloaded,
                'status_counts': job.status_counts}

    return {
        'before': {'job_id': before.id, 'seed_url': before.seed_url,
                   'created_at': before.created_at.isoformat(),
                   'counters': counters(before)},
        'after': {'job_id': after.id, 'seed_url': after.seed_url,
                  'created_at': after.created_at.isoformat(),
                  'counters': counters(after)},
        'summary': {'added': len(added), 'removed': len(removed),
                    'status_changed': len(status_changed),
                    'title_changed': len(title_changed),
                    'in_both': len(set(old) & set(new))},
        'added': added[:limit],
        'removed': removed[:limit],
        'status_changed': status_changed[:limit],
        'title_changed': title_changed[:limit],
        'truncated': max(len(old), len(new)) >= DIFF_MAX_PAGES,
        'note': ('Compares up to {0} URLs a side. Two crawls of different '
                 'seeds are diffed anyway; whether that means anything is the '
                 'caller\'s judgement.'.format(DIFF_MAX_PAGES)),
    }


async def crawl_segment(conn, db, args):
    """Create, list, delete or apply a named rule set over a crawl's URLs.

    A segment is rules rather than a list of URLs because a crawl is re-run: a
    segment written once still means "the blog" after the next crawl finds two
    hundred new posts.
    """
    _, CrawlPage, _, _ = _models()
    action = str(args.get('action') or 'apply').lower()
    if action not in ('create', 'apply', 'list', 'delete'):
        raise ConnectorError('action must be create, apply, list or delete.')
    limit = _int(args, 'limit', 25, 500)

    @sync_to_async
    def run():
        from foraging.models import CrawlSegment

        if action == 'list':
            rows = CrawlSegment.objects.filter(tenant=conn.tenant)
            return {'segments': [{'name': s.name, 'description': s.description,
                                  'match': s.match, 'rules': s.rules}
                                 for s in rows]}

        name = str(args.get('name') or '').strip()
        if not name:
            raise ConnectorError('name is required.')

        if action == 'delete':
            # By pk, not by an iexact filter: that filter matches every casing,
            # so deleting "blog" would take "Blog" with it and report one name.
            seg = CrawlSegment.objects.filter(
                tenant=conn.tenant, name__iexact=name).first()
            if seg is None:
                raise ConnectorError('No segment named {0!r}.'.format(name))
            deleted = seg.name
            CrawlSegment.objects.filter(pk=seg.pk).delete()
            return {'deleted': deleted}

        if action == 'create':
            rules = args.get('rules') or []
            if not isinstance(rules, list):
                raise ConnectorError('rules must be a list of {field, op, value}.')
            match = str(args.get('match') or 'all').lower()
            if match not in ('all', 'any'):
                raise ConnectorError('match must be all or any.')
            # Compiled before it is stored, so a bad field is rejected by the
            # call that wrote the rule rather than by whoever applies it next
            # month against a crawl they did not run.
            _segment_filter(CrawlPage.objects.none(), rules, match)
            # Matched case-insensitively, because every other action resolves a
            # name that way. An exact-match upsert here would let "Blog" and
            # "blog" both exist -- the unique constraint is case-sensitive -- and
            # then apply would pick one of them arbitrarily.
            defaults = {'rules': rules, 'match': match,
                        'description': str(args.get('description') or '')[:200]}
            seg = CrawlSegment.objects.filter(
                tenant=conn.tenant, name__iexact=name).first()
            created = seg is None
            if created:
                seg = CrawlSegment.objects.create(
                    tenant=conn.tenant, name=name[:80], **defaults)
            else:
                for key, value in defaults.items():
                    setattr(seg, key, value)
                seg.save(update_fields=list(defaults))
            return {'segment': seg.name, 'created': created,
                    'rules': seg.rules, 'match': seg.match}

        job = _job_for(conn, args.get('job_id'))
        if job is None:
            raise ConnectorError('apply needs the job_id of a crawl that exists.')
        all_pages = CrawlPage.objects.filter(job=job)
        pages, seg = _apply_named_segment(conn, all_pages, name)
        total = all_pages.count()
        matched = pages.count()
        return {'segment': seg.name, 'match': seg.match, 'rules': seg.rules,
                'job_id': job.id, 'matched': matched, 'crawl_pages': total,
                'share': round(matched / total, 4) if total else 0.0,
                'sample': list(pages.values('url', 'status_code', 'indexability',
                                            'title')[:limit])}

    payload = await run()
    payload.setdefault(
        'note', 'A segment name can be passed to crawl_report, '
                'crawl_link_scores and generate_sitemap to narrow them.')
    return payload


async def generate_sitemap(conn, db, args):
    """XML for the URLs a search engine should be told about.

    Built from the rows this server already holds rather than asked of the
    worker, because the filter is three columns wide -- answered 200, indexable,
    on this crawl -- and all three are on the page row. One file: the protocol
    caps a sitemap at 50,000 URLs, and a larger crawl needs a sitemap index and
    a place to host the parts, neither of which belongs in a tool result.
    """
    from xml.sax.saxutils import escape

    _, CrawlPage, _, _ = _models()
    max_urls = _int(args, 'max_urls', 1000, SITEMAP_URLS_PER_FILE)
    include_all = bool(args.get('include_non_indexable'))

    @sync_to_async
    def read():
        job = _job_for(conn, args.get('job_id'))
        if job is None:
            return None
        pages = CrawlPage.objects.filter(job=job, status_code=200)
        if not include_all:
            pages = pages.filter(indexability='Indexable')
        if args.get('segment'):
            pages, _seg = _apply_named_segment(conn, pages, str(args['segment']))
        eligible = pages.count()
        urls = list(pages.order_by('depth', 'url')
                    .values_list('url', flat=True)[:max_urls])
        return job, eligible, urls

    found = await read()
    if found is None:
        raise ConnectorError('No such crawl job.')
    job, eligible, urls = found

    body = ''.join('  <url><loc>{0}</loc></url>\n'.format(escape(u)) for u in urls)
    payload = {
        'job_id': job.id,
        'seed_url': job.seed_url,
        'filename': 'sitemap.xml',
        'urls': len(urls),
        'eligible': eligible,
        'filter': 'status 200' if include_all else 'status 200 and indexable',
        'xml': ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                '{0}</urlset>\n'.format(body)),
        'truncated': eligible > len(urls),
    }
    if eligible > len(urls):
        payload['note'] = (
            'Returning {0} of {1} eligible URLs. Raise max_urls, or narrow the '
            'crawl with a segment.'.format(len(urls), eligible))
    if not eligible:
        payload['note'] = ('No page in this crawl answered 200 and was '
                           'indexable. A crawl that ran before on-page '
                           'extraction has no indexability at all; pass '
                           'include_non_indexable to fall back to status 200.')
    return payload



CATALOG = {
    'start_crawl': {
        'description': (
            'Start crawling a website. Runs on a Forager worker machine, not on '
            'the Honeycomb server, and returns a job_id immediately -- the crawl '
            'itself may run for hours. Poll crawl_status with the job_id.'
        ),
        'input': {
            'type': 'object',
            'properties': {
                'url': {'type': 'string', 'description': 'Seed URL to crawl.'},
                'limit': {'type': 'integer',
                          'description': 'Stop after N pages. Omit for the whole site.'},
                'depth': {'type': 'integer',
                          'description': 'Max click depth. Omit for full depth.'},
                'rps': {'type': 'number',
                        'description': 'Requests per second. Default 5; be polite.'},
                'concurrency': {'type': 'integer'},
                'per_host': {'type': 'integer'},
                'include': {'type': 'array', 'items': {'type': 'string'},
                            'description': 'Regexes; only matching URLs are crawled.'},
                'exclude': {'type': 'array', 'items': {'type': 'string'}},
                'strip_all_params': {'type': 'boolean',
                                     'description': 'Drop every query string. '
                                                    'For facet-heavy shops.'},
                'store_html': {'type': 'boolean',
                               'description': 'Keep raw HTML on the worker for '
                                              're-analysis without re-crawling.'},
            },
            'required': ['url'],
        },
        'write': True,
    },
    'crawl_status': {
        'description': 'Progress of a crawl: counters, rate, and the last few '
                       'console lines. Omit job_id for the most recent crawl.',
        'input': {
            'type': 'object',
            'properties': {'job_id': {'type': 'integer'}},
        },
    },
    'list_crawls': {
        'description': ('Recent crawls, newest first: the job_id of each, its '
                        'seed URL, status and page counters. Start here: every '
                        'other tool in this connector takes a job_id, and this '
                        'is where they come from.'),
        'input': {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer'},
                'status': {'type': 'string',
                           'enum': ['queued', 'claimed', 'running', 'done',
                                    'failed', 'cancelled']},
            },
        },
    },
    'cancel_crawl': {
        'description': 'Ask the worker to stop a running crawl. Results already '
                       'streamed back are kept.',
        'input': {
            'type': 'object',
            'properties': {'job_id': {'type': 'integer'}},
            'required': ['job_id'],
        },
        'write': True,
    },
    'crawl_pages': {
        'description': 'Crawled URLs with status, timing and size. Filter by '
                       'status_code, min_status (e.g. 400 for errors) or a URL '
                       'substring.',
        'input': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer'},
                'limit': {'type': 'integer'},
                'status_code': {'type': 'integer'},
                'min_status': {'type': 'integer'},
                'contains': {'type': 'string'},
            },
            'required': ['job_id'],
        },
    },
    'crawl_issues': {
        'description': 'SEO issues found: broken links, redirect chains, slow '
                       'pages, fetch errors, duplicate content.',
        'input': {
            'type': 'object',
            'properties': {'job_id': {'type': 'integer'}},
            'required': ['job_id'],
        },
    },
    'list_workers': {
        'description': 'Forager worker machines and whether they are online. '
                       'Crawls need one; without it jobs queue.',
        'input': {'type': 'object', 'properties': {}},
    },
    'crawl_link_scores': {
        'description': (
            'Top pages by Link Score -- internal PageRank over the crawl\'s own '
            'link graph, 0-100. Answers "which pages does this site actually '
            'promote", which an inlink count cannot. NOTE: the worker does not '
            'ship Link Score yet, so this returns zeros and says so.'
        ),
        'input': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer'},
                'limit': {'type': 'integer'},
                'segment': {'type': 'string',
                            'description': 'Name of a stored segment to narrow to.'},
            },
            'required': ['job_id'],
        },
    },
    'crawl_duplicates': {
        'description': (
            'Duplicate content: exact clusters grouped by body hash, plus near '
            'duplicates. The exact half is real; the near half needs data the '
            'worker does not ship yet.'
        ),
        'input': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer'},
                'limit': {'type': 'integer',
                          'description': 'Clusters to return. Default 25.'},
            },
            'required': ['job_id'],
        },
    },
    'crawl_hreflang': {
        'description': (
            'Hreflang audit: pages whose alternates do not return, 404, '
            'canonicalise elsewhere or disagree about language. NOTE: the '
            'worker does not ship hreflang yet, so this returns nothing.'
        ),
        'input': {
            'type': 'object',
            'properties': {'job_id': {'type': 'integer'},
                           'limit': {'type': 'integer'}},
            'required': ['job_id'],
        },
    },
    'crawl_structured': {
        'description': (
            'Structured data: which schema.org types the site declares and how '
            'many pages carry each. Validation errors against Google\'s '
            'rich-result requirements are not shipped by the worker yet, so '
            'the type breakdown is real and the error list is empty.'
        ),
        'input': {
            'type': 'object',
            'properties': {'job_id': {'type': 'integer'},
                           'limit': {'type': 'integer'}},
            'required': ['job_id'],
        },
    },
    'list_reports': {
        'description': 'Every named report crawl_report can run, and which of '
                       'them have data behind them today.',
        'input': {'type': 'object', 'properties': {}},
    },
    'crawl_report': {
        'description': (
            'Run a named report over a crawl and return its rows. Call '
            'list_reports for the names. Optionally narrowed to a segment.'
        ),
        'input': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer'},
                'report': {'type': 'string',
                           'description': 'Report name, e.g. titles-dupe.'},
                'limit': {'type': 'integer'},
                'segment': {'type': 'string'},
            },
            'required': ['job_id', 'report'],
        },
    },
    'compare_crawls': {
        'description': (
            'Diff two crawls of the same site: URLs added and removed, and '
            'pages whose status code or title changed between them.'
        ),
        'input': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer', 'description': 'The earlier crawl.'},
                'against': {'type': 'integer', 'description': 'The later crawl.'},
                'limit': {'type': 'integer',
                          'description': 'URLs listed per section. Default 50.'},
            },
            'required': ['job_id', 'against'],
        },
    },
    'crawl_segment': {
        'description': (
            'Named rule sets that bucket a crawl\'s URLs -- "the blog", "product '
            'pages". Create one, list them, delete one, or apply one to a crawl '
            'for its share and a sample. A segment name can then be passed to '
            'crawl_report, crawl_link_scores and generate_sitemap.'
        ),
        'input': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string',
                           'enum': ['create', 'apply', 'list', 'delete']},
                'name': {'type': 'string'},
                'description': {'type': 'string'},
                'job_id': {'type': 'integer',
                           'description': 'Required for apply.'},
                'match': {'type': 'string', 'enum': ['all', 'any'],
                          'description': 'Whether every rule must hold. Default all.'},
                'rules': {
                    'type': 'array',
                    'description': 'For create. Each rule is {field, op, value}.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'field': {'type': 'string',
                                      'description': 'A CrawlPage column, e.g. '
                                                     'url, status_code, word_count.'},
                            'op': {'type': 'string',
                                   'enum': ['contains', 'not_contains', 'equals',
                                            'not_equals', 'starts_with',
                                            'ends_with', 'matches_regex',
                                            'greater_than', 'less_than',
                                            'is_empty', 'is_not_empty']},
                            'value': {},
                        },
                        'required': ['field', 'op'],
                    },
                },
                'limit': {'type': 'integer'},
            },
        },
        'write': True,
    },
    'generate_sitemap': {
        'description': (
            'XML sitemap from a crawl: the URLs that answered 200 and are '
            'indexable, as a ready-to-upload sitemap.xml.'
        ),
        'input': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer'},
                'max_urls': {'type': 'integer',
                             'description': 'Default 1000, protocol max 50000.'},
                'include_non_indexable': {
                    'type': 'boolean',
                    'description': 'Fall back to every 200. Use on a crawl that '
                                   'predates on-page extraction.'},
                'segment': {'type': 'string'},
            },
            'required': ['job_id'],
        },
    },
}

HANDLERS = {
    'start_crawl': start_crawl,
    'crawl_status': crawl_status,
    'list_crawls': list_crawls,
    'cancel_crawl': cancel_crawl,
    'crawl_pages': crawl_pages,
    'crawl_issues': crawl_issues,
    'list_workers': list_workers,
    'crawl_link_scores': crawl_link_scores,
    'crawl_duplicates': crawl_duplicates,
    'crawl_hreflang': crawl_hreflang,
    'crawl_structured': crawl_structured,
    'list_reports': list_reports,
    'crawl_report': crawl_report,
    'compare_crawls': compare_crawls,
    'crawl_segment': crawl_segment,
    'generate_sitemap': generate_sitemap,
}

registry.register(Connector(
    slug='forager',
    label='Forager (site crawler)',
    auth='api_key',
    # No credentials: there is no third party here. The connection exists so the
    # tenant boundary and MCP key model work as they do everywhere else.
    cred_fields=[],
    catalog=CATALOG,
    handlers=HANDLERS,
    category='SEO',
    description=(
        'Crawl any website at full depth from your own hardware. Jobs run on a '
        'Forager worker machine you control and stream results back here, so a '
        'million-page crawl never touches the Honeycomb server.'
    ),
))
