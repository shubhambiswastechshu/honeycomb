"""Sending one transactional email, and never failing a request because of it.

Every message goes out through `send()`: it renders a text and an HTML body
from the same context, records the attempt in EmailLog, and returns True or
False rather than raising. That last part is the point. A password change that
500s because an SMTP host was slow is a worse outcome than a change that
succeeds while its confirmation email is lost, and every caller here is doing
something the user asked for first and notifying second.

Mail is sent inline, in the request. That is honest for this volume -- a
handful of messages a day, with EMAIL_TIMEOUT capping the wait -- and the
moment it stops being true the fix is a queue behind this one function, not a
change at any call site.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from .models import EmailLog

logger = logging.getLogger(__name__)


def app_url(path=''):
    """An absolute URL into the dashboard, for links inside an email.

    The frontend is a different origin from this API, so a link built from the
    request would point at the API and 404. HONEYCOMB_APP_BASE is the one
    place that knows where people actually browse.
    """
    base = (getattr(settings, 'HONEYCOMB_APP_BASE', '') or '').rstrip('/')
    if not path:
        return base
    return '{0}/{1}'.format(base, str(path).lstrip('/'))


def send(template, to, subject, context=None, tenant=None, user=None):
    """Render `template` and send it to one address. True when it went out.

    `template` names a pair under notifications/templates/notifications/:
    <template>.txt is the message, <template>.html the same thing styled.
    Both are always sent -- plain text is what a mail client falls back to and
    what a spam filter reads.
    """
    address = str(to or '').strip()
    row = EmailLog(
        to_email=address[:254],
        subject=str(subject)[:200],
        template=str(template)[:64],
        tenant=tenant,
        user=user,
    )

    if not address:
        row.status, row.error = EmailLog.SKIPPED, 'No address to send to.'
        row.save()
        return False

    if not getattr(settings, 'HONEYCOMB_EMAIL_ENABLED', False):
        # Not an error: a deployment without EMAIL_HOST has deliberately not
        # turned email on. Recorded so the missing message is explainable.
        row.status = EmailLog.SKIPPED
        row.error = 'Email is not configured on this server (EMAIL_HOST is unset).'
        row.save()
        logger.warning('Email not configured; skipped %s to %s', template, address)
        return False

    payload = dict(context or {})
    payload.setdefault('subject', subject)
    payload.setdefault('app_base', app_url())
    payload.setdefault('product', 'Honeycomb')

    try:
        message = EmailMultiAlternatives(
            subject=str(subject),
            body=render_to_string('notifications/{0}.txt'.format(template), payload),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[address],
        )
        message.attach_alternative(
            render_to_string('notifications/{0}.html'.format(template), payload), 'text/html')
        message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 -- a lost email never fails the request
        row.status = EmailLog.FAILED
        row.error = '{0}: {1}'.format(type(exc).__name__, exc)[:500]
        row.save()
        logger.exception('Email %s to %s failed', template, address)
        return False

    row.status = EmailLog.SENT
    row.save()
    return True
