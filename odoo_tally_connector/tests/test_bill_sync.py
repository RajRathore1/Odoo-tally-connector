"""
Tests for Purchase Bill synchronization (Odoo -> Tally direction).

Mirrors test_invoice_sync.py's structure - covers the Purchase Voucher XML
builder and the service's early validation gates (move type, posted state,
sync key). Deep dependency-resolution paths are documented in the service's
docstrings and exercised manually against a real Tally instance.
"""

from odoo.tests import TransactionCase

from ..services import TallyValidationError


class TestTallyPurchaseVoucherXmlBuilder(TransactionCase):
    def test_build_create_request_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_purchase_voucher_upsert_request(
            company="Digi",
            voucher_number="BILL/001",
            voucher_date="20260907",
            party_ledger="Vendor Corp",
            guid="bill-voucher-guid-1",
            ledger_entries=[
                {
                    "ledger_name": "Vendor Corp",
                    "amount": 118.0,
                    "is_deemed_positive": False,
                    "is_party_ledger": True,
                },
                {"ledger_name": "Input CGST", "amount": -18.0, "is_deemed_positive": True},
            ],
            inventory_entries=[
                {
                    "stock_item_name": "Screw",
                    "quantity": 10,
                    "rate": 10.0,
                    "amount": 100.0,
                    "unit": "Units",
                    "accounting_allocation": {"ledger_name": "Purchase Account", "amount": -100.0},
                },
            ],
            action="Create",
        )
        self.assertIn('VCHTYPE="Purchase"', xml)
        self.assertIn('ACTION="Create"', xml)
        self.assertIn('REMOTEID="bill-voucher-guid-1"', xml)
        self.assertIn('OBJVIEW="Invoice Voucher View"', xml)
        self.assertIn("<VCHENTRYMODE>Item Invoice</VCHENTRYMODE>", xml)
        self.assertIn("<VOUCHERNUMBER>BILL/001</VOUCHERNUMBER>", xml)
        self.assertIn("<PARTYLEDGERNAME>Vendor Corp</PARTYLEDGERNAME>", xml)
        self.assertIn("<LEDGERENTRIES.LIST>", xml)
        self.assertIn("<ISPARTYLEDGER>Yes</ISPARTYLEDGER>", xml)
        self.assertIn("<AMOUNT>118.00</AMOUNT>", xml)
        self.assertIn("<STOCKITEMNAME>Screw</STOCKITEMNAME>", xml)
        self.assertIn("<ACTUALQTY>10 Units</ACTUALQTY>", xml)

        # The purchase account allocation must be nested inside the
        # inventory entry, not a sibling top-level ledger entry.
        inventory_block = xml[xml.index("<ALLINVENTORYENTRIES.LIST>"): xml.index("</ALLINVENTORYENTRIES.LIST>")]
        self.assertIn("<ACCOUNTINGALLOCATIONS.LIST>", inventory_block)
        self.assertIn("<LEDGERNAME>Purchase Account</LEDGERNAME>", inventory_block)

    def test_escapes_special_characters(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_purchase_voucher_upsert_request(
            company="Digi",
            voucher_number="BILL/002",
            voucher_date="20260907",
            party_ledger='Vendor & "Sons" <Ltd>',
            guid="g-2",
            ledger_entries=[{"ledger_name": "X & Y", "amount": 0.0, "is_deemed_positive": True}],
            inventory_entries=[],
            action="Create",
        )
        self.assertNotIn("<Ltd>", xml)
        self.assertIn("&amp;", xml)


class TestTallyBillSyncServiceValidation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.partner = self.env["res.partner"].create({"name": "Bill Test Vendor"})

    def test_sync_key_is_deterministic(self):
        from ..services import TallyBillSyncService

        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.partner.id}
        )
        service = TallyBillSyncService(self.env)
        key1 = service._compute_sync_key(move)
        key2 = service._compute_sync_key(move)
        self.assertEqual(key1, key2)

    def test_sync_non_vendor_bill_raises_validation_error(self):
        from ..services import TallyBillSyncService

        invoice = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.partner.id}
        )
        service = TallyBillSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_bill(invoice)

    def test_sync_draft_bill_raises_validation_error(self):
        from ..services import TallyBillSyncService

        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.partner.id}
        )
        self.assertEqual(move.state, "draft")
        service = TallyBillSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_bill(move)


class TestAccountMoveSyncDispatch(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Dispatch Test Partner"})

    def test_sync_dispatches_to_bill_service_for_vendor_bill(self):
        move = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.partner.id}
        )
        result = move.action_sync_to_tally()
        # Blocked by missing dependencies (no connection/vendor sync), but
        # crucially routed through TallyBillSyncService's validation, not
        # silently ignored or mis-routed to the Sales flow.
        self.assertEqual(result["type"], "ir.actions.client")
        self.assertEqual(result["params"]["type"], "danger")

    def test_sync_dispatches_to_note_service_for_credit_note(self):
        move = self.env["account.move"].create(
            {"move_type": "out_refund", "partner_id": self.partner.id}
        )
        result = move.action_sync_to_tally()
        # Blocked by missing dependencies, but routed through
        # TallyCreditDebitNoteSyncService, not reported as unsupported.
        self.assertEqual(result["params"]["type"], "danger")
        self.assertNotIn("only customer invoices", result["params"]["message"])

    def test_sync_dispatches_to_journal_service_for_misc_entry(self):
        """
        Phase 8: move_type='entry' is now a supported voucher type (routed
        through TallyJournalSyncService), not rejected as unsupported - this
        used to assert the opposite (see git history) before Journal Voucher
        sync was added.
        """
        move = self.env["account.move"].create({"move_type": "entry"})
        result = move.action_sync_to_tally()
        # Blocked by draft state (not posted), but routed through
        # TallyJournalSyncService, not reported as unsupported.
        self.assertEqual(result["params"]["type"], "danger")
        self.assertNotIn("only customer invoices", result["params"]["message"])
        self.assertIn("not posted", result["params"]["message"])

    def test_sync_dispatches_to_contra_service_when_every_line_is_cash_or_bank(self):
        """
        Phase 9: a journal entry whose every line is on a Bank/Cash account
        is a Contra voucher by Tally's own definition, not a plain Journal -
        _is_contra_entry() must detect this and route accordingly.
        """
        cash1 = self.env["account.account"].create(
            {"name": "Contra Test Cash 1", "code": "CTC001", "account_type": "asset_cash"}
        )
        cash2 = self.env["account.account"].create(
            {"name": "Contra Test Cash 2", "code": "CTC002", "account_type": "asset_cash"}
        )
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "line_ids": [
                    (0, 0, {"account_id": cash1.id, "debit": 10.0, "credit": 0.0}),
                    (0, 0, {"account_id": cash2.id, "debit": 0.0, "credit": 10.0}),
                ],
            }
        )
        self.assertTrue(move._is_contra_entry())
        result = move.action_sync_to_tally()
        # Blocked by draft state (not posted), but routed through
        # TallyContraSyncService, not TallyJournalSyncService.
        self.assertEqual(result["params"]["type"], "danger")
        self.assertIn("not posted", result["params"]["message"])

    def test_sync_dispatches_to_journal_service_when_any_line_is_not_cash(self):
        """A journal entry with even one non-Bank/Cash line is NOT a Contra."""
        cash = self.env["account.account"].create(
            {"name": "Contra Test Cash 3", "code": "CTC003", "account_type": "asset_cash"}
        )
        expense = self.env["account.account"].create(
            {"name": "Contra Test Expense", "code": "CTC004", "account_type": "expense"}
        )
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "line_ids": [
                    (0, 0, {"account_id": expense.id, "debit": 10.0, "credit": 0.0}),
                    (0, 0, {"account_id": cash.id, "debit": 0.0, "credit": 10.0}),
                ],
            }
        )
        self.assertFalse(move._is_contra_entry())
