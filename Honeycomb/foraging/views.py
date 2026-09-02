"""Two planes, both plain Django.

The worker plane (/api/forager/agent/**) is what the always-on PC talks to. It
is authenticated by a Forager worker token and by nothing else -- no session, no
cookie, so no CSRF surface.

The console plane (/api/forager/**) is what the dashboard reads, on the normal
tenant session.

Both deliberately short-poll rather than hold connections open. A long-poll or
an SSE stream parks a worker thread for its whole lifetime, and the entire
reason this app exists is that the Coolify box has little RAM to spare. A job
is picked up within a couple of seconds, and progress is pushed by the worker
the instant it happens, so nothing here is actually slower for the user.
"""
import gzip
import json

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CrawlEvent, CrawlJob, CrawlPage, Worker
from .serializers import CrawlJobSerializer, CrawlPageSerializer, WorkerSerializer

# The worker sends events in bulk; cap what one post can carry so a buggy or
# hostile worker cannot push an unbounded write into the request thread.
MAX_EVENTS_PER_POST = 500
MAX_PAGES_PER_POST = 1000

# A page's JSON columns are worker-supplied, and a worker that sends a string
# where a list belongs would otherwise store a shape every reader has to guard
# against. Coercing once at the door is cheaper than guarding at every read.
MAX_JSON_ITEMS_PER_PAGE = 50


def _as_list(value):
    if isinstance(value, list):
        return value[:MAX_JSON_ITEMS_PER_PAGE]
    if value in (None, ''):
        return []
    return [value]


class WorkerAuthMixin:
    """Resolve `Authorization: Bearer fw_...` to a live Worker.

    Returns None rather than raising so each view can answer with the same
    opaque 401: a caller holding a token should not be able to learn which
    tenant it belongs to by comparing error messages.
    """

    def worker_from(self, request):
        header = (request.META.get('HTTP_AUTHORIZATION') or '').strip()
        if not header.startswith('Bearer '):
            return None
        plain = header[7:].strip()
        if not plain.startswith(Worker.PREFIX):
            return None
        return (
            Worker.objects
            .select_related('tenant')
            .filter(token_hash=Worker.hash_token(plain), revoked_at__isnull=True)
            .first()
        )


DENIED = Response({'detail': 'Invalid or revoked worker token.'},
                  status=http.HTTP_401_UNAUTHORIZED)


# --------------------------------------------------------------------------- #
# Worker plane
# --------------------------------------------------------------------------- #
class AgentPoll(WorkerAuthMixin, APIView):
    """Heartbeat and claim in one round trip.

    The worker posts its health and gets back either a job to run or nothing.
    Combining them halves the request count and means a worker that is merely
    idle still proves it is alive.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        worker = self.worker_from(request)
        if worker is None:
            return DENIED

        body = request.data if isinstance(request.data, dict) else {}
        Worker.objects.filter(pk=worker.pk).update(
            last_seen_at=timezone.now(),
            version=str(body.get('version', ''))[:32],
            platform=str(body.get('platform', ''))[:64],
            rss_mb=body.get('rss_mb') or None,
            active_jobs=int(body.get('active_jobs') or 0),
        )
        worker.refresh_from_db()

        if worker.paused or int(body.get('active_jobs') or 0) > 0:
            return Response({'job': None, 'paused': worker.paused})

        job = self._claim(worker)
        if job is None:
            return Response({'job': None, 'paused': False})

        return Response({'job': {
            'id': job.id,
            'seed_url': job.seed_url,
            'config': job.config,
        }, 'paused': False})

    @staticmethod
    def _claim(worker):
        """Take the oldest queued job for this tenant, atomically.

        select_for_update(skip_locked) so two workers on the same tenant never
        take the same job and neither waits on the other. SQLite ignores
        skip_locked; the transaction still makes the claim safe there.
        """
        with transaction.atomic():
            job = (
                CrawlJob.objects
                .select_for_update(skip_locked=True)
                .filter(tenant=worker.tenant, status=CrawlJob.Status.QUEUED)
                .order_by('created_at')
                .first()
            )
            if job is None:
                return None
            job.status = CrawlJob.Status.CLAIMED
            job.worker = worker
            job.claimed_at = timezone.now()
            job.save(update_fields=['status', 'worker', 'claimed_at'])
        return job


class AgentProgress(WorkerAuthMixin, APIView):
    """Counters plus a batch of console lines. Called every second or so."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, job_id):
        worker = self.worker_from(request)
        if worker is None:
            return DENIED

        job = CrawlJob.objects.filter(pk=job_id, worker=worker).first()
        if job is None:
            return Response({'detail': 'No such job for this worker.'},
                            status=http.HTTP_404_NOT_FOUND)

        body = request.data if isinstance(request.data, dict) else {}
        counters = body.get('counters') or {}

        fields = {
            'status': CrawlJob.Status.RUNNING,
            'heartbeat_at': timezone.now(),
            'pages_crawled': int(counters.get('pages_crawled') or 0),
            'pages_queued': int(counters.get('pages_queued') or 0),
            'urls_discovered': int(counters.get('urls_discovered') or 0),
            'links_found': int(counters.get('links_found') or 0),
            'failures': int(counters.get('failures') or 0),
            'bytes_downloaded': int(counters.get('bytes_downloaded') or 0),
            'status_counts': counters.get('status_counts') or {},
        }
        if job.started_at is None:
            fields['started_at'] = timezone.now()
        if job.status in CrawlJob.TERMINAL:
            # A late progress post for a job already finished or cancelled must
            # not resurrect it.
            fields.pop('status')

        CrawlJob.objects.filter(pk=job.pk).update(**fields)
        self._append_events(job, body.get('events') or [])

        job.refresh_from_db(fields=['cancel_requested', 'status'])
        return Response({
            'cancel': job.cancel_requested or job.status == CrawlJob.Status.CANCELLED,
        })

    @staticmethod
    def _append_events(job, events):
        if not events:
            return
        base = CrawlEvent.objects.filter(job=job).aggregate(m=Max('seq'))['m'] or 0
        rows = []
        for i, e in enumerate(events[:MAX_EVENTS_PER_POST], start=1):
            if not isinstance(e, dict):
                continue
            rows.append(CrawlEvent(
                job=job,
                seq=base + i,
                level=e.get('level', CrawlEvent.Level.INFO)[:8],
                text=str(e.get('text', ''))[:500],
                status_code=e.get('status_code'),
                duration_ms=e.get('duration_ms'),
                size_bytes=e.get('size_bytes'),
            ))
        # ignore_conflicts because two progress posts racing on the same job
        # would otherwise collide on (job, seq); losing a duplicate line is
        # better than failing the whole batch.
        CrawlEvent.objects.bulk_create(rows, ignore_conflicts=True, batch_size=200)


class AgentPages(WorkerAuthMixin, APIView):
    """Crawled rows, gzipped NDJSON.

    NDJSON rather than a JSON array so the worker can stream rows out as it
    gets them, and gzipped because this is the only large payload in the
    protocol -- a page row is mostly URL, and URLs compress well.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = []

    def post(self, request, job_id):
        worker = self.worker_from(request)
        if worker is None:
            return DENIED

        job = CrawlJob.objects.filter(pk=job_id, worker=worker).first()
        if job is None:
            return Response({'detail': 'No such job for this worker.'},
                            status=http.HTTP_404_NOT_FOUND)

        raw = request.body
        if request.META.get('HTTP_CONTENT_ENCODING') == 'gzip':
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                return Response({'detail': 'Malformed gzip body.'},
                                status=http.HTTP_400_BAD_REQUEST)

        rows = []
        for line in raw.splitlines()[:MAX_PAGES_PER_POST]:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rows.append(CrawlPage(
                job=job,
                url=str(d.get('url', ''))[:2000],
                url_hash=bytes.fromhex(d.get('url_hash', '')) if d.get('url_hash') else b'',
                depth=int(d.get('depth') or 0),
                status_code=d.get('status_code'),
                content_type=str(d.get('content_type') or '')[:100],
                response_time_ms=d.get('response_time_ms'),
                size_bytes=int(d.get('size_bytes') or 0),
                redirect_count=int(d.get('redirect_count') or 0),
                final_url=str(d.get('final_url') or '')[:2000],
                discovered_via=str(d.get('discovered_via') or '')[:16],
                inlinks=int(d.get('inlinks') or 0),
                outlinks=int(d.get('outlinks') or 0),
                content_hash=(bytes.fromhex(d['content_hash'])
                              if d.get('content_hash') else None),
                error=str(d.get('error') or '')[:200],
                indexability=str(d.get('indexability') or '')[:16],
                indexability_status=str(d.get('indexability_status') or '')[:32],
                title=str(d.get('title') or '')[:500],
                title_length=int(d.get('title_length') or 0),
                title_pixel_width=int(d.get('title_pixel_width') or 0),
                meta_description=str(d.get('meta_description') or ''),
                meta_description_length=int(d.get('meta_description_length') or 0),
                meta_description_pixel_width=int(
                    d.get('meta_description_pixel_width') or 0),
                h1_1=str(d.get('h1_1') or '')[:500],
                h1_count=int(d.get('h1_count') or 0),
                h2_count=int(d.get('h2_count') or 0),
                canonical=str(d.get('canonical') or '')[:2000],
                meta_robots=str(d.get('meta_robots') or '')[:200],
                word_count=int(d.get('word_count') or 0),
                text_ratio=float(d.get('text_ratio') or 0),
                flesch_reading_ease=float(d.get('flesch_reading_ease') or 0),
                readability=str(d.get('readability') or '')[:24],
                language=str(d.get('language') or '')[:16],
                images=int(d.get('images') or 0),
                images_missing_alt=int(d.get('images_missing_alt') or 0),
                structured_data_types=str(d.get('structured_data_types') or '')[:200],
                issues=d.get('issues') or [],
                # Phase 3. Absent from every worker shipping today, which is
                # why each one falls back to the column default rather than
                # being required: an older worker must keep posting pages
                # against a newer server without a protocol version to check.
                link_score=float(d.get('link_score') or 0),
                near_duplicates=int(d.get('near_duplicates') or 0),
                closest_similarity=float(d.get('closest_similarity') or 0),
                hreflang_count=int(d.get('hreflang_count') or 0),
                hreflang_issues=_as_list(d.get('hreflang_issues')),
                structured_data_errors=int(d.get('structured_data_errors') or 0),
                structured_data_warnings=int(d.get('structured_data_warnings') or 0),
                structured_data_findings=_as_list(d.get('structured_data_findings')),
            ))

        # Upsert, not insert-or-ignore. The worker re-sends every row once its
        # post-crawl analysis has run, because Link Score, near-duplicates and
        # the validation counts are written onto rows that were shipped minutes
        # earlier and a forward-only upload cursor never revisits them. With
        # ignore_conflicts those columns would be posted, silently discarded,
        # and read as zero forever -- which looks exactly like a crawler that
        # cannot compute them.
        CrawlPage.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=['job', 'url_hash'],
            update_fields=[
                f.name for f in CrawlPage._meta.get_fields()
                if getattr(f, 'concrete', False)
                and f.name not in ('id', 'job', 'url_hash')
            ],
            batch_size=500,
        )
        return Response({'stored': len(rows)})


class AgentComplete(WorkerAuthMixin, APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, job_id):
        worker = self.worker_from(request)
        if worker is None:
            return DENIED

        job = CrawlJob.objects.filter(pk=job_id, worker=worker).first()
        if job is None:
            return Response({'detail': 'No such job for this worker.'},
                            status=http.HTTP_404_NOT_FOUND)

        body = request.data if isinstance(request.data, dict) else {}
        outcome = body.get('status', CrawlJob.Status.DONE)
        if outcome not in dict(CrawlJob.Status.choices):
            outcome = CrawlJob.Status.DONE

        counters = body.get('counters') or {}
        CrawlJob.objects.filter(pk=job.pk).update(
            status=outcome,
            finished_at=timezone.now(),
            error=str(body.get('error') or '')[:2000],
            pages_crawled=int(counters.get('pages_crawled') or job.pages_crawled),
            pages_queued=int(counters.get('pages_queued') or 0),
            urls_discovered=int(counters.get('urls_discovered') or job.urls_discovered),
            links_found=int(counters.get('links_found') or job.links_found),
            failures=int(counters.get('failures') or job.failures),
            bytes_downloaded=int(counters.get('bytes_downloaded') or job.bytes_downloaded),
            status_counts=counters.get('status_counts') or job.status_counts,
        )
        AgentProgress._append_events(job, body.get('events') or [])
        return Response({'ok': True})


# --------------------------------------------------------------------------- #
# Console plane
# --------------------------------------------------------------------------- #
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def jobs(request):
    if request.method == 'POST':
        seed = (request.data.get('seed_url') or '').strip()
        if not seed.startswith(('http://', 'https://')):
            return Response({'detail': 'seed_url must be an http(s) URL.'},
                            status=http.HTTP_400_BAD_REQUEST)
        job = CrawlJob.objects.create(
            tenant=request.user.tenant,
            created_by=request.user,
            seed_url=seed[:500],
            config=request.data.get('config') or {},
            source='ui',
        )
        return Response(CrawlJobSerializer(job).data, status=http.HTTP_201_CREATED)

    rows = CrawlJob.objects.filter(tenant=request.user.tenant)[:50]
    return Response(CrawlJobSerializer(rows, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def job_detail(request, job_id):
    job = CrawlJob.objects.filter(tenant=request.user.tenant, pk=job_id).first()
    if job is None:
        return Response(status=http.HTTP_404_NOT_FOUND)
    return Response(CrawlJobSerializer(job).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_job(request, job_id):
    """Ask the worker to stop. It reads this on its next progress post.

    Nothing here can reach the worker, so this sets a flag rather than killing
    anything. A queued job that no worker has claimed is cancelled outright.
    """
    job = CrawlJob.objects.filter(tenant=request.user.tenant, pk=job_id).first()
    if job is None:
        return Response(status=http.HTTP_404_NOT_FOUND)
    if job.status == CrawlJob.Status.QUEUED:
        job.status = CrawlJob.Status.CANCELLED
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'finished_at'])
    else:
        job.cancel_requested = True
        job.save(update_fields=['cancel_requested'])
    return Response(CrawlJobSerializer(job).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def job_events(request, job_id):
    """Console lines after ?since=<seq>.

    The client passes back the highest seq it has, so the stream is resumable
    and never duplicates or skips a line -- which a timestamp cursor cannot
    promise once two events share a millisecond.
    """
    job = CrawlJob.objects.filter(tenant=request.user.tenant, pk=job_id).first()
    if job is None:
        return Response(status=http.HTTP_404_NOT_FOUND)

    try:
        since = int(request.GET.get('since', 0))
    except ValueError:
        since = 0

    rows = list(CrawlEvent.objects.filter(job=job, seq__gt=since)[:400])
    return Response({
        'events': [{
            'seq': e.seq,
            'at': e.at.isoformat(),
            'level': e.level,
            'text': e.text,
            'status_code': e.status_code,
            'duration_ms': e.duration_ms,
            'size_bytes': e.size_bytes,
        } for e in rows],
        'job': CrawlJobSerializer(job).data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def job_pages(request, job_id):
    job = CrawlJob.objects.filter(tenant=request.user.tenant, pk=job_id).first()
    if job is None:
        return Response(status=http.HTTP_404_NOT_FOUND)
    rows = CrawlPage.objects.filter(job=job).order_by('-id')[:200]
    return Response(CrawlPageSerializer(rows, many=True).data)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def workers(request):
    if request.method == 'POST':
        name = (request.data.get('name') or 'Workstation').strip()
        row, plain = Worker.mint(request.user.tenant, name, request.user)
        data = WorkerSerializer(row).data
        # The only time the plaintext ever exists in a response.
        data['token'] = plain
        return Response(data, status=http.HTTP_201_CREATED)

    rows = Worker.objects.filter(tenant=request.user.tenant, revoked_at__isnull=True)
    return Response(WorkerSerializer(rows, many=True).data)


# --------------------------------------------------------------------------- #
# The console page itself
# --------------------------------------------------------------------------- #
def console(request, job_id=None):
    """Serve the terminal UI.

    A Django template rather than a Next.js route on purpose: this page has to
    work when it is the only thing running -- during setup, or when somebody is
    SSHed in wondering why a crawl stalled -- and a server-rendered page with no
    build step always does.
    """
    from django.shortcuts import render
    from django.urls import reverse

    return render(request, 'foraging/console.html', {
        'api_base': reverse('foraging:jobs'),
        'job_id': job_id if job_id is not None else 'null',
    })
