"""
Tests for Phase 6: Sales Invoice synchronization.

Covers the XML builder (structure, escaping, ledger-entry balancing) and the
service's early validation gates (move type, posted state, sync key). Deep
dependency-resolution paths (customer/product not synced, missing account or
tax mapping) are documented in the service's docstrings and exercised
manually against a real Tally instance, since they require a fully
configured chart of accounts that a minimal test database won't have.
"""

from odoo.tests import TransactionCase

from ..services import TallyValidationError


class TestTallySalesVoucherXmlBuilder(TransactionCase):
    def test_build_create_request_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_sales_voucher_upsert_request(
            company="Digi",
            voucher_number="INV/001",
            voucher_date="20260907",
            party_ledger="ABC Corp",
            guid="voucher-guid-1",
            ledger_entries=[
                {"ledger_name": "ABC Corp", "amount": -118.0, "is_deemed_positive": True},
                {"ledger_name": "Sales Account", "amount": 100.0, "is_deemed_positive": False},
                {"ledger_name": "Output CGST", "amount": 18.0, "is_deemed_positive": False},
            ],
            inventory_entries=[
                {"stock_item_name": "Screw", "quantity": 10, "rate": 10.0, "amount": 100.0, "unit": "Units"},
            ],
            action="Create",
        )
        self.assertIn('VCHTYPE="Sales"', xml)
        self.assertIn('ACTION="Create"', xml)
        self.assertIn('REMOTEID="voucher-guid-1"', xml)
        self.assertIn("<VOUCHERNUMBER>INV/001</VOUCHERNUMBER>", xml)
        self.assertIn("<PARTYLEDGERNAME>ABC Corp</PARTYLEDGERNAME>", xml)
        self.assertIn("<LEDGERNAME>ABC Corp</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>-118.00</AMOUNT>", xml)
        self.assertIn("<STOCKITEMNAME>Screw</STOCKITEMNAME>", xml)
        self.assertIn("<ACTUALQTY>10 Units</ACTUALQTY>", xml)

    def test_date_immediately_precedes_vouchertypename(self):
        """
        Real Tally quirk: the XML parser is positionally sensitive - DATE must
        directly precede VOUCHERTYPENAME or Tally reports "Voucher date is
        missing" even though a DATE tag with a valid value is present.
        """
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_sales_voucher_upsert_request(
            company="Digi",
            voucher_number="INV/003",
            voucher_date="20260907",
            party_ledger="ABC Corp",
            guid="g-3",
            ledger_entries=[{"ledger_name": "ABC Corp", "amount": 0.0, "is_deemed_positive": True}],
            inventory_entries=[],
            action="Create",
            narration="Some narration text",
        )
        date_idx = xml.index("<DATE>")
        vouchertype_idx = xml.index("<VOUCHERTYPENAME>")
        narration_idx = xml.index("<NARRATION>")
        self.assertLess(date_idx, vouchertype_idx)
        self.assertGreater(narration_idx, vouchertype_idx, "NARRATION must not sit between DATE and VOUCHERTYPENAME")

    def test_escapes_special_characters_in_party_and_ledger_names(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_sales_voucher_upsert_request(
            company="Digi",
            voucher_number="INV/002",
            voucher_date="20260907",
            party_ledger='Corp & "Sons" <Ltd>',
            guid="g-2",
            ledger_entries=[{"ledger_name": "X & Y", "amount": 0.0, "is_deemed_positive": True}],
            inventory_entries=[],
            action="Create",
        )
        self.assertNotIn("<Ltd>", xml)
        self.assertIn("&amp;", xml)

    def test_matches_real_tally_item_invoice_structure(self):
        """
        Regression test for the real root cause of "Voucher date is missing":
        our XML did not match Tally's actual Item Invoice voucher shape
        (confirmed by exporting a manually-created voucher from Tally itself).
        Required: OBJVIEW="Invoice Voucher View", VCHENTRYMODE=Item Invoice,
        top-level LEDGERENTRIES.LIST (not ALLLEDGERENTRIES.LIST) for the party,
        and the sales ledger nested inside ALLINVENTORYENTRIES.LIST as an
        ACCOUNTINGALLOCATIONS.LIST.
        """
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_sales_voucher_upsert_request(
            company="Digi",
            voucher_number="INV/004",
            voucher_date="20260907",
            party_ledger="ABC Corp",
            guid="voucher-guid-4",
            ledger_entries=[
                {
                    "ledger_name": "ABC Corp",
                    "amount": -118.0,
                    "is_deemed_positive": True,
                    "is_party_ledger": True,
                    "bill_allocation": {"name": "INV/004", "amount": -118.0},
                },
                {"ledger_name": "Output CGST", "amount": 18.0, "is_deemed_positive": False},
            ],
            inventory_entries=[
                {
                    "stock_item_name": "Screw",
                    "quantity": 10,
                    "rate": 10.0,
                    "amount": 100.0,
                    "unit": "Units",
                    "accounting_allocation": {"ledger_name": "Sales Account", "amount": 100.0},
                },
            ],
            action="Create",
        )
        self.assertIn('OBJVIEW="Invoice Voucher View"', xml)
        self.assertIn("<VCHENTRYMODE>Item Invoice</VCHENTRYMODE>", xml)
        self.assertIn("<LEDGERENTRIES.LIST>", xml)
        self.assertNotIn("ALLLEDGERENTRIES.LIST", xml)
        self.assertIn("<ISPARTYLEDGER>Yes</ISPARTYLEDGER>", xml)

        # The sales ledger allocation must be nested inside the inventory
        # entry, not a sibling top-level ledger entry.
        inventory_block = xml[xml.index("<ALLINVENTORYENTRIES.LIST>"): xml.index("</ALLINVENTORYENTRIES.LIST>")]
        self.assertIn("<ACCOUNTINGALLOCATIONS.LIST>", inventory_block)
        self.assertIn("<LEDGERNAME>Sales Account</LEDGERNAME>", inventory_block)

        # Sales Account must NOT appear as a top-level LEDGERENTRIES.LIST entry.
        ledger_block = xml[xml.index("</ALLINVENTORYENTRIES.LIST>"):]
        top_level_ledger_names = ledger_block.split("<LEDGERENTRIES.LIST>")[1:]
        for block in top_level_ledger_names:
            self.assertNotIn("<LEDGERNAME>Sales Account</LEDGERNAME>", block.split("</LEDGERENTRIES.LIST>")[0])

    def test_ledger_entries_balance_to_zero(self):
        """Sanity check on the fixture math itself - a real Tally would reject an unbalanced voucher."""
        entries = [
            {"ledger_name": "ABC Corp", "amount": -118.0, "is_deemed_positive": True},
            {"ledger_name": "Sales Account", "amount": 100.0, "is_deemed_positive": False},
            {"ledger_name": "Output CGST", "amount": 18.0, "is_deemed_positive": False},
        ]
        self.assertAlmostEqual(sum(e["amount"] for e in entries), 0.0)


class TestTallyInvoiceSyncServiceValidation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.partner = self.env["res.partner"].create({"name": "Invoice Test Customer"})

    def test_sync_key_is_deterministic(self):
        from ..services import TallyInvoiceSyncService

        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.partner.id}
        )
        service = TallyInvoiceSyncService(self.env)
        key1 = service._compute_sync_key(move)
        key2 = service._compute_sync_key(move)
        self.assertEqual(key1, key2)

    def test_sync_non_customer_invoice_raises_validation_error(self):
        from ..services import TallyInvoiceSyncService

        bill = self.env["account.move"].create(
            {"move_type": "in_invoice", "partner_id": self.partner.id}
        )
        service = TallyInvoiceSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_invoice(bill)

    def test_sync_draft_invoice_raises_validation_error(self):
        from ..services import TallyInvoiceSyncService

        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.partner.id}
        )
        self.assertEqual(move.state, "draft")
        service = TallyInvoiceSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_invoice(move)
