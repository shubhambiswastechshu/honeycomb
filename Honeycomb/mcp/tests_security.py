"""Security and robustness tests for the MCP data plane and its OAuth server.

TransactionTestCase, not TestCase: the endpoint now queries from the thread
pool (mcp/db.py), and rows created inside TestCase's wrapping transaction would
be invisible to those threads' own connections.
"""
import base64
import hashlib
import json
import secrets
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.utils import timezone
from starlette.testclient import TestClient

from accounts.models import Tenant, User
from connections.models import Connection
from connectors import registry

from . import oauth
from .endpoint import app
from .models import McpActivity, McpKey, OAuthClient, OAuthGrant, OAuthToken

TRUSTED = 'https://claude.ai/api/mcp/auth_callback'


def make_user(tenant, email, role=User.Role.OWNER):
    return User.objects.create_user(email=email, password='pw-for-tests-only', tenant=tenant,
                                    role=role)


def make_connection(tenant, user, connector='stripe'):
    connection = Connection(tenant=tenant, created_by=user, connector=connector, name='c')
    connection.set_creds({})
    connection.save()
    return connection


def rpc(method, params=None):
    return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}


class EndpointTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(name='Acme')
        self.user = make_user(self.tenant, 'owner@acme.test')
        self.connection = make_connection(self.tenant, self.user)
        self.key_row, self.key = McpKey.mint(self.connection, self.user)
        self.url = '/mcp/stripe/{0}/'.format(self.connection.endpoint_slug)
        self.client = TestClient(app)

    def post(self, body, key=None, raw=None):
        headers = {'Authorization': 'Bearer ' + (key or self.key),
                   'Content-Type': 'application/json'}
        return self.client.post(self.url, content=raw if raw is not None else json.dumps(body),
                                headers=headers)

    def test_tools_list_works_and_logs_nothing(self):
        res = self.post(rpc('tools/list'))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['result']['tools'])
        self.assertEqual(McpActivity.objects.count(), 0)

    def test_a_key_for_another_tenant_is_refused(self):
        other = Tenant.objects.create(name='Rival')
        other_conn = make_connection(other, make_user(other, 'x@rival.test'))
        _row, other_key = McpKey.mint(other_conn, None)
        self.assertEqual(self.post(rpc('tools/list'), key=other_key).status_code, 401)

    @override_settings(HONEYCOMB_MCP_MAX_BODY_BYTES=1000)
    def test_an_oversized_body_is_refused_before_parsing(self):
        res = self.post(None, raw='{"x":"' + 'a' * 5000 + '"}')
        self.assertEqual(res.status_code, 413)

    def test_deeply_nested_json_is_a_parse_error_not_a_500(self):
        res = self.post(None, raw='{"a":' + '[' * 50000 + ']' * 50000 + '}')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['error']['code'], -32700)

    def test_non_object_params_and_arguments_are_rejected(self):
        res = self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': 'x'})
        self.assertEqual(res.json()['error']['code'], -32602)
        res = self.post(rpc('tools/call', {'name': ['list'], 'arguments': {}}))
        self.assertEqual(res.json()['error']['code'], -32602)
        res = self.post(rpc('tools/call', {'name': 'list_charges', 'arguments': 'x'}))
        self.assertEqual(res.json()['error']['code'], -32602)

    def test_a_tool_call_is_logged_after_the_response(self):
        res = self.post(rpc('tools/call', {'name': 'no_such_tool', 'arguments': {}}))
        self.assertTrue(res.json()['result']['isError'])
        row = McpActivity.objects.get()
        self.assertEqual(row.tool_name, 'no_such_tool')
        self.assertEqual(row.tenant_id, self.tenant.pk)

    @override_settings(HONEYCOMB_MCP_CALLS_PER_MINUTE=3)
    def test_calls_are_rate_limited_per_key(self):
        call = rpc('tools/call', {'name': 'no_such_tool', 'arguments': {}})
        texts = [self.post(call).json()['result']['content'][0]['text'] for _ in range(4)]
        self.assertTrue(all(t.startswith('Unknown tool') for t in texts[:3]))
        self.assertIn('Rate limit', texts[3])

    def test_last_used_is_not_rewritten_on_every_request(self):
        self.post(rpc('tools/list'))
        first = McpKey.objects.get(pk=self.key_row.pk).last_used_at
        self.post(rpc('tools/list'))
        self.assertEqual(McpKey.objects.get(pk=self.key_row.pk).last_used_at, first)


class WriteToolOptInTests(TransactionTestCase):
    def setUp(self):
        tenant = Tenant.objects.create(name='Acme')
        self.connection = make_connection(tenant, make_user(tenant, 'o@acme.test'), 'openai_ads')
        self.spec = registry.get('openai_ads')

    def test_a_write_tool_is_off_until_switched_on(self):
        # Not in disabled_tools -- exactly the state of a tool shipped after
        # the connection was made -- and still off.
        self.connection.disabled_tools = []
        self.assertFalse(registry.tool_enabled(self.connection, 'create_campaign', self.spec))
        self.assertTrue(registry.tool_enabled(self.connection, 'list_campaigns', self.spec))
        self.connection.enabled_write_tools = ['create_campaign']
        self.assertTrue(registry.tool_enabled(self.connection, 'create_campaign', self.spec))
        self.connection.disabled_tools = ['create_campaign']
        self.assertFalse(registry.tool_enabled(self.connection, 'create_campaign', self.spec))


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=')
    return verifier, challenge.decode()


@override_settings(HONEYCOMB_PUBLIC_BASE='https://api.test', HONEYCOMB_FRONTEND_BASE='https://app.test')
class OAuthTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.tenant = Tenant.objects.create(name='Acme')
        self.owner = make_user(self.tenant, 'owner@acme.test')
        self.connection = make_connection(self.tenant, self.owner)
        self.resource = 'https://api.test/mcp/stripe/{0}/'.format(self.connection.endpoint_slug)

    def authorize(self, user, redirect_uri, method='get', **extra):
        client = OAuthClient.objects.create(client_id=OAuthClient.new_client_id(),
                                            redirect_uris=[redirect_uri])
        _verifier, challenge = pkce()
        params = {'client_id': client.client_id, 'redirect_uri': redirect_uri,
                  'response_type': 'code', 'code_challenge': challenge,
                  'code_challenge_method': 'S256', 'resource': self.resource, 'state': 's'}
        params.update(extra)
        request = getattr(self.factory, method)('/oauth/authorize', params)
        request.user = user
        return oauth.authorize(request)

    def test_trusted_redirect_is_auto_approved(self):
        res = self.authorize(self.owner, TRUSTED)
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res['Location'].startswith(TRUSTED + '?code='))

    def test_an_unknown_redirect_gets_the_consent_screen(self):
        res = self.authorize(self.owner, 'https://evil.example/cb')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(OAuthGrant.objects.exists())

    def test_a_member_cannot_authorize(self):
        member = make_user(self.tenant, 'm@acme.test', role=User.Role.MEMBER)
        res = self.authorize(member, TRUSTED)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(OAuthGrant.objects.exists())

    def test_errors_before_sign_in_are_pages_not_redirects(self):
        res = self.authorize(None, 'https://evil.example/cb', response_type='token')
        self.assertEqual(res.status_code, 400)

    def test_registration_limits(self):
        def register(body, ip='1.2.3.4'):
            request = self.factory.post('/oauth/register', json.dumps(body),
                                        content_type='application/json', REMOTE_ADDR=ip)
            return oauth.register(request)
        self.assertEqual(register({'redirect_uris': ['javascript://x']}).status_code, 400)
        self.assertEqual(register({'redirect_uris': ['claude://cb']}).status_code, 400)
        self.assertEqual(register({'redirect_uris': ['https://a.test/' + 'x' * 600]}).status_code, 400)
        self.assertEqual(register({'redirect_uris': ['https://a.test/%d' % i for i in range(6)]}).status_code, 400)
        self.assertEqual(register([1, 2]).status_code, 400)
        codes = [register({'redirect_uris': [TRUSTED]}, ip='9.9.9.9').status_code
                 for _ in range(oauth.REGISTER_PER_HOUR + 1)]
        self.assertEqual(codes[:-1], [201] * oauth.REGISTER_PER_HOUR)
        self.assertEqual(codes[-1], 429)

    def _token_pair(self):
        client = OAuthClient.objects.create(client_id=OAuthClient.new_client_id(),
                                            redirect_uris=[TRUSTED])
        body = json.loads(oauth._issue(client, self.owner, self.connection).content)
        return client, body

    def _refresh(self, client, refresh):
        request = self.factory.post('/oauth/token', {'grant_type': 'refresh_token',
                                                     'refresh_token': refresh,
                                                     'client_id': client.client_id})
        return oauth.token(request)

    def test_refresh_rotates_and_reuse_revokes_the_family(self):
        client, first = self._token_pair()
        res = self._refresh(client, first['refresh_token'])
        self.assertEqual(res.status_code, 200)
        second = json.loads(res.content)
        # Replaying the spent refresh token kills the live one too.
        self.assertEqual(self._refresh(client, first['refresh_token']).status_code, 400)
        self.assertEqual(self._refresh(client, second['refresh_token']).status_code, 400)
        self.assertFalse(OAuthToken.objects.filter(revoked_at__isnull=True).exists())

    def test_refresh_requires_client_id(self):
        client, first = self._token_pair()
        request = self.factory.post('/oauth/token', {'grant_type': 'refresh_token',
                                                     'refresh_token': first['refresh_token']})
        self.assertEqual(oauth.token(request).status_code, 400)

    def test_a_family_ends_after_its_lifetime(self):
        client, first = self._token_pair()
        OAuthToken.objects.update(family_started_at=timezone.now() - timedelta(days=91))
        self.assertEqual(self._refresh(client, first['refresh_token']).status_code, 400)

    def test_a_deactivated_users_token_stops_working(self):
        _client, first = self._token_pair()
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        res = TestClient(app).post(
            '/mcp/stripe/{0}/'.format(self.connection.endpoint_slug),
            content=json.dumps(rpc('tools/list')),
            headers={'Authorization': 'Bearer ' + first['access_token']})
        self.assertEqual(res.status_code, 401)

    def test_pkce_rejects_a_non_ascii_verifier_without_crashing(self):
        self.assertFalse(oauth._pkce_ok('é' * 50, 'x'))

    def test_prune_removes_dead_rows_only(self):
        client, _ = self._token_pair()
        OAuthToken.objects.update(revoked_at=timezone.now() - timedelta(days=60))
        OAuthClient.objects.create(client_id='hcc_old', redirect_uris=[TRUSTED])
        OAuthClient.objects.filter(client_id='hcc_old').update(
            created_at=timezone.now() - timedelta(days=30))
        call_command('prune_mcp', stdout=mock.MagicMock())
        self.assertFalse(OAuthToken.objects.exists())
        self.assertFalse(OAuthClient.objects.filter(client_id='hcc_old').exists())
