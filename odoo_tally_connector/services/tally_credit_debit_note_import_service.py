"""
Credit Note / Debit Note import service - Tally -> Odoo direction.

Orchestrates fetching Credit Note or Debit Note vouchers from Tally (within
a date range) and creating matching Odoo account.move records. A single
service handles both directions: unlike the push direction, the pull
direction does not need to know the Dr/Cr sign convention at all - Tally's
own RATE/AMOUNT fields are used as-is for each line (exactly like
tally_invoice_import_service.py / tally_bill_import_service.py), so the
only real difference between importing a Credit Note and a Debit Note is
which move_type the resulting Odoo record gets. See
tally_invoice_import_service.py for the full rationale behind the matching
strategy (GUID-only, no name fallback), draft-only import, and plain
accounting voucher support.
"""

import logging

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError

_logger = logging.getLogger(__name__)

# note_type -> (Tally VCHTYPE to fetch, Odoo move_type to create)
_NOTE_CONFIG = {
    "credit_note": {"vch_type": "Credit Note", "move_type": "out_refund", "party_role": "Customer"},
    "debit_note": {"vch_type": "Debit Note", "move_type": "in_refund", "party_role": "Vendor"},
}


class TallyCreditDebitNoteImportService:
    """Imports Tally Credit Note / Debit Note vouchers into Odoo as draft account.move records."""

    def __init__(self, env):
        self.env = env

    def import_notes(self, connection, date_from, date_to, note_type):
        """
        Fetch Credit Note or Debit Note vouchers from Tally within a date
        range and create matching draft Odoo records.

        Args:
            connection (tally.connection): source connection (must be active
                and enabled for sync)
            date_from (date): start date (inclusive)
            date_to (date): end date (inclusive)
            note_type (str): "credit_note" or "debit_note"

        Returns:
            dict: {
                "success": bool,
                "created": [move names],
                "skipped": [voucher numbers already imported/synced],
                "errors": [{"name": str, "error": str}],
                "message": str,
            }

        Raises:
            TallyConfigurationError: connection not usable, or note_type invalid
        """
        config = _NOTE_CONFIG.get(note_type)
        if not config:
            raise TallyConfigurationError(
                f"Unknown note_type '{note_type}' - must be 'credit_note' or 'debit_note'."
            )

        if not connection.active or not connection.enabled_for_sync:
            raise TallyConfigurationError(
                f"Connection '{connection.name}' is not active/enabled for sync."
            )

        client = connection._get_tally_client()
        result = client.fetch_credit_debit_notes(
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
        Move = self.env["account.move"]
        company = connection.company_id

        for voucher in result["vouchers"]:
            label = voucher.get("voucher_number") or "(no voucher number)"
            guid = (voucher.get("guid") or "").strip()

            if not guid:
                errors.append(
                    {"name": label, "error": "Voucher has no GUID/REMOTEID from Tally - cannot import safely."}
                )
                continue

            if Move.search_count([("tally_guid", "=", guid)]):
                skipped.append(label)
                continue

            try:
                vals = self._build_note_vals(voucher, company, config)
                move = Move.create(vals)
                created.append(move.name)
                _logger.info(
                    f"{config['vch_type']} created from Tally import: {move.name}",
                    extra={"move_id": move.id, "connection_id": connection.id, "tally_guid": guid},
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

    def _build_note_vals(self, voucher, company, config):
        partner = self._resolve_partner(voucher.get("party_ledger_name"), config["party_role"])
        ledger_entries = voucher.get("ledger_entries") or []
        inventory_entries = voucher.get("inventory_entries") or []

        if inventory_entries:
            tax_ids = self._resolve_taxes(ledger_entries)
            line_vals = []
            for entry in inventory_entries:
                product = self._resolve_product(entry.get("stock_item_name"))
                account = self._resolve_account(entry)
                line_vals.append(
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "name": product.display_name,
                            "quantity": entry.get("quantity") or 0.0,
                            "product_uom_id": product.uom_id.id,
                            "price_unit": entry.get("rate") or 0.0,
                            "account_id": account.id,
                            "tax_ids": [(6, 0, tax_ids)],
                        },
                    )
                )
        else:
            line_vals = self._build_plain_ledger_lines(ledger_entries)

        invoice_date = self._parse_date(voucher.get("date"))

        return {
            "move_type": config["move_type"],
            "partner_id": partner.id,
            "company_id": company.id,
            "invoice_date": invoice_date,
            "ref": voucher.get("reference") or False,
            "narration": voucher.get("narration") or False,
            "invoice_line_ids": line_vals,
            "tally_guid": voucher.get("guid"),
            "tally_sync_key": voucher.get("guid"),
            "tally_sync_status": "success",
            "tally_last_sync_at": fields.Datetime.now(),
        }

    def _build_plain_ledger_lines(self, ledger_entries):
        """Same plain-accounting-voucher handling as the invoice/bill import services."""
        tax_ids = []
        revenue_entries = []

        for entry in ledger_entries:
            if entry.get("is_party_ledger"):
                continue

            ledger_name = entry.get("ledger_name", "")
            taxes = self.env["account.tax"].search([("tally_ledger_name", "=", ledger_name)])
            if taxes:
                if len(taxes) > 1:
                    raise TallyMappingError(
                        f"Ambiguous match: {len(taxes)} Odoo taxes mapped to Tally ledger '{ledger_name}'."
                    )
                tax_ids.append(taxes.id)
                continue

            accounts = self.env["account.account"].search([("tally_ledger_name", "=", ledger_name)])
            if not accounts:
                raise TallyMappingError(
                    f"Voucher ledger entry '{ledger_name}' is not the party ledger and has no "
                    f"Odoo tax or account mapped to it - set 'Tally Ledger Name' on a tax "
                    f"(Accounting > Taxes) or an account (Accounting > Chart of Accounts) first."
                )
            if len(accounts) > 1:
                raise TallyMappingError(
                    f"Ambiguous match: {len(accounts)} Odoo accounts mapped to Tally ledger '{ledger_name}'."
                )
            revenue_entries.append((ledger_name, accounts, entry.get("amount") or 0.0))

        if not revenue_entries:
            raise TallyMappingError(
                "Plain accounting voucher has no revenue/expense ledger entry to build an "
                "invoice line from (only the party and/or tax ledgers were found)."
            )

        return [
            (
                0,
                0,
                {
                    "name": ledger_name,
                    "quantity": 1,
                    "price_unit": abs(amount),
                    "account_id": account.id,
                    "tax_ids": [(6, 0, tax_ids)],
                },
            )
            for ledger_name, account, amount in revenue_entries
        ]

    def _resolve_partner(self, ledger_name, party_role):
        ledger_name = (ledger_name or "").strip()
        if not ledger_name:
            raise TallyMappingError("Voucher has no party ledger name.")

        partners = self.env["res.partner"].search([("tally_synced_name", "=", ledger_name)])
        if not partners:
            raise TallyMappingError(
                f"No Odoo contact linked to Tally ledger '{ledger_name}' - "
                f"sync or import this {party_role.lower()} first."
            )
        if len(partners) > 1:
            raise TallyMappingError(
                f"Ambiguous match: {len(partners)} Odoo contacts linked to Tally ledger '{ledger_name}'."
            )
        return partners

    def _resolve_product(self, stock_item_name):
        stock_item_name = (stock_item_name or "").strip()
        if not stock_item_name:
            raise TallyMappingError("Voucher inventory entry has no stock item name.")

        products = self.env["product.product"].search([("tally_synced_name", "=", stock_item_name)])
        if not products:
            raise TallyMappingError(
                f"No Odoo product linked to Tally stock item '{stock_item_name}' - "
                f"sync or import this product first."
            )
        if len(products) > 1:
            raise TallyMappingError(
                f"Ambiguous match: {len(products)} Odoo products linked to Tally stock item '{stock_item_name}'."
            )
        return products

    def _resolve_account(self, inventory_entry):
        stock_item_name = inventory_entry.get("stock_item_name") or "(unknown item)"
        allocations = inventory_entry.get("accounting_allocations") or []

        if not allocations:
            raise TallyMappingError(
                f"Stock item '{stock_item_name}' has no accounting allocation in the voucher."
            )
        if len(allocations) > 1:
            raise TallyMappingError(
                f"Stock item '{stock_item_name}' has {len(allocations)} accounting allocations - "
                f"multiple allocations per item are not supported."
            )

        ledger_name = allocations[0].get("ledger_name", "")
        accounts = self.env["account.account"].search([("tally_ledger_name", "=", ledger_name)])
        if not accounts:
            raise TallyMappingError(
                f"No Odoo account mapped to Tally ledger '{ledger_name}' - "
                f"set 'Tally Ledger Name' on the account first (Accounting > Chart of Accounts)."
            )
        if len(accounts) > 1:
            raise TallyMappingError(
                f"Ambiguous match: {len(accounts)} Odoo accounts mapped to Tally ledger '{ledger_name}'."
            )
        return accounts

    def _resolve_taxes(self, ledger_entries):
        tax_ids = []
        for entry in ledger_entries:
            if entry.get("is_party_ledger"):
                continue

            ledger_name = entry.get("ledger_name", "")
            taxes = self.env["account.tax"].search([("tally_ledger_name", "=", ledger_name)])
            if not taxes:
                raise TallyMappingError(
                    f"Voucher ledger entry '{ledger_name}' is not the party ledger and has no Odoo "
                    f"tax mapped to it - set 'Tally Ledger Name' on a tax first (Accounting > Taxes), "
                    f"or map it as an account if it is not a tax."
                )
            if len(taxes) > 1:
                raise TallyMappingError(
                    f"Ambiguous match: {len(taxes)} Odoo taxes mapped to Tally ledger '{ledger_name}'."
                )
            tax_ids.append(taxes.id)
        return tax_ids

    def _parse_date(self, yyyymmdd):
        yyyymmdd = (yyyymmdd or "").strip()
        if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
            raise TallyMappingError(f"Voucher date '{yyyymmdd}' is not in the expected YYYYMMDD format.")
        return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
