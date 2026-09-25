"""Tests for the tenant-wide activity reads the dashboard polls.

The live endpoint is the one worth pinning down: the panel that stays open
beside every page reads it every few seconds, and everything it shows is a
count. A wrong bucket or a leaked neighbour row would be a wrong number that
looks exactly like a right one.
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Tenant, User

from .models import McpActivity
from .serializers import MAX_SUMMARY_DAYS


def make_user(tenant, email):
    return User.objects.create_user(email=email, password='pw-for-tests-only', tenant=tenant)


def record(tenant, connector, tool, status='ok', duration_ms=None, ago=timedelta(0)):
    """One activity row, back-dated by `ago`.

    created_at is auto_now_add, so it cannot be passed to create(); the row is
    moved afterwards with a queryset update, which skips the auto behaviour.
    """
    row = McpActivity.objects.create(
        tenant=tenant, connector=connector, tool_name=tool,
        status=status, duration_ms=duration_ms,
    )
    McpActivity.objects.filter(pk=row.pk).update(created_at=timezone.now() - ago)
    return row


class ActivityLiveTests(TestCase):
    def setUp(self):
        cache.clear()  # throttle counters live in the cache
        self.tenant = Tenant.objects.create(name='Acme')
        self.other = Tenant.objects.create(name='Rival')
        self.client = APIClient()
        self.client.force_authenticate(make_user(self.tenant, 'me@acme.test'))
        self.url = reverse('mcp:activity-live')

    def test_requires_authentication(self):
        anonymous = APIClient()
        self.assertIn(anonymous.get(self.url).status_code, (401, 403))

    def test_empty_tenant_is_zeros_not_invented_traffic(self):
        data = self.client.get(self.url).json()
        self.assertEqual(data['calls'], 0)
        self.assertEqual(data['errors'], 0)
        self.assertIsNone(data['avg_ms'])
        self.assertIsNone(data['last_call_at'])
        self.assertEqual(len(data['hours']), 24)
        self.assertTrue(all(h['ok'] == 0 and h['error'] == 0 for h in data['hours']))
        self.assertEqual(data['connectors'], [])
        self.assertEqual(data['tools'], [])
        self.assertEqual(data['recent'], [])

    def test_counts_are_summed_from_the_same_rows_the_chart_draws(self):
        record(self.tenant, 'shopify', 'list_orders', 'ok', 100)
        record(self.tenant, 'shopify', 'list_orders', 'ok', 300)
        record(self.tenant, 'shopify', 'get_order', 'error', None)
        record(self.tenant, 'stripe', 'list_charges', 'ok', 200)

        data = self.client.get(self.url).json()

        self.assertEqual(data['calls'], 4)
        self.assertEqual(data['errors'], 1)
        # Mean of the three TIMED calls (the errored one has no duration).
        self.assertEqual(data['avg_ms'], 200)
        self.assertEqual(sum(h['ok'] + h['error'] for h in data['hours']), data['calls'])
        self.assertEqual(sum(h['error'] for h in data['hours']), data['errors'])
        # Everything happened just now, so it is all in the newest bucket.
        self.assertEqual(data['hours'][-1]['ok'] + data['hours'][-1]['error'], 4)

    def test_connectors_and_tools_are_ranked_by_calls(self):
        for _ in range(3):
            record(self.tenant, 'shopify', 'list_orders', 'ok', 50)
        record(self.tenant, 'stripe', 'list_charges', 'error')

        data = self.client.get(self.url).json()

        self.assertEqual([c['connector'] for c in data['connectors']], ['shopify', 'stripe'])
        self.assertEqual(data['connectors'][0]['calls'], 3)
        self.assertEqual(data['connectors'][0]['avg_ms'], 50)
        self.assertEqual(data['connectors'][1]['error'], 1)
        self.assertEqual(data['tools'][0]['tool_name'], 'list_orders')
        self.assertEqual(data['tools'][0]['calls'], 3)
        # A label is always present, even for a slug the registry no longer knows.
        self.assertTrue(all(c['connector_label'] for c in data['connectors']))

    def test_the_window_is_24_hours_but_recent_and_last_call_are_not(self):
        record(self.tenant, 'shopify', 'old_call', 'ok', 10, ago=timedelta(hours=30))

        data = self.client.get(self.url).json()

        self.assertEqual(data['calls'], 0)
        self.assertEqual(data['connectors'], [])
        # The feed and "last call" say what happened last, however long ago.
        self.assertEqual([r['tool_name'] for r in data['recent']], ['old_call'])
        self.assertIsNotNone(data['last_call_at'])

    def test_recent_is_newest_first(self):
        record(self.tenant, 'shopify', 'first', ago=timedelta(minutes=30))
        record(self.tenant, 'shopify', 'second', ago=timedelta(minutes=5))

        data = self.client.get(self.url).json()

        self.assertEqual([r['tool_name'] for r in data['recent']], ['second', 'first'])

    def test_a_neighbouring_tenant_never_leaks_in(self):
        record(self.other, 'shopify', 'secret_tool', 'error', 999)

        data = self.client.get(self.url).json()

        self.assertEqual(data['calls'], 0)
        self.assertEqual(data['tools'], [])
        self.assertEqual(data['recent'], [])
        self.assertIsNone(data['last_call_at'])


class ActivitySummaryWindowTests(TestCase):
    def setUp(self):
        cache.clear()
        tenant = Tenant.objects.create(name='Acme')
        self.client = APIClient()
        self.client.force_authenticate(make_user(tenant, 'me@acme.test'))

    def test_a_year_of_days_is_available(self):
        data = self.client.get(reverse('mcp:activity-summary'), {'days': 371}).json()
        self.assertEqual(len(data['days']), 371)

    def test_the_window_is_still_clamped(self):
        data = self.client.get(reverse('mcp:activity-summary'), {'days': 100000}).json()
        self.assertEqual(len(data['days']), MAX_SUMMARY_DAYS)
