"""
Tests for the scheduled Tally sync queue (Queue/Retry/Cron phase).

Covers: disabled/inactive connections are skipped, never-synced (draft)
and previously-failed-but-under-the-attempt-cap records are picked up,
records already synced, over the attempt cap, or in a different company
are left alone, and one record's failure doesn't stop the rest of the
queue from being processed.

Real TallyClient calls are never exercised here - action_sync_to_tally is
patched (via a plain function assigned with patch(..., new=...), which
correctly captures `self` as the record being synced) so these tests stay
fast, deterministic, and precise about which records were actually synced
- the sync logic itself is covered by each service's own tests.
"""

from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase

from ..models.tally_connection import MAX_AUTO_SYNC_ATTEMPTS

_NOTIFICATION = {"type": "ir.actions.client", "tag": "display_notification", "params": {}}

_PATCH_TARGETS = [
    "odoo.addons.odoo_tally_connector.models.account_move.AccountMove.action_sync_to_tally",
    "odoo.addons.odoo_tally_connector.models.account_payment.AccountPayment.action_sync_to_tally",
    "odoo.addons.odoo_tally_connector.models.product_product.ProductProduct.action_sync_to_tally",
]


def _recording_sync(recorder):
    """A replacement action_sync_to_tally that records the id it was called on."""

    def _sync(self):
        recorder.append(self.id)
        return _NOTIFICATION

    return _sync


class _NoopOtherModels:
    """
    Patches action_sync_to_tally to a no-op on every syncable model EXCEPT
    res.partner (the one each test below actually exercises), so pending
    products/moves/payments already in the test DB don't trigger real
    TallyClient calls or interfere with assertions about partner syncing.
    """

    def __enter__(self):
        self._patchers = [patch(target, return_value=_NOTIFICATION) for target in _PATCH_TARGETS]
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc_info):
        for p in self._patchers:
            p.stop()


class TestTallySyncQueueCron(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Cron Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

    def test_disabled_connection_is_skipped(self):
        self.connection.enabled_for_sync = False
        self.env["res.partner"].create({"name": "Never Synced", "customer_rank": 1})

        synced_ids = []
        with _NoopOtherModels(), patch(
            "odoo.addons.odoo_tally_connector.models.res_partner.ResPartner.action_sync_to_tally",
            new=_recording_sync(synced_ids),
        ):
            self.env["tally.connection"]._cron_process_sync_queue()

        self.assertEqual(synced_ids, [])

    def test_inactive_connection_is_skipped(self):
        self.connection.active = False
        self.env["res.partner"].create({"name": "Never Synced", "customer_rank": 1})

        synced_ids = []
        with _NoopOtherModels(), patch(
            "odoo.addons.odoo_tally_connector.models.res_partner.ResPartner.action_sync_to_tally",
            new=_recording_sync(synced_ids),
        ):
            self.env["tally.connection"]._cron_process_sync_queue()

        self.assertEqual(synced_ids, [])

    def test_picks_up_draft_and_failed_partners_within_attempt_cap(self):
        never_synced = self.env["res.partner"].create({"name": "Never Synced", "customer_rank": 1})
        failed_within_cap = self.env["res.partner"].create(
            {
                "name": "Failed Partner",
                "customer_rank": 1,
                "tally_sync_status": "failed",
                "tally_sync_attempts": MAX_AUTO_SYNC_ATTEMPTS - 1,
            }
        )
        failed_over_cap = self.env["res.partner"].create(
            {
                "name": "Given Up Partner",
                "customer_rank": 1,
                "tally_sync_status": "failed",
                "tally_sync_attempts": MAX_AUTO_SYNC_ATTEMPTS,
            }
        )
        already_synced = self.env["res.partner"].create(
            {"name": "Already Synced", "customer_rank": 1, "tally_sync_status": "success"}
        )
        self.env["res.partner"].create({"name": "Plain Contact (not customer/vendor)"})

        synced_ids = []
        with _NoopOtherModels(), patch(
            "odoo.addons.odoo_tally_connector.models.res_partner.ResPartner.action_sync_to_tally",
            new=_recording_sync(synced_ids),
        ):
            self.connection._sync_pending_queue(auto_commit=False)

        self.assertIn(never_synced.id, synced_ids)
        self.assertIn(failed_within_cap.id, synced_ids)
        self.assertNotIn(failed_over_cap.id, synced_ids)
        self.assertNotIn(already_synced.id, synced_ids)

    def test_sync_failure_does_not_abort_the_rest_of_the_queue(self):
        partner_a = self.env["res.partner"].create({"name": "Partner A", "customer_rank": 1})
        partner_b = self.env["res.partner"].create({"name": "Partner B", "customer_rank": 1})

        call_order = []

        def fake_sync(self):
            call_order.append(self.id)
            if self.id == partner_a.id:
                raise RuntimeError("simulated unexpected failure")
            return _NOTIFICATION

        with _NoopOtherModels(), patch(
            "odoo.addons.odoo_tally_connector.models.res_partner.ResPartner.action_sync_to_tally",
            new=fake_sync,
        ):
            self.connection._sync_pending_queue(auto_commit=False)

        self.assertIn(partner_a.id, call_order)
        self.assertIn(partner_b.id, call_order)

    def test_does_not_sync_partner_from_a_different_company(self):
        other_company = self.env["res.company"].create({"name": "Other Co"})
        other_partner = self.env["res.partner"].create(
            {"name": "Other Co Partner", "customer_rank": 1, "company_id": other_company.id}
        )
        own_company_partner = self.env["res.partner"].create(
            {"name": "Own Co Partner", "customer_rank": 1, "company_id": self.company.id}
        )

        synced_ids = []
        with _NoopOtherModels(), patch(
            "odoo.addons.odoo_tally_connector.models.res_partner.ResPartner.action_sync_to_tally",
            new=_recording_sync(synced_ids),
        ):
            self.connection._sync_pending_queue(auto_commit=False)

        self.assertNotIn(other_partner.id, synced_ids)
        self.assertIn(own_company_partner.id, synced_ids)


_IMPORT_SERVICE_TARGETS = [
    "odoo.addons.odoo_tally_connector.services.tally_product_import_service.TallyProductImportService.import_products",
    "odoo.addons.odoo_tally_connector.services.tally_partner_import_service.TallyPartnerImportService.import_partners",
    "odoo.addons.odoo_tally_connector.services.tally_invoice_import_service.TallyInvoiceImportService.import_invoices",
    "odoo.addons.odoo_tally_connector.services.tally_bill_import_service.TallyBillImportService.import_bills",
    "odoo.addons.odoo_tally_connector.services.tally_credit_debit_note_import_service."
    "TallyCreditDebitNoteImportService.import_notes",
    "odoo.addons.odoo_tally_connector.services.tally_payment_import_service.TallyPaymentImportService.import_payments",
]

_OK_RESULT = {"success": True, "created": [], "skipped": [], "errors": [], "message": "ok"}


class TestTallyPullPendingFromTally(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Pull Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

    def test_calls_every_import_service(self):
        patchers = [patch(target, return_value=_OK_RESULT) for target in _IMPORT_SERVICE_TARGETS]
        mocks = [p.start() for p in patchers]
        try:
            self.connection._pull_pending_from_tally(auto_commit=False)
        finally:
            for p in patchers:
                p.stop()

        products_mock, partners_mock, invoices_mock, bills_mock, notes_mock, payments_mock = mocks
        products_mock.assert_called_once()
        partners_mock.assert_called_once()
        invoices_mock.assert_called_once()
        bills_mock.assert_called_once()
        # Called once for "credit_note" and once for "debit_note".
        self.assertEqual(notes_mock.call_count, 2)
        note_types = {call.args[-1] for call in notes_mock.call_args_list}
        self.assertEqual(note_types, {"credit_note", "debit_note"})
        # Called once for "receipt" and once for "payment".
        self.assertEqual(payments_mock.call_count, 2)
        voucher_kinds = {call.args[-1] for call in payments_mock.call_args_list}
        self.assertEqual(voucher_kinds, {"receipt", "payment"})

    def test_advances_last_auto_pull_date(self):
        self.assertFalse(self.connection.last_auto_pull_date)
        patchers = [patch(target, return_value=_OK_RESULT) for target in _IMPORT_SERVICE_TARGETS]
        for p in patchers:
            p.start()
        try:
            self.connection._pull_pending_from_tally(auto_commit=False)
        finally:
            for p in patchers:
                p.stop()

        self.assertEqual(self.connection.last_auto_pull_date, fields.Date.context_today(self.connection))

    def test_one_import_service_failing_does_not_abort_the_rest(self):
        patchers = [patch(target, return_value=_OK_RESULT) for target in _IMPORT_SERVICE_TARGETS]
        mocks = [p.start() for p in patchers]
        products_mock = mocks[0]
        products_mock.side_effect = RuntimeError("simulated failure fetching products")
        partners_mock = mocks[1]
        try:
            self.connection._pull_pending_from_tally(auto_commit=False)
        finally:
            for p in patchers:
                p.stop()

        products_mock.assert_called_once()
        partners_mock.assert_called_once()

    def test_reported_failure_result_does_not_raise(self):
        failed_result = {"success": False, "message": "connection timed out", "created": [], "skipped": [], "errors": []}
        patchers = [patch(target, return_value=_OK_RESULT) for target in _IMPORT_SERVICE_TARGETS]
        mocks = [p.start() for p in patchers]
        mocks[0].return_value = failed_result
        try:
            # Must not raise, even though the products import "succeeded"
            # in the sense of not throwing but reported success=False.
            self.connection._pull_pending_from_tally(auto_commit=False)
        finally:
            for p in patchers:
                p.stop()

        self.assertTrue(self.connection.last_auto_pull_date)
