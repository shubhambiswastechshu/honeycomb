"""SSRF guard, BigQuery read-only check and connection role rules."""
import socket
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Tenant, User
from connections.models import Connection
from connectors.catalog.bigquery import _require_readonly_sql
from connectors.shims import http
from connectors.shims.errors import ConnectorError


def _resolves_to(*ips):
    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in ips]
    return fake


@override_settings(HONEYCOMB_ALLOW_PRIVATE_UPSTREAMS=False)
class PublicRequestTests(SimpleTestCase):
    def _vet(self, url, *ips):
        # asyncio's resolver calls socket.getaddrinfo in a worker thread.
        with mock.patch('socket.getaddrinfo', side_effect=_resolves_to(*ips)):
            return async_to_sync(http._vet)(url)

    def test_private_and_metadata_addresses_are_refused(self):
        for url in ('http://169.254.169.254/latest', 'http://10.1.2.3/', 'http://[::1]/',
                    'http://127.0.0.1:8000/', 'http://0x7f000001/'):
            with self.subTest(url=url), self.assertRaises(http.UnsafeUpstream):
                async_to_sync(http._vet)(url)

    def test_a_name_resolving_to_any_private_address_is_refused(self):
        with self.assertRaises(http.UnsafeUpstream):
            self._vet('https://mixed.example/', '93.184.216.34', '10.0.0.5')

    def test_a_public_name_passes_and_is_pinned(self):
        _url, ip = self._vet('https://example.com/', '93.184.216.34')
        self.assertEqual(ip, '93.184.216.34')

    def test_credentials_and_other_schemes_are_refused(self):
        for url in ('ftp://example.com/', 'https://user:pw@example.com/', 'file:///etc/passwd'):
            with self.subTest(url=url), self.assertRaises(http.UnsafeUpstream):
                async_to_sync(http._vet)(url)


class BigQueryReadOnlyTests(SimpleTestCase):
    def test_literals_cannot_hide_a_second_statement(self):
        for sql in ("SELECT '--'; DROP TABLE ds.t",
                    'SELECT "/*"; DELETE FROM ds.t WHERE true; SELECT "*/"',
                    "SELECT 'it\\'s'; DROP TABLE t",
                    'DELETE FROM t', 'SELECT 1; SELECT 2'):
            with self.subTest(sql=sql), self.assertRaises(ConnectorError):
                _require_readonly_sql(sql)

    def test_ordinary_selects_pass(self):
        for sql in ('SELECT 1', "select 'a;b' as x", 'WITH a AS (SELECT 1) SELECT * FROM a;',
                    '-- note\nSELECT 1 # trailing', "SELECT '''x;y'''"):
            with self.subTest(sql=sql):
                _require_readonly_sql(sql)


class ConnectionRoleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(name='Acme')
        owner = User.objects.create_user(email='o@acme.test', password='pw-for-tests-only',
                                         tenant=self.tenant, role=User.Role.OWNER)
        self.member = User.objects.create_user(email='m@acme.test', password='pw-for-tests-only',
                                               tenant=self.tenant, role=User.Role.MEMBER)
        self.connection = Connection(tenant=self.tenant, created_by=owner, connector='stripe')
        self.connection.set_creds({'secret_key': 'rk_test'})
        self.connection.save()
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def test_a_member_can_read_but_not_change_connections(self):
        base = '/api/connections/{0}/'.format(self.connection.pk)
        self.assertEqual(self.client.get(base).status_code, 200)
        self.assertEqual(self.client.delete(base).status_code, 403)
        self.assertEqual(self.client.patch(base, {'name': 'x'}, format='json').status_code, 403)
        res = self.client.post(base + 'tools/', {'tools': ['list_charges'], 'enabled': False},
                               format='json')
        self.assertEqual(res.status_code, 403)
        res = self.client.post('/api/connections/', {'connector': 'stripe', 'name': 'n',
                                                     'creds': {'secret_key': 'rk'}}, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertTrue(Connection.objects.filter(pk=self.connection.pk).exists())
