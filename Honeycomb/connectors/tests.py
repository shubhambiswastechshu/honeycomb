"""Tests for connector tools that have logic of their own worth pinning down."""
import asyncio
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from connectors import registry
from connectors.catalog import google_ads

CID = '1234567890'


class GoogleAdsDailyPerformanceTests(SimpleTestCase):
    """get_daily_performance is the series behind the report's trend line and
    period comparison, so its query and its row shape are what the page reads."""

    def setUp(self):
        cache.clear()  # the tool goes through the response cache

    def run_tool(self, args, rows):
        seen = {}

        async def fake_search(conn, db, call_args, query):
            seen['query'] = query
            return rows

        with mock.patch.object(google_ads, '_execute_search', fake_search):
            result = asyncio.run(
                google_ads.get_daily_performance(SimpleNamespace(id=1), None, args))
        return result, seen['query']

    def test_it_is_a_read_tool_that_needs_a_customer(self):
        entry = google_ads.CATALOG['get_daily_performance']
        self.assertFalse(entry.get('write'))
        self.assertEqual(entry['input']['required'], ['customer_id'])
        self.assertIn('start_date', entry['input']['properties'])

    def test_a_manager_account_gets_the_friendly_empty_answer(self):
        # Wrapped like every other performance report, so an MCC is told what
        # to do instead of being shown Google's raw 400.
        self.assertIn('get_daily_performance', google_ads._MCC_BLOCKED)
        self.assertIsNot(google_ads.TOOL_HANDLERS['get_daily_performance'],
                         google_ads.get_daily_performance)

    def test_it_is_registered_on_the_connector(self):
        connector = registry.get('google_ads')
        self.assertIn('get_daily_performance', connector.catalog)
        self.assertIn('get_daily_performance', connector.handlers)
        self.assertNotIn('get_daily_performance', connector.write_tools)

    def test_query_is_one_row_per_day_from_the_customer_resource(self):
        _, query = self.run_tool({'customer_id': CID, 'date_range': 'LAST_7_DAYS'}, [])
        self.assertIn('FROM customer', query)
        self.assertIn('segments.date,', query)
        self.assertIn('segments.date DURING LAST_7_DAYS', query)

    def test_explicit_dates_become_a_between_window(self):
        _, query = self.run_tool(
            {'customer_id': CID, 'start_date': '2026-08-01', 'end_date': '2026-08-31'}, [])
        self.assertIn("BETWEEN '2026-08-01' AND '2026-08-31'", query)

    def test_rows_carry_the_date_and_clean_metrics(self):
        rows = [{
            'segments': {'date': '2026-09-01'},
            'metrics': {'impressions': '100', 'clicks': '10', 'costMicros': '2500000',
                        'conversions': 1.5, 'conversionsValue': 30.0, 'ctr': 0.1,
                        'averageCpc': '250000'},
        }]
        result, _ = self.run_tool({'customer_id': CID}, rows)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['rows'][0], {
            'date': '2026-09-01', 'impressions': 100, 'clicks': 10, 'cost': 2.5,
            'conversions': 1.5, 'conversion_value': 30.0, 'ctr': 10.0, 'avg_cpc': 0.25,
        })

    def test_a_bad_customer_id_is_refused(self):
        with self.assertRaises(google_ads.GoogleAdsApiError):
            self.run_tool({'customer_id': '12'}, [])
