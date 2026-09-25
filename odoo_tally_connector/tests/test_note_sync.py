"""
Tests for Credit Note / Debit Note synchronization (Odoo -> Tally direction).

Covers the shared XML builder, the service's dispatch/validation gates, and
- most importantly - the Dr/Cr sign convention for each note type (see
tally_credit_debit_note_sync_service.py's module docstring for why Credit
Notes mirror Purchase Vouchers and Debit Notes mirror Sales Vouchers).
"""

from odoo.tests import TransactionCase

from ..services import TallyValidationError


class TestTallyCreditDebitNoteXmlBuilder(TransactionCase):
    def test_build_credit_note_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_credit_debit_note_upsert_request(
            vch_type="Credit Note",
            company="Digi",
            voucher_number="CN/001",
            voucher_date="20260907",
            party_ledger="ABC Corp",
            guid="cn-guid-1",
            ledger_entries=[
                {"ledger_name": "ABC Corp", "amount": 100.0, "is_deemed_positive": False, "is_party_ledger": True},
            ],
            inventory_entries=[
                {
                    "stock_item_name": "Screw",
                    "quantity": 10,
                    "rate": 10.0,
                    "amount": 100.0,
                    "unit": "Units",
                    "is_deemed_positive": True,
                    "accounting_allocation": {"ledger_name": "Sales Account", "amount": -100.0, "is_deemed_positive": True},
                },
            ],
            action="Create",
        )
        self.assertIn('VCHTYPE="Credit Note"', xml)
        self.assertIn("<VOUCHERTYPENAME>Credit Note</VOUCHERTYPENAME>", xml)
        self.assertIn('OBJVIEW="Invoice Voucher View"', xml)
        self.assertIn("<VCHENTRYMODE>Item Invoice</VCHENTRYMODE>", xml)
        self.assertIn("<AMOUNT>100.00</AMOUNT>", xml)
        self.assertIn("<AMOUNT>-100.00</AMOUNT>", xml)

    def test_build_debit_note_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_credit_debit_note_upsert_request(
            vch_type="Debit Note",
            company="Digi",
            voucher_number="DN/001",
            voucher_date="20260907",
            party_ledger="Vendor Corp",
            guid="dn-guid-1",
            ledger_entries=[
                {"ledger_name": "Vendor Corp", "amount": -100.0, "is_deemed_positive": True, "is_party_ledger": True},
            ],
            inventory_entries=[],
            action="Create",
        )
        self.assertIn('VCHTYPE="Debit Note"', xml)
        self.assertIn("<VOUCHERTYPENAME>Debit Note</VOUCHERTYPENAME>", xml)


class TestTallyCreditDebitNoteSyncServiceValidation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.partner = self.env["res.partner"].create({"name": "Note Test Partner"})

    def test_sync_non_note_raises_validation_error(self):
        from ..services import TallyCreditDebitNoteSyncService

        invoice = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.partner.id}
        )
        service = TallyCreditDebitNoteSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_note(invoice)

    def test_sync_draft_note_raises_validation_error(self):
        from ..services import TallyCreditDebitNoteSyncService

        move = self.env["account.move"].create(
            {"move_type": "out_refund", "partner_id": self.partner.id}
        )
        self.assertEqual(move.state, "draft")
        service = TallyCreditDebitNoteSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_note(move)


class TestTallyCreditDebitNoteSignConvention(TransactionCase):
    """
    Verifies the actual Dr/Cr amounts and is_deemed_positive flags built for
    each note type - this is the part most likely to be wrong if the mirror
    logic in the module docstring is misapplied.
    """

    def setUp(self):
        super().setUp()
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

        self.partner = self.env["res.partner"].create(
            {
                "name": "Note Party",
                "tally_synced_name": "Note Party",
                "tally_guid": "party-guid",
                "tally_sync_status": "success",
            }
        )
        self.product = self.env["product.product"].create(
            {
                "name": "Note Item",
                "uom_id": self.uom_units.id,
                "tally_synced_name": "Note Item",
                "tally_guid": "item-guid",
                "tally_sync_status": "success",
            }
        )
        self.account = self.env["account.account"].create(
            {
                "code": "TALLYNOTE1",
                "name": "Tally Note Account",
                "account_type": "income",
                "tally_ledger_name": "Sales Account",
            }
        )

    def _move_with_line(self, move_type):
        move = self.env["account.move"].create(
            {
                "move_type": move_type,
                "partner_id": self.partner.id,
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "quantity": 2,
                            "price_unit": 50.0,
                            "account_id": self.account.id,
                            "tax_ids": [(6, 0, [])],
                        },
                    )
                ],
            }
        )
        return move

    def test_credit_note_mirrors_purchase_sign_convention(self):
        from ..services import TallyCreditDebitNoteSyncService

        move = self._move_with_line("out_refund")
        service = TallyCreditDebitNoteSyncService(self.env)
        config = {"vch_type": "Credit Note", "party_is_debit": False, "party_role": "Customer"}
        ledger_entries, inventory_entries, partner = service._validate_and_build_entries(move, config)

        party_entry = next(e for e in ledger_entries if e["is_party_ledger"])
        self.assertAlmostEqual(party_entry["amount"], move.amount_total)
        self.assertFalse(party_entry["is_deemed_positive"])

        alloc = inventory_entries[0]["accounting_allocation"]
        self.assertAlmostEqual(alloc["amount"], -100.0)
        self.assertTrue(alloc["is_deemed_positive"])

    def test_debit_note_mirrors_sales_sign_convention(self):
        from ..services import TallyCreditDebitNoteSyncService

        move = self._move_with_line("in_refund")
        service = TallyCreditDebitNoteSyncService(self.env)
        config = {"vch_type": "Debit Note", "party_is_debit": True, "party_role": "Vendor"}
        ledger_entries, inventory_entries, partner = service._validate_and_build_entries(move, config)

        party_entry = next(e for e in ledger_entries if e["is_party_ledger"])
        self.assertAlmostEqual(party_entry["amount"], -move.amount_total)
        self.assertTrue(party_entry["is_deemed_positive"])

        alloc = inventory_entries[0]["accounting_allocation"]
        self.assertAlmostEqual(alloc["amount"], 100.0)
        self.assertFalse(alloc["is_deemed_positive"])

        # Whole voucher must balance to zero (party + all allocations).
        total = party_entry["amount"] + sum(e["accounting_allocation"]["amount"] for e in inventory_entries)
        self.assertAlmostEqual(total, 0.0)
