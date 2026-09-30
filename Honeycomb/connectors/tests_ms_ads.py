"""Unit tests for the Microsoft Advertising connector's pure helpers.

Everything here runs without the network. The SOAP envelopes and the report
ZIP are the two places this connector can silently go wrong — an envelope
Microsoft rejects, or a CSV whose trailing junk becomes a row of data — so
those are what is covered. The live API is not exercised: it needs a real
developer token and a real account.

In its own file rather than ``connectors/tests.py`` so it does not collide
with the tests already on the branch.
"""
import io
import zipfile
from xml.etree import ElementTree as ET

from django.test import SimpleTestCase

from connectors import registry
from connectors.catalog import ms_ads
from connectors.shims.errors import ConnectorError


class RegistrationTests(SimpleTestCase):
    def test_registered_read_only(self):
        connector = registry.get("ms_ads")
        self.assertIsNotNone(connector)
        self.assertEqual(connector.label, "Microsoft Advertising")
        self.assertEqual(connector.category, "Advertising")
        # Nothing in this connector mutates an account.
        self.assertEqual(connector.write_tools, ())

    def test_every_tool_has_a_handler_and_a_schema(self):
        connector = registry.get("ms_ads")
        self.assertEqual(set(connector.catalog), set(connector.handlers))
        for name, entry in connector.catalog.items():
            with self.subTest(tool=name):
                self.assertTrue(entry["description"].strip(), "tool needs a description")
                self.assertEqual(entry["input"]["type"], "object")
                self.assertIn("properties", entry["input"])


class TimeXmlTests(SimpleTestCase):
    def test_named_period(self):
        xml = ms_ads._time_xml({"date_range": "last_month"})
        self.assertIn("<PredefinedTime>LastMonth</PredefinedTime>", xml)

    def test_defaults_to_last_seven_days(self):
        self.assertIn("LastSevenDays", ms_ads._time_xml({}))

    def test_custom_range_wins_over_named_period(self):
        xml = ms_ads._time_xml(
            {"date_range": "last_month", "start_date": "2026-01-05", "end_date": "2026-02-09"}
        )
        self.assertNotIn("PredefinedTime", xml)
        self.assertIn("<Day>5</Day><Month>1</Month><Year>2026</Year>", xml)
        self.assertIn("<Day>9</Day><Month>2</Month><Year>2026</Year>", xml)

    def test_unknown_period_is_refused_by_name(self):
        with self.assertRaises(ConnectorError) as caught:
            ms_ads._time_xml({"date_range": "last_fortnight"})
        self.assertIn("last_fortnight", str(caught.exception))


class EnvelopeTests(SimpleTestCase):
    def test_columns_are_wrapped_per_report(self):
        xml = ms_ads._columns_xml("CampaignPerformanceReport", ["Clicks", "Spend"])
        self.assertEqual(
            xml,
            "<Columns>"
            "<CampaignPerformanceReportColumn>Clicks</CampaignPerformanceReportColumn>"
            "<CampaignPerformanceReportColumn>Spend</CampaignPerformanceReportColumn>"
            "</Columns>",
        )

    def test_values_going_into_xml_are_escaped(self):
        self.assertEqual(ms_ads._escape('a&b<c>"d"'), "a&amp;b&lt;c&gt;&quot;d&quot;")


class FaultTests(SimpleTestCase):
    def test_the_useful_sentence_is_pulled_out_of_a_fault(self):
        fault = ET.fromstring(
            '<s:Fault xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            "<faultstring>Invalid client data</faultstring>"
            "<detail><ApiFaultDetail><OperationErrors><OperationError>"
            "<ErrorCode>InvalidCredentials</ErrorCode>"
            "<Message>Authentication failed for the developer token.</Message>"
            "</OperationError></OperationErrors></ApiFaultDetail></detail>"
            "</s:Fault>"
        )
        text = ms_ads._fault_text(fault)
        self.assertIn("Authentication failed for the developer token.", text)
        self.assertIn("InvalidCredentials", text)

    def test_a_fault_with_no_detail_still_says_something(self):
        fault = ET.fromstring("<Fault><other>went wrong</other></Fault>")
        self.assertTrue(ms_ads._fault_text(fault))


class ObjectParsingTests(SimpleTestCase):
    def test_flat_entity(self):
        el = ET.fromstring("<Campaign><Id>7</Id><Name>Brand</Name></Campaign>")
        self.assertEqual(ms_ads._obj(el), {"Id": "7", "Name": "Brand"})

    def test_repeated_leaf_children_keep_their_values(self):
        el = ET.fromstring(
            "<AdGroup><Id>3</Id>"
            "<Targets><Target>a</Target><Target>b</Target></Targets>"
            "</AdGroup>"
        )
        self.assertEqual(ms_ads._obj(el)["Targets"], ["a", "b"])

    def test_repeated_entity_children_become_a_list_of_dicts(self):
        el = ET.fromstring(
            "<Result><Items>"
            "<Item><Id>1</Id></Item><Item><Id>2</Id></Item>"
            "</Items></Result>"
        )
        self.assertEqual(ms_ads._obj(el)["Items"], [{"Id": "1"}, {"Id": "2"}])

    def test_named_wrapper_is_collected(self):
        el = ET.fromstring(
            "<Result><Campaigns>"
            "<Campaign><Id>1</Id></Campaign><Campaign><Id>2</Id></Campaign>"
            "</Campaigns></Result>"
        )
        self.assertEqual(ms_ads._collect(el, "Campaigns"), [{"Id": "1"}, {"Id": "2"}])

    def test_a_missing_wrapper_is_empty_not_an_error(self):
        el = ET.fromstring("<Result/>")
        self.assertEqual(ms_ads._collect(el, "Campaigns"), [])


def _zip_of(csv_text: str, name: str = "report.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, csv_text)
    return buffer.getvalue()


class ReportZipTests(SimpleTestCase):
    def test_rows_are_read_from_the_csv_member(self):
        payload = _zip_of("CampaignName,Clicks,Spend\nBrand,10,4.50\nGeneric,3,1.20\n")
        rows = ms_ads._rows_from_zip(payload, 500, "CampaignPerformanceReport")
        self.assertEqual(
            rows,
            [
                {"CampaignName": "Brand", "Clicks": "10", "Spend": "4.50"},
                {"CampaignName": "Generic", "Clicks": "3", "Spend": "1.20"},
            ],
        )

    def test_the_trailing_blank_line_is_not_a_row(self):
        payload = _zip_of("CampaignName,Clicks\nBrand,10\n\n")
        self.assertEqual(len(ms_ads._rows_from_zip(payload, 500, "r")), 1)

    def test_a_copyright_footer_is_not_a_row(self):
        # A footer line has more fields than the header, so DictReader hands
        # the overflow back under a None key. Those lines are not data.
        payload = _zip_of(
            "CampaignName,Clicks\nBrand,10\n"
            '"(c) 2026 Microsoft Corporation. All rights reserved.",,,\n'
        )
        rows = ms_ads._rows_from_zip(payload, 500, "r")
        self.assertEqual(rows, [{"CampaignName": "Brand", "Clicks": "10"}])

    def test_the_limit_is_honoured(self):
        body = "".join(f"C{i},{i}\n" for i in range(50))
        payload = _zip_of("CampaignName,Clicks\n" + body)
        self.assertEqual(len(ms_ads._rows_from_zip(payload, 5, "r")), 5)

    def test_a_utf8_bom_does_not_break_the_first_column_name(self):
        payload = _zip_of("﻿CampaignName,Clicks\nBrand,10\n")
        rows = ms_ads._rows_from_zip(payload, 500, "r")
        self.assertEqual(rows[0]["CampaignName"], "Brand")

    def test_something_that_is_not_a_zip_is_reported_plainly(self):
        with self.assertRaises(ConnectorError) as caught:
            ms_ads._rows_from_zip(b"<html>error</html>", 500, "CampaignPerformanceReport")
        self.assertIn("not a ZIP", str(caught.exception))

    def test_a_zip_with_no_csv_is_reported_plainly(self):
        payload = _zip_of("nothing here", name="readme.txt")
        with self.assertRaises(ConnectorError) as caught:
            ms_ads._rows_from_zip(payload, 500, "CampaignPerformanceReport")
        self.assertIn("no CSV", str(caught.exception))
