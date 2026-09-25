"""
Tests for Customer Receipt / Vendor Payment synchronization (Odoo -> Tally
direction). Covers the XML builder, the service's validation gates, and -
most importantly - the Dr/Cr sign convention for Receipt vs Payment (see
tally_payment_sync_service.py's module docstring).
"""

from odoo.tests import TransactionCase

from ..services import TallyValidationError


class TestTallyReceiptPaymentXmlBuilder(TransactionCase):
    def test_build_receipt_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_receipt_payment_voucher_upsert_request(
            vch_type="Receipt",
            company="Digi",
            voucher_number="RCPT/001",
            voucher_date="20260907",
            party_ledger="ABC Corp",
            guid="rcpt-guid-1",
            ledger_entries=[
                {"ledger_name": "Cash", "amount": -100.0, "is_deemed_positive": True},
                {"ledger_name": "ABC Corp", "amount": 100.0, "is_deemed_positive": False, "is_party_ledger": True},
            ],
            action="Create",
        )
        self.assertIn('VCHTYPE="Receipt"', xml)
        self.assertIn("<VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>", xml)
        self.assertIn("<VOUCHERNUMBER>RCPT/001</VOUCHERNUMBER>", xml)
        self.assertIn("<PARTYLEDGERNAME>ABC Corp</PARTYLEDGERNAME>", xml)
        self.assertIn("<LEDGERNAME>Cash</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>-100.00</AMOUNT>", xml)
        # Plain accounting voucher - "Accounting Voucher View", not the
        # Item Invoice attributes used by Sales/Purchase/Notes.
        self.assertIn('OBJVIEW="Accounting Voucher View"', xml)
        self.assertNotIn("VCHENTRYMODE", xml)
        self.assertNotIn("ALLINVENTORYENTRIES.LIST", xml)

    def test_build_payment_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_receipt_payment_voucher_upsert_request(
            vch_type="Payment",
            company="Digi",
            voucher_number="PAY/001",
            voucher_date="20260907",
            party_ledger="Vendor Corp",
            guid="pay-guid-1",
            ledger_entries=[
                {"ledger_name": "Vendor Corp", "amount": -100.0, "is_deemed_positive": True, "is_party_ledger": True},
                {"ledger_name": "Cash", "amount": 100.0, "is_deemed_positive": False},
            ],
            action="Create",
        )
        self.assertIn('VCHTYPE="Payment"', xml)
        self.assertIn("<VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>", xml)


class TestTallyPaymentSyncServiceValidation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Payment Test Partner"})

    def test_sync_draft_payment_raises_validation_error(self):
        from ..services import TallyPaymentSyncService

        payment = self.env["account.payment"].create(
            {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": self.partner.id,
                "amount": 100.0,
            }
        )
        self.assertEqual(payment.state, "draft")
        service = TallyPaymentSyncService(self.env)
        with self.assertRaises(TallyValidationError):
            service.sync_payment(payment)


class TestTallyPaymentSignConvention(TransactionCase):
    """
    Verifies the actual Dr/Cr amounts and is_deemed_positive flags built for
    Receipt vs Payment - the part most likely to be wrong if the sign
    convention in the module docstring is misapplied (as happened once
    already for Credit/Debit Notes before tests caught it).
    """

    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create(
            {"name": "Cash Party", "tally_synced_name": "Cash Party", "tally_guid": "party-guid",
             "tally_sync_status": "success"}
        )
        self.journal = self.env["account.journal"].search(
            [("type", "=", "cash"), ("company_id", "=", self.env.company.id)], limit=1
        )
        if not self.journal:
            self.journal = self.env["account.journal"].create(
                {"name": "Test Cash", "type": "cash", "code": "TCSH", "company_id": self.env.company.id}
            )
        self.journal.tally_ledger_name = "Cash"

    def test_receipt_sign_convention(self):
        from ..services import TallyPaymentSyncService

        payment = self.env["account.payment"].create(
            {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": self.partner.id,
                "journal_id": self.journal.id,
                "amount": 100.0,
            }
        )
        service = TallyPaymentSyncService(self.env)
        config = {"vch_type": "Receipt", "party_role": "Customer"}
        partner, journal, ledger_entries = service._validate_and_build_entries(payment, config)

        cash_entry = next(e for e in ledger_entries if not e.get("is_party_ledger"))
        party_entry = next(e for e in ledger_entries if e.get("is_party_ledger"))

        self.assertAlmostEqual(cash_entry["amount"], -100.0)
        self.assertTrue(cash_entry["is_deemed_positive"])
        self.assertAlmostEqual(party_entry["amount"], 100.0)
        self.assertFalse(party_entry["is_deemed_positive"])
        self.assertAlmostEqual(cash_entry["amount"] + party_entry["amount"], 0.0)

    def test_payment_sign_convention(self):
        from ..services import TallyPaymentSyncService

        payment = self.env["account.payment"].create(
            {
                "payment_type": "outbound",
                "partner_type": "supplier",
                "partner_id": self.partner.id,
                "journal_id": self.journal.id,
                "amount": 100.0,
            }
        )
        service = TallyPaymentSyncService(self.env)
        config = {"vch_type": "Payment", "party_role": "Vendor"}
        partner, journal, ledger_entries = service._validate_and_build_entries(payment, config)

        cash_entry = next(e for e in ledger_entries if not e.get("is_party_ledger"))
        party_entry = next(e for e in ledger_entries if e.get("is_party_ledger"))

        self.assertAlmostEqual(cash_entry["amount"], 100.0)
        self.assertFalse(cash_entry["is_deemed_positive"])
        self.assertAlmostEqual(party_entry["amount"], -100.0)
        self.assertTrue(party_entry["is_deemed_positive"])
        self.assertAlmostEqual(cash_entry["amount"] + party_entry["amount"], 0.0)

    def test_unmapped_journal_reports_error(self):
        from ..services import TallyPaymentSyncService, TallyMappingError

        self.journal.tally_ledger_name = False
        payment = self.env["account.payment"].create(
            {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": self.partner.id,
                "journal_id": self.journal.id,
                "amount": 100.0,
            }
        )
        service = TallyPaymentSyncService(self.env)
        config = {"vch_type": "Receipt", "party_role": "Customer"}
        with self.assertRaises(TallyMappingError):
            service._validate_and_build_entries(payment, config)
