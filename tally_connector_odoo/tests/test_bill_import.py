"""
Tests for Tally -> Odoo Purchase voucher import (reverse sync direction).

Mirrors test_invoice_import.py's structure and coverage exactly, for the
vendor-bill direction: GUID-based skip of already-synced vouchers, mapping
resolution for vendor/product/account/tax, explicit errors on missing
mappings, and plain (non-item) accounting voucher support - using a mocked
TallyClient.
"""

from datetime import date
from unittest.mock import patch

from odoo.tests import TransactionCase


class TestTallyBillImportService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

        self.connection = self.env["tally.connection"].create(
            {
                "name": "Import Bill Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

        self.partner = self.env["res.partner"].create(
            {"name": "Some Vendor", "supplier_rank": 1, "tally_synced_name": "Some Vendor", "tally_guid": "vendor-guid"}
        )
        self.product = self.env["product.product"].create(
            {
                "name": "Screw",
                "uom_id": self.uom_units.id,
                "tally_synced_name": "Screw",
                "tally_guid": "item-guid",
            }
        )
        self.expense_account = self.env["account.account"].create(
            {
                "code": "TALLYIMP2",
                "name": "Tally Import Purchase Account",
                "account_type": "expense",
                "tally_ledger_name": "Purchase Account",
            }
        )

    def _voucher(self, guid="guid-1", voucher_number="PUR/001", tax_ledger_entries=None):
        return {
            "guid": guid,
            "vch_type": "Purchase",
            "date": "20260902",
            "voucher_number": voucher_number,
            "party_ledger_name": "Some Vendor",
            "narration": "Imported from Tally",
            "reference": "REF-1",
            "ledger_entries": [
                {
                    "ledger_name": "Some Vendor",
                    "amount": 100.0,
                    "is_deemed_positive": False,
                    "is_party_ledger": True,
                },
            ]
            + (tax_ledger_entries or []),
            "inventory_entries": [
                {
                    "stock_item_name": "Screw",
                    "quantity": 10.0,
                    "unit": "Units",
                    "rate": 10.0,
                    "amount": 100.0,
                    "accounting_allocations": [{"ledger_name": "Purchase Account", "amount": -100.0}],
                }
            ],
        }

    def _mock_fetch(self, vouchers):
        return {"success": True, "vouchers": vouchers, "message": "ok", "error": None}

    def test_import_creates_draft_bill(self):
        from ..services import TallyBillImportService

        service = TallyBillImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_purchase_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher()])
            result = service.import_bills(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["created"]), 1)
        move = self.env["account.move"].search([("tally_guid", "=", "guid-1")], limit=1)
        self.assertTrue(move)
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(move.partner_id, self.partner)
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertEqual(move.invoice_line_ids.product_id, self.product)
        self.assertEqual(move.invoice_line_ids.account_id, self.expense_account)

    def test_import_skips_already_synced_voucher(self):
        from ..services import TallyBillImportService

        existing = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.partner.id, "tally_guid": "guid-1"}
        )

        service = TallyBillImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_purchase_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher(guid="guid-1")])
            result = service.import_bills(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(result["created"], [])
        self.assertIn("PUR/001", result["skipped"])
        self.assertEqual(
            self.env["account.move"].search_count([("tally_guid", "=", "guid-1")]), 1
        )
        self.assertEqual(existing.state, "draft")

    def test_import_unmapped_vendor_reports_error(self):
        from ..services import TallyBillImportService

        voucher = self._voucher()
        voucher["party_ledger_name"] = "Unknown Vendor Ledger"

        service = TallyBillImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_purchase_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_bills(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Unknown Vendor Ledger", result["errors"][0]["error"])

    def test_import_maps_tax_ledger_when_configured(self):
        from ..services import TallyBillImportService

        tax = self.env["account.tax"].create(
            {
                "name": "Tally Imported Input CGST",
                "amount": 9.0,
                "type_tax_use": "purchase",
                "tally_ledger_name": "Input CGST",
            }
        )
        voucher = self._voucher(
            tax_ledger_entries=[
                {"ledger_name": "Input CGST", "amount": -9.0, "is_deemed_positive": True, "is_party_ledger": False}
            ]
        )

        service = TallyBillImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_purchase_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_bills(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(result["errors"], [])
        move = self.env["account.move"].search([("tally_guid", "=", "guid-1")], limit=1)
        self.assertIn(tax, move.invoice_line_ids.tax_ids)

    def test_import_plain_accounting_voucher_creates_ledger_line(self):
        from ..services import TallyBillImportService

        voucher = self._voucher()
        voucher["inventory_entries"] = []
        voucher["ledger_entries"].append(
            {"ledger_name": "Purchase Account", "amount": -100.0, "is_deemed_positive": True, "is_party_ledger": False}
        )

        service = TallyBillImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_purchase_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_bills(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["created"]), 1)
        move = self.env["account.move"].search([("tally_guid", "=", "guid-1")], limit=1)
        line = move.invoice_line_ids
        self.assertFalse(line.product_id)
        self.assertEqual(line.account_id, self.expense_account)
        self.assertAlmostEqual(line.price_unit, 100.0)

    def test_import_missing_guid_reports_error(self):
        from ..services import TallyBillImportService

        voucher = self._voucher(guid="")

        service = TallyBillImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_purchase_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_bills(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("GUID", result["errors"][0]["error"])
