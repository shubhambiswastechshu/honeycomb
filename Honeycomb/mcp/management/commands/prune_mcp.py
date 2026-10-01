"""Delete MCP rows nothing can use or read any more.

Nothing else ever deletes from these tables: activity is append-only, every
OAuth refresh adds a token row, every authorization adds a grant, and
registration is open to anyone. Run at container start (see the Dockerfile)
and safe to run at any time -- every rule below only touches rows that can no
longer authenticate or be shown.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from mcp.models import McpActivity, OAuthClient, OAuthGrant, OAuthToken

#: The dashboard's longest window is 371 days; keep a little more than that.
ACTIVITY_DAYS = 400
#: Revoked or dead tokens are kept this long so an owner can still see them.
TOKEN_GRACE_DAYS = 30
#: A registered client that never obtained a token is dropped after this.
UNUSED_CLIENT_DAYS = 7


class Command(BaseCommand):
    help = 'Delete expired OAuth grants and tokens, unused OAuth clients and old activity.'

    def handle(self, *args, **options):
        now = timezone.now()
        grace = now - timedelta(days=TOKEN_GRACE_DAYS)
        family_dead = now - timedelta(seconds=OAuthToken.FAMILY_LIFETIME_SECONDS) - timedelta(
            days=TOKEN_GRACE_DAYS)

        grants, _ = OAuthGrant.objects.filter(
            created_at__lt=now - timedelta(seconds=OAuthGrant.LIFETIME_SECONDS) - timedelta(days=1),
        ).delete()
        tokens, _ = OAuthToken.objects.filter(
            Q(revoked_at__lt=grace)
            | Q(family_started_at__lt=family_dead)
            | Q(family_started_at__isnull=True, created_at__lt=family_dead)
        ).delete()
        clients, _ = OAuthClient.objects.filter(
            created_at__lt=now - timedelta(days=UNUSED_CLIENT_DAYS),
            tokens__isnull=True, grants__isnull=True,
        ).delete()
        activity, _ = McpActivity.objects.filter(
            created_at__lt=now - timedelta(days=ACTIVITY_DAYS),
        ).delete()
        self.stdout.write('prune_mcp: grants={0} tokens={1} clients={2} activity={3}'.format(
            grants, tokens, clients, activity))
