"""
Tests for Tally -> Odoo Receipt / Payment voucher import (reverse sync
direction). Mirrors test_note_import.py's coverage for both voucher kinds,
since a single service (TallyPaymentImportService) handles both.
"""

from datetime import date
from unittest.mock import patch

from odoo.tests import TransactionCase

from ..services import TallyConfigurationError


class TestTallyPaymentImportService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company

        self.connection = self.env["tally.connection"].create(
            {
                "name": "Import Payment Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

        self.partner = self.env["res.partner"].create(
            {"name": "Cash Party", "tally_synced_name": "Cash Party", "tally_guid": "party-guid"}
        )
        self.journal = self.env["account.journal"].search(
            [("type", "=", "cash"), ("company_id", "=", self.company.id)], limit=1
        )
        if not self.journal:
            self.journal = self.env["account.journal"].create(
                {"name": "Test Cash", "type": "cash", "code": "TCSH", "company_id": self.company.id}
            )
        self.journal.tally_ledger_name = "Cash"

    def _voucher(self, guid="guid-1", voucher_number="RCPT/001", cash_amount=-100.0, party_amount=100.0):
        return {
            "guid": guid,
            "date": "20260902",
            "voucher_number": voucher_number,
            "party_ledger_name": "Cash Party",
            "narration": "Imported from Tally",
            "reference": "REF-1",
            "ledger_entries": [
                {"ledger_name": "Cash", "amount": cash_amount, "is_deemed_positive": True, "is_party_ledger": False},
                {
                    "ledger_name": "Cash Party",
                    "amount": party_amount,
                    "is_deemed_positive": False,
                    "is_party_ledger": True,
                },
            ],
            "inventory_entries": [],
        }

    def _mock_fetch(self, vouchers):
        return {"success": True, "vouchers": vouchers, "message": "ok", "error": None}

    def test_import_handles_both_entries_marked_party_ledger(self):
        """
        Real Tally quirk (confirmed by exporting a manually-created Receipt
        voucher): vouchers entered via the simple Account/Particulars mode
        carry ISPARTYLEDGER=Yes on BOTH ledger entries, not just the actual
        party. The Cash/Bank leg must still be identified correctly via its
        journal mapping.
        """
        from ..services import TallyPaymentImportService

        voucher = self._voucher()
        for entry in voucher["ledger_entries"]:
            entry["is_party_ledger"] = True

        service = TallyPaymentImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_receipt_payment_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_payments(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), voucher_kind="receipt"
            )

        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["created"]), 1)
        payment = self.env["account.payment"].search([("tally_guid", "=", "guid-1")], limit=1)
        self.assertEqual(payment.journal_id, self.journal)
        self.assertEqual(payment.partner_id, self.partner)

    def test_unknown_voucher_kind_raises(self):
        from ..services import TallyPaymentImportService

        service = TallyPaymentImportService(self.env)
        with self.assertRaises(TallyConfigurationError):
            service.import_payments(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), voucher_kind="bogus"
            )

    def test_import_creates_draft_receipt(self):
        from ..services import TallyPaymentImportService

        service = TallyPaymentImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_receipt_payment_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher()])
            result = service.import_payments(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), voucher_kind="receipt"
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["created"]), 1)
        payment = self.env["account.payment"].search([("tally_guid", "=", "guid-1")], limit=1)
        self.assertTrue(payment)
        self.assertEqual(payment.state, "draft")
        self.assertEqual(payment.payment_type, "inbound")
        self.assertEqual(payment.partner_type, "customer")
        self.assertEqual(payment.partner_id, self.partner)
        self.assertEqual(payment.journal_id, self.journal)
        self.assertAlmostEqual(payment.amount, 100.0)

    def test_import_creates_draft_payment(self):
        from ..services import TallyPaymentImportService

        service = TallyPaymentImportService(self.env)
        voucher = self._voucher(guid="guid-2", voucher_number="PAY/001", cash_amount=100.0, party_amount=-100.0)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_receipt_payment_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_payments(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), voucher_kind="payment"
            )

        self.assertTrue(result["success"])
        payment = self.env["account.payment"].search([("tally_guid", "=", "guid-2")], limit=1)
        self.assertTrue(payment)
        self.assertEqual(payment.payment_type, "outbound")
        self.assertEqual(payment.partner_type, "supplier")
        self.assertAlmostEqual(payment.amount, 100.0)

    def test_import_skips_already_synced_voucher(self):
        from ..services import TallyPaymentImportService

        existing = self.env["account.payment"].create(
            {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": self.partner.id,
                "journal_id": self.journal.id,
                "amount": 1.0,
                "tally_guid": "guid-1",
            }
        )

        service = TallyPaymentImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_receipt_payment_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher(guid="guid-1")])
            result = service.import_payments(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), voucher_kind="receipt"
            )

        self.assertEqual(result["created"], [])
        self.assertIn("RCPT/001", result["skipped"])
        self.assertEqual(existing.state, "draft")

    def test_import_unmapped_journal_reports_error(self):
        from ..services import TallyPaymentImportService

        self.journal.tally_ledger_name = False
        service = TallyPaymentImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_receipt_payment_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([self._voucher()])
            result = service.import_payments(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), voucher_kind="receipt"
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Cash", result["errors"][0]["error"])

    def test_import_missing_guid_reports_error(self):
        from ..services import TallyPaymentImportService

        voucher = self._voucher(guid="")

        service = TallyPaymentImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_receipt_payment_vouchers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch([voucher])
            result = service.import_payments(
                self.connection, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), voucher_kind="receipt"
            )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("GUID", result["errors"][0]["error"])
