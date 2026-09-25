"""
Tests for Tally -> Odoo Credit Note / Debit Note import (reverse sync
direction). Mirrors test_bill_import.py's coverage for both note types,
since a single service (TallyCreditDebitNoteImportService) handles both.
"""

from datetime import date
from unittest.mock import patch

from odoo.tests import TransactionCase

from ..services import TallyConfigurationError


class TestTallyCreditDebitNoteImportService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

        self.connection = self.env["tally.connection"].create(
            {
                "name": "Import Note Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

        self.partner = self.env["res.partner"].create(
            {"name": "Note Party", "tally_synced_name": "Note Party", "tally_guid": "party-guid"}
        )
        self.product = self.env["product.product"].create(
            {
                "name": "Screw",
                "uom_id": self.uom_units.id,
                "tally_synced_name": "Screw",
                "tally_guid": "item-guid",
            }
        )
        self.account = self.env["account.account"].create(
            {
                "code": "TALLYNOTEIMP1",
                "name": "Tally Import Note Account",
                "account_type": "income",
                "tally_ledger_name": "Sales Account",
            }
        )

    def _voucher(self, guid="guid-1", voucher_number="CN/001"):
        return {
            "guid": guid,
            "date": "20260902",
            "voucher_number": voucher_number,
            "party_ledger_name": "Note Party",
            "narration": "Imported from Tally",
            "reference": "REF-1",
            "ledger_entries": [
                {"ledger_name": "Note Party", "amount": 100.0, "is_deemed_positive": False, "is_party_ledger": True},
            ],
            "inventory_entries": [
                {
                    "stock_item_name": "Screw",
                    "quantity": 10.0,
                    "unit": "Units",
                    "rate": 10.0,
                    "amount": 100.0,
                    "accounting_allocations": [{"ledger_name": "Sales Account", "amount": -100.0}],
                }
            ],
        }

    def _mock_fetch(self, vouchers):
        return {"success": True, "vouchers": vouchers, "message": "ok", "error": None}

    def test_unknown_note_type_raises(self):
        from ..services import TallyCreditDebitNoteImportService

        service = TallyCreditDebitNoteImportService(self.env)
        with self.assertRaises(TallyConfigurationError):
            service.import_notes(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), note_type="bogus"
            )

    def test_import_creates_draft_credit_note(self):
        from ..services import TallyCreditDebitNoteImportService

        service = TallyCreditDebitNoteImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_credit_debit_notes"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher()])
            result = service.import_notes(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), note_type="credit_note"
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["created"]), 1)
        move = self.env["account.move"].search([("tally_guid", "=", "guid-1")], limit=1)
        self.assertTrue(move)
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.move_type, "out_refund")
        self.assertEqual(move.partner_id, self.partner)

    def test_import_creates_draft_debit_note(self):
        from ..services import TallyCreditDebitNoteImportService

        service = TallyCreditDebitNoteImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_credit_debit_notes"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher(guid="guid-2", voucher_number="DN/001")])
            result = service.import_notes(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), note_type="debit_note"
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["created"]), 1)
        move = self.env["account.move"].search([("tally_guid", "=", "guid-2")], limit=1)
        self.assertTrue(move)
        self.assertEqual(move.move_type, "in_refund")

    def test_import_skips_already_synced_voucher(self):
        from ..services import TallyCreditDebitNoteImportService

        existing = self.env["account.move"].create(
            {"move_type": "out_refund", "partner_id": self.partner.id, "tally_guid": "guid-1"}
        )

        service = TallyCreditDebitNoteImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_credit_debit_notes"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher(guid="guid-1")])
            result = service.import_notes(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), note_type="credit_note"
            )

        self.assertEqual(result["created"], [])
        self.assertIn("CN/001", result["skipped"])
        self.assertEqual(existing.state, "draft")

    def test_import_unmapped_party_reports_error(self):
        from ..services import TallyCreditDebitNoteImportService

        voucher = self._voucher()
        voucher["party_ledger_name"] = "Unknown Ledger"

        service = TallyCreditDebitNoteImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_credit_debit_notes"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_notes(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), note_type="credit_note"
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Unknown Ledger", result["errors"][0]["error"])

    def test_import_missing_guid_reports_error(self):
        from ..services import TallyCreditDebitNoteImportService

        voucher = self._voucher(guid="")

        service = TallyCreditDebitNoteImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_credit_debit_notes"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_notes(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), note_type="credit_note"
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("GUID", result["errors"][0]["error"])
