"""Forager: crawl jobs dispatched to a machine that is not this server.

The whole point of this app is that Honeycomb never crawls. A Forager worker --
an always-on PC in the office, not the Coolify box -- claims jobs, does the
memory-hungry work on its own hardware, and streams rows back. The server holds
the queue, the log and the results; it never holds a page.

That inverts the usual direction of control. The worker sits behind NAT with no
stable address, so it dials out and polls; nothing here ever opens a connection
to it. If the machine is off, jobs simply wait in `queued` and the web app is
unaffected.
"""
import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from accounts.models import TenantOwnedModel


class Worker(TenantOwnedModel):
    """A registered machine allowed to claim crawl jobs.

    Authenticated the same way as McpKey: only the SHA-256 of the token is
    stored, the plaintext exists once in the response that mints it, and a lost
    token is re-minted rather than recovered.
    """

    PREFIX = 'fw_'

    class Status(models.TextChoices):
        ONLINE = 'online', 'Online'
        OFFLINE = 'offline', 'Offline'
        PAUSED = 'paused', 'Paused'

    name = models.CharField(max_length=80)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='forager_workers',
    )
    token_prefix = models.CharField(max_length=16, db_index=True)
    token_hash = models.CharField(max_length=64, db_index=True)

    version = models.CharField(max_length=32, blank=True)
    platform = models.CharField(max_length=64, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    # Reported by the worker on each heartbeat, so the dashboard can show that
    # the machine is healthy without this server measuring anything.
    rss_mb = models.FloatField(null=True, blank=True)
    active_jobs = models.IntegerField(default=0)
    paused = models.BooleanField(
        default=False,
        help_text='Set from the dashboard. The worker reads it on its next '
                  'poll and stops claiming new jobs; running jobs finish.',
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-last_seen_at', 'name']

    def __str__(self):
        return self.name

    # A worker that has not checked in for this long is presumed off. It polls
    # every few seconds, so a minute is many missed beats, not a slow network.
    OFFLINE_AFTER = 60

    @property
    def status(self):
        if self.paused:
            return self.Status.PAUSED
        if not self.last_seen_at:
            return self.Status.OFFLINE
        age = (timezone.now() - self.last_seen_at).total_seconds()
        return self.Status.ONLINE if age < self.OFFLINE_AFTER else self.Status.OFFLINE

    @property
    def is_online(self):
        return self.status == self.Status.ONLINE

    @staticmethod
    def hash_token(plain):
        return hashlib.sha256(plain.encode()).hexdigest()

    @classmethod
    def mint(cls, tenant, name, created_by=None):
        plain = cls.PREFIX + secrets.token_urlsafe(32)
        row = cls.objects.create(
            tenant=tenant, name=name[:80], created_by=created_by,
            token_prefix=plain[:len(cls.PREFIX) + 4],
            token_hash=cls.hash_token(plain),
        )
        return row, plain


class CrawlJob(TenantOwnedModel):
    """One crawl: what to fetch, who is fetching it, and how far it has got.

    Counters live on the row and are overwritten by each worker progress post
    rather than derived from CrawlPage. Counting three million rows to render a
    dashboard is exactly the kind of work this architecture exists to keep off
    the server.
    """

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        CLAIMED = 'claimed', 'Claimed'
        RUNNING = 'running', 'Running'
        DONE = 'done', 'Done'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    TERMINAL = (Status.DONE, Status.FAILED, Status.CANCELLED)

    seed_url = models.URLField(max_length=500)
    config = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)

    worker = models.ForeignKey(
        Worker, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='jobs',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='forager_jobs',
    )
    # Set when an MCP tool created the job, so the dashboard can show that an
    # AI client started this rather than a person.
    source = models.CharField(max_length=16, default='ui')

    claimed_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    pages_crawled = models.IntegerField(default=0)
    pages_queued = models.IntegerField(default=0)
    urls_discovered = models.IntegerField(default=0)
    links_found = models.IntegerField(default=0)
    failures = models.IntegerField(default=0)
    bytes_downloaded = models.BigIntegerField(default=0)
    status_counts = models.JSONField(default=dict, blank=True)

    # Requested from the dashboard or an MCP tool; the worker reads it on its
    # next progress post and stops. Nothing here can reach into the worker.
    cancel_requested = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'status', '-created_at'],
                         name='forage_job_tenant_idx'),
            models.Index(fields=['status', 'created_at'], name='forage_job_queue_idx'),
        ]

    def __str__(self):
        return '{0} ({1})'.format(self.seed_url, self.status)

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL

    @property
    def duration_seconds(self):
        if not self.started_at:
            return 0
        end = self.finished_at or timezone.now()
        return (end - self.started_at).total_seconds()

    @property
    def rate(self):
        seconds = self.duration_seconds
        return self.pages_crawled / seconds if seconds > 0 else 0.0


class CrawlEvent(models.Model):
    """A line in the live console.

    Append-only and deliberately not tenant-scoped through TenantOwnedModel:
    it is reached only through its job, which is, and this table takes one row
    per log line. Keeping it thin matters more than a redundant foreign key.
    """

    class Level(models.TextChoices):
        INFO = 'info', 'Info'
        FETCH = 'fetch', 'Fetch'
        WARN = 'warn', 'Warn'
        ERROR = 'error', 'Error'
        DONE = 'done', 'Done'

    job = models.ForeignKey(CrawlJob, on_delete=models.CASCADE, related_name='events')
    seq = models.BigIntegerField(
        help_text='Monotonic per job. The console polls with ?since=<seq>, so '
                  'this is what makes the stream resumable and gap-free.',
    )
    at = models.DateTimeField(default=timezone.now)
    level = models.CharField(max_length=8, choices=Level.choices, default=Level.INFO)
    text = models.CharField(max_length=500)
    # Optional structured detail for fetch lines: status, ms, bytes.
    status_code = models.IntegerField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    size_bytes = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['seq']
        indexes = [models.Index(fields=['job', 'seq'], name='forage_event_job_seq_idx')]
        constraints = [
            models.UniqueConstraint(fields=['job', 'seq'], name='forage_event_unique_seq'),
        ]


class CrawlPage(models.Model):
    """One crawled URL, as streamed back by the worker.

    Rows, never pages: the HTML stays on the worker unless somebody explicitly
    asks for it. This is the table Phase 2's ~70 on-page columns will extend;
    Phase 1 fills the response half.
    """

    job = models.ForeignKey(CrawlJob, on_delete=models.CASCADE, related_name='pages')
    url = models.URLField(max_length=2000)
    # blake2b-128 of the normalized URL, as computed on the worker. Indexed
    # instead of the URL because a 2000-character index is a poor one.
    url_hash = models.BinaryField(max_length=16, db_index=True)
    depth = models.IntegerField(default=0)
    status_code = models.IntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    response_time_ms = models.IntegerField(null=True, blank=True)
    size_bytes = models.IntegerField(default=0)
    redirect_count = models.IntegerField(default=0)
    final_url = models.URLField(max_length=2000, blank=True)
    discovered_via = models.CharField(max_length=16, blank=True)
    inlinks = models.IntegerField(default=0)
    outlinks = models.IntegerField(default=0)
    content_hash = models.BinaryField(max_length=16, null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)

    # --- Phase 2: Screaming Frog's Internal tab -------------------------
    # A subset, not all seventy. These are the columns the reports and the
    # filters actually read; the rest stay on the worker, which is where the
    # full crawl database lives anyway.
    indexability = models.CharField(max_length=16, blank=True)
    indexability_status = models.CharField(max_length=32, blank=True)
    title = models.CharField(max_length=500, blank=True)
    title_length = models.IntegerField(default=0)
    title_pixel_width = models.IntegerField(default=0)
    meta_description = models.TextField(blank=True)
    meta_description_length = models.IntegerField(default=0)
    meta_description_pixel_width = models.IntegerField(default=0)
    h1_1 = models.CharField(max_length=500, blank=True)
    h1_count = models.IntegerField(default=0)
    h2_count = models.IntegerField(default=0)
    canonical = models.CharField(max_length=2000, blank=True)
    meta_robots = models.CharField(max_length=200, blank=True)
    word_count = models.IntegerField(default=0)
    text_ratio = models.FloatField(default=0)
    flesch_reading_ease = models.FloatField(default=0)
    readability = models.CharField(max_length=24, blank=True)
    language = models.CharField(max_length=16, blank=True)
    images = models.IntegerField(default=0)
    images_missing_alt = models.IntegerField(default=0)
    structured_data_types = models.CharField(max_length=200, blank=True)
    # Issue codes for this page, as a list. Stored on the row rather than in a
    # side table: they are always read with the page and never on their own,
    # and a join per page would be the most expensive query in the console.
    issues = models.JSONField(default=list, blank=True)

    # --- Phase 3: the analyses that only make sense once a crawl is whole ---
    # Every one of these is a summary the worker computed, never the working
    # set it computed from. Link Score is an eigenvector over the site's whole
    # link graph and near-duplicate detection is a MinHash index over every
    # page's shingles; both of those structures are gigabytes on a large crawl
    # and both belong on the machine that already holds them. What travels is
    # the one number per page a report actually prints, which is the same
    # bargain the rest of this table makes.
    #
    # Nothing populates these yet: the worker's page payload stops at the
    # on-page columns above, so a fresh crawl leaves them at their defaults.
    # They are declared here anyway because the column is the contract -- the
    # ingest below already reads them, so shipping them becomes a change to
    # the worker alone rather than a migration on a live database.
    link_score = models.FloatField(
        default=0,
        help_text='Internal PageRank scaled 0-100. Zero means not computed, '
                  'not unlinked -- an orphan scores above zero.',
    )
    near_duplicates = models.IntegerField(default=0)
    closest_similarity = models.FloatField(
        default=0,
        help_text='Jaccard estimate against the nearest other page, 0-1.',
    )
    hreflang_count = models.IntegerField(default=0)
    # Audit findings for this page's cluster, as codes. A finding is about a
    # pair of pages, and it is stored on the page that declared the annotation
    # because that is the page somebody has to edit to fix it.
    hreflang_issues = models.JSONField(default=list, blank=True)
    structured_data_errors = models.IntegerField(default=0)
    structured_data_warnings = models.IntegerField(default=0)
    # The validator's messages, already truncated on the worker. A generated
    # feed can produce thousands per page and the row must stay a row.
    structured_data_findings = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['job', 'status_code'], name='forage_page_job_status_idx'),
            models.Index(fields=['job', '-size_bytes'], name='forage_page_job_size_idx'),
            models.Index(fields=['job', 'indexability'], name='forage_page_job_index_idx'),
            models.Index(fields=['job', '-link_score'], name='forage_page_job_score_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['job', 'url_hash'],
                                    name='forage_page_unique_url'),
        ]

    def __str__(self):
        return self.url


class CrawlSegment(TenantOwnedModel):
    """A named rule set that buckets a crawl's URLs.

    Rules rather than a list of URLs, because a crawl is re-run: a segment
    written once still means "the blog" after the next crawl finds two hundred
    new posts. The rules are stored as JSON and compiled to a queryset when
    they are applied, which keeps the filtering in the database instead of
    pulling a million rows into the web process to test a predicate.

    Tenant-scoped and not job-scoped on purpose. A segment outlives the crawl
    it was first written against, and being able to apply the same one to two
    crawls is what makes it worth naming at all.
    """

    class Match(models.TextChoices):
        ALL = 'all', 'All rules'
        ANY = 'any', 'Any rule'

    name = models.CharField(max_length=80)
    description = models.CharField(max_length=200, blank=True)
    rules = models.JSONField(
        default=list,
        help_text='List of {field, op, value}. Validated when applied, since '
                  'the column set a rule may name belongs to CrawlPage and '
                  'not to this row.',
    )
    match = models.CharField(max_length=3, choices=Match.choices, default=Match.ALL)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='forager_segments',
    )

    class Meta:
        ordering = ['name']
        constraints = [
            # The name is the key a caller applies a segment by, so two
            # segments that differ only in name casing would make "apply blog"
            # ambiguous in a way nobody would think to look for.
            models.UniqueConstraint(fields=['tenant', 'name'],
                                    name='forage_segment_unique_name'),
        ]

    def __str__(self):
        return self.name
