"""
Credit Note / Debit Note synchronization service.

Orchestrates Odoo account.move (posted customer credit note or vendor debit
note) -> Tally Credit Note / Debit Note voucher upsert. A single service
handles both directions because their XML shape and validation logic are
identical to each other - only the Dr/Cr sign convention and VCHTYPE differ,
and both of those are looked up once (in _config_for) from move_type. See
tally_invoice_sync_service.py and tally_bill_sync_service.py for the two
sign conventions this mirrors:

- Customer Credit Note (move_type=out_refund) reverses a sale: it uses the
  Purchase Voucher's sign convention (party is the credit leg, the
  sales/revenue account being reversed is the debit leg) - a credit note
  reduces the customer's debt and reduces revenue, which is exactly a
  Purchase Voucher's shape.
- Vendor Debit Note (move_type=in_refund) reverses a purchase: it uses the
  Sales Voucher's sign convention (party is the debit leg, the
  purchase/expense account being reversed is the credit leg) - a debit note
  reduces the vendor's payable and reduces expense, which is exactly a
  Sales Voucher's shape.
"""

import logging
import uuid

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError, TallyValidationError

_logger = logging.getLogger(__name__)

# move_type -> (Tally VCHTYPE, is the party the debit leg?, party role label)
_NOTE_CONFIG = {
    "out_refund": {"vch_type": "Credit Note", "party_is_debit": False, "party_role": "Customer"},
    "in_refund": {"vch_type": "Debit Note", "party_is_debit": True, "party_role": "Vendor"},
}


class TallyCreditDebitNoteSyncService:
    """Synchronizes posted Odoo credit/debit notes to Tally Credit/Debit Note vouchers."""

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

    def _compute_sync_key(self, move):
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, self.env.cr.dbname)
        return str(uuid.uuid5(namespace, f"odoo:account.move:{move.id}"))

    def _validate_and_build_entries(self, move, config):
        """
        Validate every dependency and build the ledger/inventory entry lists,
        with signs following config["party_is_debit"] (see module docstring
        for which move_type maps to which sign convention).
        """
        party_is_debit = config["party_is_debit"]
        party_role = config["party_role"]

        partner = move.partner_id.commercial_partner_id or move.partner_id
        if partner.tally_sync_status != "success" or not partner.tally_guid:
            raise TallyMappingError(
                f"{party_role} '{partner.display_name}' has not been synced to Tally yet. "
                f"Sync it first (Contacts > Tally tab > Sync to Tally)."
            )

        product_lines = move.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        if not product_lines:
            raise TallyValidationError(f"'{move.name}' has no product lines to sync.")

        inventory_entries = []
        tax_totals = {}  # tally_ledger_name -> amount

        for line in product_lines:
            product = line.product_id
            if not product or product.tally_sync_status != "success" or not product.tally_guid:
                raise TallyMappingError(
                    f"Product '{product.display_name if product else line.name}' on '{move.name}' "
                    f"has not been synced to Tally yet. Sync it first "
                    f"(Inventory > Products > Product Variants > Tally tab)."
                )

            account = line.account_id
            if not account or not account.tally_ledger_name:
                raise TallyMappingError(
                    f"Account '{account.display_name if account else '(none)'}' on '{move.name}' "
                    f"line '{line.name}' has no Tally Ledger Name mapping. Set it on the account "
                    f"(Accounting > Chart of Accounts)."
                )

            for tax in line.tax_ids:
                if not tax.tally_ledger_name:
                    raise TallyMappingError(
                        f"Tax '{tax.name}' on '{move.name}' line '{line.name}' has no Tally Ledger "
                        f"Name mapping. Set it on the tax (Accounting > Taxes)."
                    )

            line_amount = line.price_subtotal
            account_amount = -line_amount if not party_is_debit else line_amount
            account_is_debit = not party_is_debit

            inventory_entries.append(
                {
                    "stock_item_name": product.tally_synced_name or product.display_name,
                    "quantity": line.quantity,
                    "rate": line.price_unit,
                    "amount": line_amount,
                    "unit": product.uom_id.name,
                    "is_deemed_positive": account_is_debit,
                    "accounting_allocation": {
                        "ledger_name": account.tally_ledger_name,
                        "amount": account_amount,
                        "is_deemed_positive": account_is_debit,
                    },
                }
            )

            for tax_line_amount, tax_ledger_name in self._distribute_line_taxes(line):
                signed = -tax_line_amount if not party_is_debit else tax_line_amount
                tax_totals[tax_ledger_name] = tax_totals.get(tax_ledger_name, 0.0) + signed

        party_amount = -move.amount_total if party_is_debit else move.amount_total
        ledger_entries = [
            {
                "ledger_name": partner.tally_synced_name or partner.display_name,
                "amount": party_amount,
                "is_deemed_positive": party_is_debit,
                "is_party_ledger": True,
                "bill_allocation": {"name": move.name, "amount": party_amount},
            }
        ]
        for ledger_name, amount in tax_totals.items():
            ledger_entries.append(
                {"ledger_name": ledger_name, "amount": amount, "is_deemed_positive": not party_is_debit}
            )

        return ledger_entries, inventory_entries, partner

    def _distribute_line_taxes(self, line):
        results = []
        if not line.tax_ids:
            return results

        taxes_res = line.tax_ids.compute_all(
            line.price_unit,
            currency=line.currency_id,
            quantity=line.quantity,
            product=line.product_id,
            partner=line.partner_id,
        )
        tax_by_id = {tax.id: tax for tax in line.tax_ids}
        for tax_detail in taxes_res.get("taxes", []):
            tax = tax_by_id.get(tax_detail["id"])
            if tax and tax.tally_ledger_name:
                results.append((tax_detail["amount"], tax.tally_ledger_name))
        return results

    def sync_note(self, move):
        """
        Sync a single posted credit/debit note to Tally.

        Args:
            move (account.move): record to sync (move_type must be
                out_refund or in_refund)

        Returns:
            dict: raw result from TallyClient.upsert_credit_debit_note()

        Raises:
            TallyConfigurationError: No usable connection for the move's company
            TallyValidationError: Not a posted credit/debit note, or has no lines
            TallyMappingError: Party/product not synced, or account/tax mapping missing
        """
        config = _NOTE_CONFIG.get(move.move_type)
        if not config:
            raise TallyValidationError(
                f"'{move.name}' is a {move.move_type} - only customer credit notes "
                f"(out_refund) and vendor debit notes (in_refund) can sync via this service."
            )
        if move.state != "posted":
            raise TallyValidationError(
                f"'{move.name}' is not posted (state={move.state}). Only posted notes sync to Tally."
            )

        company = move.company_id or self.env.company
        connection = self._get_connection(company)

        ledger_entries, inventory_entries, partner = self._validate_and_build_entries(move, config)

        if not move.tally_sync_key:
            move.tally_sync_key = self._compute_sync_key(move)

        already_synced = move.tally_sync_status == "success" and bool(move.tally_guid)
        action = "Alter" if already_synced else "Create"

        voucher_date = fields.Date.to_string(move.invoice_date or move.date).replace("-", "")

        client = connection._get_tally_client()
        result = client.upsert_credit_debit_note(
            vch_type=config["vch_type"],
            company=connection.tally_company_name or company.name,
            voucher_number=move.name,
            voucher_date=voucher_date,
            party_ledger=partner.tally_synced_name or partner.display_name,
            guid=move.tally_sync_key,
            ledger_entries=ledger_entries,
            inventory_entries=inventory_entries,
            action=action,
            narration=move.narration and move.narration[:500] or None,
            reference=move.ref,
            res_model="account.move",
            res_id=move.id,
        )

        vals = {
            "tally_last_sync_at": fields.Datetime.now(),
            "tally_sync_attempts": move.tally_sync_attempts + 1,
        }

        if result.get("queued"):
            vals["tally_sync_status"] = "queued"
            move.write(vals)
            _logger.info(
                f"{config['vch_type']} queued for async Tally Agent processing: {move.name}",
                extra={"move_id": move.id, "connection_id": connection.id, "job_id": result.get("job_id")},
            )
            return result

        if result["success"]:
            vals.update(
                {
                    "tally_sync_status": "success",
                    "tally_guid": move.tally_sync_key,
                    "tally_last_sync_error": False,
                }
            )
            _logger.info(
                f"{config['vch_type']} synced to Tally: {move.name} ({action})",
                extra={"move_id": move.id, "connection_id": connection.id, "action": action},
            )
        else:
            vals.update(
                {
                    "tally_sync_status": "failed",
                    "tally_last_sync_error": result.get("error") or result.get("message"),
                }
            )
            _logger.warning(
                f"{config['vch_type']} sync to Tally failed: {move.name}",
                extra={"move_id": move.id, "connection_id": connection.id, "error": result.get("error")},
            )

        move.write(vals)
        return result
