"""Tests for the saved-report endpoints, /api/reports/.

A report is a dashboard somebody keeps editing for months, so what has to hold
are the guarantees around it rather than the happy path: nobody sees or touches
a neighbouring organization's reports (and a guessed id looks exactly like a
missing one); a save can never store a layout the run route or the browser
would choke on, nor one that reaches into someone else's connection; two people
editing the same report cannot silently overwrite each other when they say
which version they saw; only the creator or an admin can delete; and each kind
of traffic has a ceiling of its own, so a busy builder cannot starve the page
that merely reads.

Validation is exercised through create AND update on purpose. They are two
routes into the same rules, and a rule only one of them enforces is exactly how
a bad layout gets stored.
"""
import json
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from django.core.cache import cache
from django.db import connection as db_connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from accounts.models import Tenant, User
from connections.models import Connection

from ..models import Report

PASSWORD = 'pw-for-tests-only'

# Deliberately not a registered connector: saving a layout has to care whose
# connection a widget names, never whether the connector or its tool exists
# (the connector may change after the save; the run gate handles that).
CONNECTOR = 'fakereports'

LIST_KEYS = {
    'id', 'name', 'description', 'client', 'version', 'created_by', 'created_by_name',
    'updated_by_name', 'created_at', 'updated_at', 'can_delete',
}
DETAIL_KEYS = LIST_KEYS | {'layout', 'filters', 'schema_version'}

PRESET_RANGES = ('LAST_7_DAYS', 'LAST_14_DAYS', 'LAST_30_DAYS', 'LAST_90_DAYS', 'THIS_MONTH', 'LAST_MONTH')
WIDGET_TYPES = ('kpi', 'bar', 'column', 'line', 'area', 'stacked_bar', 'donut', 'table', 'calendar')
DATE_TOKENS = ('$date.start', '$date.end', '$date.prev_start', '$date.prev_end')

# Far enough back that any real save is newer, so "moved to the top" never
# depends on how close together two saves happen to land on the clock.
LONG_AGO = datetime(2001, 1, 1, tzinfo=timezone.utc)


def list_url():
    return reverse('reports:report-list')


def detail_url(pk):
    return reverse('reports:report-detail', args=[pk])


def run_url(pk):
    return reverse('reports:report-run', args=[pk])


def default_filters():
    return {'date': {'range': 'LAST_30_DAYS'}, 'compare': False}


def make_user(tenant, email, role=User.Role.MEMBER, **extra):
    return User.objects.create_user(
        email=email, password=PASSWORD, tenant=tenant, role=role, **extra)


def make_report(tenant, created_by=None, **fields):
    fields.setdefault('name', 'A report')
    # Set explicitly so these tests do not depend on what the model's own
    # default for filters happens to be; the API default is asserted separately.
    fields.setdefault('filters', default_filters())
    return Report.objects.create(tenant=tenant, created_by=created_by, **fields)


def bulk_reports(tenant, count, created_by=None):
    """Many rows at once, straight through the ORM: the API would be far too slow."""
    Report.objects.bulk_create([
        Report(tenant=tenant, name='Bulk {0}'.format(i), created_by=created_by, filters=default_filters())
        for i in range(count)
    ])


def stamp(report, when):
    """Move updated_at. It is auto_now, so only a queryset update can set it."""
    Report.objects.filter(pk=report.pk).update(updated_at=when)


def nested_dicts(count):
    value = 1
    for _ in range(count):
        value = {'a': value}
    return value


def nested_lists(count):
    value = 1
    for _ in range(count):
        value = [value]
    return value


# MD5 is only here for speed: every test builds several users, and the real
# hasher is deliberately slow. Nothing in this module checks a password.
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReportTestCase(TestCase):
    """Two organizations, a few people in the first, and one connection each."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name='Acme')
        cls.other = Tenant.objects.create(name='Rival')
        cls.me = make_user(cls.tenant, 'me@acme.test', full_name='Mia Member')
        # No full name on purpose: the email is what shows up for this person.
        cls.colleague = make_user(cls.tenant, 'colleague@acme.test')
        cls.admin = make_user(cls.tenant, 'admin@acme.test', User.Role.ADMIN, full_name='Ada Admin')
        cls.owner = make_user(cls.tenant, 'owner@acme.test', User.Role.OWNER, full_name='Otto Owner')
        cls.rival = make_user(cls.other, 'boss@rival.test', User.Role.OWNER, full_name='Rita Rival')
        cls.connection = Connection.objects.create(tenant=cls.tenant, connector=CONNECTOR, name='Mine')
        cls.rival_connection = Connection.objects.create(tenant=cls.other, connector=CONNECTOR, name='Theirs')

    def setUp(self):
        cache.clear()  # throttle counters live in the cache
        self.client = self.client_for(self.me)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    # -- requests -----------------------------------------------------------

    # `by` picks which signed-in client makes the call. It is not called
    # `client`, because `client` is also a field of the report and arrives here
    # as part of the body.

    def create(self, by=None, **body):
        return (by or self.client).post(list_url(), body, format='json')

    def patch(self, report, by=None, **body):
        return (by or self.client).patch(detail_url(getattr(report, 'pk', report)), body, format='json')

    def put(self, report, by=None, **body):
        return (by or self.client).put(detail_url(getattr(report, 'pk', report)), body, format='json')

    def fetch(self, report, by=None):
        return (by or self.client).get(detail_url(getattr(report, 'pk', report)))

    def names(self, response):
        return [item['name'] for item in response.json()]

    # -- layout building blocks ---------------------------------------------
    # Every widget uses the caller's own connection, so a refusal in a matrix
    # can only be about the one thing the case changed.

    def source(self, drop=(), **overrides):
        base = {'connection_id': self.connection.pk, 'tool': 'read_a'}
        base.update(overrides)
        for key in drop:
            del base[key]
        return base

    def widget(self, drop=(), **overrides):
        base = {'id': 'w1', 'type': 'kpi', 'x': 0, 'y': 0, 'w': 3, 'h': 2, 'source': self.source()}
        base.update(overrides)
        for key in drop:
            del base[key]
        return base

    def widgets(self, count, connection_ids=None):
        """`count` valid, non-overlapping widgets, spread over the given connections."""
        ids = connection_ids or [self.connection.pk]
        return [
            self.widget(id='w{0}'.format(i), y=i * 2, source=self.source(connection_id=ids[i % len(ids)]))
            for i in range(count)
        ]


class ValidationCase(ReportTestCase):
    """Shared assertions: a body is refused on create AND on update, and writes nothing."""

    def setUp(self):
        super().setUp()
        self.report = make_report(
            self.tenant, created_by=self.me, name='Baseline', layout=[self.widget(id='keep')])

    def snapshot(self):
        return Report.objects.filter(pk=self.report.pk).values().get()

    def assertRefused(self, key, **body):
        # A matrix makes far more writes than the write ceiling allows in a
        # minute; the ceiling has its own tests, so it is reset here.
        cache.clear()
        count = Report.objects.count()
        response = self.client.post(list_url(), body, format='json')
        self.assertEqual(response.status_code, 400, 'create: {0}'.format(response.content[:300]))
        self.assertIn(key, response.json(), 'create: wrong error key')
        self.assertEqual(Report.objects.count(), count, 'a refused create wrote a row')

        before = self.snapshot()
        response = self.client.patch(detail_url(self.report.pk), body, format='json')
        self.assertEqual(response.status_code, 400, 'update: {0}'.format(response.content[:300]))
        self.assertIn(key, response.json(), 'update: wrong error key')
        self.assertEqual(self.snapshot(), before, 'a refused update changed the row')

    def assertAccepted(self, **body):
        cache.clear()
        created = self.client.post(list_url(), body, format='json')
        self.assertEqual(created.status_code, 201, 'create: {0}'.format(created.content[:300]))
        updated = self.client.patch(detail_url(self.report.pk), body, format='json')
        self.assertEqual(updated.status_code, 200, 'update: {0}'.format(updated.content[:300]))
        return created.json(), updated.json()


# ---------------------------------------------------------------------------
# Who may call at all
# ---------------------------------------------------------------------------

class AuthenticationTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.report = make_report(self.tenant, created_by=self.me, name='Kept')
        self.anonymous = APIClient()

    def assertRefused(self, response):
        self.assertIn(response.status_code, (401, 403))

    def assertUntouched(self):
        self.assertEqual(Report.objects.count(), 1)
        row = Report.objects.get()
        self.assertEqual((row.name, row.version), ('Kept', 1))

    def test_anonymous_cannot_list(self):
        self.assertRefused(APIClient().get(list_url()))

    def test_anonymous_cannot_read_one(self):
        self.assertRefused(APIClient().get(detail_url(self.report.pk)))

    def test_anonymous_cannot_create(self):
        self.assertRefused(self.anonymous.post(list_url(), {'name': 'Sneaky'}, format='json'))
        self.assertUntouched()

    def test_anonymous_cannot_patch(self):
        self.assertRefused(self.anonymous.patch(detail_url(self.report.pk), {'name': 'Sneaky'}, format='json'))
        self.assertUntouched()

    def test_anonymous_cannot_put(self):
        self.assertRefused(self.anonymous.put(detail_url(self.report.pk), {'name': 'Sneaky'}, format='json'))
        self.assertUntouched()

    def test_anonymous_cannot_delete(self):
        self.assertRefused(self.anonymous.delete(detail_url(self.report.pk)))
        self.assertUntouched()

    def test_anonymous_cannot_run(self):
        self.assertRefused(self.anonymous.post(run_url(self.report.pk), {}, format='json'))

    def test_a_platform_superuser_without_a_tenant_is_refused_everywhere(self):
        # A tenant-less account must not fall through to an unfiltered queryset.
        root = User.objects.create_superuser(email='root@platform.test', password=PASSWORD)
        client = self.client_for(root)
        pk = self.report.pk
        attempts = [
            ('list', lambda: client.get(list_url())),
            ('create', lambda: client.post(list_url(), {'name': 'Nope'}, format='json')),
            ('retrieve', lambda: client.get(detail_url(pk))),
            ('patch', lambda: client.patch(detail_url(pk), {'name': 'Nope'}, format='json')),
            ('put', lambda: client.put(detail_url(pk), {'name': 'Nope'}, format='json')),
            ('delete', lambda: client.delete(detail_url(pk))),
            ('run', lambda: client.post(run_url(pk), {}, format='json')),
        ]
        for label, attempt in attempts:
            with self.subTest(label):
                self.assertEqual(attempt().status_code, 403)
        self.assertUntouched()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

class TenantIsolationTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.mine = make_report(self.tenant, created_by=self.me, name='Mine')
        self.theirs = make_report(self.other, created_by=self.rival, name='Theirs')
        top = Report.objects.order_by('-pk').values_list('pk', flat=True).first()
        self.unreachable = [
            ('a neighbour report', self.theirs.pk),
            ('an id nobody owns', top + 1000),
        ]

    def assertTheirsUntouched(self):
        row = Report.objects.get(pk=self.theirs.pk)
        self.assertEqual((row.name, row.version, row.tenant_id), ('Theirs', 1, self.other.pk))

    def test_the_list_holds_only_the_callers_own_reports(self):
        self.assertEqual(self.names(self.client.get(list_url())), ['Mine'])

    def test_the_neighbour_sees_only_theirs(self):
        response = self.client_for(self.rival).get(list_url())
        self.assertEqual(self.names(response), ['Theirs'])

    def test_a_neighbours_report_cannot_be_retrieved(self):
        for label, pk in self.unreachable:
            with self.subTest(label):
                response = self.client.get(detail_url(pk))
                self.assertEqual(response.status_code, 404)
                self.assertNotIn('Theirs', response.content.decode())

    def test_a_neighbours_report_cannot_be_patched(self):
        for label, pk in self.unreachable:
            with self.subTest(label):
                self.assertEqual(self.patch(pk, name='Hijacked').status_code, 404)
        self.assertTheirsUntouched()

    def test_a_neighbours_report_cannot_be_replaced(self):
        for label, pk in self.unreachable:
            with self.subTest(label):
                self.assertEqual(self.put(pk, name='Hijacked').status_code, 404)
        self.assertTheirsUntouched()

    def test_a_neighbours_report_cannot_be_deleted_even_by_an_owner_or_admin(self):
        for label, pk in self.unreachable:
            for user in (self.me, self.admin, self.owner):
                with self.subTest('{0} as {1}'.format(label, user.email)):
                    self.assertEqual(self.client_for(user).delete(detail_url(pk)).status_code, 404)
        self.assertTheirsUntouched()

    def test_a_neighbours_report_cannot_be_run(self):
        for label, pk in self.unreachable:
            with self.subTest(label):
                self.assertEqual(self.client.post(run_url(pk), {}, format='json').status_code, 404)

    def test_the_other_side_cannot_reach_the_callers_report_either(self):
        rival = self.client_for(self.rival)
        self.assertEqual(rival.get(detail_url(self.mine.pk)).status_code, 404)
        self.assertEqual(rival.patch(detail_url(self.mine.pk), {'name': 'Hijacked'}, format='json').status_code, 404)
        self.assertEqual(rival.delete(detail_url(self.mine.pk)).status_code, 404)
        self.assertEqual(Report.objects.get(pk=self.mine.pk).name, 'Mine')

    def test_a_create_body_naming_another_tenant_still_lands_in_the_callers(self):
        # Tenant scope comes from the session, never from the client.
        for label, key in (('tenant', 'tenant'), ('tenant_id', 'tenant_id')):
            with self.subTest(label):
                response = self.create(name='Smuggled {0}'.format(label), **{key: self.other.pk})
                self.assertEqual(response.status_code, 201)
                row = Report.objects.get(pk=response.json()['id'])
                self.assertEqual(row.tenant_id, self.tenant.pk)
        self.assertEqual(Report.objects.filter(tenant=self.other).count(), 1)  # only their own

    def test_an_update_body_naming_another_tenant_does_not_move_the_report(self):
        response = self.patch(self.mine, name='Still mine', tenant=self.other.pk, tenant_id=self.other.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Report.objects.get(pk=self.mine.pk).tenant_id, self.tenant.pk)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class CreateTests(ReportTestCase):
    def test_an_empty_body_gets_the_documented_defaults(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['name'], 'Untitled report')
        self.assertEqual(body['description'], '')
        self.assertEqual(body['client'], '')
        self.assertEqual(body['layout'], [])
        self.assertEqual(body['filters'], {'date': {'range': 'LAST_30_DAYS'}, 'compare': False})
        self.assertEqual(body['version'], 1)
        self.assertEqual(body['schema_version'], 1)

    def test_the_response_is_the_full_detail_and_the_creator_may_delete(self):
        body = self.create(name='Q3 review').json()
        self.assertEqual(set(body), DETAIL_KEYS)
        self.assertEqual(body['created_by'], self.me.pk)
        self.assertEqual(body['created_by_name'], 'Mia Member')
        self.assertIs(body['can_delete'], True)
        self.assertEqual(self.fetch(body['id']).json()['name'], 'Q3 review')

    def test_the_row_belongs_to_the_caller_and_carries_the_defaults(self):
        body = self.create(name='Stored').json()
        row = Report.objects.get(pk=body['id'])
        self.assertEqual(row.tenant_id, self.tenant.pk)
        self.assertEqual(row.created_by_id, self.me.pk)
        self.assertEqual((row.version, row.schema_version), (1, 1))

    def test_a_given_name_description_and_client_are_kept(self):
        body = self.create(name='Q3', description='Quarterly numbers', client='Acme Ltd').json()
        self.assertEqual((body['name'], body['description'], body['client']), ('Q3', 'Quarterly numbers', 'Acme Ltd'))

    def test_a_blank_name_is_refused(self):
        for label, name in (('empty', ''), ('spaces', '   '), ('tab and newline', '\t\n')):
            with self.subTest(label):
                response = self.create(name=name)
                self.assertEqual(response.status_code, 400)
                self.assertIn('name', response.json())
        self.assertEqual(Report.objects.count(), 0)

    def test_name_and_client_are_stripped(self):
        body = self.create(name='  Q3 report  ', client='  Acme  ').json()
        self.assertEqual(body['name'], 'Q3 report')
        self.assertEqual(body['client'], 'Acme')

    def test_a_blank_client_is_fine(self):
        for client in ('', '   '):
            with self.subTest(repr(client)):
                response = self.create(name='No client', client=client)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()['client'], '')

    def test_a_120_character_name_is_fine_and_a_121_character_one_is_not(self):
        self.assertEqual(self.create(name='n' * 120).status_code, 201)
        response = self.create(name='n' * 121)
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json())

    def test_an_overlong_client_is_refused(self):
        self.assertEqual(self.create(name='ok', client='c' * 120).status_code, 201)
        response = self.create(name='too long', client='c' * 121)
        self.assertEqual(response.status_code, 400)
        self.assertIn('client', response.json())

    def test_an_overlong_description_is_refused(self):
        self.assertEqual(self.create(name='ok', description='d' * 500).status_code, 201)
        response = self.create(name='too long', description='d' * 501)
        self.assertEqual(response.status_code, 400)
        self.assertIn('description', response.json())

    def test_a_control_character_in_name_client_or_description_is_refused(self):
        # A NUL byte is already refused by DRF's own CharField validator
        # ("Null characters are not allowed"), which this deliberately does NOT
        # use to prove the point: \x01 has no such built-in protection, so this
        # exercises this app's own control-character check (ReportSerializer.
        # _no_control_chars), not DRF's. PostgreSQL's text storage cannot hold
        # either kind of byte, so both must end up refused before the database.
        for field in ('name', 'description', 'client'):
            with self.subTest(field):
                response = self.create(**{'name': 'ok', field: 'bad\x01value'})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.json())
        self.assertEqual(Report.objects.count(), 0)

    def test_a_nul_byte_is_also_refused(self):
        # Belt and braces: DRF's own validator covers this one specifically,
        # and this pins that down too so a future DRF change is caught.
        response = self.create(name='bad\x00value')
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json())

    def test_server_owned_keys_in_the_body_are_ignored(self):
        response = self.create(
            name='Spoofed', created_by=self.colleague.pk, updated_by=self.colleague.pk, id=987654,
            version=50, schema_version=9, created_at='2001-01-01T00:00:00Z',
            updated_at='2001-01-01T00:00:00Z')
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['created_by'], self.me.pk)
        self.assertNotEqual(body['id'], 987654)
        self.assertEqual(body['version'], 1)
        self.assertEqual(body['schema_version'], 1)
        self.assertFalse(body['created_at'].startswith('2001'))
        self.assertFalse(body['updated_at'].startswith('2001'))
        row = Report.objects.get(pk=body['id'])
        self.assertEqual(row.created_by_id, self.me.pk)
        self.assertNotEqual(row.updated_by_id, self.colleague.pk)
        self.assertFalse(Report.objects.filter(pk=987654).exists())

    def test_a_layout_and_filters_given_at_create_are_stored(self):
        layout = [self.widget()]
        filters = {'date': {'range': 'LAST_7_DAYS'}, 'compare': True}
        body = self.create(name='With content', layout=layout, filters=filters).json()
        self.assertEqual(body['layout'], layout)
        self.assertEqual(body['filters'], filters)
        row = Report.objects.get(pk=body['id'])
        self.assertEqual(row.layout, layout)
        self.assertEqual(row.filters, filters)

    def test_the_500th_report_in_a_tenant_is_fine_and_the_501st_is_refused(self):
        bulk_reports(self.tenant, 499)
        self.assertEqual(self.create(name='Number 500').status_code, 201)
        response = self.create(name='Number 501')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Report.objects.filter(tenant=self.tenant).count(), 500)

    def test_the_cap_counts_only_the_callers_own_tenant(self):
        bulk_reports(self.other, 500)
        self.assertEqual(self.create(name='First of ours').status_code, 201)

    def test_room_opens_again_after_a_delete(self):
        bulk_reports(self.tenant, 500)
        victim = Report.objects.filter(tenant=self.tenant).first()
        self.assertEqual(self.client_for(self.admin).delete(detail_url(victim.pk)).status_code, 204)
        self.assertEqual(self.create(name='Back under the cap').status_code, 201)

    def test_a_tenant_at_the_cap_can_still_edit_what_it_has(self):
        # The cap is about creating rows. A check that ran on every save would
        # lock a full workspace out of its own reports.
        bulk_reports(self.tenant, 499)
        mine = make_report(self.tenant, created_by=self.me, name='Mine')
        self.assertEqual(Report.objects.filter(tenant=self.tenant).count(), 500)
        response = self.patch(mine, name='Renamed at the cap')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.put(mine, name='Replaced at the cap').status_code, 200)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class ListTests(ReportTestCase):
    def test_an_empty_tenant_gets_an_empty_list(self):
        response = self.client.get(list_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_an_item_is_a_summary_without_layout_or_filters(self):
        make_report(
            self.tenant, created_by=self.me, name='Summary', description='About it', client='Acme',
            layout=[self.widget()])
        data = self.client.get(list_url()).json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        item = data[0]
        self.assertEqual(set(item), LIST_KEYS)
        self.assertNotIn('layout', item)
        self.assertNotIn('filters', item)
        self.assertEqual(item['name'], 'Summary')
        self.assertEqual(item['description'], 'About it')
        self.assertEqual(item['client'], 'Acme')
        self.assertEqual(item['version'], 1)
        self.assertEqual(item['created_by'], self.me.pk)
        self.assertIsInstance(item['can_delete'], bool)

    def test_the_newest_update_comes_first(self):
        # Created in a different order from how they were touched, so neither
        # creation order nor its reverse can stand in for update order.
        middle = make_report(self.tenant, name='Middle')
        newest = make_report(self.tenant, name='Newest')
        oldest = make_report(self.tenant, name='Oldest')
        stamp(middle, LONG_AGO + timedelta(days=1))
        stamp(newest, LONG_AGO + timedelta(days=2))
        stamp(oldest, LONG_AGO)
        self.assertEqual(self.names(self.client.get(list_url())), ['Newest', 'Middle', 'Oldest'])

    def test_editing_a_report_moves_it_to_the_top(self):
        older = make_report(self.tenant, created_by=self.me, name='Older')
        newer = make_report(self.tenant, created_by=self.me, name='Newer')
        stamp(older, LONG_AGO)
        stamp(newer, LONG_AGO + timedelta(days=1))
        self.assertEqual(self.names(self.client.get(list_url())), ['Newer', 'Older'])
        self.assertEqual(self.patch(older, name='Older, edited').status_code, 200)
        self.assertEqual(self.names(self.client.get(list_url())), ['Older, edited', 'Newer'])

    def _clients(self):
        make_report(self.tenant, name='r-acme', client='Acme')
        make_report(self.tenant, name='r-ACME', client='ACME')
        make_report(self.tenant, name='r-ltd', client='Acme Ltd')
        make_report(self.tenant, name='r-group', client='Acme Group')
        make_report(self.tenant, name='r-other', client='Other')
        make_report(self.tenant, name='r-none', client='')
        make_report(self.other, name='r-rival', client='Acme')

    def test_client_matches_exactly_ignoring_case(self):
        self._clients()
        for query in ('acme', 'Acme', 'ACME', 'aCmE'):
            with self.subTest(query):
                names = self.names(self.client.get(list_url(), {'client': query}))
                self.assertCountEqual(names, ['r-acme', 'r-ACME'])

    def test_client_is_not_a_substring_match(self):
        self._clients()
        names = self.names(self.client.get(list_url(), {'client': 'Acm'}))
        self.assertEqual(names, [])

    def test_client_filter_never_reaches_into_a_neighbours_reports(self):
        self._clients()
        names = self.names(self.client.get(list_url(), {'client': 'acme'}))
        self.assertNotIn('r-rival', names)

    def test_a_client_that_nobody_has_gives_an_empty_list(self):
        self._clients()
        self.assertEqual(self.client.get(list_url(), {'client': 'Nobody'}).json(), [])

    def test_q_finds_a_substring_of_the_name_ignoring_case(self):
        make_report(self.tenant, name='The Needle report')
        make_report(self.tenant, name='Haystack', client='Straw', description='Just hay')
        for query in ('needle', 'NEEDLE', 'eedl'):
            with self.subTest(query):
                self.assertEqual(self.names(self.client.get(list_url(), {'q': query})), ['The Needle report'])

    def test_q_finds_a_substring_of_the_client_ignoring_case(self):
        make_report(self.tenant, name='One', client='Needle Industries')
        make_report(self.tenant, name='Two', client='Straw Co', description='nothing here')
        for query in ('needle', 'NEEDLE', 'dle ind'):
            with self.subTest(query):
                self.assertEqual(self.names(self.client.get(list_url(), {'q': query})), ['One'])

    def test_q_finds_a_substring_of_the_description_ignoring_case(self):
        make_report(self.tenant, name='One', description='Has a NEEDLE inside')
        make_report(self.tenant, name='Two', description='Only hay', client='Straw Co')
        for query in ('needle', 'Needle', 'a needle in'):
            with self.subTest(query):
                self.assertEqual(self.names(self.client.get(list_url(), {'q': query})), ['One'])

    def test_q_returns_a_report_once_however_many_fields_match(self):
        make_report(self.tenant, name='Needle', client='Needle', description='Needle')
        self.assertEqual(self.names(self.client.get(list_url(), {'q': 'needle'})), ['Needle'])

    def test_q_treats_wildcard_characters_literally(self):
        make_report(self.tenant, name='100% growth')
        make_report(self.tenant, name='plain growth')
        make_report(self.tenant, name='snake_case')
        make_report(self.tenant, name='snakeXcase')
        make_report(self.tenant, name='a.*b')
        make_report(self.tenant, name='axxb')
        for query, expected in (('%', ['100% growth']), ('_', ['snake_case']), ('.*', ['a.*b'])):
            with self.subTest(query):
                self.assertEqual(self.names(self.client.get(list_url(), {'q': query})), expected)

    def test_q_never_reaches_into_a_neighbours_reports(self):
        make_report(self.other, name='Needle for the rival')
        self.assertEqual(self.client.get(list_url(), {'q': 'needle'}).json(), [])

    def test_client_and_q_combine_as_and(self):
        make_report(self.tenant, name='Alpha launch', client='Acme')
        make_report(self.tenant, name='Beta', client='Acme')
        make_report(self.tenant, name='Alpha launch', client='Zed')
        response = self.client.get(list_url(), {'client': 'acme', 'q': 'alpha'})
        data = response.json()
        self.assertEqual([(item['name'], item['client']) for item in data], [('Alpha launch', 'Acme')])

    def test_only_500_rows_come_back_when_the_tenant_holds_more(self):
        bulk_reports(self.tenant, 501)
        oldest = Report.objects.filter(tenant=self.tenant).order_by('pk').first()
        stamp(oldest, LONG_AGO)
        response = self.client.get(list_url())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 500)
        # The cut falls on the oldest, not on an arbitrary row.
        self.assertNotIn(oldest.pk, [item['id'] for item in data])

    def test_can_delete_says_whether_this_caller_may_delete_each_report(self):
        make_report(self.tenant, created_by=self.me, name='Mine')
        make_report(self.tenant, created_by=self.colleague, name='Colleagues')
        make_report(self.tenant, created_by=None, name='Orphan')
        expectations = [
            (self.me, {'Mine': True, 'Colleagues': False, 'Orphan': False}),
            (self.colleague, {'Mine': False, 'Colleagues': True, 'Orphan': False}),
            (self.admin, {'Mine': True, 'Colleagues': True, 'Orphan': True}),
            (self.owner, {'Mine': True, 'Colleagues': True, 'Orphan': True}),
        ]
        for user, expected in expectations:
            with self.subTest(user.email):
                data = self.client_for(user).get(list_url()).json()
                self.assertEqual({item['name']: item['can_delete'] for item in data}, expected)
                self.assertTrue(all(isinstance(item['can_delete'], bool) for item in data))

    def test_who_made_and_who_last_touched_a_report_fall_back_from_name_to_email_to_nothing(self):
        make_report(self.tenant, created_by=self.me, updated_by=self.colleague, name='named-then-email')
        make_report(self.tenant, created_by=self.colleague, updated_by=self.me, name='email-then-named')
        make_report(self.tenant, created_by=None, updated_by=None, name='nobody')
        by_name = {item['name']: item for item in self.client.get(list_url()).json()}

        item = by_name['named-then-email']
        self.assertEqual((item['created_by_name'], item['updated_by_name']), ('Mia Member', 'colleague@acme.test'))
        item = by_name['email-then-named']
        self.assertEqual((item['created_by_name'], item['updated_by_name']), ('colleague@acme.test', 'Mia Member'))
        item = by_name['nobody']
        self.assertEqual((item['created_by'], item['created_by_name'], item['updated_by_name']), (None, '', ''))

    def test_a_report_outlives_its_creator(self):
        leaver = make_user(self.tenant, 'leaver@acme.test', full_name='Lee Aver')
        make_report(self.tenant, created_by=leaver, updated_by=leaver, name='Left behind')
        leaver.delete()
        item = self.client.get(list_url()).json()[0]
        self.assertIsNone(item['created_by'])
        self.assertEqual(item['created_by_name'], '')
        self.assertEqual(item['updated_by_name'], '')


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

class DetailTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.layout = [self.widget(id='a', title='Clicks'), self.widget(id='b', y=2, type='bar')]
        self.filters = {'date': {'range': 'LAST_7_DAYS'}, 'compare': True}
        self.report = make_report(
            self.tenant, created_by=self.me, updated_by=self.colleague, name='Full', description='About',
            client='Acme', layout=self.layout, filters=self.filters)
        Report.objects.filter(pk=self.report.pk).update(version=4)

    def test_the_detail_has_the_full_shape_and_the_stored_values(self):
        response = self.fetch(self.report)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), DETAIL_KEYS)
        self.assertEqual(body['id'], self.report.pk)
        self.assertEqual(body['name'], 'Full')
        self.assertEqual(body['description'], 'About')
        self.assertEqual(body['client'], 'Acme')
        self.assertEqual(body['layout'], self.layout)
        self.assertEqual(body['filters'], self.filters)
        self.assertEqual(body['schema_version'], 1)
        self.assertEqual(body['version'], 4)
        self.assertEqual(body['created_by'], self.me.pk)
        self.assertEqual(body['created_by_name'], 'Mia Member')
        self.assertEqual(body['updated_by_name'], 'colleague@acme.test')
        self.assertIsInstance(body['created_at'], str)
        self.assertIsInstance(body['updated_at'], str)
        self.assertIs(body['can_delete'], True)

    def test_can_delete_on_the_detail_follows_the_same_rule_as_the_list(self):
        orphan = make_report(self.tenant, created_by=None, name='Orphan')
        expectations = [
            (self.me, self.report, True), (self.me, orphan, False),
            (self.colleague, self.report, False), (self.colleague, orphan, False),
            (self.admin, self.report, True), (self.admin, orphan, True),
            (self.owner, self.report, True), (self.owner, orphan, True),
        ]
        for user, report, expected in expectations:
            with self.subTest('{0} on {1}'.format(user.email, report.name)):
                self.assertIs(self.fetch(report, self.client_for(user)).json()['can_delete'], expected)

    def test_names_fall_back_from_full_name_to_email_to_nothing(self):
        orphan = make_report(self.tenant, created_by=None, updated_by=None, name='Orphan')
        bare = make_report(self.tenant, created_by=self.colleague, updated_by=self.me, name='Bare')
        body = self.fetch(orphan).json()
        self.assertEqual((body['created_by'], body['created_by_name'], body['updated_by_name']), (None, '', ''))
        body = self.fetch(bare).json()
        self.assertEqual((body['created_by_name'], body['updated_by_name']), ('colleague@acme.test', 'Mia Member'))

    def test_a_colleague_may_read_a_report_they_did_not_make(self):
        response = self.fetch(self.report, self.client_for(self.colleague))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['name'], 'Full')


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class UpdateTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.layout = [self.widget(id='a'), self.widget(id='b', y=2)]
        self.filters = {'date': {'range': 'LAST_7_DAYS'}, 'compare': True}
        self.report = make_report(
            self.tenant, created_by=self.me, name='Before', description='About', client='Acme',
            layout=self.layout, filters=self.filters)

    def row(self):
        return Report.objects.get(pk=self.report.pk)

    def test_a_rename_bumps_the_version_by_exactly_one_and_stamps_the_editor(self):
        response = self.patch(self.report, self.client_for(self.colleague), name='After')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['name'], 'After')
        self.assertEqual(body['version'], 2)
        self.assertEqual(body['created_by'], self.me.pk)
        self.assertEqual(body['updated_by_name'], 'colleague@acme.test')
        row = self.row()
        self.assertEqual(row.version, 2)
        self.assertEqual(row.updated_by_id, self.colleague.pk)
        self.assertEqual(row.created_by_id, self.me.pk)

    def test_editing_anything_else_bumps_the_version_too(self):
        self.assertEqual(self.patch(self.report, description='New words').json()['version'], 2)
        self.assertEqual(self.patch(self.report, client='Other').json()['version'], 3)
        self.assertEqual(self.patch(self.report, layout=[]).json()['version'], 4)
        self.assertEqual(self.patch(self.report, filters=default_filters()).json()['version'], 5)
        self.assertEqual(self.row().version, 5)

    def test_a_partial_update_leaves_what_it_did_not_name_alone(self):
        body = self.patch(self.report, name='Only the name').json()
        self.assertEqual(body['description'], 'About')
        self.assertEqual(body['client'], 'Acme')
        self.assertEqual(body['layout'], self.layout)
        self.assertEqual(body['filters'], self.filters)
        row = self.row()
        self.assertEqual((row.description, row.client, row.layout, row.filters),
                         ('About', 'Acme', self.layout, self.filters))

    def test_the_response_is_the_full_detail_and_agrees_with_a_fresh_read(self):
        body = self.patch(self.report, name='After').json()
        self.assertEqual(set(body), DETAIL_KEYS)
        self.assertEqual(body, self.fetch(self.report).json())

    def test_the_layout_is_replaced_wholesale_not_merged(self):
        replacement = [self.widget(id='c', type='bar', x=4)]
        body = self.patch(self.report, layout=replacement).json()
        self.assertEqual(body['layout'], replacement)
        self.assertEqual(self.row().layout, replacement)

    def test_the_layout_can_be_emptied(self):
        self.assertEqual(self.patch(self.report, layout=[]).json()['layout'], [])
        self.assertEqual(self.row().layout, [])

    def test_the_filters_are_replaced_wholesale_not_merged(self):
        custom = {'date': {'range': 'CUSTOM', 'start': '2026-01-01', 'end': '2026-01-31'}, 'compare': True}
        self.assertEqual(self.patch(self.report, filters=custom).status_code, 200)
        body = self.patch(self.report, filters={'date': {'range': 'LAST_14_DAYS'}}).json()
        self.assertEqual(body['filters']['date'], {'range': 'LAST_14_DAYS'})
        self.assertFalse(body['filters'].get('compare', False))
        self.assertEqual(self.row().filters['date'], {'range': 'LAST_14_DAYS'})

    def test_writing_a_layout_stamps_the_current_schema_version(self):
        Report.objects.filter(pk=self.report.pk).update(schema_version=0)
        body = self.patch(self.report, layout=[self.widget(id='fresh')]).json()
        self.assertEqual(body['schema_version'], 1)
        self.assertEqual(self.row().schema_version, 1)

    def test_an_update_that_does_not_touch_layout_leaves_schema_version_alone(self):
        # "schema_version is set ... whenever layout is written" -- a rename is
        # not that. A future schema v2 would otherwise have an unrelated PATCH
        # silently mark an old, unmigrated report's layout as already upgraded.
        Report.objects.filter(pk=self.report.pk).update(schema_version=0)
        body = self.patch(self.report, name='Renamed only').json()
        self.assertEqual(body['schema_version'], 0)
        self.assertEqual(self.row().schema_version, 0)

    def test_an_update_moves_updated_at_forward(self):
        stamp(self.report, LONG_AGO)
        self.assertEqual(self.patch(self.report, name='Touched').status_code, 200)
        self.assertGreater(self.row().updated_at, LONG_AGO + timedelta(days=1))

    def test_server_owned_keys_in_the_body_are_ignored(self):
        before = self.row()
        response = self.patch(
            self.report, name='Renamed', id=self.report.pk + 1000, tenant=self.other.pk,
            tenant_id=self.other.pk, created_by=self.colleague.pk, updated_by=self.colleague.pk,
            schema_version=42, created_at='2001-01-01T00:00:00Z', updated_at='2001-01-01T00:00:00Z')
        self.assertEqual(response.status_code, 200)
        row = self.row()
        self.assertEqual(row.name, 'Renamed')
        self.assertEqual(row.tenant_id, self.tenant.pk)
        self.assertEqual(row.created_by_id, self.me.pk)
        self.assertEqual(row.updated_by_id, self.me.pk)  # the caller, whatever the body said
        self.assertEqual(row.schema_version, 1)
        self.assertEqual(row.created_at, before.created_at)
        self.assertNotEqual(row.updated_at.year, 2001)
        self.assertFalse(Report.objects.filter(pk=self.report.pk + 1000).exists())
        body = response.json()
        self.assertEqual(body['id'], self.report.pk)
        self.assertEqual(body['created_by'], self.me.pk)

    def test_a_put_without_a_name_is_refused_and_changes_nothing(self):
        before = Report.objects.filter(pk=self.report.pk).values().get()
        response = self.put(self.report, description='No name given', client='Other')
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json())
        self.assertEqual(Report.objects.filter(pk=self.report.pk).values().get(), before)

    def test_a_put_with_everything_replaces_everything(self):
        layout = [self.widget(id='z', type='line')]
        filters = {'date': {'range': 'THIS_MONTH'}, 'compare': False}
        response = self.put(
            self.report, name='Replaced', description='New', client='Zed', layout=layout, filters=filters)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body['name'], body['description'], body['client']), ('Replaced', 'New', 'Zed'))
        self.assertEqual(body['layout'], layout)
        self.assertEqual(body['filters'], filters)
        self.assertEqual(body['version'], 2)

    def test_a_blank_name_is_refused_on_update(self):
        for label, name in (('empty', ''), ('spaces', '   ')):
            for verb in ('patch', 'put'):
                with self.subTest('{0} via {1}'.format(label, verb)):
                    response = getattr(self, verb)(self.report, name=name)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('name', response.json())
        row = self.row()
        self.assertEqual((row.name, row.version), ('Before', 1))

    def test_name_and_client_are_stripped_on_update(self):
        body = self.patch(self.report, name='  Trimmed  ', client='  Zed  ').json()
        self.assertEqual((body['name'], body['client']), ('Trimmed', 'Zed'))

    def test_a_client_can_be_cleared(self):
        self.assertEqual(self.patch(self.report, client='').json()['client'], '')

    def test_overlong_values_are_refused_on_update(self):
        for key, limit in (('name', 120), ('client', 120), ('description', 500)):
            with self.subTest(key):
                self.assertEqual(self.patch(self.report, **{key: 'x' * limit}).status_code, 200)
                response = self.patch(self.report, **{key: 'x' * (limit + 1)})
                self.assertEqual(response.status_code, 400)
                self.assertIn(key, response.json())


# ---------------------------------------------------------------------------
# Optimistic concurrency
# ---------------------------------------------------------------------------

class OptimisticConcurrencyTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.report = make_report(
            self.tenant, created_by=self.me, name='Original', description='Kept', client='Acme',
            layout=[self.widget(id='old')])
        # Stored at 3; the "other tab" below last saw 1.
        Report.objects.filter(pk=self.report.pk).update(version=3)

    def row(self):
        return Report.objects.get(pk=self.report.pk)

    def stale_body(self, **extra):
        body = {
            'name': 'Hijack', 'description': 'Overwritten', 'client': 'Elsewhere',
            'layout': [self.widget(id='new')], 'filters': {'date': {'range': 'LAST_90_DAYS'}},
        }
        body.update(extra)
        return body

    def test_a_stale_version_is_a_409_and_changes_nothing_at_all(self):
        before = self.fetch(self.report).json()
        stored = Report.objects.filter(pk=self.report.pk).values().get()

        response = self.patch(self.report, **self.stale_body(version=1))

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertIn('detail', body)
        self.assertIsInstance(body['detail'], str)
        self.assertEqual(body['current_version'], 3)
        self.assertIs(type(body['current_version']), int)  # a JSON integer, not "3"
        # Nothing: not the fields, not the version, not who touched it, not when.
        self.assertEqual(self.fetch(self.report).json(), before)
        self.assertEqual(Report.objects.filter(pk=self.report.pk).values().get(), stored)

    def test_a_version_from_the_future_is_a_409_too(self):
        response = self.patch(self.report, **self.stale_body(version=4))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['current_version'], 3)
        self.assertEqual(self.row().name, 'Original')

    def test_a_put_is_held_to_the_same_version_check(self):
        response = self.put(self.report, **self.stale_body(version=2))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['current_version'], 3)
        self.assertEqual(self.row().name, 'Original')

    def test_a_matching_version_succeeds_and_moves_on_by_one(self):
        response = self.patch(self.report, name='Agreed', version=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['version'], 4)
        self.assertEqual(self.row().name, 'Agreed')
        self.assertEqual(self.row().version, 4)

    def test_leaving_the_version_out_means_last_write_wins(self):
        response = self.patch(self.report, name='Whoever comes last')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['version'], 4)
        self.assertEqual(self.row().name, 'Whoever comes last')

    def test_a_version_that_is_not_an_integer_is_never_applied(self):
        for label, value in (('text', 'abc'), ('a fraction', 1.5), ('a list', [3])):
            with self.subTest(label):
                response = self.patch(self.report, **self.stale_body(version=value))
                self.assertIn(response.status_code, (400, 409))
        row = self.row()
        self.assertEqual((row.name, row.version), ('Original', 3))

    def test_each_save_carrying_the_version_from_the_last_one_succeeds(self):
        created = self.create(name='Fresh').json()
        self.assertEqual(created['version'], 1)

        first = self.patch(created['id'], name='First', version=created['version'])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['version'], 2)

        second = self.patch(created['id'], name='Second', version=first.json()['version'])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['version'], 3)
        self.assertEqual(Report.objects.get(pk=created['id']).name, 'Second')

    def test_reusing_an_earlier_response_version_is_a_409(self):
        created = self.create(name='Fresh').json()
        first = self.patch(created['id'], name='First', version=created['version']).json()

        # The create response's version is already spent.
        replay = self.patch(created['id'], name='Replay of create', version=created['version'])
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()['current_version'], 2)

        second = self.patch(created['id'], name='Second', version=first['version'])
        self.assertEqual(second.status_code, 200)

        # So is the first response's, once a second save has happened.
        replay = self.patch(created['id'], name='Replay of first', version=first['version'])
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()['current_version'], 3)
        row = Report.objects.get(pk=created['id'])
        self.assertEqual((row.name, row.version), ('Second', 3))

    def test_a_colleagues_save_makes_your_version_stale(self):
        created = self.create(name='Shared').json()
        theirs = self.patch(created['id'], self.client_for(self.colleague), name='Theirs', version=1)
        self.assertEqual(theirs.status_code, 200)
        mine = self.patch(created['id'], name='Mine', version=1)
        self.assertEqual(mine.status_code, 409)
        self.assertEqual(Report.objects.get(pk=created['id']).name, 'Theirs')

    def test_a_version_in_a_create_body_is_ignored(self):
        body = self.create(name='Numbered', version=50).json()
        self.assertEqual(body['version'], 1)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class DeleteTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.report = make_report(self.tenant, created_by=self.me, name='Mine')

    def remove(self, user, report=None):
        return self.client_for(user).delete(detail_url((report or self.report).pk))

    def exists(self, report=None):
        return Report.objects.filter(pk=(report or self.report).pk).exists()

    def test_the_creator_can_delete_their_own_report(self):
        self.assertEqual(self.remove(self.me).status_code, 204)
        self.assertFalse(self.exists())

    def test_a_deleted_report_is_gone_from_reads(self):
        self.remove(self.me)
        self.assertEqual(self.fetch(self.report).status_code, 404)
        self.assertEqual(self.client.get(list_url()).json(), [])

    def test_deleting_twice_is_a_404_the_second_time(self):
        self.assertEqual(self.remove(self.me).status_code, 204)
        self.assertEqual(self.remove(self.me).status_code, 404)

    def test_a_different_member_may_not_delete_it(self):
        response = self.remove(self.colleague)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.exists())
        self.assertEqual(Report.objects.get(pk=self.report.pk).name, 'Mine')

    def test_an_admin_may_delete_a_report_they_did_not_make(self):
        self.assertEqual(self.remove(self.admin).status_code, 204)
        self.assertFalse(self.exists())

    def test_an_owner_may_delete_a_report_they_did_not_make(self):
        self.assertEqual(self.remove(self.owner).status_code, 204)
        self.assertFalse(self.exists())

    def test_without_a_creator_a_member_may_not_delete(self):
        orphan = make_report(self.tenant, created_by=None, name='Orphan')
        self.assertEqual(self.remove(self.me, orphan).status_code, 403)
        self.assertEqual(self.remove(self.colleague, orphan).status_code, 403)
        self.assertTrue(self.exists(orphan))

    def test_without_a_creator_an_admin_or_owner_may(self):
        for user in (self.admin, self.owner):
            with self.subTest(user.email):
                orphan = make_report(self.tenant, created_by=None, name='Orphan')
                self.assertEqual(self.remove(user, orphan).status_code, 204)
                self.assertFalse(self.exists(orphan))

    def test_a_report_whose_creator_was_deleted_is_admin_only(self):
        leaver = make_user(self.tenant, 'leaver@acme.test')
        orphan = make_report(self.tenant, created_by=leaver, name='Orphaned by deletion')
        leaver.delete()
        self.assertIsNone(Report.objects.get(pk=orphan.pk).created_by_id)
        self.assertEqual(self.remove(self.colleague, orphan).status_code, 403)
        self.assertTrue(self.exists(orphan))
        self.assertEqual(self.remove(self.admin, orphan).status_code, 204)

    def test_another_tenants_report_is_a_404_even_for_an_owner(self):
        theirs = make_report(self.other, created_by=self.rival, name='Theirs')
        for user in (self.me, self.admin, self.owner):
            with self.subTest(user.email):
                self.assertEqual(self.remove(user, theirs).status_code, 404)
        self.assertTrue(self.exists(theirs))

    def test_an_unknown_id_is_a_404(self):
        response = self.client.delete(detail_url(self.report.pk + 1000))
        self.assertEqual(response.status_code, 404)

    def test_someone_who_may_not_delete_may_still_edit(self):
        # Delete is the one restricted verb; any member may update.
        self.assertEqual(self.patch(self.report, self.client_for(self.colleague), name='Edited').status_code, 200)


# ---------------------------------------------------------------------------
# Layout validation
# ---------------------------------------------------------------------------

class LayoutValidationTests(ValidationCase):
    PLACES = ('fields', 'options', 'args')

    def each_refused(self, cases):
        for label, layout in cases.items():
            with self.subTest(label):
                self.assertRefused('layout', layout=layout)

    def each_accepted(self, cases):
        for label, layout in cases.items():
            with self.subTest(label):
                self.assertAccepted(layout=layout)

    def holding(self, place, value):
        """A valid widget whose fields, options or args is `value`."""
        if place == 'args':
            return self.widget(source=self.source(args=value))
        return self.widget(**{place: value})

    def check_opaque(self, refused=None, accepted=None, places=None):
        """The same bound must hold in fields, options and source.args alike."""
        for place in places or self.PLACES:
            for label, value in (refused or {}).items():
                with self.subTest('{0} refused in {1}'.format(label, place)):
                    self.assertRefused('layout', layout=[self.holding(place, value)])
            for label, value in (accepted or {}).items():
                with self.subTest('{0} accepted in {1}'.format(label, place)):
                    self.assertAccepted(layout=[self.holding(place, value)])

    # -- shape of the whole layout -------------------------------------------

    def test_an_empty_layout_is_valid(self):
        created, updated = self.assertAccepted(layout=[])
        self.assertEqual(created['layout'], [])
        self.assertEqual(updated['layout'], [])

    def test_a_minimal_widget_is_accepted_and_keeps_what_it_was_given(self):
        created, updated = self.assertAccepted(layout=[self.widget()])
        for body in (created, updated):
            saved = body['layout'][0]
            self.assertEqual(
                {key: saved[key] for key in ('id', 'type', 'x', 'y', 'w', 'h')},
                {'id': 'w1', 'type': 'kpi', 'x': 0, 'y': 0, 'w': 3, 'h': 2})
            self.assertEqual(saved['source']['connection_id'], self.connection.pk)
            self.assertEqual(saved['source']['tool'], 'read_a')
            # Defaults may be written back or left out; either way they are empty.
            self.assertEqual(saved.get('fields', {}), {})
            self.assertEqual(saved.get('options', {}), {})
            self.assertEqual(saved['source'].get('args', {}), {})

    def test_a_full_layout_round_trips_exactly_and_in_order(self):
        layout = [
            self.widget(id='c', type='donut', x=6, y=4, w=6, h=3, title='Third first',
                        fields={'label': 'campaign', 'value': 'cost'}, options={'compare': True, 'top': 5},
                        source=self.source(tool='by_campaign', args={'from': '$date.start', 'to': '$date.end'})),
            self.widget(id='a', type='line', x=0, y=0, w=12, h=4, title='Trend',
                        fields={'x': 'date', 'y': ['clicks', 'cost']}, options={},
                        source=self.source(tool='daily', args={'limit': 10})),
            self.widget(id='b', type='table', x=0, y=4, w=6, h=3, title='',
                        fields={}, options={'sort': {'by': 'cost', 'desc': True}}, source=self.source(args={})),
        ]
        created, updated = self.assertAccepted(layout=layout)
        self.assertEqual(created['layout'], layout)
        self.assertEqual(updated['layout'], layout)
        self.assertEqual(self.fetch(created['id']).json()['layout'], layout)
        self.assertEqual(Report.objects.get(pk=created['id']).layout, layout)

    def test_every_widget_type_is_accepted(self):
        for widget_type in WIDGET_TYPES:
            with self.subTest(widget_type):
                self.assertAccepted(layout=[self.widget(type=widget_type)])

    def test_the_layout_must_be_a_list(self):
        self.each_refused({
            'an object': {'id': 'w1'},
            'a string': 'w1',
            'a number': 5,
            'true': True,
            'an object holding a list': {'widgets': [self.widget()]},
        })

    def test_every_entry_must_be_an_object(self):
        self.each_refused({
            'a number': [1], 'a string': ['w1'], 'null': [None], 'a list': [[]], 'true': [True],
            'a good widget then junk': [self.widget(), 'junk'],
        })

    def test_thirty_widgets_are_fine_and_thirty_one_are_not(self):
        self.assertAccepted(layout=self.widgets(30))
        self.assertRefused('layout', layout=self.widgets(31))

    def test_duplicate_widget_ids_are_refused(self):
        far_apart = self.widgets(5)
        far_apart[4]['id'] = 'w0'
        self.each_refused({
            'adjacent': [self.widget(id='same'), self.widget(id='same', y=2)],
            'far apart': far_apart,
        })

    # -- one widget ------------------------------------------------------------

    def test_a_widget_id_must_be_1_to_40_letters_digits_underscores_or_hyphens(self):
        refused = {
            'empty': '', '41 characters': 'a' * 41, 'a space': 'a b', 'a dot': 'a.b', 'a slash': 'a/b',
            'a colon': 'a:b', 'a non-ascii letter': 'café', 'a non-ascii digit': '٣',
            # `$` in a regex matches before a trailing newline; the id must not.
            'a trailing newline': 'w1\n', 'a leading newline': '\nw1',
            'a number': 5, 'null': None, 'true': True, 'a list': ['w1'], 'an object': {'id': 'w1'},
        }
        self.each_refused({label: [self.widget(id=value)] for label, value in refused.items()})

    def test_the_edges_of_the_id_rule_are_accepted(self):
        self.each_accepted({
            'one character': [self.widget(id='a')],
            '40 characters': [self.widget(id='a' * 40)],
            'every allowed kind': [self.widget(id='Aa_-09')],
        })

    def test_an_unknown_widget_type_is_refused(self):
        refused = {
            'pie': 'pie', 'scatter': 'scatter', 'empty': '', 'wrong case': 'KPI', 'padded': ' kpi',
            'a number': 5, 'null': None, 'true': True, 'a list': ['kpi'], 'an object': {'kpi': 1},
        }
        self.each_refused({label: [self.widget(type=value)] for label, value in refused.items()})

    def test_a_missing_required_key_is_refused(self):
        self.each_refused({
            key: [self.widget(drop=(key,))] for key in ('id', 'type', 'x', 'y', 'w', 'h', 'source')
        })

    def test_an_unknown_widget_key_is_refused(self):
        self.each_refused({
            key: [self.widget(**{key: 'anything'})]
            for key in ('color', 'Title', 'connection_id', 'tool', 'args', 'sort')
        })

    def test_grid_numbers_must_be_integers(self):
        bad = {
            'a fraction': 1.5, 'a whole-number float': 2.0, 'a string': '1', 'true': True, 'false': False,
            'null': None, 'a list': [1], 'an object': {},
        }
        cases = {}
        for key in ('x', 'y', 'w', 'h'):
            for label, value in bad.items():
                cases['{0} as {1}'.format(key, label)] = [self.widget(**{key: value})]
        self.each_refused(cases)

    def test_x_and_y_may_not_be_negative(self):
        self.each_refused({
            'x': [self.widget(x=-1)], 'y': [self.widget(y=-1)], 'x and y': [self.widget(x=-3, y=-3)],
        })

    def test_a_widget_is_at_least_one_cell_wide_and_tall(self):
        self.each_refused({
            'w=0': [self.widget(w=0)], 'h=0': [self.widget(h=0)],
            'w=-1': [self.widget(w=-1)], 'h=-1': [self.widget(h=-1)],
        })

    def test_a_widget_may_not_run_off_the_right_edge_of_the_12_column_grid(self):
        self.each_refused({
            'x=10 w=3': [self.widget(x=10, w=3)],
            'w=13': [self.widget(x=0, w=13)],
            'x=12 w=1': [self.widget(x=12, w=1)],
            'a huge x': [self.widget(x=2 ** 31, w=1)],
        })
        self.each_accepted({
            'x+w=12 at the edge': [self.widget(x=9, w=3)],
            'the full width': [self.widget(x=0, w=12)],
            'the last column': [self.widget(x=11, w=1)],
        })

    def test_a_widget_may_not_run_past_row_500(self):
        self.each_refused({
            'y=499 h=2': [self.widget(y=499, h=2)],
            'h=501': [self.widget(y=0, h=501)],
            'y=500 h=1': [self.widget(y=500, h=1)],
            'a huge y': [self.widget(y=2 ** 31, h=1)],
        })
        self.each_accepted({
            'y+h=500 at the edge': [self.widget(y=499, h=1)],
            'a full-height widget': [self.widget(y=0, h=500)],
            'split down the middle': [self.widget(y=250, h=250)],
        })

    def test_a_title_is_a_string_of_at_most_120_characters(self):
        self.each_accepted({
            '120 characters': [self.widget(title='t' * 120)],
            '120 multi-byte characters': [self.widget(title='é' * 120)],
            'empty': [self.widget(title='')],
        })
        self.each_refused({
            '121 characters': [self.widget(title='t' * 121)],
            '121 multi-byte characters': [self.widget(title='é' * 121)],
            'a number': [self.widget(title=5)],
            'a list': [self.widget(title=['t'])],
            'an object': [self.widget(title={'t': 1})],
        })

    # -- source ----------------------------------------------------------------

    def test_source_must_be_an_object(self):
        self.each_refused({
            'a list': [self.widget(source=[])],
            'a string': [self.widget(source='read_a')],
            'a number': [self.widget(source=5)],
            'null': [self.widget(source=None)],
        })

    def test_source_takes_only_its_three_keys(self):
        self.each_refused({
            'an unknown key': [self.widget(source=self.source(extra='x'))],
            'a camelCase twin of a real key': [self.widget(source=self.source(connectionId=self.connection.pk))],
        })

    def test_source_needs_a_connection_and_a_tool(self):
        self.each_refused({
            'no connection': [self.widget(source=self.source(drop=('connection_id',)))],
            'no tool': [self.widget(source=self.source(drop=('tool',)))],
            'an empty source': [self.widget(source={})],
        })

    def test_the_connection_id_must_be_a_positive_integer(self):
        real = self.connection.pk
        refused = {
            'true': True, 'false': False, 'zero': 0, 'negative': -1, 'a string': str(real),
            'a fraction': 1.5, 'null': None, 'a list': [real], 'an object': {'id': real},
        }
        self.each_refused({
            label: [self.widget(source=self.source(connection_id=value))] for label, value in refused.items()
        })

    def test_a_tool_is_1_to_64_letters_digits_or_underscores(self):
        refused = {
            'empty': '', '65 characters': 'x' * 65, 'a hyphen': 'read-a', 'a space': 'read a',
            'a dot': 'read.a', 'a trailing newline': 'read_a\n', 'a non-ascii letter': 'lecture_é',
            'a number': 5, 'null': None, 'true': True, 'a list': ['read_a'], 'an object': {'a': 1},
        }
        self.each_refused({
            label: [self.widget(source=self.source(tool=value))] for label, value in refused.items()
        })

    def test_the_edges_of_the_tool_rule_are_accepted(self):
        self.each_accepted({
            '64 characters': [self.widget(source=self.source(tool='x' * 64))],
            'mixed case and digits': [self.widget(source=self.source(tool='Read_A_9'))],
            'a lone underscore': [self.widget(source=self.source(tool='_'))],
        })

    def test_a_tool_that_does_not_exist_is_still_accepted_on_save(self):
        # The connector can change after a save; only the run gate judges tools.
        self.assertAccepted(layout=[self.widget(source=self.source(tool='no_such_tool_anywhere'))])

    def test_args_must_be_an_object(self):
        self.each_refused({
            'a list': [self.widget(source=self.source(args=[]))],
            'a string': [self.widget(source=self.source(args='x'))],
            'a number': [self.widget(source=self.source(args=5))],
            'true': [self.widget(source=self.source(args=True))],
            'a list of objects': [self.widget(source=self.source(args=[{}]))],
        })

    def test_args_are_optional(self):
        self.assertAccepted(layout=[self.widget(source=self.source())])
        self.assertAccepted(layout=[self.widget(source=self.source(args={}))])

    def test_fields_and_options_must_be_objects(self):
        cases = {}
        for key in ('fields', 'options'):
            for label, value in (('a list', []), ('a string', 'x'), ('a number', 5), ('true', True),
                                 ('a list of objects', [{}])):
                cases['{0} as {1}'.format(key, label)] = [self.widget(**{key: value})]
        self.each_refused(cases)

    # -- the opaque objects ------------------------------------------------------

    def test_any_json_type_is_fine_inside_the_opaque_objects(self):
        rich = {
            'columns': ['clicks', 'cost'], 'flag': True, 'off': False, 'nothing': None, 'ratio': 1.5,
            'count': 3, 'negative': -7, 'nested': {'x': [1, {'y': 'z'}]}, 'text': 'héllo',
        }
        layout = [self.widget(title='Rich', fields=rich, options=rich, source=self.source(args=rich))]
        created, updated = self.assertAccepted(layout=layout)
        self.assertEqual(created['layout'], layout)
        self.assertEqual(updated['layout'], layout)

    def test_nesting_deeper_than_6_is_refused(self):
        self.check_opaque(
            refused={
                'eight dicts': nested_dicts(8),
                'a dict then seven lists': {'a': nested_lists(7)},
                'dicts and lists alternating': {'a': [{'b': [{'c': [{'d': [1]}]}]}]},
                'very deep': nested_dicts(200),
            },
            accepted={
                'five dicts': nested_dicts(5),
                'dicts and lists alternating': {'a': [{'b': [{'c': 1}]}]},
            })

    def test_a_string_longer_than_2000_characters_is_refused(self):
        self.check_opaque(
            refused={
                '2001 characters': {'a': 'x' * 2001},
                'in a list': {'a': ['x' * 2001]},
                'deep in a dict': {'a': {'b': 'x' * 2001}},
                '2001 multi-byte characters': {'a': 'é' * 2001},
            },
            accepted={
                '2000 characters': {'a': 'x' * 2000},
                '2000 characters in a list': {'a': ['x' * 2000]},
            })

    def test_string_length_counts_characters_not_bytes(self):
        # Not in args, whose own 4096-byte ceiling is a byte count.
        self.check_opaque(accepted={'2000 multi-byte characters': {'a': 'é' * 2000}},
                          places=('fields', 'options'))

    def test_an_array_longer_than_200_items_is_refused(self):
        self.check_opaque(
            refused={
                '201 items': {'a': [0] * 201},
                'in a list of objects': {'a': [{'b': [0] * 201}]},
                'in a list of lists': {'a': [[0] * 201]},
            },
            accepted={
                '200 items': {'a': [0] * 200},
                '200 items nested': {'a': [{'b': [0] * 200}]},
            })

    def test_an_object_key_longer_than_64_characters_is_refused(self):
        self.check_opaque(
            refused={
                '65 characters': {'k' * 65: 1},
                'nested': {'a': {'k' * 65: 1}},
                'in a list': {'a': [{'k' * 65: 1}]},
            },
            accepted={
                '64 characters': {'k' * 64: 1},
                'nested': {'a': {'k' * 64: 1}},
            })

    def test_args_are_capped_at_4096_bytes_serialised(self):
        many_small = {'key{0:03d}'.format(i): 'v' * 40 for i in range(120)}  # ~6 KB, no single bound broken
        self.each_refused({
            'three 1500-character strings': [self.widget(source=self.source(args={
                'a': 'x' * 1500, 'b': 'x' * 1500, 'c': 'x' * 1500}))],
            'many small values': [self.widget(source=self.source(args=many_small))],
        })
        self.assertAccepted(layout=[self.widget(source=self.source(args={'a': 'x' * 1900, 'b': 'x' * 1900}))])

    def test_the_args_cap_is_not_applied_to_fields_or_options(self):
        big = {'a': 'x' * 2000, 'b': 'x' * 2000, 'c': 'x' * 2000}
        self.assertAccepted(layout=[self.widget(fields=big, options=big)])

    def test_the_whole_layout_is_capped_at_131072_bytes(self):
        one_huge_widget = [self.widget(fields={'rows': ['x' * 2000] * 70})]  # ~140 KB
        many_heavy_widgets = [
            self.widget(id='w{0}'.format(i), y=i * 2, options={'rows': ['y' * 1500] * 3}) for i in range(30)
        ]  # ~135 KB of strings alone
        self.each_refused({'one huge widget': one_huge_widget, 'thirty heavy widgets': many_heavy_widgets})
        under = [
            self.widget(id='w{0}'.format(i), y=i * 2, options={'rows': ['y' * 1000] * 3}) for i in range(30)
        ]  # ~97 KB
        self.assertAccepted(layout=under)

    def test_non_finite_numbers_are_refused(self):
        template = json.dumps({'layout': [self.widget(fields={'n': '__NUMBER__'})]})
        literals = (('NaN', 'NaN'), ('Infinity', 'Infinity'), ('-Infinity', '-Infinity'),
                    ('a float that overflows to infinity', '1e999'))
        for label, literal in literals:
            with self.subTest(label):
                cache.clear()
                body = template.replace('"__NUMBER__"', literal)
                count = Report.objects.count()
                response = self.client.post(list_url(), data=body, content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(Report.objects.count(), count)

                before = self.snapshot()
                response = self.client.patch(detail_url(self.report.pk), data=body, content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.snapshot(), before)

    # -- date tokens -------------------------------------------------------------

    def test_the_four_real_date_tokens_are_accepted_and_stored_as_written(self):
        # Substitution happens at run time; a save must keep the token itself.
        shapes = {
            'as a value': lambda token: {'d': token},
            'in a nested dict': lambda token: {'a': {'b': token}},
            'in a list': lambda token: {'a': [token]},
            'in a dict in a list in a dict': lambda token: {'ranges': [{'from': token}]},
        }
        for token in DATE_TOKENS:
            for label, shape in shapes.items():
                with self.subTest('{0} {1}'.format(token, label)):
                    args = shape(token)
                    created, updated = self.assertAccepted(layout=[self.widget(source=self.source(args=args))])
                    self.assertEqual(created['layout'][0]['source']['args'], args)
                    self.assertEqual(updated['layout'][0]['source']['args'], args)

    def test_all_the_tokens_together_in_one_awkward_shape(self):
        args = {
            'from': '$date.start', 'to': '$date.end',
            'prev': ['$date.prev_start', {'to': '$date.prev_end', 'also': [['$date.start']]}],
        }
        created, _ = self.assertAccepted(layout=[self.widget(source=self.source(args=args))])
        self.assertEqual(created['layout'][0]['source']['args'], args)

    def test_a_date_dot_string_that_is_not_one_of_the_four_tokens_is_refused(self):
        typos = {
            'a made-up token': '$date.bogus', 'the bare prefix': '$date.', 'a trailing space': '$date.start ',
            'the wrong case': '$date.START', 'a longer token': '$date.startx', 'a shortened one': '$date.prev',
        }
        shapes = {
            'as a value': lambda text: {'d': text},
            'in a nested dict': lambda text: {'a': {'b': text}},
            'in a list': lambda text: {'a': [text]},
            'in a dict in a list in a dict': lambda text: {'ranges': [{'from': text}]},
            'after a good token': lambda text: {'a': ['$date.start', text]},
        }
        for typo_label, typo in typos.items():
            for shape_label, shape in shapes.items():
                with self.subTest('{0} {1}'.format(typo_label, shape_label)):
                    self.assertRefused('layout', layout=[self.widget(source=self.source(args=shape(typo)))])

    def test_only_strings_that_start_with_date_dot_are_policed(self):
        self.assertAccepted(layout=[self.widget(source=self.source(args={
            'mid': 'x$date.bogus', 'no dot': '$date', 'no dollar': 'date.bogus', 'a number': 5,
        }))])

    def test_the_token_rule_is_for_source_args_only(self):
        # fields and options are opaque to the server; they are bounded, not interpreted.
        self.assertAccepted(layout=[self.widget(
            fields={'label': '$date.bogus'}, options={'note': '$date.bogus'})])


# ---------------------------------------------------------------------------
# Connection ownership on save
# ---------------------------------------------------------------------------

class ConnectionOwnershipTests(ValidationCase):
    def foreign(self, widget_id='foreign'):
        return self.widget(id=widget_id, source=self.source(connection_id=self.rival_connection.pk))

    def test_a_neighbours_connection_is_refused_and_nothing_is_saved(self):
        self.assertRefused('layout', layout=[self.foreign()])

    def test_a_connection_that_does_not_exist_is_refused(self):
        top = Connection.objects.order_by('-pk').values_list('pk', flat=True).first()
        ghost = self.widget(source=self.source(connection_id=top + 1000))
        self.assertRefused('layout', layout=[ghost])

    def test_one_foreign_connection_among_good_ones_sinks_the_whole_save(self):
        layout = self.widgets(3) + [self.foreign('sneaky')]
        layout[3]['y'] = 6
        self.assertRefused('layout', layout=layout)

    def test_the_foreign_connection_is_refused_wherever_it_sits(self):
        for position in (0, 1, 2):
            with self.subTest('position {0}'.format(position)):
                layout = self.widgets(3)
                layout[position] = self.foreign('sneaky')
                layout[position]['y'] = position * 2
                self.assertRefused('layout', layout=layout)

    def test_the_callers_own_connections_are_accepted(self):
        second = Connection.objects.create(tenant=self.tenant, connector=CONNECTOR, name='Second')
        layout = self.widgets(4, connection_ids=[self.connection.pk, second.pk])
        created, updated = self.assertAccepted(layout=layout)
        expected = [w['source']['connection_id'] for w in layout]
        for body in (created, updated):
            self.assertEqual([w['source']['connection_id'] for w in body['layout']], expected)

    def test_saving_does_not_query_once_per_widget_on_create(self):
        second = Connection.objects.create(tenant=self.tenant, connector=CONNECTOR, name='Second')
        ids = [self.connection.pk, second.pk]
        self.create(name='warm-up', layout=self.widgets(1))  # one-off caches must not skew the count

        with CaptureQueriesContext(db_connection) as few:
            self.assertEqual(self.create(name='few', layout=self.widgets(2, ids)).status_code, 201)
        with CaptureQueriesContext(db_connection) as many:
            self.assertEqual(self.create(name='many', layout=self.widgets(30, ids)).status_code, 201)

        self.assertEqual(len(many), len(few), 'query count grew with the number of widgets')
        self.assertLess(len(many), 25)

    def test_saving_does_not_query_once_per_widget_on_update(self):
        second = Connection.objects.create(tenant=self.tenant, connector=CONNECTOR, name='Second')
        ids = [self.connection.pk, second.pk]
        self.patch(self.report, layout=self.widgets(1))  # warm-up

        with CaptureQueriesContext(db_connection) as few:
            self.assertEqual(self.patch(self.report, layout=self.widgets(2, ids)).status_code, 200)
        with CaptureQueriesContext(db_connection) as many:
            self.assertEqual(self.patch(self.report, layout=self.widgets(30, ids)).status_code, 200)

        self.assertEqual(len(many), len(few), 'query count grew with the number of widgets')
        self.assertLess(len(many), 25)


# ---------------------------------------------------------------------------
# Filters validation
# ---------------------------------------------------------------------------

class FiltersValidationTests(ValidationCase):
    def custom(self, **dates):
        date_filter = {'range': 'CUSTOM'}
        date_filter.update(dates)
        return {'date': date_filter}

    def each_refused(self, cases):
        for label, filters in cases.items():
            with self.subTest(label):
                self.assertRefused('filters', filters=filters)

    def each_accepted(self, cases):
        for label, filters in cases.items():
            with self.subTest(label):
                self.assertAccepted(filters=filters)

    def test_every_preset_range_is_accepted_and_stored_as_given(self):
        for preset in PRESET_RANGES:
            with self.subTest(preset):
                created, updated = self.assertAccepted(filters={'date': {'range': preset}})
                self.assertEqual(created['filters']['date'], {'range': preset})
                self.assertEqual(updated['filters']['date'], {'range': preset})

    def test_a_custom_range_with_both_dates_is_accepted(self):
        filters = self.custom(start='2026-01-01', end='2026-01-31')
        created, updated = self.assertAccepted(filters=filters)
        self.assertEqual(created['filters']['date'], filters['date'])
        self.assertEqual(updated['filters']['date'], filters['date'])

    def test_a_single_day_is_a_valid_custom_range(self):
        self.assertAccepted(filters=self.custom(start='2026-03-05', end='2026-03-05'))

    def test_compare_true_is_accepted(self):
        for value in (True, False):
            with self.subTest(str(value)):
                created, updated = self.assertAccepted(filters={'date': {'range': 'LAST_7_DAYS'}, 'compare': value})
                self.assertIs(created['filters']['compare'], value)
                self.assertIs(updated['filters']['compare'], value)

    def test_a_leap_day_is_a_real_date(self):
        self.assertAccepted(filters=self.custom(start='2024-02-29', end='2024-03-01'))

    def test_filters_must_be_an_object(self):
        self.each_refused({
            'a list': [], 'a string': 'LAST_7_DAYS', 'a number': 5, 'true': True,
            'a list holding the object': [{'date': {'range': 'LAST_7_DAYS'}}],
        })

    def test_unknown_filter_keys_are_refused(self):
        valid = {'range': 'LAST_7_DAYS'}
        self.each_refused({
            'only an unknown key': {'foo': 1},
            'an extra key beside date': {'date': valid, 'extra': 1},
            'an extra key beside compare': {'compare': False, 'Compare': True},
            'range at the top level': {'range': 'LAST_7_DAYS'},
            'a client filter': {'date': valid, 'client': 'Acme'},
        })

    def test_date_must_be_an_object(self):
        self.each_refused({
            'a string': {'date': 'LAST_7_DAYS'}, 'a list': {'date': ['LAST_7_DAYS']},
            'a number': {'date': 5}, 'true': {'date': True},
        })

    def test_an_unknown_range_is_refused(self):
        bad = {
            'a made-up range': 'LAST_1_DAY', 'lower case': 'last_7_days', 'padded': 'LAST_7_DAYS ',
            'empty': '', 'today': 'TODAY', 'yesterday': 'YESTERDAY', 'all time': 'ALL_TIME',
            'a number': 5, 'null': None, 'true': True, 'a list': ['LAST_7_DAYS'], 'an object': {'a': 1},
        }
        self.each_refused({label: {'date': {'range': value}} for label, value in bad.items()})

    def test_a_custom_range_needs_both_dates(self):
        self.each_refused({
            'neither': self.custom(),
            'only a start': self.custom(start='2026-01-01'),
            'only an end': self.custom(end='2026-01-31'),
            'a null start': self.custom(start=None, end='2026-01-31'),
            'a null end': self.custom(start='2026-01-01', end=None),
        })

    def test_the_start_may_not_be_after_the_end(self):
        self.each_refused({
            'a month round': self.custom(start='2026-09-10', end='2026-09-01'),
            'one day round': self.custom(start='2026-09-02', end='2026-09-01'),
            'a year round': self.custom(start='2027-01-01', end='2026-01-01'),
        })

    def test_a_span_of_731_days_is_fine_and_a_longer_one_is_not(self):
        start = date(2024, 1, 1)  # 2024-01-01 .. 2025-12-31 is two whole years, 731 days inclusive
        self.assertAccepted(filters=self.custom(start=start.isoformat(), end=(start + timedelta(days=730)).isoformat()))
        self.each_refused({
            'two days over': self.custom(start=start.isoformat(), end=(start + timedelta(days=732)).isoformat()),
            'a week over': self.custom(start=start.isoformat(), end=(start + timedelta(days=737)).isoformat()),
            'a quarter century': self.custom(start='2000-01-01', end='2026-01-01'),
        })

    def test_start_and_end_belong_to_custom_only(self):
        cases = {}
        for preset in PRESET_RANGES:
            cases['{0} with a start'.format(preset)] = {'date': {'range': preset, 'start': '2026-01-01'}}
            cases['{0} with an end'.format(preset)] = {'date': {'range': preset, 'end': '2026-01-31'}}
            cases['{0} with both'.format(preset)] = {
                'date': {'range': preset, 'start': '2026-01-01', 'end': '2026-01-31'}}
        self.each_refused(cases)

    def test_dates_must_be_iso_year_month_day_strings(self):
        bad = {
            'unpadded': '2026-9-1', 'slashes': '2026/09/01', 'day first': '01-09-2026',
            'a month of 13': '2026-13-01', 'a day that never was': '2026-02-30',
            'a leap day in a common year': '2026-02-29', 'empty': '', 'leading space': ' 2026-09-01',
            'trailing space': '2026-09-01 ', 'a timestamp': '2026-09-01T00:00:00',
            'a number': 20260901, 'true': True, 'a list': ['2026-09-01'], 'an object': {'d': 1},
            'a float': 1.5,
        }
        cases = {}
        for label, value in bad.items():
            cases['start as {0}'.format(label)] = self.custom(start=value, end='2026-12-31')
            cases['end as {0}'.format(label)] = self.custom(start='2026-01-01', end=value)
        self.each_refused(cases)

    def test_compare_must_be_a_real_boolean(self):
        bad = {
            'the string true': 'true', 'the string false': 'false', 'the string True': 'True',
            'yes': 'yes', 'one': 1, 'zero': 0, 'a list': [], 'an object': {},
        }
        self.each_refused({
            label: {'date': {'range': 'LAST_7_DAYS'}, 'compare': value} for label, value in bad.items()
        })


# ---------------------------------------------------------------------------
# Throttles
# ---------------------------------------------------------------------------

class ThrottleTests(ReportTestCase):
    """Each kind of traffic has a ceiling of its own, per user.

    The throttle's own clock is frozen so that a slow machine cannot let the
    window roll over mid-test; cache.clear() is what starts a fresh window.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(SimpleRateThrottle, 'timer', return_value=1000.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.report = make_report(self.tenant, created_by=self.me, name='Throttled')

    def read_list(self, client=None):
        return (client or self.client).get(list_url())

    def read_detail(self, client=None):
        return (client or self.client).get(detail_url(self.report.pk))

    def write(self, i=0, client=None):
        return (client or self.client).patch(
            detail_url(self.report.pk), {'name': 'n{0}'.format(i)}, format='json')

    # Not called `run`: that is unittest.TestCase.run, the runner's entry point.
    def run_report(self, client=None):
        return (client or self.client).post(run_url(self.report.pk), {}, format='json')

    def statuses(self, count, call):
        return [call(i).status_code for i in range(count)]

    def exhaust_reads(self, client=None):
        self.assertEqual(self.statuses(120, lambda i: self.read_list(client)), [200] * 120)
        self.assertEqual(self.read_list(client).status_code, 429)

    def exhaust_writes(self, client=None):
        self.assertEqual(self.statuses(60, lambda i: self.write(i, client)), [200] * 60)
        self.assertEqual(self.write(60, client).status_code, 429)

    def exhaust_runs(self, client=None):
        self.assertEqual(self.statuses(20, lambda i: self.run_report(client)), [200] * 20)
        self.assertEqual(self.run_report(client).status_code, 429)

    # -- each scope on its own ---------------------------------------------------

    def test_the_121st_list_read_in_a_minute_is_refused(self):
        self.assertEqual(self.statuses(120, lambda i: self.read_list()), [200] * 120)
        self.assertEqual(self.read_list().status_code, 429)

    def test_list_and_detail_reads_draw_on_one_budget(self):
        codes = []
        for _ in range(60):
            codes.append(self.read_list().status_code)
            codes.append(self.read_detail().status_code)
        self.assertEqual(codes, [200] * 120)
        self.assertEqual(self.read_list().status_code, 429)
        self.assertEqual(self.read_detail().status_code, 429)

    def test_the_61st_write_in_a_minute_is_refused_and_not_applied(self):
        self.assertEqual(self.statuses(60, lambda i: self.write(i)), [200] * 60)
        self.assertEqual(self.write(60).status_code, 429)
        row = Report.objects.get(pk=self.report.pk)
        self.assertEqual(row.name, 'n59')
        self.assertEqual(row.version, 61)  # 1 + 60 accepted saves; the refused one never ran

    def test_every_write_verb_draws_on_the_same_write_budget(self):
        doomed = [make_report(self.tenant, created_by=self.me, name='Doomed {0}'.format(i)) for i in range(15)]
        expected = {'post': 201, 'patch': 200, 'put': 200, 'delete': 204}
        results = []
        for i in range(15):
            results.append(('post', self.create(name='Made {0}'.format(i)).status_code))
            results.append(('patch', self.write(i).status_code))
            results.append(('put', self.put(self.report, name='Put {0}'.format(i)).status_code))
            results.append(('delete', self.client.delete(detail_url(doomed[i].pk)).status_code))
        for verb, status in results:
            self.assertEqual(status, expected[verb], verb)

        # 15 of each is 60 in all; the next of every kind is refused.
        self.assertEqual(self.create(name='Late').status_code, 429)
        self.assertEqual(self.write(99).status_code, 429)
        self.assertEqual(self.put(self.report, name='Late').status_code, 429)
        self.assertEqual(self.client.delete(detail_url(self.report.pk)).status_code, 429)
        self.assertTrue(Report.objects.filter(pk=self.report.pk).exists())

    def test_the_21st_run_in_a_minute_is_refused(self):
        # An empty layout is a valid run and costs nothing; only the scope is under test.
        self.assertEqual(self.statuses(20, lambda i: self.run_report()), [200] * 20)
        self.assertEqual(self.run_report().status_code, 429)

    # -- independence ------------------------------------------------------------

    def test_spent_reads_do_not_block_writes_or_runs(self):
        self.exhaust_reads()
        self.assertEqual(self.write(1).status_code, 200)
        self.assertEqual(self.create(name='Still allowed').status_code, 201)
        self.assertEqual(self.run_report().status_code, 200)

    def test_spent_writes_do_not_block_reads_or_runs(self):
        self.exhaust_writes()
        self.assertEqual(self.read_list().status_code, 200)
        self.assertEqual(self.read_detail().status_code, 200)
        self.assertEqual(self.run_report().status_code, 200)

    def test_spent_runs_do_not_block_reads_or_writes(self):
        self.exhaust_runs()
        self.assertEqual(self.read_list().status_code, 200)
        self.assertEqual(self.read_detail().status_code, 200)
        self.assertEqual(self.write(1).status_code, 200)

    def test_spent_reads_and_writes_do_not_block_runs(self):
        self.exhaust_reads()
        self.exhaust_writes()
        self.assertEqual(self.run_report().status_code, 200)

    # -- who is counted ----------------------------------------------------------

    def test_two_users_have_separate_read_counters(self):
        self.exhaust_reads()
        self.assertEqual(self.read_list(self.client_for(self.colleague)).status_code, 200)
        self.assertEqual(self.read_list(self.client_for(self.rival)).status_code, 200)

    def test_two_users_have_separate_write_counters(self):
        self.exhaust_writes()
        self.assertEqual(self.write(1, self.client_for(self.colleague)).status_code, 200)

    def test_two_users_have_separate_run_counters(self):
        self.exhaust_runs()
        self.assertEqual(self.run_report(self.client_for(self.colleague)).status_code, 200)

    def test_clearing_the_cache_starts_a_fresh_window(self):
        self.exhaust_reads()
        self.exhaust_writes()
        self.exhaust_runs()
        cache.clear()
        self.assertEqual(self.read_list().status_code, 200)
        self.assertEqual(self.write(1).status_code, 200)
        self.assertEqual(self.run_report().status_code, 200)
