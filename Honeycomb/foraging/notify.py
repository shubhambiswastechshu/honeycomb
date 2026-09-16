"""Telling someone their crawl is over.

A crawl of a real site runs for minutes or hours, and nobody watches a progress
bar for that long -- they start it and go away. The email is what brings them
back, so it carries the numbers they would have come to read.

Only crawls with an owner are mailed. A public crawl belongs to whoever opened
the page (there is no account to write to), and a crawl started by an AI client
records no user either -- the client is already holding the answer.
"""
import logging

from django.conf import settings

from notifications import mail

logger = logging.getLogger(__name__)

#: Outcomes worth an email. A cancelled crawl was stopped by a person who was
#: therefore already looking at it.
NOTIFY_ON = ('done', 'failed')


def _duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return '{0} seconds'.format(seconds)
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return '{0} min {1} s'.format(minutes, rest)
    hours, minutes = divmod(minutes, 60)
    return '{0} h {1} min'.format(hours, minutes)


def crawl_finished(job):
    """Email the person who started `job` that it is over. Never raises."""
    if job.status not in NOTIFY_ON or not job.created_by_id:
        return False
    user = job.created_by
    if not user.is_active or not user.email:
        return False

    host = job.seed_url
    for prefix in ('https://', 'http://'):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    host = host.split('/', 1)[0]
    failed = job.status == 'failed'

    # The crawl opens in the Forager console, which is served by this API rather
    # than by the dashboard, so this link is built from the API's own base.
    console = (getattr(settings, 'HONEYCOMB_PUBLIC_BASE', '') or '').rstrip('/')

    return mail.send(
        'crawl_finished',
        user.email,
        '{0}: your crawl of {1}'.format('Stopped' if failed else 'Finished', host),
        context={
            'host': host,
            'seed_url': job.seed_url,
            'failed': failed,
            'pages': '{0:,}'.format(job.pages_crawled or 0),
            'links': '{0:,}'.format(job.links_found or 0),
            'failures': '{0:,}'.format(job.failures or 0),
            'duration': _duration(job.duration_seconds),
            'error': (job.error or '')[:300],
            'crawl_url': '{0}/forager/{1}/'.format(console, job.id),
        },
        tenant=job.tenant,
        user=user,
    )
