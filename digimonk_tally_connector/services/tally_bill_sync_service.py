"""
Vendor Bill synchronization service.

Orchestrates Odoo account.move (posted vendor bill) -> Tally Purchase
Voucher upsert. Mirrors tally_invoice_sync_service.py's design exactly,
with the double-entry roles mirrored: the vendor (creditor) is the
increasing/credit leg here instead of the decreasing/debit leg a customer
is on a Sales voucher, and each expense/purchase account allocation is the
debit leg instead of a credit leg. See
TallyXmlBuilder.build_purchase_voucher_upsert_request for the XML shape
this produces (identical structure to Sales, just the Dr/Cr sides and
VCHTYPE differ).
"""

import logging
import uuid

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError, TallyValidationError

_logger = logging.getLogger(__name__)


class TallyBillSyncService:
    """Synchronizes posted Odoo vendor bills to Tally Purchase Vouchers."""

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

    def _validate_and_build_entries(self, move):
        """
        Validate every dependency and build the ledger/inventory entry lists.

        Mirrors TallyInvoiceSyncService._validate_and_build_entries - see
        its docstring for the real-Tally-XML evidence behind this shape.
        Sign convention is mirrored: the vendor (party) entry is the credit
        leg (amount = +total), each line's expense/purchase account
        allocation is the debit leg (amount = -line amount), and input tax
        entries are the debit leg too (amount = -tax amount).

        Raises:
            TallyMappingError: vendor not synced, product not synced, or
                account/tax missing a Tally ledger name mapping
        """
        partner = move.partner_id.commercial_partner_id or move.partner_id
        if partner.tally_sync_status != "success" or not partner.tally_guid:
            raise TallyMappingError(
                f"Vendor '{partner.display_name}' has not been synced to Tally yet. "
                f"Sync the vendor first (Contacts > Tally tab > Sync to Tally)."
            )

        product_lines = move.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        if not product_lines:
            raise TallyValidationError(
                f"Bill '{move.name}' has no product lines to sync."
            )

        inventory_entries = []
        tax_totals = {}  # tally_ledger_name -> amount

        for line in product_lines:
            product = line.product_id
            if not product or product.tally_sync_status != "success" or not product.tally_guid:
                raise TallyMappingError(
                    f"Product '{product.display_name if product else line.name}' on bill "
                    f"'{move.name}' has not been synced to Tally yet. Sync it first "
                    f"(Inventory > Products > Product Variants > Tally tab)."
                )

            account = line.account_id
            if not account or not account.tally_ledger_name:
                raise TallyMappingError(
                    f"Account '{account.display_name if account else '(none)'}' on bill "
                    f"'{move.name}' line '{line.name}' has no Tally Ledger Name mapping. "
                    f"Set it on the account (Accounting > Chart of Accounts)."
                )

            for tax in line.tax_ids:
                if not tax.tally_ledger_name:
                    raise TallyMappingError(
                        f"Tax '{tax.name}' on bill '{move.name}' line '{line.name}' has no "
                        f"Tally Ledger Name mapping. Set it on the tax (Accounting > Taxes)."
                    )

            unit_name = product.uom_id.name
            quantity = line.quantity
            rate = line.price_unit
            amount = line.price_subtotal

            inventory_entries.append(
                {
                    "stock_item_name": product.tally_synced_name or product.display_name,
                    "quantity": quantity,
                    "rate": rate,
                    "amount": amount,
                    "unit": unit_name,
                    # This line's own expense/purchase allocation - nested
                    # under the inventory entry, mirrored sign (debit leg).
                    "accounting_allocation": {
                        "ledger_name": account.tally_ledger_name,
                        "amount": -line.price_subtotal,
                    },
                }
            )

            for tax_line_amount, tax_ledger_name in self._distribute_line_taxes(line):
                tax_totals[tax_ledger_name] = tax_totals.get(tax_ledger_name, 0.0) - tax_line_amount

        ledger_entries = [
            {
                "ledger_name": partner.tally_synced_name or partner.display_name,
                "amount": move.amount_total,
                "is_deemed_positive": False,
                "is_party_ledger": True,
                # Tally's Sundry Creditors ledgers default to "Maintain
                # balances bill-by-bill" - without a bill allocation on the
                # party entry, Tally can reject the voucher.
                "bill_allocation": {"name": move.name, "amount": move.amount_total},
            }
        ]
        for ledger_name, amount in tax_totals.items():
            ledger_entries.append({"ledger_name": ledger_name, "amount": amount, "is_deemed_positive": True})

        return ledger_entries, inventory_entries, partner

    def _distribute_line_taxes(self, line):
        """
        Compute each tax's share of a line's tax amount, keyed by Tally ledger name.

        Uses the line's own tax computation (compute_all) rather than
        re-deriving tax math.
        """
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

    def sync_bill(self, move):
        """
        Sync a single posted vendor bill to Tally as a Purchase Voucher.

        Args:
            move (account.move): record to sync (single record expected)

        Returns:
            dict: raw result from TallyClient.upsert_purchase_voucher()

        Raises:
            TallyConfigurationError: No usable connection for the bill's company
            TallyValidationError: Bill is not a posted vendor bill, or has no lines
            TallyMappingError: Vendor/product not synced, or account/tax mapping missing
        """
        if move.move_type != "in_invoice":
            raise TallyValidationError(
                f"'{move.name}' is not a vendor bill - only in_invoice sync is supported."
            )
        if move.state != "posted":
            raise TallyValidationError(
                f"'{move.name}' is not posted (state={move.state}). Only posted bills sync to Tally."
            )

        company = move.company_id or self.env.company
        connection = self._get_connection(company)

        ledger_entries, inventory_entries, partner = self._validate_and_build_entries(move)

        if not move.tally_sync_key:
            move.tally_sync_key = self._compute_sync_key(move)

        already_synced = move.tally_sync_status == "success" and bool(move.tally_guid)
        action = "Alter" if already_synced else "Create"

        voucher_date = fields.Date.to_string(move.invoice_date or move.date).replace("-", "")

        client = connection._get_tally_client()
        result = client.upsert_purchase_voucher(
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
                f"Bill queued for async Tally Agent processing: {move.name}",
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
                f"Bill synced to Tally: {move.name} ({action})",
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
                f"Bill sync to Tally failed: {move.name}",
                extra={"move_id": move.id, "connection_id": connection.id, "error": result.get("error")},
            )

        move.write(vals)
        return result
