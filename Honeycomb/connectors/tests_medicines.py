"""Offline tests for the medicines connector's bundled India data and schemas."""
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from connectors import registry
from connectors.catalog import medicines
from connectors.shims.errors import ConnectorError


class _Conn:
    id = 1


def run(name, args):
    return async_to_sync(medicines.HANDLERS[name])(_Conn(), None, args)


class MedicinesCatalogTests(SimpleTestCase):
    def test_every_tool_has_a_handler_and_none_writes(self):
        spec = registry.get('medicines')
        self.assertEqual(set(spec.catalog), set(spec.handlers))
        self.assertEqual(spec.write_tools, ())
        for name, entry in spec.catalog.items():
            for required in entry['input']['required']:
                self.assertIn(required, entry['input']['properties'], name)


class IndiaDataTests(SimpleTestCase):
    def test_bundled_datasets_load(self):
        self.assertGreater(len(medicines._india('cdsco_new_drugs.json')['rows']), 3000)
        self.assertGreater(len(medicines._india('nlem_2022.json')['rows']), 350)
        self.assertGreater(len(medicines._india('nppa_ceiling_prices.json')['rows']), 800)

    def test_approvals_by_name_and_by_indication(self):
        out = run('india_approved_drugs', {'query': 'tirzepatide'})
        self.assertEqual(out['approvals'][0]['date'], '2024-01-19')
        self.assertTrue(out['approvals'][0]['source_pdf'].startswith('https://cdsco.gov.in/'))
        out = run('india_approved_drugs', {'query': 'type 2 diabetes', 'search_indications': True,
                                           'year_from': 2022, 'year_to': 2024})
        self.assertTrue(out['approvals'])
        self.assertTrue(all('2022' <= a['date'][:4] <= '2024' for a in out['approvals']))

    def test_essential_medicines_and_prices(self):
        self.assertTrue(run('india_essential_medicines', {'query': 'metformin'})['on_nlem_2022'])
        prices = run('india_ceiling_prices', {'query': 'amlodipine'})
        self.assertEqual(prices['as_of'], '2020-09-30')
        self.assertIn('2020-09-30', prices['note'])
        self.assertTrue(all(p['ceiling_price_inr'] for p in prices['prices']))

    def test_profile_combines_all_three(self):
        out = run('india_drug_profile', {'query': 'amlodipine'})
        self.assertIn('cdsco_approvals', out)
        self.assertTrue(out['essential_medicine']['on_nlem_2022'])
        self.assertTrue(out['price_control']['price_controlled'])

    def test_a_query_is_required(self):
        with self.assertRaises(ConnectorError):
            run('india_approved_drugs', {})

    def test_trial_inputs_are_validated(self):
        with self.assertRaises(ConnectorError):
            run('get_clinical_trial', {'nct_id': 'not-an-id'})
        with self.assertRaises(ConnectorError):
            run('search_clinical_trials', {})


class ReferenceToolTests(SimpleTestCase):
    def test_inputs_are_validated_before_any_request(self):
        for name, args in (('europe_pmc_full_text', {'pmcid': '12345'}),
                           ('europe_pmc_search', {}),
                           ('ema_medicines', {}),
                           ('fda_approval_history', {})):
            with self.subTest(tool=name), self.assertRaises(ConnectorError):
                run(name, args)

    def test_orange_book_tables_are_parsed(self):
        page = ('<table><tr><th>Product No</th><th>Patent No</th><th>Patent Expiration</th>'
                '<th>Drug Substance</th><th>Drug Product</th><th>Patent Use Code</th>'
                '<th>Delist Requested</th><th>Submission Date</th></tr>'
                '<tr><td>001</td><td>8129343</td><td>12/05/2031</td><td>DS</td><td>DP</td>'
                '<td>U-2202</td><td></td><td>12/20/2017</td></tr></table>')
        tables = medicines._html_tables(page)
        self.assertEqual(tables[0][1][1], '8129343')
        self.assertEqual(medicines._us_date('12/05/2031'), '2031-12-05')
        self.assertEqual(medicines._eu_date('06/01/2022'), '2022-01-06')
        self.assertEqual(medicines._fda_date('20190920'), '2019-09-20')


class CanadaDpdTests(SimpleTestCase):
    def test_products_are_shaped_from_the_dpd(self):
        from unittest import mock
        replies = {
            'drugproduct': [{'drug_code': 97796, 'drug_identification_number': '02471477',
                             'brand_name': 'OZEMPIC', 'company_name': 'NOVO NORDISK CANADA INC',
                             'class_name': 'Human', 'descriptor': '', 'last_update_date': '2026-01-01'}],
            'activeingredient': [{'ingredient_name': 'SEMAGLUTIDE', 'strength': '1.34', 'strength_unit': 'MG'}],
            'status': {'status': 'Marketed', 'original_market_date': '2018-04-09'},
        }

        async def fake(url, params):
            return replies[url.rstrip('/').rsplit('/', 1)[-1]]

        class Conn:
            id = 'canada-test'
        with mock.patch.object(medicines, '_get_json', side_effect=fake):
            out = async_to_sync(medicines.canada_drug_products)(Conn(), None, {'name': 'ozempic'})
        product = out['products'][0]
        self.assertEqual(product['din'], '02471477')
        self.assertEqual(product['status'], 'Marketed')
        self.assertEqual(product['ingredients'][0], {'name': 'SEMAGLUTIDE', 'strength': '1.34 MG'})
