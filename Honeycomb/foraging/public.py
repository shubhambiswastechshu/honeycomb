"""The public crawler: see crawls and start one, with no account.

A Screaming-Frog-style page for anyone with the URL. That is a deliberate choice
by the account holder, and the price of it is that every limit has to live here,
on the server, because there is no login in front of it to stop anybody.

What an anonymous caller can do, and what stops it becoming something worse:

* Start a crawl. The seed must be a public http(s) site on a standard port. The
  hostname is resolved and every address it resolves to must be globally
  routable -- the worker runs on a real machine inside a real network, and an
  open form that accepted http://192.168.1.1 would be a way to make that machine
  crawl its own LAN. Pages, speed and concurrency are capped, public crawls are
  capped at a few running at once and a daily total, and starting one is rate
  limited per client address.
* Watch crawls and read their results -- but only crawls started here. Jobs
  started from an AI client or the dashboard can be a client's site; the public
  page learns only how many of those are ahead in the queue, never their URLs.
* Cancel a crawl -- only with the token handed back when that crawl was started,
  so a stranger cannot stop somebody else's.

What this does not do: re-check addresses the worker meets mid-crawl. The worker
stays on the seed's own site, so a crawl cannot wander to an arbitrary internal
host, but a public domain whose DNS is changed to a private address between this
check and the fetch would get past it. Closing that needs the same check in the
worker itself.

Switched off unless HONEYCOMB_PUBLIC_CRAWL_TENANT names the organization whose
workers should run public crawls.
"""
import csv
import ipaddress
import socket
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.core import signing
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status as http
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Tenant

from .models import CrawlEvent, CrawlJob, CrawlPage, Worker

SOURCE = 'public'
CANCEL_SALT = 'foraging.public.cancel'
#: How long a cancel token stays valid. Long enough to outlive any crawl.
CANCEL_MAX_AGE = 7 * 24 * 3600

ACTIVE = (CrawlJob.Status.QUEUED, CrawlJob.Status.CLAIMED, CrawlJob.Status.RUNNING)

#: Speed limits applied to every public crawl, whatever the caller asks for.
#: Polite by default: a public form must not be a way to hammer somebody's site.
PUBLIC_RPS = 2.0
PUBLIC_CONCURRENCY = 4
PUBLIC_PER_HOST = 2
MAX_DEPTH = 10

#: The columns the results table shows. A deliberate subset of CrawlPage: enough
#: to read like a crawler's main grid, without shipping every analysis column.
PAGE_FIELDS = (
    'url', 'status_code', 'content_type', 'title', 'title_length',
    'meta_description_length', 'h1_1', 'word_count', 'indexability',
    'indexability_status', 'response_time_ms', 'size_bytes', 'depth',
    'inlinks', 'outlinks', 'canonical', 'final_url',
)

STATUS_BUCKETS = {
    '2xx': (200, 300),
    '3xx': (300, 400),
    '4xx': (400, 500),
    '5xx': (500, 600),
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _int_setting(name, default):
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def max_pages():
    return max(10, _int_setting('HONEYCOMB_PUBLIC_CRAWL_MAX_PAGES', 500))


def max_active():
    return max(1, _int_setting('HONEYCOMB_PUBLIC_CRAWL_MAX_ACTIVE', 3))


def daily_cap():
    return max(1, _int_setting('HONEYCOMB_PUBLIC_CRAWL_DAILY', 100))


def public_tenant():
    slug = (getattr(settings, 'HONEYCOMB_PUBLIC_CRAWL_TENANT', '') or '').strip()
    if not slug:
        return None
    return Tenant.objects.filter(slug=slug).first()


class PublicView(APIView):
    """No authentication, no CSRF, and a per-method rate limit.

    authentication_classes is empty on purpose, not just permissive: with the
    cookie authenticator in place, a signed-in visitor's session would attach to
    these requests and the throttle would key on their account instead of their
    address, which is not the limit this page is meant to have.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    write_scope = 'public_crawl'
    read_scope = 'public_crawl_read'

    def get_throttles(self):
        self.throttle_scope = (
            self.write_scope if self.request.method == 'POST' else self.read_scope)
        return super().get_throttles()

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.tenant = public_tenant()

    def disabled(self):
        return Response({'detail': 'The public crawler is switched off.'},
                        status=http.HTTP_404_NOT_FOUND)


# --------------------------------------------------------------------------- #
# The seed URL -- the one input that decides where the worker goes
# --------------------------------------------------------------------------- #
class SeedError(ValueError):
    pass


def resolve_addresses(host):
    """Every address a hostname resolves to. Split out so tests can stub DNS."""
    return {info[4][0] for info in socket.getaddrinfo(host, None)}


def validate_seed(raw):
    """Return a normalised, publicly routable http(s) URL, or raise SeedError."""
    raw = (raw or '').strip()
    if not raw:
        raise SeedError('Enter the address of a website to crawl.')
    if len(raw) > 500:
        raise SeedError('That address is too long.')
    if '://' not in raw:
        raw = 'https://' + raw

    parts = urlsplit(raw)
    if parts.scheme not in ('http', 'https'):
        raise SeedError('Only http and https websites can be crawled.')
    if parts.username or parts.password:
        raise SeedError('Remove the username and password from the address.')
    host = (parts.hostname or '').lower()
    if not host:
        raise SeedError('That is not a website address.')
    try:
        port = parts.port
    except ValueError:
        raise SeedError('That address has an invalid port.')
    if port not in (None, 80, 443):
        raise SeedError('Only websites on the standard ports (80 and 443) can be crawled.')

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
        if '.' not in host:
            raise SeedError('Enter a public domain name, like example.com.')
    if literal is not None:
        addresses = {str(literal)}
    else:
        try:
            addresses = resolve_addresses(host)
        except (socket.gaierror, UnicodeError):
            raise SeedError('{0} could not be found. Check the spelling.'.format(host))
        if not addresses:
            raise SeedError('{0} could not be found. Check the spelling.'.format(host))

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split('%', 1)[0])
        except ValueError:
            raise SeedError('{0} resolves to an address that cannot be checked.'.format(host))
        if not ip.is_global:
            # Loopback, private ranges, link-local, carrier-grade NAT, reserved:
            # everything a public form must never point a real machine at.
            raise SeedError('{0} points at a private or internal address, which '
                            'cannot be crawled from here.'.format(host))

    netloc = host if port is None else '{0}:{1}'.format(host, port)
    return urlunsplit((parts.scheme, netloc, parts.path or '/', parts.query, ''))


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #
def _job_payload(job, queue_position=None):
    config = job.config or {}
    payload = {
        'id': job.id,
        'seed_url': job.seed_url,
        'status': job.status,
        'max_pages': config.get('limit'),
        'pages_crawled': job.pages_crawled,
        'pages_queued': job.pages_queued,
        'urls_discovered': job.urls_discovered,
        'links_found': job.links_found,
        'failures': job.failures,
        'bytes_downloaded': job.bytes_downloaded,
        'status_counts': job.status_counts or {},
        'rate': round(job.rate, 2),
        'duration_seconds': round(job.duration_seconds, 1),
        'cancel_requested': job.cancel_requested,
        'created_at': job.created_at,
        'started_at': job.started_at,
        'finished_at': job.finished_at,
    }
    if queue_position is not None:
        payload['queue_position'] = queue_position
    return payload


def _queue_position(tenant, job):
    """How many jobs are ahead of a queued one, across every source.

    Counts private jobs too -- they genuinely are ahead -- without saying
    anything about them beyond the number.
    """
    if job.status != CrawlJob.Status.QUEUED:
        return None
    ahead_queued = CrawlJob.objects.filter(
        tenant=tenant, status=CrawlJob.Status.QUEUED, created_at__lt=job.created_at).count()
    in_progress = CrawlJob.objects.filter(
        tenant=tenant, status__in=(CrawlJob.Status.CLAIMED, CrawlJob.Status.RUNNING)).count()
    return ahead_queued + in_progress


def _public_job(tenant, job_id):
    return CrawlJob.objects.filter(tenant=tenant, source=SOURCE, pk=job_id).first()


def _overview(tenant):
    now = timezone.now()
    online = Worker.objects.filter(
        tenant=tenant, revoked_at__isnull=True,
        last_seen_at__gte=now - timedelta(seconds=Worker.OFFLINE_AFTER),
    ).count()
    private_active = CrawlJob.objects.filter(
        tenant=tenant, status__in=ACTIVE).exclude(source=SOURCE).count()
    public_active = CrawlJob.objects.filter(
        tenant=tenant, status__in=ACTIVE, source=SOURCE).count()
    return {
        'workers_online': online,
        'private_crawls_active': private_active,
        'public_crawls_active': public_active,
        'limits': {
            'max_pages': max_pages(),
            'max_active': max_active(),
            'daily': daily_cap(),
            'requests_per_second': PUBLIC_RPS,
        },
    }


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
class PublicJobs(PublicView):
    """GET: the overview plus recent public crawls. POST: start one."""

    def get(self, request):
        if self.tenant is None:
            return self.disabled()
        rows = CrawlJob.objects.filter(tenant=self.tenant, source=SOURCE)[:30]
        return Response({
            'overview': _overview(self.tenant),
            'jobs': [_job_payload(job, _queue_position(self.tenant, job)) for job in rows],
        })

    def post(self, request):
        if self.tenant is None:
            return self.disabled()
        data = request.data if isinstance(request.data, dict) else {}

        try:
            seed = validate_seed(data.get('url') or data.get('seed_url'))
        except SeedError as exc:
            return Response({'detail': str(exc)}, status=http.HTTP_400_BAD_REQUEST)

        active = CrawlJob.objects.filter(tenant=self.tenant, source=SOURCE, status__in=ACTIVE)

        # The same site already crawling publicly: send the caller to that crawl
        # rather than starting a second one against the same server.
        existing = active.filter(seed_url=seed).first()
        if existing is not None:
            return Response({
                'job': _job_payload(existing, _queue_position(self.tenant, existing)),
                'existing': True,
            }, status=http.HTTP_200_OK)

        if active.count() >= max_active():
            return Response({
                'detail': '{0} public crawls are already running. Try again in a few '
                          'minutes.'.format(max_active()),
            }, status=http.HTTP_429_TOO_MANY_REQUESTS)

        since = timezone.now() - timedelta(hours=24)
        if CrawlJob.objects.filter(tenant=self.tenant, source=SOURCE,
                                   created_at__gte=since).count() >= daily_cap():
            return Response({
                'detail': 'The public crawler has reached its limit for today. Try again tomorrow.',
            }, status=http.HTTP_429_TOO_MANY_REQUESTS)

        try:
            pages = int(data.get('max_pages') or 200)
        except (TypeError, ValueError):
            pages = 200
        pages = max(10, min(pages, max_pages()))
        config = {
            'limit': pages,
            'rps': PUBLIC_RPS,
            'concurrency': PUBLIC_CONCURRENCY,
            'per_host': PUBLIC_PER_HOST,
        }
        try:
            depth = int(data.get('depth')) if data.get('depth') not in (None, '') else None
        except (TypeError, ValueError):
            depth = None
        if depth is not None:
            config['depth'] = max(1, min(depth, MAX_DEPTH))

        job = CrawlJob.objects.create(
            tenant=self.tenant, seed_url=seed, config=config, source=SOURCE)
        return Response({
            'job': _job_payload(job, _queue_position(self.tenant, job)),
            # The only way to cancel this crawl later. Returned once; the page
            # keeps it in the browser that started the crawl.
            'cancel_token': signing.dumps({'job': job.id}, salt=CANCEL_SALT),
        }, status=http.HTTP_201_CREATED)


class PublicJobDetail(PublicView):
    """One public crawl, plus its console lines after ?since=<seq>."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return Response({'detail': 'No such public crawl.'}, status=http.HTTP_404_NOT_FOUND)
        try:
            since = int(request.GET.get('since', 0))
        except ValueError:
            since = 0
        events = CrawlEvent.objects.filter(job=job, seq__gt=since)[:400]
        return Response({
            'job': _job_payload(job, _queue_position(self.tenant, job)),
            'events': [{
                'seq': e.seq,
                'at': e.at,
                'level': e.level,
                'text': e.text,
                'status_code': e.status_code,
                'duration_ms': e.duration_ms,
            } for e in events],
        })


def _non_indexable(rows):
    """Pages known to be non-indexable.

    The worker writes "Indexable" with a capital I, so the match is
    case-insensitive. A blank value means analysis has not reached that page yet
    -- counting it as non-indexable made every page of a live crawl look broken.
    """
    return rows.exclude(indexability__iexact='indexable').exclude(indexability='')


def _filtered_pages(job, bucket, query):
    rows = CrawlPage.objects.filter(job=job)
    if bucket in STATUS_BUCKETS:
        low, high = STATUS_BUCKETS[bucket]
        rows = rows.filter(status_code__gte=low, status_code__lt=high)
    elif bucket == 'errors':
        rows = rows.filter(status_code__isnull=True)
    elif bucket == 'noindex':
        rows = _non_indexable(rows)
    if query:
        rows = rows.filter(url__icontains=query)
    return rows.order_by('id')


class PublicJobPages(PublicView):
    """The results grid: crawled pages, filterable by status and URL."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return Response({'detail': 'No such public crawl.'}, status=http.HTTP_404_NOT_FOUND)

        bucket = request.GET.get('status', 'all')
        query = (request.GET.get('q') or '').strip()[:200]
        try:
            offset = max(0, int(request.GET.get('offset', 0)))
            limit = max(1, min(200, int(request.GET.get('limit', 100))))
        except ValueError:
            offset, limit = 0, 100

        everything = CrawlPage.objects.filter(job=job)
        counts = {'all': everything.count()}
        for name, (low, high) in STATUS_BUCKETS.items():
            counts[name] = everything.filter(status_code__gte=low, status_code__lt=high).count()
        counts['errors'] = everything.filter(status_code__isnull=True).count()
        counts['noindex'] = _non_indexable(everything).count()

        rows = _filtered_pages(job, bucket, query)
        total = rows.count()
        page = list(rows.values(*PAGE_FIELDS)[offset:offset + limit])
        return Response({
            'counts': counts,
            'total': total,
            'offset': offset,
            'limit': limit,
            'results': page,
        })


class PublicJobExport(PublicView):
    """Every crawled page as CSV -- the export button every crawler has."""

    def get(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return Response({'detail': 'No such public crawl.'}, status=http.HTTP_404_NOT_FOUND)

        response = HttpResponse(content_type='text/csv; charset=utf-8')
        host = urlsplit(job.seed_url).hostname or 'crawl'
        response['Content-Disposition'] = 'attachment; filename="{0}-crawl-{1}.csv"'.format(
            host.replace('"', ''), job.id)
        writer = csv.writer(response)
        writer.writerow(PAGE_FIELDS)
        for row in CrawlPage.objects.filter(job=job).order_by('id').values_list(*PAGE_FIELDS):
            # A cell starting with = + - @ is executed as a formula by Excel and
            # Sheets. Crawled titles are attacker-controlled text, so neutralise.
            writer.writerow([
                "'" + v if isinstance(v, str) and v[:1] in ('=', '+', '-', '@') else v
                for v in row
            ])
        return response


class PublicJobCancel(PublicView):
    """Stop a public crawl -- only with the token its starter was given."""

    write_scope = 'public_crawl_read'

    def post(self, request, job_id):
        if self.tenant is None:
            return self.disabled()
        job = _public_job(self.tenant, job_id)
        if job is None:
            return Response({'detail': 'No such public crawl.'}, status=http.HTTP_404_NOT_FOUND)

        token = (request.data or {}).get('cancel_token') if isinstance(request.data, dict) else None
        try:
            claim = signing.loads(token or '', salt=CANCEL_SALT, max_age=CANCEL_MAX_AGE)
        except signing.BadSignature:
            claim = None
        if not isinstance(claim, dict) or claim.get('job') != job.id:
            return Response({'detail': 'Only the browser that started this crawl can stop it.'},
                            status=http.HTTP_403_FORBIDDEN)

        if job.status == CrawlJob.Status.QUEUED:
            job.status = CrawlJob.Status.CANCELLED
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'finished_at'])
        elif job.status in ACTIVE:
            job.cancel_requested = True
            job.save(update_fields=['cancel_requested'])
        return Response({'job': _job_payload(job)})
