"""Unit tests for the OpenAI Ads (ChatGPT Ads) connector.

Nothing here reaches the network. The pure helpers are tested directly, and
the handlers run against a stubbed transport that records each request, so
what is checked is the request OpenAI would receive: the path, the query
pairs, the JSON body and the idempotency header. The live API is not
exercised; it needs an approved ad account and its key.
"""
import json
from datetime import date
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import SimpleTestCase

from connectors import registry
from connectors.catalog import openai_ads
from connectors.shims.errors import ConnectorError


class _Conn:
    """Enough of a Connection for the handlers: an id and decrypted creds."""

    id = 4242

    def __init__(self, creds=None):
        self._creds = {"api_key": "test-ads-key"} if creds is None else creds

    def creds(self):
        return self._creds


class _Res:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.text = json.dumps(self._payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


class _Recorder:
    """Stands in for shims.http.request; answers by path and remembers calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = url.replace(openai_ads.BASE + "/", "")
        payload = self.routes.get((method, path), self.routes.get(path, {}))
        return payload if isinstance(payload, _Res) else _Res(200, payload)


ACCOUNT = {"id": "adacct_1", "timezone": "America/New_York", "currency_code": "USD"}


def _run(handler, args, routes):
    recorder = _Recorder({"ad_account": ACCOUNT, **routes})
    with mock.patch.object(openai_ads, "http_request", recorder):
        result = async_to_sync(handler)(_Conn(), None, args)
    return result, recorder


class RegistrationTests(SimpleTestCase):
    def test_registered(self):
        connector = registry.get("openai_ads")
        self.assertIsNotNone(connector)
        self.assertEqual(connector.auth, "api_key")
        self.assertEqual(connector.cred_fields, ["api_key"])
        self.assertEqual(connector.category, "Advertising")

    def test_every_tool_has_a_handler_and_a_schema(self):
        connector = registry.get("openai_ads")
        self.assertEqual(set(connector.catalog), set(connector.handlers))
        for name, entry in connector.catalog.items():
            with self.subTest(tool=name):
                self.assertTrue(entry["description"].strip())
                self.assertEqual(entry["input"]["type"], "object")
                for required in entry["input"]["required"]:
                    self.assertIn(required, entry["input"]["properties"])

    def test_mutations_are_write_tools_and_reads_are_not(self):
        write = set(registry.get("openai_ads").write_tools)
        for name in ("create_campaign", "set_ad_status", "pause_account", "add_audience_members",
                     "update_feed_products", "submit_bulk_job", "set_daily_spend_limit"):
            self.assertIn(name, write)
        for name in ("list_campaigns", "insights", "conversion_insights", "preview_ad",
                     "query_feed_products", "get_bulk_job", "search_locations"):
            self.assertNotIn(name, write)

    def test_no_tool_hands_out_a_secret(self):
        # Conversions API keys and SFTP passwords would land in the activity log.
        catalog = registry.get("openai_ads").catalog
        self.assertNotIn("create_conversions_api_key", catalog)
        props = catalog["set_feed_sftp_access"]["input"]["properties"]
        self.assertNotIn("password", props["action"]["enum"])


class MoneyTests(SimpleTestCase):
    def test_micros_get_a_major_unit_twin(self):
        out = openai_ads._with_major({
            "budget": {"lifetime_spend_limit_micros": 25_000_000},
            "items": [{"max_bid_micros": 60_000}],
        })
        self.assertEqual(out["budget"]["lifetime_spend_limit"], 25.0)
        self.assertEqual(out["budget"]["lifetime_spend_limit_micros"], 25_000_000)
        self.assertEqual(out["items"][0]["max_bid"], 0.06)

    def test_an_existing_key_is_not_overwritten(self):
        out = openai_ads._with_major({"spend_micros": 1_000_000, "spend": "as sent"})
        self.assertEqual(out["spend"], "as sent")

    def test_to_micros(self):
        self.assertEqual(openai_ads._to_micros(25, "x"), 25_000_000)
        self.assertEqual(openai_ads._to_micros("0.06", "x"), 60_000)
        with self.assertRaises(ConnectorError):
            openai_ads._to_micros(-1, "x")
        with self.assertRaises(ConnectorError):
            openai_ads._to_micros("lots", "x")


class TimeRangeTests(SimpleTestCase):
    TODAY = date(2026, 10, 1)

    def _range(self, args, hour=14):
        text, whole = openai_ads._time_range(args, self.TODAY, hour)
        return json.loads(text), whole

    def test_default_is_the_seven_days_before_today(self):
        rng, whole = self._range({})
        self.assertEqual(rng, {"type": "date_range", "since": "2026-09-24", "until": "2026-09-30"})
        self.assertTrue(whole)

    def test_last_month(self):
        rng, _ = self._range({"date_range": "last_month"})
        self.assertEqual((rng["since"], rng["until"]), ("2026-09-01", "2026-09-30"))

    def test_today_is_hours_and_not_whole_days(self):
        rng, whole = self._range({"date_range": "today"})
        self.assertEqual(rng, {"type": "hour_range", "since": "2026-10-01T00", "until": "2026-10-01T14"})
        self.assertFalse(whole)

    def test_this_month_on_the_first_falls_back_to_today(self):
        rng, whole = self._range({"date_range": "this_month"})
        self.assertEqual(rng["type"], "hour_range")
        self.assertFalse(whole)

    def test_custom_range(self):
        rng, whole = self._range({"start_date": "2026-09-01", "end_date": "2026-09-07"})
        self.assertEqual((rng["since"], rng["until"]), ("2026-09-01", "2026-09-07"))
        self.assertTrue(whole)

    def test_bad_ranges_are_refused(self):
        for args in ({"start_date": "2026-09-01"},
                     {"start_date": "2026-09-07", "end_date": "2026-09-01"},
                     {"start_date": "2026-09-01", "end_date": "2026-10-05"},
                     {"start_date": "01/09/2026", "end_date": "2026-09-07"},
                     {"date_range": "last_fortnight"}):
            with self.subTest(args=args), self.assertRaises(ConnectorError):
                self._range(args)


class HelperTests(SimpleTestCase):
    def test_default_fields(self):
        fields = openai_ads._default_fields("campaign", None, "daily", True)
        self.assertEqual(fields[:3], ["readable_time", "campaign_id", "campaign_name"])
        self.assertIn("order_created_roas", fields)
        self.assertNotIn("conversions", openai_ads._default_fields("ad", None, "none", False))

    def test_segment_fields_stay_inside_the_segment(self):
        fields = openai_ads._default_fields("campaign", "country", "none", True)
        self.assertEqual(fields, ["campaign_id", "country.name", *[f"country.{m}" for m in openai_ads.DELIVERY_METRICS]])

    def test_scope_follows_the_most_specific_id(self):
        self.assertEqual(openai_ads._scope({}), ("ad_account/insights", "ad_account"))
        self.assertEqual(openai_ads._scope({"campaign_id": "c", "ad_id": "a"}), ("ads/a/insights", "ad"))

    def test_level_above_scope_is_refused(self):
        with self.assertRaises(ConnectorError):
            openai_ads._check_level("ad_group", "campaign")
        openai_ads._check_level("campaign", "ad")

    def test_targeting_from_convenience_fields(self):
        targeting = openai_ads._targeting({
            "countries": ["us"], "location_ids": ["2000043"], "platforms": ["ios_app"],
            "exclude_audience_ids": ["caud_9"],
        })
        self.assertEqual(targeting, {
            "locations": {"countries": ["US"], "include": [{"id": "2000043"}]},
            "platforms": {"included": ["ios_app"]},
            "excluded_custom_audiences": {"ids": ["caud_9"]},
        })

    def test_raw_targeting_wins_and_null_is_kept(self):
        self.assertIsNone(openai_ads._targeting({"targeting": None, "countries": ["US"]}))

    def test_membership_body(self):
        body = openai_ads._membership_body(
            {"identifiers": [{"identifier_type": "email", "identifier": "a@b.co"}], "expected_revision": 2}, True
        )
        self.assertEqual(body["expected_revision"], 2)
        with self.assertRaises(ConnectorError):
            openai_ads._membership_body({"identifiers": [{"identifier_type": "fax", "identifier": "1"}]}, True)
        with self.assertRaises(ConnectorError):
            openai_ads._membership_body({"file_id": "f", "identifiers": [{}]}, True)
        with self.assertRaises(ConnectorError):
            openai_ads._membership_body({"identifiers": [{}]}, False)

    def test_error_messages(self):
        with self.assertRaises(ConnectorError) as caught:
            openai_ads._fail(_Res(401, {"error": {"message": "bad"}}), "GET")
        self.assertIn("Ads Manager", str(caught.exception))
        with self.assertRaises(ConnectorError) as caught:
            openai_ads._fail(_Res(409, {"error": {"message": "stale", "code": "custom_audience_mutation_conflict"}}), "POST")
        self.assertIn("custom_audience_mutation_conflict", str(caught.exception))
        with self.assertRaises(ConnectorError) as caught:
            openai_ads._fail(_Res(503, {"detail": "down"}), "POST")
        self.assertIn("may still have been saved", str(caught.exception))

    def test_missing_key(self):
        with self.assertRaises(ConnectorError):
            openai_ads._key(_Conn({}))


class HandlerTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_campaign_performance_request(self):
        result, rec = _run(openai_ads.campaign_performance,
                           {"start_date": "2026-09-01", "end_date": "2026-09-07"},
                           {"ad_account/insights": {"data": [{"campaign_id": "cmpn_1", "spend": 3.5}],
                                                    "has_more": False}})
        method, url, kwargs = rec.calls[-1]
        self.assertEqual((method, url), ("GET", openai_ads.BASE + "/ad_account/insights"))
        params = kwargs["params"]
        self.assertIn(("aggregation_level", "campaign"), params)
        self.assertIn(("fields[]", "conversions"), params)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-ads-key")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["timezone"], "America/New_York")

    def test_insights_follows_pages_when_asked(self):
        pages = iter([
            _Res(200, {"data": [{"id": 1}], "has_more": True, "last_id": "p1"}),
            _Res(200, {"data": [{"id": 2}], "has_more": False, "last_id": "p2"}),
        ])

        class _Paging(_Recorder):
            async def __call__(self, method, url, **kwargs):
                if url.endswith("/insights"):
                    self.calls.append((method, url, kwargs))
                    return next(pages)
                return await super().__call__(method, url, **kwargs)

        rec = _Paging({"ad_account": ACCOUNT})
        with mock.patch.object(openai_ads, "http_request", rec):
            result = async_to_sync(openai_ads.insights)(_Conn(), None, {"campaign_id": "cmpn_1", "all_pages": True})
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["level"], "campaign")
        self.assertIn(("after", "p1"), rec.calls[-1][2]["params"])

    def test_create_campaign_body_and_idempotency(self):
        result, rec = _run(openai_ads.create_campaign,
                           {"name": "Spring launch", "lifetime_budget": 250, "countries": ["US"],
                            "bidding_type": "clicks"},
                           {("POST", "campaigns"): {"id": "cmpn_9", "budget": {"lifetime_spend_limit_micros": 250_000_000}}})
        method, url, kwargs = rec.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(kwargs["json"], {
            "name": "Spring launch", "status": "paused",
            "budget": {"lifetime_spend_limit_micros": 250_000_000},
            "bidding_type": "clicks",
            "targeting": {"locations": {"countries": ["US"]}},
        })
        self.assertTrue(kwargs["headers"]["Idempotency-Key"].startswith("honeycomb-"))
        self.assertEqual(result["budget"]["lifetime_spend_limit"], 250.0)

    def test_conversions_campaign_needs_one_event_setting(self):
        with self.assertRaises(ConnectorError):
            _run(openai_ads.create_campaign,
                 {"name": "Buys", "lifetime_budget": 100, "bidding_type": "conversions"}, {})

    def test_ad_group_billing_event_comes_from_the_campaign(self):
        _, rec = _run(openai_ads.create_ad_group,
                      {"campaign_id": "cmpn_1", "name": "US English", "max_bid": 2},
                      {"campaigns/cmpn_1": {"id": "cmpn_1", "bidding_type": "clicks"},
                       ("POST", "ad_groups"): {"id": "adgrp_1"}})
        body = rec.calls[-1][2]["json"]
        self.assertEqual(body["bidding_config"], {"max_bid_micros": 2_000_000, "billing_event_type": "click"})

    def test_create_ad_uploads_the_image_first(self):
        _, rec = _run(openai_ads.create_ad,
                      {"ad_group_id": "adgrp_1", "name": "Card", "title": "Plan faster",
                       "body": "Tasks in one place.", "target_url": "https://example.com",
                       "image_url": "https://example.com/card.png"},
                      {("POST", "upload"): {"file_id": "file_7"}, ("POST", "ads"): {"id": "ad_1"}})
        self.assertEqual(rec.calls[0][2]["json"], {"image_url": "https://example.com/card.png"})
        self.assertEqual(rec.calls[-1][2]["json"]["creative"]["file_id"], "file_7")

    def test_status_action(self):
        _, rec = _run(openai_ads.set_ad_status, {"ad_id": "ad_1", "action": "pause"},
                      {("POST", "ads/ad_1/pause"): {"id": "ad_1", "status": "paused"}})
        self.assertEqual(rec.calls[-1][1], openai_ads.BASE + "/ads/ad_1/pause")

    def test_daily_limit_reads_the_revision(self):
        _, rec = _run(openai_ads.set_daily_spend_limit, {"amount": 100},
                      {"ad_account/spend_limit_windows": {"revision": 12},
                       ("POST", "ad_account/daily_spend_limit"): {"spend_limits": {}}})
        self.assertEqual(rec.calls[-1][2]["json"], {"amount_micros": 100_000_000, "expected_revision": 12})

    def test_audience_file_upload_is_multipart(self):
        result, rec = _run(openai_ads.upload_audience_file,
                           {"content": "email\na@b.co\n", "filename": "list.csv"},
                           {("POST", "uploads"): {"file_id": "oais_1"}})
        kwargs = rec.calls[-1][2]
        self.assertEqual(kwargs["data"], {"purpose": "custom_audience"})
        self.assertEqual(kwargs["files"]["file"][2], "text/csv")
        self.assertEqual(result, {"file_id": "oais_1", "filename": "list.csv", "mimetype": "text/csv",
                                  "file_size": 13})

    def test_delta_feed_uses_patch(self):
        _, rec = _run(openai_ads.update_feed_products,
                      {"feed_id": "fd_1", "products": [{"id": "SKU", "variants": [{"id": "SKU"}]}]},
                      {("PATCH", "feeds/fd_1/products"): {"accepted": True}})
        self.assertEqual(rec.calls[-1][0], "PATCH")

    def test_sftp_refuses_a_private_key(self):
        with self.assertRaises(ConnectorError):
            _run(openai_ads.set_feed_sftp_access,
                 {"feed_id": "fd_1", "action": "configure_ssh_key",
                  "ssh_public_key": "-----BEGIN OPENSSH PRIVATE KEY-----"}, {})
