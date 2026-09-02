"""Requeue jobs whose worker went away.

A worker claims a job and then posts progress every second. If the machine is
unplugged, the process is killed, or the network drops mid-crawl, nothing ever
posts again and the job sits in `running` forever -- no other worker will touch
it, because claiming only ever looks at `queued`.

Deliberately a management command rather than a Celery beat task. Celery would
mean Redis plus a worker process on the Coolify box, which is precisely the RAM
this whole architecture exists to avoid spending. This is one query on a timer:

    */5 * * * *  python manage.py forager_sweep

On Render or Coolify, a scheduled job. On a bare box, cron.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from foraging.models import CrawlJob

# A worker heartbeats every second. Three minutes of silence is 180 missed
# beats -- not a slow network, a machine that is gone.
STALE_AFTER = timedelta(minutes=3)
# A claim that never became a running job: the worker died between claiming and
# its first progress post.
CLAIM_TIMEOUT = timedelta(minutes=2)


class Command(BaseCommand):
    help = 'Requeue crawl jobs whose worker stopped reporting.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        now = timezone.now()
        dry = options['dry_run']

        stalled = CrawlJob.objects.filter(
            status=CrawlJob.Status.RUNNING,
            heartbeat_at__lt=now - STALE_AFTER,
        )
        unclaimed = CrawlJob.objects.filter(
            status=CrawlJob.Status.CLAIMED,
            claimed_at__lt=now - CLAIM_TIMEOUT,
        )

        for label, queryset in (('running', stalled), ('claimed', unclaimed)):
            for job in queryset:
                self.stdout.write(
                    '{0} job {1} ({2}) last seen {3} -- requeueing'.format(
                        'would requeue' if dry else 'requeued', job.id, label,
                        job.heartbeat_at or job.claimed_at))
            if not dry:
                # Back to queued, not failed: the crawl is resumable on the
                # worker's own disk, and pages already streamed back are kept.
                # Clearing the worker lets any machine on the tenant pick it up.
                queryset.update(status=CrawlJob.Status.QUEUED, worker=None,
                                claimed_at=None)

        total = stalled.count() + unclaimed.count() if dry else 0
        if dry:
            self.stdout.write('{0} job(s) would be requeued.'.format(total))
