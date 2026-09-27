"""Tests for POST /api/reports/<id>/run/, the call that turns a saved report into data.

A report is a grid of widgets and opening one must cost as few provider calls
as possible while being exactly as careful as the single-tool routes. What has
to hold:

* Identical reads are made ONCE. Twenty widgets asking the provider the same
  question are one call and one entry in ``runs``; anything less multiplies the
  bill (and the provider's rate limit) by the number of widgets.
* Date tokens turn into real dates before the tool is called, and the
  comparison period is the SAME LENGTH as the window and ends the day before it
  starts. A comparison that is off by a day, or is a calendar month when the
  window is thirty days, is a wrong number that looks exactly like a right one.
* Every unique run passes the same gate as the connection report route: the
  connection is looked up among the CALLER'S tenant's connections only, write
  and switched-off tools are refused, provider errors are redacted. A stored
  layout is not trusted (connections get deleted, tools get switched off), so
  the gate runs at run time, not only at save time.
* One failure costs one run. The response is a 200 whatever happened to the
  individual runs.
* No more than eight provider calls are in flight at once, and the timeout
  clock only starts when a call does, not while it waits for a slot.
* A run reads and reports; it never writes: no ``McpActivity`` rows (a person
  opening a report is not an AI tool call) and no change to the Report row,
  even when the body previews a different layout or filters.

Fixtures that would not survive the save-time validation (a token typo, a
neighbour's connection id) are built with the ORM on purpose, because the run
must cope with whatever is stored. The clock is frozen for every request so the
windows are literal dates, not arithmetic that mirrors the implementation.
"""
import asyncio
import copy
import json
import logging
import threading
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from accounts.models import Tenant, User
from connections.models import Connection
from connectors import registry
from connectors.shims.errors import ConnectorError
from mcp.models import McpActivity

from reports.models import Report

SLUG = 'fakereports'

# 04:45 UTC on 27 September 2026: "today" is the 27th and "yesterday" the 26th.
FROZEN = datetime(2026, 9, 27, 4, 45, tzinfo=dt_timezone.utc)

# Every secret-shaped string below is one the redaction really removes (see
# connectors/shims/errors.py): a query-string access token, a bearer header and
# a prefixed key. Only the unique tails are asserted absent.
SECRET_TAIL_TOKEN = 'EAAB1234567890abcdefXYZ'
SECRET_TAIL_BEARER = 's3cr3tbearervalue99'
SECRET_TAIL_KEY = 'abcdefghij1234567890'
SECRETS = (SECRET_TAIL_TOKEN, SECRET_TAIL_BEARER, SECRET_TAIL_KEY)
LEAKY_MESSAGE = (
    'GET https://api.example.test/v1/insights?access_token={0}&fields=id failed; '
    'upstream echoed Authorization: Bearer {1}; key ghp_{2} was rejected'
).format(SECRET_TAIL_TOKEN, SECRET_TAIL_BEARER, SECRET_TAIL_KEY)

# Every provider call the fakes see: {'connection', 'tool', 'args'}. The args
# are deep-copied at call time so a later mutation by the code under test
# cannot rewrite history.
CALLS = []
# In-flight bookkeeping for the concurrency tests. A lock, not because the
# handlers run in threads today, but so the test does not depend on that.
FLIGHT = {'now': 0, 'max': 0}
_FLIGHT_LOCK = threading.Lock()


def _record(conn, tool, args):
    CALLS.append({'connection': conn.id, 'tool': tool, 'args': copy.deepcopy(args)})


def _echo(tool):
    async def handler(conn, db, args):
        _record(conn, tool, args)
        return {'echo': args, 'connection': conn.id}
    return handler


async def _boom(conn, db, args):
    _record(conn, 'boom', args)
    raise ConnectorError('upstream said no')


async def _boom_with_secret(conn, db, args):
    _record(conn, 'boom_secret', args)
    raise ConnectorError(LEAKY_MESSAGE)


async def _leaky(conn, db, args):
    _record(conn, 'leaky', args)
    raise Exception(LEAKY_MESSAGE)


async def _slow(conn, db, args):
    _record(conn, 'slow', args)
    await asyncio.sleep(2)
    return {}


def _tracked(tool, seconds):
    """A tool that sleeps and keeps count of how many are asleep at once."""
    async def handler(conn, db, args):
        _record(conn, tool, args)
        with _FLIGHT_LOCK:
            FLIGHT['now'] += 1
            FLIGHT['max'] = max(FLIGHT['max'], FLIGHT['now'])
        try:
            await asyncio.sleep(seconds)
        finally:
            with _FLIGHT_LOCK:
                FLIGHT['now'] -= 1
        return {'echo': args, 'connection': conn.id}
    return handler


def _spec(description, write=False):
    entry = {'description': description, 'input': {'type': 'object', 'properties': {}}}
    if write:
        entry['write'] = True
    return entry


def make_user(tenant, email, role=User.Role.MEMBER):
    return User.objects.create_user(
        email=email, password='pw-for-tests-only', tenant=tenant, role=role)


def widget(wid, connection, tool='read_a', args=None, options=None, wtype='kpi', fields=None):
    """A valid widget in the top-left corner; ``layout_of`` moves it to a slot."""
    body = {
        'id': wid, 'type': wtype, 'x': 0, 'y': 0, 'w': 3, 'h': 2,
        'source': {
            'connection_id': getattr(connection, 'pk', connection),
            'tool': tool,
            'args': {} if args is None else args,
        },
    }
    if options is not None:
        body['options'] = options
    if fields is not None:
        body['fields'] = fields
    return body


def layout_of(*widgets):
    """Give each widget its own slot on a four-per-row grid.

    Three columns wide, so any count up to the 30-widget cap satisfies the
    12-column rule and no two widgets overlap.
    """
    for index, item in enumerate(widgets):
        item.update({'x': (index % 4) * 3, 'y': (index // 4) * 2, 'w': 3, 'h': 2})
    return list(widgets)


def filters_of(range_='LAST_30_DAYS', compare=False, start=None, end=None):
    date = {'range': range_}
    if start is not None:
        date['start'] = start
        date['end'] = end
    return {'date': date, 'compare': compare}


def window_of(start, end, prev_start, prev_end):
    return {'start': start, 'end': end, 'prev_start': prev_start, 'prev_end': prev_end}


def run_of(payload, wid):
    """The run entry a widget's main data comes from."""
    return payload['runs'][payload['widgets'][wid]['run']]


def prev_run_of(payload, wid):
    key = payload['widgets'][wid]['prev_run']
    return None if key is None else payload['runs'][key]


def called_args(tool=None):
    return [c['args'] for c in CALLS if tool is None or c['tool'] == tool]


class RunCase(TestCase):
    """Shared fixtures. Has no tests of its own."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A write tool that must never be reached through a report run.
        cls.write_handler = mock.AsyncMock()
        registry.register(registry.Connector(
            slug=SLUG, label='Fake Reports', auth='api_key',
            catalog={
                'read_a': _spec('read a'),
                'read_b': _spec('read b'),
                'boom': _spec('fails with a provider error'),
                'boom_secret': _spec('fails with a provider error that names a secret'),
                'leaky': _spec('fails unexpectedly with a secret in the message'),
                'slow': _spec('never finishes in time'),
                'tick': _spec('sleeps briefly and counts overlaps'),
                'tick_long': _spec('sleeps a little longer and counts overlaps'),
                'writes': _spec('mutates', write=True),
                'nohandler': _spec('in the catalog, but nothing implements it'),
            },
            handlers={
                'read_a': _echo('read_a'),
                'read_b': _echo('read_b'),
                'boom': _boom,
                'boom_secret': _boom_with_secret,
                'leaky': _leaky,
                'slow': _slow,
                'tick': _tracked('tick', 0.05),
                'tick_long': _tracked('tick_long', 0.15),
                'writes': cls.write_handler,
            },
        ))

    @classmethod
    def tearDownClass(cls):
        registry.REGISTRY.pop(SLUG, None)
        super().tearDownClass()

    def setUp(self):
        cache.clear()  # throttle counters live in the cache
        self.write_handler.reset_mock()
        del CALLS[:]
        FLIGHT.update({'now': 0, 'max': 0})
        self.tenant = Tenant.objects.create(name='Acme')
        self.other = Tenant.objects.create(name='Rival')
        self.connection = Connection.objects.create(tenant=self.tenant, connector=SLUG, name='One')
        self.second = Connection.objects.create(tenant=self.tenant, connector=SLUG, name='Two')
        self.theirs = Connection.objects.create(
            tenant=self.other, connector=SLUG, name='Rival Secret Name')
        self.user = make_user(self.tenant, 'me@acme.test')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.report = self.make_report()

    def make_report(self, *widgets, filters=None, tenant=None):
        # Report.objects.create skips the save-time validation on purpose: the
        # run has to cope with whatever is stored.
        return Report.objects.create(
            tenant=tenant or self.tenant,
            name='Weekly',
            created_by=None if tenant else self.user,
            layout=layout_of(*widgets),
            filters=filters_of() if filters is None else filters,
        )

    def post_run(self, report=None, body=None, now=FROZEN, client=None):
        report = report or self.report
        url = reverse('reports:report-run', args=[report.pk])
        with mock.patch('django.utils.timezone.now', return_value=now):
            return (client or self.client).post(url, {} if body is None else body, format='json')

    def run_ok(self, report=None, body=None, now=FROZEN):
        response = self.post_run(report, body, now)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def assertFailedRun(self, entry, status, tool, connection_id):
        self.assertIs(entry['ok'], False)
        self.assertEqual(entry['status'], status)
        self.assertEqual(entry['tool'], tool)
        self.assertEqual(entry['connection_id'], connection_id)
        self.assertIsInstance(entry['error'], str)
        self.assertTrue(entry['error'])
        # Only successful entries carry data.
        self.assertNotIn('data', entry)

    def snapshot(self, report):
        """Every stored column of the report, for "nothing changed" checks."""
        return Report.objects.filter(pk=report.pk).values().get()


class RunAccessTests(RunCase):
    def test_requires_authentication(self):
        url = reverse('reports:report-run', args=[self.report.pk])
        self.assertIn(APIClient().post(url, {}, format='json').status_code, (401, 403))

    def test_a_user_with_no_tenant_is_refused(self):
        root = User.objects.create_superuser(email='root@platform.test', password='pw-for-tests-only')
        client = APIClient()
        client.force_authenticate(root)
        self.assertEqual(self.post_run(client=client).status_code, 403)

    def test_any_member_of_the_tenant_may_run_a_report_someone_else_made(self):
        report = self.make_report(widget('w', self.connection, 'read_a', {'n': 1}))
        for role in (User.Role.OWNER, User.Role.ADMIN, User.Role.MEMBER):
            with self.subTest(role=role):
                client = APIClient()
                client.force_authenticate(make_user(self.tenant, '{0}@acme.test'.format(role.lower()), role))
                self.assertEqual(self.post_run(report, client=client).status_code, 200)

    def test_the_run_route_takes_post_only(self):
        url = reverse('reports:report-run', args=[self.report.pk])
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_a_report_that_does_not_exist_is_a_404(self):
        url = reverse('reports:report-run', args=[self.report.pk + 9999])
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 404)

    def test_a_neighbouring_tenants_report_does_not_exist(self):
        theirs = self.make_report(widget('w', self.theirs, 'read_a', {'n': 1}), tenant=self.other)
        self.assertEqual(self.post_run(theirs).status_code, 404)
        self.assertEqual(CALLS, [])

    def test_a_neighbours_report_is_a_404_even_with_a_layout_of_my_own_in_the_body(self):
        theirs = self.make_report(tenant=self.other)
        body = {'layout': layout_of(widget('w', self.connection, 'read_a', {'n': 1}))}
        self.assertEqual(self.post_run(theirs, body).status_code, 404)
        self.assertEqual(CALLS, [])


class RunResponseTests(RunCase):
    def test_the_response_has_exactly_the_documented_top_level_keys(self):
        report = self.make_report(widget('a', self.connection, 'read_a', {'n': 1}))
        payload = self.run_ok(report)
        self.assertEqual(
            set(payload), {'generated_at', 'duration_ms', 'window', 'runs', 'widgets'})
        self.assertIsInstance(payload['duration_ms'], int)
        self.assertGreaterEqual(payload['duration_ms'], 0)

    def test_generated_at_is_the_clock_in_utc_to_the_second(self):
        payload = self.run_ok(now=datetime(2026, 9, 27, 4, 45, 7, 123456, tzinfo=dt_timezone.utc))
        self.assertEqual(payload['generated_at'], '2026-09-27T04:45:07Z')

    def test_every_widget_maps_to_a_run_that_exists(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'n': 1}),
            widget('b', self.connection, 'read_b', {'n': 2}),
            widget('c', self.connection, 'read_a', {'n': 3}),
        )
        payload = self.run_ok(report)
        self.assertEqual(set(payload['widgets']), {'a', 'b', 'c'})
        for wid, entry in payload['widgets'].items():
            with self.subTest(widget=wid):
                self.assertEqual(set(entry), {'run', 'prev_run'})
                self.assertIn(entry['run'], payload['runs'])
                self.assertIsNone(entry['prev_run'])

    def test_a_successful_run_carries_its_data_connection_tool_and_timing(self):
        report = self.make_report(widget('a', self.connection, 'read_b', {'n': 7}))
        entry = run_of(self.run_ok(report), 'a')
        self.assertIs(entry['ok'], True)
        self.assertEqual(entry['tool'], 'read_b')
        self.assertEqual(entry['connection_id'], self.connection.id)
        self.assertEqual(entry['data'], {'echo': {'n': 7}, 'connection': self.connection.id})
        self.assertIsInstance(entry['duration_ms'], int)
        self.assertGreaterEqual(entry['duration_ms'], 0)

    def test_the_handler_gets_the_stored_args_untouched_when_there_is_no_token(self):
        args = {'n': 7, 'names': ['a', 'b'], 'nested': {'k': None, 'flag': True}}
        self.run_ok(self.make_report(widget('a', self.connection, 'read_a', args)))
        self.assertEqual(called_args(), [args])

    def test_run_keys_are_r1_to_rn_in_first_use_order_over_the_layout_order(self):
        # Ids are not alphabetical and the y positions run backwards, so an
        # implementation that sorts by id or by position numbers these wrongly.
        widgets = [
            widget('zeta', self.connection, 'read_a', {'n': 1}),
            widget('alpha', self.connection, 'read_a', {'from': '$date.start'},
                   options={'compare': True}),
            widget('mid', self.connection, 'read_b', {'n': 1}),
            widget('beta', self.connection, 'read_a', {'n': 1}),
        ]
        for index, item in enumerate(widgets):
            item.update({'x': 0, 'y': (4 - index) * 2})
        report = Report.objects.create(
            tenant=self.tenant, name='Ordered', layout=widgets, filters=filters_of(compare=True))

        payload = self.run_ok(report)

        self.assertEqual(set(payload['runs']), {'r1', 'r2', 'r3', 'r4'})
        widgets_out = payload['widgets']
        self.assertEqual(widgets_out['zeta'], {'run': 'r1', 'prev_run': None})
        # A widget's main run is numbered before its comparison run.
        self.assertEqual(widgets_out['alpha'], {'run': 'r2', 'prev_run': 'r3'})
        self.assertEqual(widgets_out['mid'], {'run': 'r4', 'prev_run': None})
        # beta repeats zeta's read, so it reuses r1 and takes no new number.
        self.assertEqual(widgets_out['beta'], {'run': 'r1', 'prev_run': None})

    def test_an_empty_layout_is_a_200_with_nothing_in_it(self):
        payload = self.run_ok(self.make_report())
        self.assertEqual(payload['runs'], {})
        self.assertEqual(payload['widgets'], {})
        self.assertEqual(CALLS, [])

    def test_an_empty_layout_still_reports_its_window(self):
        payload = self.run_ok(self.make_report())
        self.assertEqual(payload['window'], window_of(
            '2026-08-28', '2026-09-26', '2026-07-29', '2026-08-27'))

    def test_the_response_is_deterministic_for_a_fixed_clock_and_layout(self):
        report = self.make_report(
            widget('ok', self.connection, 'read_a', {'from': '$date.start', 'to': '$date.end'},
                   options={'compare': True}),
            widget('other', self.second, 'read_b', {'n': 2}),
            widget('refused', self.connection, 'writes', {}),
            widget('failed', self.connection, 'boom', {}),
            widget('gone', self.theirs, 'read_a', {}),
            filters=filters_of(compare=True),
        )

        def stable(payload):
            payload = copy.deepcopy(payload)
            payload.pop('duration_ms')
            for entry in payload['runs'].values():
                entry.pop('duration_ms', None)
            return payload

        first = stable(self.run_ok(report))
        second = stable(self.run_ok(report))
        self.assertEqual(first, second)


class DedupeTests(RunCase):
    def test_three_widgets_with_the_same_read_make_one_provider_call(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'n': 1}),
            widget('b', self.connection, 'read_a', {'n': 1}),
            widget('c', self.connection, 'read_a', {'n': 1}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(list(payload['runs']), ['r1'])
        self.assertEqual({payload['widgets'][w]['run'] for w in 'abc'}, {'r1'})

    def test_different_args_are_different_runs(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'n': 1}),
            widget('b', self.connection, 'read_a', {'n': 2}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(CALLS), 2)
        self.assertEqual(len(payload['runs']), 2)
        self.assertNotEqual(payload['widgets']['a']['run'], payload['widgets']['b']['run'])
        self.assertCountEqual(called_args(), [{'n': 1}, {'n': 2}])

    def test_the_same_args_in_a_different_key_order_still_dedupe(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'a': 1, 'b': 2, 'nested': {'p': 1, 'q': 2}}),
            widget('b', self.connection, 'read_a', {'nested': {'q': 2, 'p': 1}, 'b': 2, 'a': 1}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(len(payload['runs']), 1)

    def test_key_order_is_irrelevant_on_the_wire_too(self):
        # Raw JSON, so the order in which the keys arrive is exactly this.
        raw = (
            '{"layout": ['
            '{"id": "a", "type": "kpi", "x": 0, "y": 0, "w": 3, "h": 2, "source": '
            '{"connection_id": %(c)d, "tool": "read_a", "args": {"a": 1, "b": {"p": 1, "q": 2}}}},'
            '{"id": "b", "type": "kpi", "x": 3, "y": 0, "w": 3, "h": 2, "source": '
            '{"tool": "read_a", "args": {"b": {"q": 2, "p": 1}, "a": 1}, "connection_id": %(c)d}}'
            ']}'
        ) % {'c': self.connection.id}
        url = reverse('reports:report-run', args=[self.report.pk])
        response = self.client.post(url, data=raw, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()['runs']), 1)
        self.assertEqual(len(CALLS), 1)

    def test_array_order_matters_so_reordered_lists_are_different_runs(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'ids': [1, 2]}),
            widget('b', self.connection, 'read_a', {'ids': [2, 1]}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(payload['runs']), 2)
        self.assertEqual(len(CALLS), 2)

    def test_the_same_tool_and_args_on_two_connections_do_not_dedupe(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'n': 1}),
            widget('b', self.second, 'read_a', {'n': 1}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(payload['runs']), 2)
        self.assertCountEqual([c['connection'] for c in CALLS], [self.connection.id, self.second.id])
        self.assertEqual(run_of(payload, 'a')['connection_id'], self.connection.id)
        self.assertEqual(run_of(payload, 'b')['connection_id'], self.second.id)
        self.assertEqual(run_of(payload, 'a')['data']['connection'], self.connection.id)
        self.assertEqual(run_of(payload, 'b')['data']['connection'], self.second.id)

    def test_the_same_args_on_two_different_tools_do_not_dedupe(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'n': 1}),
            widget('b', self.connection, 'read_b', {'n': 1}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(payload['runs']), 2)
        self.assertCountEqual([c['tool'] for c in CALLS], ['read_a', 'read_b'])

    def test_a_token_that_resolves_to_another_widgets_literal_date_dedupes_with_it(self):
        report = self.make_report(
            widget('token', self.connection, 'read_a', {'from': '$date.start', 'to': '$date.end'}),
            widget('literal', self.connection, 'read_a', {'from': '2026-08-28', 'to': '2026-09-26'}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(len(payload['runs']), 1)
        self.assertEqual(payload['widgets']['token']['run'], payload['widgets']['literal']['run'])

    def test_only_connection_tool_and_args_decide_sameness(self):
        # Type, title, fields and options are presentation. A table and a KPI
        # over the same read must share the call.
        report = self.make_report(
            widget('kpi', self.connection, 'read_a', {'n': 1}, wtype='kpi', fields={'value': 'x'}),
            widget('tbl', self.connection, 'read_a', {'n': 1}, wtype='table',
                   fields={'columns': ['a', 'b']}, options={'sort': 'desc'}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(len(payload['runs']), 1)

    def test_a_widget_without_args_and_one_with_empty_args_are_the_same_run(self):
        # args is optional and defaults to {}; only a validated body layout
        # is guaranteed to be allowed to omit it.
        bare = widget('bare', self.connection, 'read_a')
        del bare['source']['args']
        body = {'layout': layout_of(bare, widget('empty', self.connection, 'read_a', {}))}
        payload = self.run_ok(body=body)
        self.assertEqual(len(payload['runs']), 1)
        self.assertEqual(called_args(), [{}])


class DateTokenTests(RunCase):
    def test_tokens_are_replaced_at_every_depth(self):
        args = {
            'range': {'from': '$date.start', 'to': '$date.end'},
            'windows': [{'a': '$date.prev_start'}, ['$date.prev_end', {'deep': ['$date.start']}]],
            'top': '$date.end',
        }
        payload = self.run_ok(self.make_report(widget('a', self.connection, 'read_a', args)))
        expected = {
            'range': {'from': '2026-08-28', 'to': '2026-09-26'},
            'windows': [{'a': '2026-07-29'}, ['2026-08-27', {'deep': ['2026-08-28']}]],
            'top': '2026-09-26',
        }
        self.assertEqual(called_args(), [expected])
        self.assertEqual(run_of(payload, 'a')['data']['echo'], expected)

    def test_all_four_tokens_resolve_to_their_dates_as_iso_strings(self):
        args = {
            's': '$date.start', 'e': '$date.end',
            'ps': '$date.prev_start', 'pe': '$date.prev_end',
        }
        self.run_ok(self.make_report(widget('a', self.connection, 'read_a', args)))
        self.assertEqual(called_args(), [{
            's': '2026-08-28', 'e': '2026-09-26', 'ps': '2026-07-29', 'pe': '2026-08-27',
        }])

    def test_only_a_string_that_is_exactly_a_token_is_replaced(self):
        # Save-time validation rejects a string that STARTS with '$date.' and is
        # not a token, so the ones that do are stored through the ORM.
        args = {
            'prefixed': 'prefix $date.start',
            'suffixed': '$date.start suffix',
            'padded': ' $date.start',
            'longer': '$date.starts',
            'shouty': '$DATE.START',
            'bare': 'date.start',
            'exact': '$date.start',
        }
        self.run_ok(self.make_report(widget('a', self.connection, 'read_a', args)))
        self.assertEqual(called_args(), [{
            'prefixed': 'prefix $date.start',
            'suffixed': '$date.start suffix',
            'padded': ' $date.start',
            'longer': '$date.starts',
            'shouty': '$DATE.START',
            'bare': 'date.start',
            'exact': '2026-08-28',
        }])

    def test_values_that_are_not_strings_are_left_alone(self):
        args = {'n': 5, 'ratio': 1.5, 'flag': True, 'off': False, 'nothing': None, 'list': [1, None, True]}
        self.run_ok(self.make_report(widget('a', self.connection, 'read_a', args)))
        self.assertEqual(called_args(), [args])

    def test_a_key_that_looks_like_a_token_is_not_replaced(self):
        # Tokens stand in for VALUES; a key is a name the provider expects.
        self.run_ok(self.make_report(
            widget('a', self.connection, 'read_a', {'$date.start': 'x', 'from': '$date.start'})))
        self.assertEqual(called_args(), [{'$date.start': 'x', 'from': '2026-08-28'}])

    def test_the_stored_layout_is_not_rewritten_by_substitution(self):
        report = self.make_report(widget('a', self.connection, 'read_a', {'from': '$date.start'}))
        before = self.snapshot(report)
        self.run_ok(report)
        self.assertEqual(self.snapshot(report), before)
        self.assertEqual(Report.objects.get(pk=report.pk).layout[0]['source']['args'], {'from': '$date.start'})

    def test_a_token_in_a_body_layout_resolves_the_same_way(self):
        body = {'layout': layout_of(widget('a', self.connection, 'read_a', {'from': '$date.end'}))}
        self.run_ok(body=body)
        self.assertEqual(called_args(), [{'from': '2026-09-26'}])


class WindowTests(RunCase):
    def window(self, now, filters):
        report = self.make_report(widget('a', self.connection, 'read_a', {'n': 1}), filters=filters)
        return self.run_ok(report, now=now)['window']

    def at(self, year, month, day, hour=4, minute=45, second=0):
        return datetime(year, month, day, hour, minute, second, tzinfo=dt_timezone.utc)

    def test_rolling_ranges_end_yesterday_and_span_their_length_inclusive(self):
        expected = {
            'LAST_7_DAYS': window_of('2026-09-20', '2026-09-26', '2026-09-13', '2026-09-19'),
            'LAST_14_DAYS': window_of('2026-09-13', '2026-09-26', '2026-08-30', '2026-09-12'),
            'LAST_30_DAYS': window_of('2026-08-28', '2026-09-26', '2026-07-29', '2026-08-27'),
            'LAST_90_DAYS': window_of('2026-06-29', '2026-09-26', '2026-03-31', '2026-06-28'),
        }
        for name, window in expected.items():
            with self.subTest(range=name):
                self.assertEqual(self.window(FROZEN, filters_of(name)), window)

    def test_rolling_ranges_cross_month_year_and_leap_day_boundaries(self):
        cases = [
            ('month boundary', self.at(2026, 10, 1), 'LAST_7_DAYS',
             window_of('2026-09-24', '2026-09-30', '2026-09-17', '2026-09-23')),
            ('year boundary', self.at(2027, 1, 1, 0, 30), 'LAST_7_DAYS',
             window_of('2026-12-25', '2026-12-31', '2026-12-18', '2026-12-24')),
            ('leap day is yesterday', self.at(2028, 3, 1), 'LAST_7_DAYS',
             window_of('2028-02-23', '2028-02-29', '2028-02-16', '2028-02-22')),
            ('leap day inside the span', self.at(2028, 3, 1), 'LAST_30_DAYS',
             window_of('2028-01-31', '2028-02-29', '2028-01-01', '2028-01-30')),
        ]
        for label, now, name, window in cases:
            with self.subTest(case=label):
                self.assertEqual(self.window(now, filters_of(name)), window)

    def test_this_month_runs_from_the_first_to_today(self):
        cases = [
            ('mid month', self.at(2026, 9, 27), window_of('2026-09-01', '2026-09-27', '2026-08-05', '2026-08-31')),
            ('the first is a one day window', self.at(2026, 10, 1),
             window_of('2026-10-01', '2026-10-01', '2026-09-30', '2026-09-30')),
            ('the 31st', self.at(2026, 5, 31), window_of('2026-05-01', '2026-05-31', '2026-03-31', '2026-04-30')),
            ('leap day', self.at(2028, 2, 29), window_of('2028-02-01', '2028-02-29', '2028-01-03', '2028-01-31')),
        ]
        for label, now, window in cases:
            with self.subTest(case=label):
                self.assertEqual(self.window(now, filters_of('THIS_MONTH')), window)

    def test_last_month_is_the_whole_previous_calendar_month(self):
        cases = [
            ('mid month', self.at(2026, 9, 27), window_of('2026-08-01', '2026-08-31', '2026-07-01', '2026-07-31')),
            ('on the 1st', self.at(2026, 10, 1), window_of('2026-09-01', '2026-09-30', '2026-08-02', '2026-08-31')),
            ('across a year boundary', self.at(2027, 1, 1, 0, 30),
             window_of('2026-12-01', '2026-12-31', '2026-10-31', '2026-11-30')),
            ('on the 31st of January', self.at(2027, 1, 31),
             window_of('2026-12-01', '2026-12-31', '2026-10-31', '2026-11-30')),
            # A 31st has no previous-month counterpart to "replace(month=...)".
            ('on the 31st of May', self.at(2026, 5, 31),
             window_of('2026-04-01', '2026-04-30', '2026-03-02', '2026-03-31')),
            ('February of a leap year', self.at(2028, 3, 5),
             window_of('2028-02-01', '2028-02-29', '2028-01-03', '2028-01-31')),
            ('leap February on the 1st of March', self.at(2028, 3, 1),
             window_of('2028-02-01', '2028-02-29', '2028-01-03', '2028-01-31')),
            ('February of an ordinary year', self.at(2026, 3, 1),
             window_of('2026-02-01', '2026-02-28', '2026-01-04', '2026-01-31')),
            ('February from the 31st of March', self.at(2026, 3, 31),
             window_of('2026-02-01', '2026-02-28', '2026-01-04', '2026-01-31')),
        ]
        for label, now, window in cases:
            with self.subTest(case=label):
                self.assertEqual(self.window(now, filters_of('LAST_MONTH')), window)

    def test_custom_is_used_exactly_as_given(self):
        cases = [
            ('ordinary', filters_of('CUSTOM', start='2026-03-10', end='2026-03-20'),
             window_of('2026-03-10', '2026-03-20', '2026-02-27', '2026-03-09')),
            ('a single day', filters_of('CUSTOM', start='2026-05-05', end='2026-05-05'),
             window_of('2026-05-05', '2026-05-05', '2026-05-04', '2026-05-04')),
            ('across a year boundary', filters_of('CUSTOM', start='2025-12-25', end='2026-01-05'),
             window_of('2025-12-25', '2026-01-05', '2025-12-13', '2025-12-24')),
            ('ending in the future', filters_of('CUSTOM', start='2026-10-01', end='2026-10-31'),
             window_of('2026-10-01', '2026-10-31', '2026-08-31', '2026-09-30')),
            ('the longest span allowed, 731 days', filters_of('CUSTOM', start='2024-01-01', end='2025-12-31'),
             window_of('2024-01-01', '2025-12-31', '2021-12-31', '2023-12-31')),
        ]
        for label, filters, window in cases:
            with self.subTest(case=label):
                self.assertEqual(self.window(FROZEN, filters), window)

    def test_custom_does_not_depend_on_the_clock(self):
        filters = filters_of('CUSTOM', start='2026-03-10', end='2026-03-20')
        self.assertEqual(self.window(self.at(2026, 9, 27), filters),
                         self.window(self.at(2031, 1, 1), filters))

    def test_the_previous_period_has_the_same_length_not_the_previous_calendar_month(self):
        # September has 30 days, so the period before it is 2-31 August. A
        # calendar-month implementation would say 1-31 August.
        window = self.window(self.at(2026, 10, 1), filters_of('LAST_MONTH'))
        self.assertEqual((window['prev_start'], window['prev_end']), ('2026-08-02', '2026-08-31'))

    def test_the_time_of_day_never_moves_the_window(self):
        expected = window_of('2026-08-28', '2026-09-26', '2026-07-29', '2026-08-27')
        for label, now in (('midnight', self.at(2026, 9, 27, 0, 0, 0)),
                           ('a second before midnight', self.at(2026, 9, 27, 23, 59, 59))):
            with self.subTest(clock=label):
                self.assertEqual(self.window(now, filters_of('LAST_30_DAYS')), expected)

    def test_a_report_with_no_date_in_its_filters_uses_the_last_30_days(self):
        expected = window_of('2026-08-28', '2026-09-26', '2026-07-29', '2026-08-27')
        for label, filters in (('empty', {}), ('compare only', {'compare': False})):
            with self.subTest(filters=label):
                self.assertEqual(self.window(FROZEN, filters), expected)

    def test_the_window_is_echoed_as_iso_date_strings(self):
        window = self.window(FROZEN, filters_of('LAST_7_DAYS'))
        self.assertEqual(set(window), {'start', 'end', 'prev_start', 'prev_end'})
        self.assertTrue(all(isinstance(value, str) and len(value) == 10 for value in window.values()))


class CompareTests(RunCase):
    def test_a_comparison_run_needs_the_filter_the_option_and_a_token(self):
        for filter_compare in (False, True):
            for option_compare in (False, True):
                for has_token in (False, True):
                    with self.subTest(filter=filter_compare, option=option_compare, token=has_token):
                        del CALLS[:]
                        args = {'from': '$date.start'} if has_token else {'from': '2026-01-01'}
                        report = self.make_report(
                            widget('w', self.connection, 'read_a', args, options={'compare': option_compare}),
                            filters=filters_of(compare=filter_compare),
                        )
                        payload = self.run_ok(report)
                        expects_prev = filter_compare and option_compare and has_token
                        self.assertEqual(payload['widgets']['w']['prev_run'] is not None, expects_prev)
                        self.assertEqual(len(payload['runs']), 2 if expects_prev else 1)
                        self.assertEqual(len(CALLS), 2 if expects_prev else 1)

    def test_a_widget_with_no_options_at_all_gets_no_comparison(self):
        report = self.make_report(
            widget('w', self.connection, 'read_a', {'from': '$date.start'}),
            filters=filters_of(compare=True),
        )
        payload = self.run_ok(report)
        self.assertIsNone(payload['widgets']['w']['prev_run'])
        self.assertEqual(len(CALLS), 1)

    def test_a_token_buried_deep_in_the_args_counts(self):
        args = {'a': {'b': [{'c': '$date.end'}]}}
        report = self.make_report(
            widget('w', self.connection, 'read_a', args, options={'compare': True}),
            filters=filters_of(compare=True),
        )
        self.assertIsNotNone(self.run_ok(report)['widgets']['w']['prev_run'])

    def test_a_string_that_only_contains_a_token_is_not_a_token(self):
        # It is left alone by substitution, so there is nothing to shift.
        report = self.make_report(
            widget('w', self.connection, 'read_a', {'from': 'prefix $date.start'},
                   options={'compare': True}),
            filters=filters_of(compare=True),
        )
        payload = self.run_ok(report)
        self.assertIsNone(payload['widgets']['w']['prev_run'])
        self.assertEqual(len(CALLS), 1)

    def test_the_comparison_run_carries_the_window_shifted_back_one_period(self):
        report = self.make_report(
            widget('w', self.connection, 'read_a', {'from': '$date.start', 'to': '$date.end'},
                   options={'compare': True}),
            filters=filters_of(compare=True),
        )
        payload = self.run_ok(report)
        self.assertEqual(run_of(payload, 'w')['data']['echo'], {'from': '2026-08-28', 'to': '2026-09-26'})
        prev = prev_run_of(payload, 'w')
        self.assertIs(prev['ok'], True)
        self.assertEqual(prev['tool'], 'read_a')
        self.assertEqual(prev['connection_id'], self.connection.id)
        self.assertEqual(prev['data']['echo'], {'from': '2026-07-29', 'to': '2026-08-27'})
        self.assertCountEqual(called_args(), [
            {'from': '2026-08-28', 'to': '2026-09-26'},
            {'from': '2026-07-29', 'to': '2026-08-27'},
        ])

    def test_the_comparison_of_a_custom_window_is_the_same_length_before_it(self):
        report = self.make_report(
            widget('w', self.connection, 'read_a', {'from': '$date.start', 'to': '$date.end'},
                   options={'compare': True}),
            filters=filters_of('CUSTOM', compare=True, start='2026-03-10', end='2026-03-20'),
        )
        payload = self.run_ok(report)
        self.assertEqual(prev_run_of(payload, 'w')['data']['echo'],
                         {'from': '2026-02-27', 'to': '2026-03-09'})

    def test_the_comparison_window_is_shifted_as_a_whole_so_its_own_previous_period_follows(self):
        # $date.prev_* inside the comparison run mean the period before the
        # SHIFTED window, i.e. two periods back from the report's window.
        report = self.make_report(
            widget('w', self.connection, 'read_a',
                   {'s': '$date.start', 'ps': '$date.prev_start', 'pe': '$date.prev_end'},
                   options={'compare': True}),
            filters=filters_of('CUSTOM', compare=True, start='2026-03-10', end='2026-03-20'),
        )
        payload = self.run_ok(report)
        self.assertEqual(run_of(payload, 'w')['data']['echo'],
                         {'s': '2026-03-10', 'ps': '2026-02-27', 'pe': '2026-03-09'})
        self.assertEqual(prev_run_of(payload, 'w')['data']['echo'],
                         {'s': '2026-02-27', 'ps': '2026-02-16', 'pe': '2026-02-26'})

    def test_two_widgets_that_share_a_comparison_window_share_the_comparison_run(self):
        args = {'from': '$date.start', 'to': '$date.end'}
        report = self.make_report(
            widget('a', self.connection, 'read_a', args, options={'compare': True}),
            widget('b', self.connection, 'read_a', dict(args), options={'compare': True}),
            filters=filters_of(compare=True),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(payload['runs']), 2)
        self.assertEqual(len(CALLS), 2)
        self.assertEqual(payload['widgets']['a'], payload['widgets']['b'])
        self.assertIsNotNone(payload['widgets']['a']['prev_run'])

    def test_a_comparison_run_dedupes_with_another_widgets_literal_read(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'from': '$date.start', 'to': '$date.end'},
                   options={'compare': True}),
            widget('literal', self.connection, 'read_a', {'from': '2026-07-29', 'to': '2026-08-27'}),
            filters=filters_of(compare=True),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(CALLS), 2)
        self.assertEqual(payload['widgets']['literal']['run'], payload['widgets']['a']['prev_run'])

    def test_widgets_with_different_reads_each_get_their_own_comparison_run(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'from': '$date.start'}, options={'compare': True}),
            widget('b', self.connection, 'read_b', {'from': '$date.start'}, options={'compare': True}),
            filters=filters_of(compare=True),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(payload['runs']), 4)
        self.assertEqual(len(CALLS), 4)
        prev_keys = {payload['widgets'][w]['prev_run'] for w in 'ab'}
        self.assertEqual(len(prev_keys), 2)
        self.assertFalse(prev_keys & {payload['widgets'][w]['run'] for w in 'ab'})

    def test_a_failing_comparison_run_does_not_fail_the_main_run(self):
        report = self.make_report(
            widget('w', self.connection, 'writes', {'from': '$date.start'}, options={'compare': True}),
            widget('ok', self.connection, 'read_a', {'from': '$date.start'}, options={'compare': True}),
            filters=filters_of(compare=True),
        )
        payload = self.run_ok(report)
        self.assertFailedRun(run_of(payload, 'w'), 400, 'writes', self.connection.id)
        self.assertFailedRun(prev_run_of(payload, 'w'), 400, 'writes', self.connection.id)
        self.assertTrue(run_of(payload, 'ok')['ok'])
        self.assertTrue(prev_run_of(payload, 'ok')['ok'])
        self.write_handler.assert_not_called()
        self.write_handler.assert_not_awaited()


class GateTests(RunCase):
    """Each refusal is per run: the response stays 200 and the neighbour succeeds."""

    def run_beside_a_good_widget(self, tool, args=None, connection=None):
        report = self.make_report(
            widget('bad', connection or self.connection, tool, args),
            widget('good', self.connection, 'read_a', {'n': 1}),
        )
        response = self.post_run(report)
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        good = run_of(payload, 'good')
        self.assertIs(good['ok'], True)
        self.assertEqual(good['data']['echo'], {'n': 1})
        return payload, run_of(payload, 'bad')

    def test_a_write_tool_is_refused_and_its_handler_is_never_awaited(self):
        _, bad = self.run_beside_a_good_widget('writes')
        self.assertFailedRun(bad, 400, 'writes', self.connection.id)
        self.write_handler.assert_not_called()
        self.write_handler.assert_not_awaited()

    def test_a_switched_off_tool_is_refused_and_the_error_names_it(self):
        self.connection.disabled_tools = ['read_b']
        self.connection.save()
        _, bad = self.run_beside_a_good_widget('read_b')
        self.assertFailedRun(bad, 400, 'read_b', self.connection.id)
        self.assertIn('read_b', bad['error'])
        self.assertEqual(called_args('read_b'), [])

    def test_switching_a_tool_off_on_another_connection_changes_nothing_here(self):
        self.second.disabled_tools = ['read_a']
        self.second.save()
        report = self.make_report(
            widget('on_first', self.connection, 'read_a', {'n': 1}),
            widget('on_second', self.second, 'read_a', {'n': 1}),
        )
        payload = self.run_ok(report)
        self.assertTrue(run_of(payload, 'on_first')['ok'])
        self.assertFailedRun(run_of(payload, 'on_second'), 400, 'read_a', self.second.id)

    def test_an_unknown_tool_is_refused(self):
        _, bad = self.run_beside_a_good_widget('nope')
        self.assertFailedRun(bad, 400, 'nope', self.connection.id)

    def test_a_tool_with_no_handler_is_refused(self):
        _, bad = self.run_beside_a_good_widget('nohandler')
        self.assertFailedRun(bad, 400, 'nohandler', self.connection.id)

    def test_a_connector_that_is_no_longer_installed_is_a_failed_run_not_a_500(self):
        gone = Connection.objects.create(tenant=self.tenant, connector='connector-removed', name='Old')
        _, bad = self.run_beside_a_good_widget('read_a', {'n': 9}, connection=gone)
        self.assertFailedRun(bad, 400, 'read_a', gone.id)
        self.assertEqual(called_args(), [{'n': 1}])

    def test_a_provider_error_is_a_502_carrying_the_message(self):
        _, bad = self.run_beside_a_good_widget('boom')
        self.assertFailedRun(bad, 502, 'boom', self.connection.id)
        self.assertEqual(bad['error'], 'upstream said no')

    def test_a_provider_error_that_names_a_secret_is_redacted(self):
        payload, bad = self.run_beside_a_good_widget('boom_secret')
        self.assertFailedRun(bad, 502, 'boom_secret', self.connection.id)
        for secret in SECRETS:
            with self.subTest(secret=secret[:6]):
                self.assertNotIn(secret, bad['error'])
                self.assertNotIn(secret, json.dumps(payload))

    def test_an_unexpected_exception_is_a_redacted_502(self):
        logging.disable(logging.CRITICAL)  # the traceback is expected, not news
        self.addCleanup(logging.disable, logging.NOTSET)
        payload, bad = self.run_beside_a_good_widget('leaky')
        self.assertFailedRun(bad, 502, 'leaky', self.connection.id)
        for secret in SECRETS:
            with self.subTest(secret=secret[:6]):
                self.assertNotIn(secret, bad['error'])
                self.assertNotIn(secret, json.dumps(payload))

    def test_a_tool_slower_than_the_timeout_is_a_504(self):
        with mock.patch('mcp.endpoint._tool_timeout', return_value=0.05):
            _, bad = self.run_beside_a_good_widget('slow')
        self.assertFailedRun(bad, 504, 'slow', self.connection.id)

    def test_every_kind_of_failure_at_once_still_answers_200_and_spares_the_good_runs(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.connection.disabled_tools = ['read_b']
        self.connection.save()
        report = self.make_report(
            widget('good1', self.connection, 'read_a', {'n': 1}),
            widget('write', self.connection, 'writes', {}),
            widget('off', self.connection, 'read_b', {}),
            widget('unknown', self.connection, 'nope', {}),
            widget('provider', self.connection, 'boom', {}),
            widget('unexpected', self.connection, 'leaky', {}),
            widget('slow', self.connection, 'slow', {}),
            widget('foreign', self.theirs, 'read_a', {'n': 1}),
            widget('good2', self.second, 'read_a', {'n': 2}),
        )
        with mock.patch('mcp.endpoint._tool_timeout', return_value=0.05):
            payload = self.run_ok(report)

        # A failed run says why (its status); a good one has nothing to add.
        outcomes = {}
        for wid in payload['widgets']:
            entry = run_of(payload, wid)
            outcomes[wid] = (True, None) if entry['ok'] else (False, entry['status'])
        self.assertEqual(outcomes, {
            'good1': (True, None), 'good2': (True, None),
            'write': (False, 400), 'off': (False, 400), 'unknown': (False, 400),
            'provider': (False, 502), 'unexpected': (False, 502),
            'slow': (False, 504), 'foreign': (False, 404),
        })
        # Whatever became of a run, it still says which tool and connection it was.
        for wid, entry in ((w, run_of(payload, w)) for w in payload['widgets']):
            with self.subTest(widget=wid):
                self.assertIn('tool', entry)
                self.assertIn('connection_id', entry)
        self.write_handler.assert_not_called()

    def test_a_report_where_every_run_failed_is_still_a_200(self):
        report = self.make_report(
            widget('a', self.connection, 'boom', {}),
            widget('b', self.connection, 'nope', {}),
        )
        payload = self.run_ok(report)
        self.assertEqual({e['ok'] for e in payload['runs'].values()}, {False})


class TenancyAtRunTimeTests(RunCase):
    def assertConnectionGone(self, entry, connection_id):
        self.assertFailedRun(entry, 404, 'read_a', connection_id)
        self.assertIn('no longer exist', entry['error'].lower())

    def test_a_stored_widget_pointing_at_a_neighbours_connection_is_a_failed_404_run(self):
        report = self.make_report(
            widget('mine', self.connection, 'read_a', {'n': 1}),
            widget('theirs', self.theirs, 'read_a', {'n': 1}),
        )
        payload = self.run_ok(report)
        self.assertTrue(run_of(payload, 'mine')['ok'])
        self.assertConnectionGone(run_of(payload, 'theirs'), self.theirs.id)

    def test_the_neighbours_provider_is_never_called_and_nothing_of_theirs_leaks(self):
        report = self.make_report(widget('theirs', self.theirs, 'read_a', {'n': 1}))
        response = self.post_run(report)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CALLS, [])
        body = response.content.decode()
        self.assertNotIn('Rival Secret Name', body)
        self.assertNotIn(self.theirs.endpoint_slug, body)

    def test_the_same_read_on_my_connection_and_a_neighbours_is_two_runs_and_only_mine_is_called(self):
        report = self.make_report(
            widget('mine', self.connection, 'read_a', {'n': 1}),
            widget('theirs', self.theirs, 'read_a', {'n': 1}),
        )
        payload = self.run_ok(report)
        self.assertEqual(len(payload['runs']), 2)
        self.assertEqual([c['connection'] for c in CALLS], [self.connection.id])

    def test_a_connection_id_that_does_not_exist_is_the_same_failed_404_run(self):
        missing = self.theirs.id + 9999
        report = self.make_report(widget('ghost', missing, 'read_a', {'n': 1}))
        payload = self.run_ok(report)
        self.assertConnectionGone(run_of(payload, 'ghost'), missing)
        self.assertEqual(CALLS, [])

    def test_a_connection_deleted_after_the_report_was_saved_is_a_failed_404_run(self):
        doomed = Connection.objects.create(tenant=self.tenant, connector=SLUG, name='Doomed')
        report = self.make_report(
            widget('doomed', doomed, 'read_a', {'n': 1}),
            widget('mine', self.connection, 'read_a', {'n': 2}),
        )
        doomed_id = doomed.id
        doomed.delete()
        payload = self.run_ok(report)
        self.assertConnectionGone(run_of(payload, 'doomed'), doomed_id)
        self.assertTrue(run_of(payload, 'mine')['ok'])
        self.assertEqual(called_args(), [{'n': 2}])

    def test_the_comparison_run_of_a_foreign_widget_is_refused_too(self):
        report = self.make_report(
            widget('theirs', self.theirs, 'read_a', {'from': '$date.start'}, options={'compare': True}),
            filters=filters_of(compare=True),
        )
        payload = self.run_ok(report)
        self.assertConnectionGone(run_of(payload, 'theirs'), self.theirs.id)
        self.assertConnectionGone(prev_run_of(payload, 'theirs'), self.theirs.id)
        self.assertEqual(CALLS, [])

    def test_a_body_layout_naming_a_neighbours_connection_is_a_200_with_a_failed_run_not_a_400(self):
        body = {'layout': layout_of(
            widget('theirs', self.theirs, 'read_a', {'n': 1}),
            widget('mine', self.connection, 'read_a', {'n': 2}),
        )}
        response = self.post_run(body=body)
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertConnectionGone(run_of(payload, 'theirs'), self.theirs.id)
        self.assertTrue(run_of(payload, 'mine')['ok'])
        self.assertEqual(called_args(), [{'n': 2}])


class MalformedStoredReportTests(RunCase):
    """A row too broken to plan at all is a clean 400, never a 500.

    These fixtures could never be saved through the API -- check_layout would
    refuse every one of them -- so they are written straight to the row with
    the ORM, standing in for data that reached the table some other way (a
    schema tightened after the row was written, a hand edit). ReportViewSet.run
    guards exactly this: resolve_window/build_plan for a structurally broken
    layout or filters, and execution.execute_plan for a connection id too wide
    for the database to look up.
    """

    def assertRefusedToRun(self, report):
        response = self.post_run(report)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('layout', response.json())
        self.assertEqual(CALLS, [])
        return response

    def test_a_widget_missing_source_is_refused_not_a_500(self):
        broken = widget('a', self.connection, 'read_a', {'n': 1})
        del broken['source']
        self.assertRefusedToRun(self.make_report(broken))

    def test_a_custom_date_range_missing_start_and_end_is_refused_not_a_500(self):
        report = self.make_report(
            widget('a', self.connection, 'read_a', {'n': 1}),
            filters={'date': {'range': 'CUSTOM'}},
        )
        self.assertRefusedToRun(report)

    def test_a_filters_value_that_is_not_an_object_is_refused_not_a_500(self):
        report = self.make_report(widget('a', self.connection, 'read_a', {'n': 1}), filters='bogus')
        self.assertRefusedToRun(report)

    def test_a_connection_id_too_wide_for_the_database_is_refused_not_a_500(self):
        # layout.py refuses this on every save-time path now (MAX_CONNECTION_ID);
        # this is the residual case of a row that predates that bound.
        report = self.make_report(widget('a', 10 ** 30, 'read_a', {'n': 1}))
        self.assertRefusedToRun(report)
        self.assertEqual(CALLS, [])


class RunBodyTests(RunCase):
    def stored_report(self):
        return self.make_report(
            widget('stored', self.connection, 'read_a', {'n': 1}),
            filters=filters_of('LAST_7_DAYS', compare=True),
        )

    def test_an_empty_body_runs_the_stored_layout_and_filters(self):
        report = self.make_report(
            widget('stored', self.connection, 'read_a', {'from': '$date.start'}, options={'compare': True}),
            filters=filters_of('LAST_7_DAYS', compare=True),
        )
        payload = self.run_ok(report, body={})
        self.assertEqual(set(payload['widgets']), {'stored'})
        self.assertEqual(payload['window']['start'], '2026-09-20')
        self.assertIsNotNone(payload['widgets']['stored']['prev_run'])

    def test_a_body_layout_runs_instead_of_the_stored_one(self):
        report = self.stored_report()
        body = {'layout': layout_of(widget('preview', self.connection, 'read_b', {'n': 2}))}
        payload = self.run_ok(report, body)
        self.assertEqual(set(payload['widgets']), {'preview'})
        self.assertEqual(called_args(), [{'n': 2}])
        self.assertEqual([c['tool'] for c in CALLS], ['read_b'])

    def test_a_body_layout_saves_nothing(self):
        report = self.stored_report()
        before = self.snapshot(report)
        self.run_ok(report, {'layout': layout_of(widget('preview', self.connection, 'read_b', {'n': 2}))})
        after = self.snapshot(report)
        self.assertEqual(after, before)
        for column in ('layout', 'version', 'updated_at'):
            self.assertEqual(after[column], before[column])
        self.assertEqual(Report.objects.count(), 2)  # this one and the fixture's empty one

    def test_an_empty_body_layout_previews_an_empty_report(self):
        payload = self.run_ok(self.stored_report(), {'layout': []})
        self.assertEqual(payload['runs'], {})
        self.assertEqual(payload['widgets'], {})
        self.assertEqual(CALLS, [])

    def test_body_filters_replace_the_stored_filters_wholesale_not_merged(self):
        # The stored filters compare over the last 7 days. The body names neither,
        # so the run must be a 30 day window with no comparison.
        report = self.make_report(
            widget('w', self.connection, 'read_a', {'from': '$date.start'}, options={'compare': True}),
            filters=filters_of('LAST_7_DAYS', compare=True),
        )
        payload = self.run_ok(report, {'filters': {'date': {'range': 'LAST_30_DAYS'}}})
        self.assertEqual(payload['window']['start'], '2026-08-28')
        self.assertIsNone(payload['widgets']['w']['prev_run'])
        self.assertEqual(called_args(), [{'from': '2026-08-28'}])

    def test_body_filters_without_a_date_fall_back_to_30_days_not_the_stored_date(self):
        report = self.make_report(
            widget('w', self.connection, 'read_a', {'n': 1}),
            filters=filters_of('LAST_7_DAYS'),
        )
        payload = self.run_ok(report, {'filters': {'compare': True}})
        self.assertEqual(payload['window']['start'], '2026-08-28')

    def test_body_filters_can_switch_comparison_on_for_one_run(self):
        report = self.make_report(
            widget('w', self.connection, 'read_a', {'from': '$date.start'}, options={'compare': True}),
            filters=filters_of('LAST_30_DAYS', compare=False),
        )
        payload = self.run_ok(report, {'filters': filters_of('LAST_30_DAYS', compare=True)})
        self.assertIsNotNone(payload['widgets']['w']['prev_run'])

    def test_body_filters_save_nothing(self):
        report = self.stored_report()
        before = self.snapshot(report)
        self.run_ok(report, {'filters': filters_of('LAST_90_DAYS')})
        self.assertEqual(self.snapshot(report), before)
        self.assertEqual(Report.objects.get(pk=report.pk).filters, filters_of('LAST_7_DAYS', compare=True))

    def test_a_body_can_carry_both_a_layout_and_filters(self):
        report = self.stored_report()
        body = {
            'layout': layout_of(widget('p', self.connection, 'read_a', {'from': '$date.start'},
                                       options={'compare': True})),
            'filters': filters_of('CUSTOM', compare=True, start='2026-03-10', end='2026-03-20'),
        }
        payload = self.run_ok(report, body)
        self.assertEqual(payload['window'], window_of('2026-03-10', '2026-03-20', '2026-02-27', '2026-03-09'))
        self.assertEqual(set(payload['widgets']), {'p'})
        self.assertIsNotNone(payload['widgets']['p']['prev_run'])

    def test_custom_filters_at_the_731_day_limit_are_accepted(self):
        body = {'filters': filters_of('CUSTOM', start='2024-01-01', end='2025-12-31')}
        self.assertEqual(self.post_run(body=body).status_code, 200)

    def test_thirty_widgets_in_a_body_layout_are_accepted(self):
        widgets = [widget('w%02d' % i, self.connection, 'read_a', {'n': i}) for i in range(30)]
        self.assertEqual(self.post_run(body={'layout': layout_of(*widgets)}).status_code, 200)

    def test_an_invalid_body_layout_is_a_400_naming_layout_and_runs_nothing(self):
        good = widget('w', self.connection, 'read_a', {'n': 1})
        no_type = {k: v for k, v in good.items() if k != 'type'}
        too_many = layout_of(*[widget('w%02d' % i, self.connection, 'read_a', {'n': i}) for i in range(31)])
        cases = {
            'an object instead of a list': {'id': 'w'},
            'a widget that is not an object': ['nope'],
            'missing type': [no_type],
            'unknown widget key': [dict(good, colour='red')],
            'unknown type': [dict(good, type='pie')],
            'overflows the twelve columns': [dict(good, x=10, w=3)],
            'boolean coordinate': [dict(good, x=True)],
            'duplicate ids': [good, copy.deepcopy(good)],
            'a mistyped date token': [widget('w', self.connection, 'read_a', {'from': '$date.bogus'})],
            'thirty one widgets': too_many,
        }
        report = self.stored_report()
        before = self.snapshot(report)
        for label, layout in cases.items():
            with self.subTest(layout=label):
                response = self.post_run(report, {'layout': layout})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('layout', response.json())
        self.assertEqual(CALLS, [])
        self.assertEqual(self.snapshot(report), before)

    def test_invalid_body_filters_are_a_400_naming_filters_and_run_nothing(self):
        cases = {
            'a list instead of an object': ['LAST_7_DAYS'],
            'unknown key': {'timezone': 'UTC'},
            'unknown range': {'date': {'range': 'LAST_YEAR'}},
            'date is not an object': {'date': 'LAST_7_DAYS'},
            'custom without dates': {'date': {'range': 'CUSTOM'}},
            'custom start after end': {'date': {'range': 'CUSTOM', 'start': '2026-03-20', 'end': '2026-03-10'}},
            'custom start is not a date': {'date': {'range': 'CUSTOM', 'start': '10/03/2026', 'end': '20/03/2026'}},
            'custom span of 732 days': {'date': {'range': 'CUSTOM', 'start': '2024-01-01', 'end': '2026-01-01'}},
            'dates on a rolling range': {'date': {'range': 'LAST_7_DAYS', 'start': '2026-03-10',
                                                  'end': '2026-03-20'}},
            'compare is not a boolean': {'compare': 'nope'},
        }
        report = self.stored_report()
        before = self.snapshot(report)
        for label, filters in cases.items():
            with self.subTest(filters=label):
                response = self.post_run(report, {'filters': filters})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('filters', response.json())
        self.assertEqual(CALLS, [])
        self.assertEqual(self.snapshot(report), before)

    def test_a_valid_layout_beside_invalid_filters_is_still_a_400_on_filters(self):
        body = {
            'layout': layout_of(widget('w', self.connection, 'read_a', {'n': 1})),
            'filters': {'date': {'range': 'LAST_YEAR'}},
        }
        response = self.post_run(body=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn('filters', response.json())
        self.assertEqual(CALLS, [])

    def test_a_body_that_is_not_an_object_is_a_400(self):
        for label, body in (('list', []), ('list of one', [{'layout': []}]), ('string', 'x'), ('number', 5)):
            with self.subTest(body=label):
                self.assertEqual(self.post_run(body=body).status_code, 400)
        self.assertEqual(CALLS, [])


class RunSideEffectTests(RunCase):
    def busy_report(self):
        return self.make_report(
            widget('ok', self.connection, 'read_a', {'from': '$date.start'}, options={'compare': True}),
            widget('failed', self.connection, 'boom', {}),
            widget('refused', self.connection, 'writes', {}),
            widget('unknown', self.connection, 'nope', {}),
            widget('foreign', self.theirs, 'read_a', {}),
            filters=filters_of(compare=True),
        )

    def test_a_run_writes_no_activity_rows_however_it_ends(self):
        self.run_ok(self.busy_report())
        self.assertEqual(McpActivity.objects.count(), 0)

    def test_a_run_never_changes_the_report_row(self):
        report = self.busy_report()
        before = self.snapshot(report)
        self.run_ok(report)
        self.run_ok(report, {'layout': [], 'filters': filters_of('LAST_7_DAYS')})
        self.assertEqual(self.snapshot(report), before)
        self.assertEqual(Report.objects.count(), 2)

    def test_a_run_does_not_touch_a_neighbouring_tenants_reports(self):
        theirs = self.make_report(widget('w', self.theirs, 'read_a', {}), tenant=self.other)
        before = self.snapshot(theirs)
        self.run_ok(self.busy_report())
        self.assertEqual(self.snapshot(theirs), before)


class ConcurrencyTests(RunCase):
    def test_runs_overlap_but_never_more_than_eight_are_in_flight(self):
        # Overlap is asserted through the in-flight high-water mark rather than
        # wall time, which would be a flake waiting for a slow machine.
        report = self.make_report(
            *[widget('w%02d' % i, self.connection, 'tick', {'n': i}) for i in range(20)])
        payload = self.run_ok(report)
        self.assertEqual(len(payload['runs']), 20)
        self.assertTrue(all(entry['ok'] for entry in payload['runs'].values()))
        self.assertEqual(len(CALLS), 20)
        self.assertGreater(FLIGHT['max'], 1)
        self.assertLessEqual(FLIGHT['max'], 8)
        self.assertEqual(FLIGHT['now'], 0)

    def test_the_cap_of_eight_spans_every_connection_in_the_request(self):
        # Twelve per connection: a cap applied per connection would let sixteen
        # run at once.
        widgets = [widget('a%02d' % i, self.connection, 'tick', {'n': i}) for i in range(12)]
        widgets += [widget('b%02d' % i, self.second, 'tick', {'n': i}) for i in range(12)]
        payload = self.run_ok(self.make_report(*widgets))
        self.assertEqual(len(payload['runs']), 24)
        self.assertEqual(len(CALLS), 24)
        self.assertGreater(FLIGHT['max'], 1)
        self.assertLessEqual(FLIGHT['max'], 8)

    def test_the_timeout_clock_starts_with_the_call_not_while_it_waits_for_a_slot(self):
        # Ten calls of 0.15s against a 0.25s timeout and a cap of eight: the
        # last two wait 0.15s for a slot and then run 0.15s. None of them is
        # slower than the timeout, so none may become a 504.
        report = self.make_report(
            *[widget('w%02d' % i, self.connection, 'tick_long', {'n': i}) for i in range(10)])
        with mock.patch('mcp.endpoint._tool_timeout', return_value=0.25):
            payload = self.run_ok(report)
        self.assertEqual({entry['ok'] for entry in payload['runs'].values()}, {True})
        self.assertLessEqual(FLIGHT['max'], 8)


class RunSizeTests(RunCase):
    def test_thirty_widgets_with_thirty_distinct_reads_make_thirty_runs(self):
        widgets = [widget('w%02d' % i, self.connection, 'read_a', {'n': i}) for i in range(30)]
        payload = self.run_ok(self.make_report(*widgets))
        self.assertEqual(len(payload['runs']), 30)
        self.assertEqual(len(payload['widgets']), 30)
        self.assertEqual(len(CALLS), 30)
        self.assertEqual(
            [payload['widgets']['w%02d' % i]['run'] for i in range(30)],
            ['r%d' % (i + 1) for i in range(30)])
        for i in range(30):
            self.assertEqual(run_of(payload, 'w%02d' % i)['data']['echo'], {'n': i})

    def test_thirty_comparing_widgets_make_sixty_runs_main_before_comparison(self):
        widgets = [
            widget('w%02d' % i, self.connection, 'read_a', {'n': i, 'from': '$date.start'},
                   options={'compare': True})
            for i in range(30)
        ]
        payload = self.run_ok(self.make_report(*widgets, filters=filters_of(compare=True)))
        self.assertEqual(len(payload['runs']), 60)
        self.assertEqual(len(CALLS), 60)
        for i in range(30):
            entry = payload['widgets']['w%02d' % i]
            self.assertEqual(entry, {'run': 'r%d' % (2 * i + 1), 'prev_run': 'r%d' % (2 * i + 2)})
            self.assertEqual(run_of(payload, 'w%02d' % i)['data']['echo'], {'n': i, 'from': '2026-08-28'})
            self.assertEqual(prev_run_of(payload, 'w%02d' % i)['data']['echo'], {'n': i, 'from': '2026-07-29'})


class RunThrottleTests(RunCase):
    """The throttle's own clock is frozen so a slow machine cannot let the
    60-second window roll over mid-test, the same way test_api.py's
    ThrottleTests does; cache.clear() (in RunCase.setUp) is what starts a
    fresh window."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(SimpleRateThrottle, 'timer', return_value=1000.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_twenty_first_run_in_a_minute_is_a_429(self):
        codes = [self.post_run().status_code for _ in range(21)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)

    def test_each_person_has_their_own_allowance(self):
        for _ in range(21):
            self.post_run()
        colleague = APIClient()
        colleague.force_authenticate(make_user(self.tenant, 'colleague@acme.test'))
        self.assertEqual(self.post_run(client=colleague).status_code, 200)

    def test_running_out_of_runs_leaves_reads_and_the_connection_report_alone(self):
        for _ in range(21):
            self.post_run()
        detail = self.client.get(reverse('reports:report-detail', args=[self.report.pk]))
        self.assertEqual(detail.status_code, 200)
        connection_report = self.client.post(
            reverse('connections:connection-report', args=[self.connection.pk]),
            {'runs': [{'tool': 'read_a'}]}, format='json')
        self.assertEqual(connection_report.status_code, 200)

    def test_running_out_of_reads_leaves_runs_alone(self):
        url = reverse('reports:report-detail', args=[self.report.pk])
        codes = [self.client.get(url).status_code for _ in range(121)]
        self.assertEqual(codes[120], 429)
        self.assertEqual(self.post_run().status_code, 200)
