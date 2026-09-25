"""
Tests for Tally -> Odoo Sales voucher import (reverse sync direction).

Covers:
- XML builder for the raw Day Book style voucher export request
- Response parser for the raw voucher export response (nested ledger/
  inventory entries, accounting allocations)
- Import service matching strategy (GUID-based skip of already-synced
  vouchers, mapping resolution for partner/product/account/tax, explicit
  errors on missing mappings) using a mocked TallyClient.
"""

from datetime import date
from unittest.mock import patch

from odoo.tests import TransactionCase


class TestTallyXmlBuilderVoucherExport(TransactionCase):
    def test_build_voucher_export_request(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_voucher_export_request(
            company="Digi", from_date="20260901", to_date="20260930"
        )
        self.assertIn("<TALLYREQUEST>Export Data</TALLYREQUEST>", xml)
        self.assertIn("<REPORTNAME>Day Book</REPORTNAME>", xml)
        self.assertIn("<SVFROMDATE>20260901</SVFROMDATE>", xml)
        self.assertIn("<SVTODATE>20260930</SVTODATE>", xml)
        self.assertIn("<SVCURRENTCOMPANY>Digi</SVCURRENTCOMPANY>", xml)


class TestTallyVoucherExportParser(TransactionCase):
    SAMPLE_XML = """<ENVELOPE>
        <BODY><DATA><TALLYMESSAGE>
            <VOUCHER REMOTEID="guid-1" VCHTYPE="Sales" ACTION="Create" OBJVIEW="Invoice Voucher View">
                <DATE>20260902</DATE>
                <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
                <VOUCHERNUMBER>INV/2026/00007</VOUCHERNUMBER>
                <PARTYLEDGERNAME>raja</PARTYLEDGERNAME>
                <REFERENCE>PO-1</REFERENCE>
                <NARRATION>Test narration</NARRATION>
                <ALLINVENTORYENTRIES.LIST>
                    <STOCKITEMNAME>Screw</STOCKITEMNAME>
                    <RATE>10.00/Units</RATE>
                    <AMOUNT>100.00</AMOUNT>
                    <ACTUALQTY>10 Units</ACTUALQTY>
                    <ACCOUNTINGALLOCATIONS.LIST>
                        <LEDGERNAME>Sales Account</LEDGERNAME>
                        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                        <AMOUNT>100.00</AMOUNT>
                    </ACCOUNTINGALLOCATIONS.LIST>
                </ALLINVENTORYENTRIES.LIST>
                <LEDGERENTRIES.LIST>
                    <LEDGERNAME>raja</LEDGERNAME>
                    <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
                    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                    <AMOUNT>-100.00</AMOUNT>
                </LEDGERENTRIES.LIST>
            </VOUCHER>
            <VOUCHER REMOTEID="guid-2" VCHTYPE="Purchase" ACTION="Create">
                <DATE>20260902</DATE>
                <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
                <VOUCHERNUMBER>PUR/001</VOUCHERNUMBER>
                <PARTYLEDGERNAME>Some Vendor</PARTYLEDGERNAME>
            </VOUCHER>
        </TALLYMESSAGE></DATA></BODY>
    </ENVELOPE>"""

    def test_parse_filters_by_voucher_type(self):
        from ..services import TallyResponseParser

        result = TallyResponseParser.parse_voucher_export_response(self.SAMPLE_XML, vch_type_filter="Sales")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        voucher = result["data"][0]
        self.assertEqual(voucher["guid"], "guid-1")
        self.assertEqual(voucher["voucher_number"], "INV/2026/00007")
        self.assertEqual(voucher["party_ledger_name"], "raja")
        self.assertEqual(voucher["reference"], "PO-1")
        self.assertEqual(voucher["narration"], "Test narration")

    def test_parse_ledger_entries_and_party_flag(self):
        from ..services import TallyResponseParser

        result = TallyResponseParser.parse_voucher_export_response(self.SAMPLE_XML, vch_type_filter="Sales")
        voucher = result["data"][0]
        self.assertEqual(len(voucher["ledger_entries"]), 1)
        party_entry = voucher["ledger_entries"][0]
        self.assertEqual(party_entry["ledger_name"], "raja")
        self.assertTrue(party_entry["is_party_ledger"])
        self.assertAlmostEqual(party_entry["amount"], -100.0)

    def test_parse_inventory_entries_with_nested_allocation(self):
        from ..services import TallyResponseParser

        result = TallyResponseParser.parse_voucher_export_response(self.SAMPLE_XML, vch_type_filter="Sales")
        voucher = result["data"][0]
        self.assertEqual(len(voucher["inventory_entries"]), 1)
        entry = voucher["inventory_entries"][0]
        self.assertEqual(entry["stock_item_name"], "Screw")
        self.assertAlmostEqual(entry["quantity"], 10.0)
        self.assertEqual(entry["unit"], "Units")
        self.assertAlmostEqual(entry["rate"], 10.0)
        self.assertAlmostEqual(entry["amount"], 100.0)
        self.assertEqual(len(entry["accounting_allocations"]), 1)
        self.assertEqual(entry["accounting_allocations"][0]["ledger_name"], "Sales Account")
        self.assertAlmostEqual(entry["accounting_allocations"][0]["amount"], 100.0)

    def test_parse_error_response(self):
        from ..services import TallyResponseParser

        xml = "<ENVELOPE><LINEERROR>Company does not exist</LINEERROR></ENVELOPE>"
        result = TallyResponseParser.parse_voucher_export_response(xml)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Company does not exist")


class TestTallyInvoiceImportService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

        self.connection = self.env["tally.connection"].create(
            {
                "name": "Import Invoice Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

        self.partner = self.env["res.partner"].create(
            {"name": "Raja", "customer_rank": 1, "tally_synced_name": "raja", "tally_guid": "party-guid"}
        )
        self.product = self.env["product.product"].create(
            {
                "name": "Screw",
                "uom_id": self.uom_units.id,
                "tally_synced_name": "Screw",
                "tally_guid": "item-guid",
            }
        )
        self.income_account = self.env["account.account"].create(
            {
                "code": "TALLYIMP1",
                "name": "Tally Import Sales Account",
                "account_type": "income",
                "tally_ledger_name": "Sales Account",
            }
        )

    def _voucher(self, guid="guid-1", voucher_number="INV/2026/00007", tax_ledger_entries=None):
        return {
            "guid": guid,
            "vch_type": "Sales",
            "date": "20260902",
            "voucher_number": voucher_number,
            "party_ledger_name": "raja",
            "narration": "Imported from Tally",
            "reference": "REF-1",
            "ledger_entries": [
                {"ledger_name": "raja", "amount": -100.0, "is_deemed_positive": True, "is_party_ledger": True},
            ]
            + (tax_ledger_entries or []),
            "inventory_entries": [
                {
                    "stock_item_name": "Screw",
                    "quantity": 10.0,
                    "unit": "Units",
                    "rate": 10.0,
                    "amount": 100.0,
                    "accounting_allocations": [{"ledger_name": "Sales Account", "amount": 100.0}],
                }
            ],
        }

    def _mock_fetch(self, vouchers):
        return {"success": True, "vouchers": vouchers, "message": "ok", "error": None}

    def test_import_creates_draft_invoice(self):
        from ..services import TallyInvoiceImportService

        service = TallyInvoiceImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_sales_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher()])
            result = service.import_invoices(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["created"]), 1)
        move = self.env["account.move"].search([("tally_guid", "=", "guid-1")], limit=1)
        self.assertTrue(move)
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.move_type, "out_invoice")
        self.assertEqual(move.partner_id, self.partner)
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertEqual(move.invoice_line_ids.product_id, self.product)
        self.assertEqual(move.invoice_line_ids.account_id, self.income_account)

    def test_import_skips_already_synced_voucher(self):
        from ..services import TallyInvoiceImportService

        existing = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.partner.id, "tally_guid": "guid-1"}
        )

        service = TallyInvoiceImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_sales_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher(guid="guid-1")])
            result = service.import_invoices(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(result["created"], [])
        self.assertIn("INV/2026/00007", result["skipped"])
        # No second invoice created for the same GUID.
        self.assertEqual(
            self.env["account.move"].search_count([("tally_guid", "=", "guid-1")]), 1
        )
        self.assertEqual(existing.state, "draft")

    def test_import_unmapped_customer_reports_error_not_silent_skip(self):
        from ..services import TallyInvoiceImportService

        voucher = self._voucher()
        voucher["party_ledger_name"] = "Unknown Ledger"

        service = TallyInvoiceImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_sales_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_invoices(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Unknown Ledger", result["errors"][0]["error"])
        self.assertFalse(self.env["account.move"].search([("tally_guid", "=", "guid-1")]))

    def test_import_unmapped_tax_ledger_reports_error(self):
        from ..services import TallyInvoiceImportService

        voucher = self._voucher(
            tax_ledger_entries=[
                {"ledger_name": "Output CGST", "amount": 9.0, "is_deemed_positive": False, "is_party_ledger": False}
            ]
        )

        service = TallyInvoiceImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_sales_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_invoices(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Output CGST", result["errors"][0]["error"])

    def test_import_maps_tax_ledger_when_configured(self):
        from ..services import TallyInvoiceImportService

        tax = self.env["account.tax"].create(
            {
                "name": "Tally Imported CGST",
                "amount": 9.0,
                "type_tax_use": "sale",
                "tally_ledger_name": "Output CGST",
            }
        )
        voucher = self._voucher(
            tax_ledger_entries=[
                {"ledger_name": "Output CGST", "amount": 9.0, "is_deemed_positive": False, "is_party_ledger": False}
            ]
        )

        service = TallyInvoiceImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_sales_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_invoices(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(result["errors"], [])
        move = self.env["account.move"].search([("tally_guid", "=", "guid-1")], limit=1)
        self.assertIn(tax, move.invoice_line_ids.tax_ids)

    def test_import_plain_accounting_voucher_creates_ledger_line(self):
        """
        A Tally voucher entered via 'Accounting Voucher' view (no stock
        items - ledgers posted directly) must still import: one invoice
        line per non-party, non-tax ledger entry, using its mapped account
        and no product.
        """
        from ..services import TallyInvoiceImportService

        voucher = self._voucher()
        voucher["inventory_entries"] = []
        voucher["ledger_entries"].append(
            {"ledger_name": "Sales Account", "amount": 100.0, "is_deemed_positive": False, "is_party_ledger": False}
        )

        service = TallyInvoiceImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_sales_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_invoices(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["created"]), 1)
        move = self.env["account.move"].search([("tally_guid", "=", "guid-1")], limit=1)
        self.assertEqual(len(move.invoice_line_ids), 1)
        line = move.invoice_line_ids
        self.assertFalse(line.product_id)
        self.assertEqual(line.account_id, self.income_account)
        self.assertAlmostEqual(line.price_unit, 100.0)

    def test_import_plain_accounting_voucher_unmapped_ledger_reports_error(self):
        from ..services import TallyInvoiceImportService

        voucher = self._voucher()
        voucher["inventory_entries"] = []
        voucher["ledger_entries"].append(
            {"ledger_name": "Unmapped Ledger", "amount": 100.0, "is_deemed_positive": False, "is_party_ledger": False}
        )

        service = TallyInvoiceImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_sales_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_invoices(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Unmapped Ledger", result["errors"][0]["error"])

    def test_import_missing_guid_reports_error(self):
        from ..services import TallyInvoiceImportService

        voucher = self._voucher(guid="")

        service = TallyInvoiceImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_sales_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_invoices(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("GUID", result["errors"][0]["error"])
