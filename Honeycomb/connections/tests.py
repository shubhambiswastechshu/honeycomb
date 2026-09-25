"""Tests for the batch report endpoint, POST /api/connections/<id>/report/.

A report page runs a dozen read tools at once. What has to hold is that the
batch is exactly as careful as the single-tool ``run`` route -- read-only,
switched-off tools respected, tenant-scoped -- while failing one section at a
time instead of all at once, and that it never writes the activity rows the
Overview counts as AI tool calls.
"""
import asyncio
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Tenant, User
from connectors import registry
from connectors.shims.errors import ConnectorError
from mcp.models import McpActivity

from .models import Connection
from .serializers import MAX_REPORT_RUNS

SLUG = 'faketest'


async def _ok(conn, db, args):
    return {'echo': args, 'connection': conn.id}


async def _boom(conn, db, args):
    raise ConnectorError('upstream said no')


async def _slow(conn, db, args):
    await asyncio.sleep(2)
    return {}


def make_user(tenant, email):
    return User.objects.create_user(email=email, password='pw-for-tests-only', tenant=tenant)


class ReportEndpointTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A write tool that must never be reached through the report route.
        cls.write_handler = mock.AsyncMock()
        registry.register(registry.Connector(
            slug=SLUG, label='Fake', auth='api_key',
            catalog={
                'read_a': {'description': 'read a', 'input': {'type': 'object', 'properties': {}}},
                'read_b': {'description': 'read b', 'input': {'type': 'object', 'properties': {}}},
                'explodes': {'description': 'fails', 'input': {'type': 'object', 'properties': {}}},
                'slow': {'description': 'slow', 'input': {'type': 'object', 'properties': {}}},
                'writes': {'description': 'mutates', 'write': True,
                           'input': {'type': 'object', 'properties': {}}},
            },
            handlers={'read_a': _ok, 'read_b': _ok, 'explodes': _boom, 'slow': _slow,
                      'writes': cls.write_handler},
        ))

    @classmethod
    def tearDownClass(cls):
        registry.REGISTRY.pop(SLUG, None)
        super().tearDownClass()

    def setUp(self):
        cache.clear()  # throttle counters live in the cache
        self.write_handler.reset_mock()
        self.tenant = Tenant.objects.create(name='Acme')
        self.other = Tenant.objects.create(name='Rival')
        self.connection = Connection.objects.create(tenant=self.tenant, connector=SLUG, name='One')
        self.client = APIClient()
        self.client.force_authenticate(make_user(self.tenant, 'me@acme.test'))
        self.url = reverse('connections:connection-report', args=[self.connection.pk])

    def post(self, runs):
        return self.client.post(self.url, {'runs': runs}, format='json')

    def test_requires_authentication(self):
        self.assertIn(APIClient().post(self.url, {'runs': [{'tool': 'read_a'}]}, format='json').status_code,
                      (401, 403))

    def test_results_come_back_in_request_order_with_their_args(self):
        response = self.post([
            {'tool': 'read_b', 'args': {'n': 2}},
            {'tool': 'read_a', 'args': {'n': 1}},
        ])
        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual([r['tool'] for r in results], ['read_b', 'read_a'])
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(results[0]['data']['echo'], {'n': 2})
        self.assertEqual(results[1]['data']['echo'], {'n': 1})
        self.assertIn('duration_ms', response.json())

    def test_one_failing_tool_costs_its_own_section_only(self):
        results = self.post([{'tool': 'read_a'}, {'tool': 'explodes'}, {'tool': 'read_b'}]).json()['results']
        self.assertEqual([r['ok'] for r in results], [True, False, True])
        self.assertEqual(results[1]['error'], 'upstream said no')
        self.assertEqual(results[1]['status'], 502)

    def test_a_write_tool_is_refused_per_item_and_never_runs(self):
        results = self.post([{'tool': 'writes'}, {'tool': 'read_a'}]).json()['results']
        self.write_handler.assert_not_called()
        self.assertFalse(results[0]['ok'])
        self.assertEqual(results[0]['status'], 400)
        self.assertIn('changes data', results[0]['error'])
        self.assertTrue(results[1]['ok'])

    def test_a_switched_off_tool_is_refused_per_item(self):
        self.connection.disabled_tools = ['read_b']
        self.connection.save()
        results = self.post([{'tool': 'read_a'}, {'tool': 'read_b'}]).json()['results']
        self.assertTrue(results[0]['ok'])
        self.assertFalse(results[1]['ok'])
        self.assertIn('switched off', results[1]['error'])

    def test_an_unknown_tool_is_refused_per_item(self):
        results = self.post([{'tool': 'nope'}, {'tool': 'read_a'}]).json()['results']
        self.assertFalse(results[0]['ok'])
        self.assertIn('No tool named', results[0]['error'])
        self.assertTrue(results[1]['ok'])

    def test_a_slow_tool_times_out_without_stalling_the_rest(self):
        with mock.patch('mcp.endpoint._tool_timeout', return_value=0.05):
            results = self.post([{'tool': 'slow'}, {'tool': 'read_a'}]).json()['results']
        self.assertEqual(results[0]['status'], 504)
        self.assertFalse(results[0]['ok'])
        self.assertTrue(results[1]['ok'])

    def test_the_envelope_is_validated(self):
        self.assertEqual(self.post([]).status_code, 400)
        self.assertEqual(self.post([{'tool': 'read_a'}] * (MAX_REPORT_RUNS + 1)).status_code, 400)
        self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 400)

    def test_a_neighbouring_tenants_connection_does_not_exist(self):
        theirs = Connection.objects.create(tenant=self.other, connector=SLUG, name='Theirs')
        response = self.client.post(
            reverse('connections:connection-report', args=[theirs.pk]),
            {'runs': [{'tool': 'read_a'}]}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_no_activity_rows_are_written(self):
        self.post([{'tool': 'read_a'}, {'tool': 'explodes'}])
        self.assertEqual(McpActivity.objects.count(), 0)

    def test_it_has_a_ceiling_of_its_own(self):
        codes = [self.post([{'tool': 'read_a'}]).status_code for _ in range(13)]
        self.assertEqual(codes[:12], [200] * 12)
        self.assertEqual(codes[12], 429)
