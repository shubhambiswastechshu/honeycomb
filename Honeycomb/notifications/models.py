"""A record of every email this app tried to send.

Transactional email fails quietly by nature: the provider accepts it and the
person never mentions the invite that did not arrive. One row per attempt --
including the ones refused before a connection was opened -- is what turns
"did it go out?" into a question with an answer.

Bodies are deliberately not stored. A password-reset link in a table is a
password-reset link an admin can use, and the subject plus the template name
already identify which message this was.
"""
from django.conf import settings
from django.db import models

from accounts.models import Tenant


class EmailLog(models.Model):
    SENT = 'sent'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    STATUS_CHOICES = (
        (SENT, 'Sent'),
        (FAILED, 'Failed'),
        (SKIPPED, 'Skipped'),
    )

    to_email = models.EmailField(max_length=254)
    subject = models.CharField(max_length=200)
    template = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=SENT, db_index=True)
    error = models.CharField(max_length=500, blank=True)
    # Both optional: a password reset is sent to an address before anyone is
    # signed in, and a platform-level message belongs to no organization.
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name='emails')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='emails')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)
        verbose_name = 'email'
        verbose_name_plural = 'emails'

    def __str__(self):
        return '{0} to {1} ({2})'.format(self.template, self.to_email, self.status)
