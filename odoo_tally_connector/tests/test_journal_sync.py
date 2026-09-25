"""
Tests for Phase 8: Journal Voucher synchronization.

Covers the XML builder (structure, escaping, ledger-entry balancing) and the
service's early validation gates (move type, posted state, sync key). Deep
dependency-resolution paths (missing account mapping) are documented in the
service's docstrings and exercised manually against a real Tally instance,
matching the same testing philosophy as test_invoice_sync.py - this voucher
shape has never been verified against real Tally yet (see
TallyXmlBuilder.build_journal_voucher_upsert_request's docstring).
"""

from odoo.tests import TransactionCase

from ..services import TallyValidationError


class TestTallyJournalVoucherXmlBuilder(TransactionCase):
    def test_build_create_request_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_journal_voucher_upsert_request(
            company="Digi",
            voucher_number="JNL/001",
            voucher_date="20260907",
            guid="journal-guid-1",
            ledger_entries=[
                {"ledger_name": "Depreciation Expense", "amount": -500.0, "is_deemed_positive": True},
                {"ledger_name": "Accumulated Depreciation", "amount": 500.0, "is_deemed_positive": False},
            ],
            action="Create",
        )
        self.assertIn('VCHTYPE="Journal"', xml)
        self.assertIn('ACTION="Create"', xml)
        self.assertIn('REMOTEID="journal-guid-1"', xml)
        self.assertIn('OBJVIEW="Accounting Voucher View"', xml)
        self.assertNotIn("VCHENTRYMODE", xml)
        self.assertNotIn("PARTYLEDGERNAME", xml)
        self.assertIn("<VOUCHERNUMBER>JNL/001</VOUCHERNUMBER>", xml)
        self.assertIn("<LEDGERNAME>Depreciation Expense</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>-500.00</AMOUNT>", xml)
        self.assertIn("<LEDGERNAME>Accumulated Depreciation</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>500.00</AMOUNT>", xml)
        self.assertIn("<ISPARTYLEDGER>No</ISPARTYLEDGER>", xml)

    def test_date_immediately_precedes_vouchertypename(self):
        """
        Same real Tally quirk as every other voucher builder in this module:
        DATE must directly precede VOUCHERTYPENAME or Tally reports "Voucher
        date is missing" even though a DATE tag with a valid value is present.
        """
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_journal_voucher_upsert_request(
            company="Digi",
            voucher_number="JNL/002",
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

        xml = TallyXmlBuilder.build_journal_voucher_upsert_request(
            company="Digi",
            voucher_number="JNL/003",
            voucher_date="20260907",
            guid="g-3",
            ledger_entries=[{"ledger_name": 'X & "Y" <Z>', "amount": 0.0, "is_deemed_positive": True}],
            action="Create",
        )
        self.assertNotIn("<Z>", xml)
        self.assertIn("&amp;", xml)

    def test_supports_more_than_two_ledger_entries(self):
        """Unlike Receipt/Payment's fixed 2-leg shape, a Journal voucher must
        support N legs (e.g. one debit split across several credit lines)."""
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_journal_voucher_upsert_request(
            company="Digi",
            voucher_number="JNL/004",
            voucher_date="20260907",
            guid="g-4",
            ledger_entries=[
                {"ledger_name": "Provision Expense", "amount": -300.0, "is_deemed_positive": True},
                {"ledger_name": "Provision A", "amount": 100.0, "is_deemed_positive": False},
                {"ledger_name": "Provision B", "amount": 100.0, "is_deemed_positive": False},
                {"ledger_name": "Provision C", "amount": 100.0, "is_deemed_positive": False},
            ],
            action="Create",
        )
        self.assertEqual(xml.count("<LEDGERENTRIES.LIST>"), 4)

    def test_ledger_entries_balance_to_zero(self):
        """Sanity check on the fixture math itself - a real Tally would reject an unbalanced voucher."""
        entries = [
            {"ledger_name": "Depreciation Expense", "amount": -500.0, "is_deemed_positive": True},
            {"ledger_name": "Accumulated Depreciation", "amount": 500.0, "is_deemed_positive": False},
        ]
        self.assertAlmostEqual(sum(e["amount"] for e in entries), 0.0)


class TestTallyJournalSyncServiceValidation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company

    def test_sync_key_is_deterministic(self):
        from ..services import TallyJournalSyncService

        move = self.env["account.move"].create({"move_type": "entry"})
        service = TallyJournalSyncService(self.env)
        key1 = service._compute_sync_key(move)
        key2 = service._compute_sync_key(move)
        self.assertEqual(key1, key2)

    def test_wrong_move_type_raises_validation_error(self):
        from ..services import TallyJournalSyncService

        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.env["res.partner"].create({"name": "X"}).id}
        )
        service = TallyJournalSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_journal_entry(move)

    def test_unposted_journal_entry_raises_validation_error(self):
        from ..services import TallyJournalSyncService

        move = self.env["account.move"].create({"move_type": "entry"})
        self.assertEqual(move.state, "draft")
        service = TallyJournalSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_journal_entry(move)
