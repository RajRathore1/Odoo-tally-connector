"""
Tests for Phase 9: Contra Voucher synchronization.

Covers the XML builder (structure, escaping, ledger-entry balancing) and the
service's early validation gates (move type, posted state, sync key, and
the Contra-specific "every line must be Bank/Cash" gate). Deep
dependency-resolution paths (missing account mapping) are documented in the
service's docstrings and exercised manually against a real Tally instance,
matching test_journal_sync.py's testing philosophy. Dispatch-level routing
(Contra vs Journal detection) is covered in test_bill_sync.py's
TestAccountMoveSyncDispatch, not here.
"""

from odoo.tests import TransactionCase

from ..services import TallyValidationError


class TestTallyContraVoucherXmlBuilder(TransactionCase):
    def test_build_create_request_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_contra_voucher_upsert_request(
            company="Digi",
            voucher_number="CONTRA/001",
            voucher_date="20260907",
            guid="contra-guid-1",
            ledger_entries=[
                {"ledger_name": "HDFC Bank", "amount": -1000.0, "is_deemed_positive": True},
                {"ledger_name": "Cash", "amount": 1000.0, "is_deemed_positive": False},
            ],
            action="Create",
        )
        self.assertIn('VCHTYPE="Contra"', xml)
        self.assertIn('ACTION="Create"', xml)
        self.assertIn('REMOTEID="contra-guid-1"', xml)
        self.assertIn('OBJVIEW="Accounting Voucher View"', xml)
        self.assertNotIn("VCHENTRYMODE", xml)
        self.assertNotIn("PARTYLEDGERNAME", xml)
        self.assertIn("<VOUCHERTYPENAME>Contra</VOUCHERTYPENAME>", xml)
        self.assertIn("<VOUCHERNUMBER>CONTRA/001</VOUCHERNUMBER>", xml)
        self.assertIn("<LEDGERNAME>HDFC Bank</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>-1000.00</AMOUNT>", xml)
        self.assertIn("<LEDGERNAME>Cash</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>1000.00</AMOUNT>", xml)
        self.assertIn("<ISPARTYLEDGER>No</ISPARTYLEDGER>", xml)

    def test_date_immediately_precedes_vouchertypename(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_contra_voucher_upsert_request(
            company="Digi",
            voucher_number="CONTRA/002",
            voucher_date="20260907",
            guid="g-2",
            ledger_entries=[{"ledger_name": "A", "amount": 0.0, "is_deemed_positive": True}],
            action="Create",
            narration="Some narration text",
        )
        date_idx = xml.index("<DATE>")
        vouchertype_idx = xml.index("<VOUCHERTYPENAME>")
        narration_idx = xml.index("<NARRATION>")
        self.assertLess(date_idx, vouchertype_idx)
        self.assertGreater(narration_idx, vouchertype_idx, "NARRATION must not sit between DATE and VOUCHERTYPENAME")

    def test_escapes_special_characters_in_ledger_names(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_contra_voucher_upsert_request(
            company="Digi",
            voucher_number="CONTRA/003",
            voucher_date="20260907",
            guid="g-3",
            ledger_entries=[{"ledger_name": 'X & "Y" <Z>', "amount": 0.0, "is_deemed_positive": True}],
            action="Create",
        )
        self.assertNotIn("<Z>", xml)
        self.assertIn("&amp;", xml)

    def test_ledger_entries_balance_to_zero(self):
        """Sanity check on the fixture math itself - a real Tally would reject an unbalanced voucher."""
        entries = [
            {"ledger_name": "HDFC Bank", "amount": -1000.0, "is_deemed_positive": True},
            {"ledger_name": "Cash", "amount": 1000.0, "is_deemed_positive": False},
        ]
        self.assertAlmostEqual(sum(e["amount"] for e in entries), 0.0)


class TestTallyContraSyncServiceValidation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.cash1 = self.env["account.account"].create(
            {"name": "Contra Svc Test Cash 1", "code": "CST001", "account_type": "asset_cash"}
        )
        self.cash2 = self.env["account.account"].create(
            {"name": "Contra Svc Test Cash 2", "code": "CST002", "account_type": "asset_cash"}
        )
        self.expense = self.env["account.account"].create(
            {"name": "Contra Svc Test Expense", "code": "CST003", "account_type": "expense"}
        )

    def test_sync_key_is_deterministic(self):
        from ..services import TallyContraSyncService

        move = self.env["account.move"].create({"move_type": "entry"})
        service = TallyContraSyncService(self.env)
        key1 = service._compute_sync_key(move)
        key2 = service._compute_sync_key(move)
        self.assertEqual(key1, key2)

    def test_wrong_move_type_raises_validation_error(self):
        from ..services import TallyContraSyncService

        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.env["res.partner"].create({"name": "X"}).id}
        )
        service = TallyContraSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_contra_entry(move)

    def test_unposted_entry_raises_validation_error(self):
        from ..services import TallyContraSyncService

        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "line_ids": [
                    (0, 0, {"account_id": self.cash1.id, "debit": 10.0, "credit": 0.0}),
                    (0, 0, {"account_id": self.cash2.id, "debit": 0.0, "credit": 10.0}),
                ],
            }
        )
        self.assertEqual(move.state, "draft")
        service = TallyContraSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_contra_entry(move)

    def test_non_cash_line_raises_validation_error_even_if_called_directly(self):
        """
        Defensive re-validation: even if a caller bypasses account_move.py's
        own _is_contra_entry() routing and calls this service directly on a
        mixed-account entry, it must still refuse rather than mis-sync it.
        """
        from ..services import TallyContraSyncService

        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "line_ids": [
                    (0, 0, {"account_id": self.expense.id, "debit": 10.0, "credit": 0.0}),
                    (0, 0, {"account_id": self.cash1.id, "debit": 0.0, "credit": 10.0}),
                ],
            }
        )
        service = TallyContraSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service._validate_and_build_entries(move)
