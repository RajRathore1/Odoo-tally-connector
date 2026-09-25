"""
Receipt / Payment import service - Tally -> Odoo direction.

Orchestrates fetching Receipt or Payment vouchers from Tally (within a date
range) and creating matching Odoo account.payment records. A single
service handles both, parameterized by voucher_kind, mirroring
tally_credit_debit_note_import_service.py's design.

Matching strategy (same rationale as every other import service - see
tally_invoice_import_service.py): GUID-only, no name fallback; every
dependency (party, journal) must already be linked to Tally; imported
payments are left in draft (never auto-posted).
"""

import logging

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError

_logger = logging.getLogger(__name__)

# voucher_kind -> (Tally VCHTYPE to fetch, Odoo payment_type/partner_type to create)
_VOUCHER_CONFIG = {
    "receipt": {"vch_type": "Receipt", "payment_type": "inbound", "partner_type": "customer"},
    "payment": {"vch_type": "Payment", "payment_type": "outbound", "partner_type": "supplier"},
}


class TallyPaymentImportService:
    """Imports Tally Receipt/Payment vouchers into Odoo as draft account.payment records."""

    def __init__(self, env):
        self.env = env

    def import_payments(self, connection, date_from, date_to, voucher_kind):
        """
        Fetch Receipt or Payment vouchers from Tally within a date range and
        create matching draft Odoo payments.

        Args:
            connection (tally.connection): source connection (must be active
                and enabled for sync)
            date_from (date): start date (inclusive)
            date_to (date): end date (inclusive)
            voucher_kind (str): "receipt" or "payment"

        Returns:
            dict: {
                "success": bool,
                "created": [payment names/labels],
                "skipped": [voucher numbers already imported/synced],
                "errors": [{"name": str, "error": str}],
                "message": str,
            }

        Raises:
            TallyConfigurationError: connection not usable, or voucher_kind invalid
        """
        config = _VOUCHER_CONFIG.get(voucher_kind)
        if not config:
            raise TallyConfigurationError(
                f"Unknown voucher_kind '{voucher_kind}' - must be 'receipt' or 'payment'."
            )

        if not connection.active or not connection.enabled_for_sync:
            raise TallyConfigurationError(
                f"Connection '{connection.name}' is not active/enabled for sync."
            )

        client = connection._get_tally_client()
        result = client.fetch_receipt_payment_vouchers(
            vch_type=config["vch_type"],
            company=connection.tally_company_name,
            from_date=fields.Date.to_string(date_from).replace("-", ""),
            to_date=fields.Date.to_string(date_to).replace("-", ""),
        )

        if not result["success"]:
            return {
                "success": False,
                "created": [],
                "skipped": [],
                "errors": [],
                "message": result.get("message", f"Failed to fetch {config['vch_type']} vouchers from Tally"),
            }

        created, skipped, errors = [], [], []
        Payment = self.env["account.payment"]
        company = connection.company_id

        for voucher in result["vouchers"]:
            label = voucher.get("voucher_number") or "(no voucher number)"
            guid = (voucher.get("guid") or "").strip()

            if not guid:
                errors.append(
                    {"name": label, "error": "Voucher has no GUID/REMOTEID from Tally - cannot import safely."}
                )
                continue

            if Payment.search_count([("tally_guid", "=", guid)]):
                skipped.append(label)
                continue

            try:
                vals = self._build_payment_vals(voucher, company, config)
                payment = Payment.create(vals)
                created.append(payment.name or label)
                _logger.info(
                    f"{config['vch_type']} created from Tally import: {payment.name or label}",
                    extra={"payment_id": payment.id, "connection_id": connection.id, "tally_guid": guid},
                )
            except Exception as e:
                errors.append({"name": label, "error": str(e)})
                _logger.warning(
                    f"Error importing Tally {config['vch_type']} voucher '{label}': {str(e)}",
                    extra={"connection_id": connection.id},
                )

        message = f"Created {len(created)}, skipped {len(skipped)} (already synced), {len(errors)} error(s)"
        return {
            "success": True,
            "created": created,
            "skipped": skipped,
            "errors": errors,
            "message": message,
        }

    def _build_payment_vals(self, voucher, company, config):
        """
        Identify the Cash/Bank leg and the party leg among the voucher's 2
        ledger entries.

        Real Tally quirk (confirmed by exporting a manually-created Receipt
        voucher and reading its XML): vouchers entered via the simple
        "Account/Particulars" single-entry mode carry ISPARTYLEDGER=Yes on
        BOTH ledger entries, not just the actual party - so that flag
        cannot be used to tell them apart here (only our own generated
        vouchers, which explicitly set it on one side, would have been
        reliable). Instead, whichever entry's ledger name matches a
        configured journal's Tally Ledger Name is the Cash/Bank leg; the
        other is the party, resolved the normal way.
        """
        ledger_entries = voucher.get("ledger_entries") or []
        if len(ledger_entries) != 2:
            raise TallyMappingError(
                f"Receipt/Payment voucher must have exactly 2 ledger entries, found "
                f"{len(ledger_entries)} - multi-ledger vouchers are not supported."
            )

        journal = None
        cash_entry = None
        party_entry = None
        for entry in ledger_entries:
            journals = self.env["account.journal"].search(
                [("tally_ledger_name", "=", entry.get("ledger_name") or "")]
            )
            if not journals:
                party_entry = entry
                continue
            if len(journals) > 1:
                raise TallyMappingError(
                    f"Ambiguous match: {len(journals)} Odoo journals mapped to Tally ledger "
                    f"'{entry.get('ledger_name')}'."
                )
            if journal is not None:
                raise TallyMappingError(
                    "Both ledger entries in this voucher match a configured journal - cannot "
                    "identify which one is the party."
                )
            journal = journals
            cash_entry = entry

        if journal is None:
            raise TallyMappingError(
                "Neither ledger entry in this voucher matches a configured journal - set "
                "'Tally Ledger Name' on the correct Cash/Bank journal first "
                "(Accounting > Configuration > Journals)."
            )
        if party_entry is None:
            raise TallyMappingError(
                "Both ledger entries in this voucher match a configured journal - cannot "
                "identify the party."
            )

        partner = self._resolve_partner(party_entry.get("ledger_name"), config["partner_type"])
        invoice_date = self._parse_date(voucher.get("date"))

        return {
            "payment_type": config["payment_type"],
            "partner_type": config["partner_type"],
            "partner_id": partner.id,
            "journal_id": journal.id,
            "amount": abs(cash_entry.get("amount") or 0.0),
            "date": invoice_date,
            "memo": voucher.get("narration") or False,
            "payment_reference": voucher.get("reference") or False,
            "company_id": company.id,
            "tally_guid": voucher.get("guid"),
            "tally_sync_key": voucher.get("guid"),
            "tally_sync_status": "success",
            "tally_last_sync_at": fields.Datetime.now(),
        }

    def _resolve_partner(self, ledger_name, partner_type):
        ledger_name = (ledger_name or "").strip()
        if not ledger_name:
            raise TallyMappingError("Voucher party ledger entry has no ledger name.")

        partners = self.env["res.partner"].search([("tally_synced_name", "=", ledger_name)])
        if not partners:
            role = "customer" if partner_type == "customer" else "vendor"
            raise TallyMappingError(
                f"No Odoo contact linked to Tally ledger '{ledger_name}' - sync or import this {role} first."
            )
        if len(partners) > 1:
            raise TallyMappingError(
                f"Ambiguous match: {len(partners)} Odoo contacts linked to Tally ledger '{ledger_name}'."
            )
        return partners

    def _parse_date(self, yyyymmdd):
        yyyymmdd = (yyyymmdd or "").strip()
        if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
            raise TallyMappingError(f"Voucher date '{yyyymmdd}' is not in the expected YYYYMMDD format.")
        return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
