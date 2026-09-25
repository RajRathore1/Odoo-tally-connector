"""
Customer Receipt / Vendor Payment synchronization service.

Orchestrates Odoo account.payment (confirmed) -> Tally Receipt/Payment
voucher upsert. Unlike invoices/bills/notes, a Receipt or Payment is a
plain 2-leg accounting voucher - no stock items, no tax lines - just the
party ledger and a Cash/Bank ledger (see
TallyXmlBuilder.build_receipt_payment_voucher_upsert_request).

Sign convention (derived the same way as Credit/Debit Notes' - see
tally_credit_debit_note_sync_service.py's module docstring for the
Dr/Cr-vs-amount-sign pattern this project has consistently used and
verified against real Tally each time it was applied):
- Receipt (money received from a customer): Cash/Bank is the debit leg
  (amount negative), the customer is the credit leg (amount positive) -
  receiving money reduces what the customer owes.
- Payment (money paid to a vendor): the vendor is the debit leg (amount
  negative), Cash/Bank is the credit leg (amount positive) - paying money
  reduces what is owed to the vendor.
"""

import logging
import uuid

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError, TallyValidationError

_logger = logging.getLogger(__name__)

# payment_type -> (Tally VCHTYPE, party role label)
_PAYMENT_CONFIG = {
    "inbound": {"vch_type": "Receipt", "party_role": "Customer"},
    "outbound": {"vch_type": "Payment", "party_role": "Vendor"},
}

_SYNCABLE_STATES = ("paid", "reconciled")


class TallyPaymentSyncService:
    """Synchronizes confirmed Odoo payments to Tally Receipt/Payment vouchers."""

    def __init__(self, env):
        self.env = env

    def _get_connection(self, company):
        connection = self.env["tally.connection"].search(
            [
                ("company_id", "=", company.id),
                ("active", "=", True),
                ("enabled_for_sync", "=", True),
            ],
            limit=1,
        )
        if not connection:
            raise TallyConfigurationError(
                f"No active, sync-enabled Tally connection found for company '{company.name}'. "
                f"Configure one under Tally > Configuration > Connections and test it first."
            )
        return connection

    def _compute_sync_key(self, payment):
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, self.env.cr.dbname)
        return str(uuid.uuid5(namespace, f"odoo:account.payment:{payment.id}"))

    def _validate_and_build_entries(self, payment, config):
        """
        Validate dependencies and build the 2-leg ledger entry list, with
        signs following the module docstring's Receipt/Payment convention.

        Returns:
            tuple: (partner, journal, ledger_entries)
        """
        partner = payment.partner_id.commercial_partner_id or payment.partner_id
        if not partner or partner.tally_sync_status != "success" or not partner.tally_guid:
            raise TallyMappingError(
                f"{config['party_role']} '{partner.display_name if partner else '(none)'}' has "
                f"not been synced to Tally yet. Sync it first (Contacts > Tally tab > Sync to Tally)."
            )

        journal = payment.journal_id
        if not journal or not journal.tally_ledger_name:
            raise TallyMappingError(
                f"Journal '{journal.display_name if journal else '(none)'}' on payment "
                f"'{payment.name}' has no Tally Ledger Name mapping. Set it on the journal "
                f"(Accounting > Configuration > Journals)."
            )

        is_receipt = payment.payment_type == "inbound"
        cash_amount = -payment.amount if is_receipt else payment.amount
        party_amount = payment.amount if is_receipt else -payment.amount

        voucher_label = payment.name or payment.payment_reference or f"Payment {payment.id}"
        ledger_entries = [
            {
                "ledger_name": journal.tally_ledger_name,
                "amount": cash_amount,
                "is_deemed_positive": is_receipt,
            },
            {
                "ledger_name": partner.tally_synced_name or partner.display_name,
                "amount": party_amount,
                "is_deemed_positive": not is_receipt,
                "is_party_ledger": True,
                "bill_allocation": {"name": voucher_label, "amount": party_amount},
            },
        ]
        return partner, journal, ledger_entries

    def sync_payment(self, payment):
        """
        Sync a single confirmed Odoo payment to Tally as a Receipt/Payment voucher.

        Args:
            payment (account.payment): record to sync (payment_type must be
                inbound or outbound)

        Returns:
            dict: raw result from TallyClient.upsert_receipt_payment()

        Raises:
            TallyConfigurationError: No usable connection for the payment's company
            TallyValidationError: Not a confirmed inbound/outbound payment
            TallyMappingError: Party not synced, or journal missing a Tally ledger mapping
        """
        config = _PAYMENT_CONFIG.get(payment.payment_type)
        if not config:
            raise TallyValidationError(
                f"'{payment.name}' is a '{payment.payment_type}' payment - only inbound "
                f"(Receive) and outbound (Send) payments can sync to Tally."
            )
        if payment.state not in _SYNCABLE_STATES:
            raise TallyValidationError(
                f"'{payment.name}' is not confirmed (state={payment.state}). "
                f"Only confirmed payments sync to Tally."
            )

        company = payment.company_id or self.env.company
        connection = self._get_connection(company)

        partner, journal, ledger_entries = self._validate_and_build_entries(payment, config)
        voucher_label = payment.name or payment.payment_reference or f"Payment {payment.id}"

        if not payment.tally_sync_key:
            payment.tally_sync_key = self._compute_sync_key(payment)

        already_synced = payment.tally_sync_status == "success" and bool(payment.tally_guid)
        action = "Alter" if already_synced else "Create"

        voucher_date = fields.Date.to_string(payment.date).replace("-", "")

        client = connection._get_tally_client()
        result = client.upsert_receipt_payment(
            vch_type=config["vch_type"],
            company=connection.tally_company_name or company.name,
            voucher_number=voucher_label,
            voucher_date=voucher_date,
            party_ledger=partner.tally_synced_name or partner.display_name,
            guid=payment.tally_sync_key,
            ledger_entries=ledger_entries,
            action=action,
            narration=payment.memo and payment.memo[:500] or None,
            reference=payment.payment_reference,
            res_model="account.payment",
            res_id=payment.id,
        )

        vals = {
            "tally_last_sync_at": fields.Datetime.now(),
            "tally_sync_attempts": payment.tally_sync_attempts + 1,
        }

        if result.get("queued"):
            vals["tally_sync_status"] = "queued"
            payment.write(vals)
            _logger.info(
                f"{config['vch_type']} queued for async Tally Agent processing: {voucher_label}",
                extra={"payment_id": payment.id, "connection_id": connection.id, "job_id": result.get("job_id")},
            )
            return result

        if result["success"]:
            vals.update(
                {
                    "tally_sync_status": "success",
                    "tally_guid": payment.tally_sync_key,
                    "tally_last_sync_error": False,
                }
            )
            _logger.info(
                f"{config['vch_type']} synced to Tally: {voucher_label} ({action})",
                extra={"payment_id": payment.id, "connection_id": connection.id, "action": action},
            )
        else:
            vals.update(
                {
                    "tally_sync_status": "failed",
                    "tally_last_sync_error": result.get("error") or result.get("message"),
                }
            )
            _logger.warning(
                f"{config['vch_type']} sync to Tally failed: {voucher_label}",
                extra={"payment_id": payment.id, "connection_id": connection.id, "error": result.get("error")},
            )

        payment.write(vals)
        return result
