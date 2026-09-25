"""
Manual stock adjustment service - the "fix" half of the detection-only
reconciliation pair (see tally_stock_reconciliation_service.py). Nothing
here runs automatically: every adjustment is a deliberate, one-click action
a human takes from the reconciliation wizard after deciding which number
(Odoo's or Tally's) is actually correct - usually after a physical stock
count. This service only executes that decision and logs it; it never
decides the target quantity itself.

Two directions:
- adjust_odoo_quantity: set Odoo's on-hand quantity to match a target
  value (normally Tally's), via Odoo's own Inventory Adjustment mechanism
  (stock.quant.inventory_quantity + action_apply_inventory) at the
  company's main warehouse stock location. Odoo has no concept of Tally's
  "godowns" in this integration, so multi-location nuance is deliberately
  out of scope - this adjusts the single location Tally's own quantity is
  being compared against (see tally_stock_reconciliation_service.py).
- adjust_tally_quantity: push a Tally "Physical Stock" voucher recording
  the counted quantity, via the same native voucher-import mechanism as
  every other voucher type. This is a brand new, never-tested-against-real-
  Tally voucher shape - the XML follows the same structural conventions
  already proven for every other voucher (see
  build_sales_voucher_upsert_request's docstring for that history), but
  should be expected to need at least one round of live-Tally correction,
  same as Sales/Receipt/Payment vouchers did.
"""

import logging
import uuid

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError

_logger = logging.getLogger(__name__)


class TallyStockAdjustmentService:
    """Executes a manually-decided stock quantity correction, on one side or the other."""

    def __init__(self, env):
        self.env = env

    def adjust_odoo_quantity(self, product, new_qty, company):
        """
        Set Odoo's on-hand quantity for product to new_qty at the company's
        main warehouse stock location, via a standard Inventory Adjustment.

        Args:
            product (product.product): product to adjust
            new_qty (float): the counted/target quantity
            company (res.company): company whose main warehouse is used

        Returns:
            dict: {"success": bool, "message": str, "old_qty": float, "new_qty": float}

        Raises:
            TallyMappingError: no warehouse configured for this company
        """
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
        if not warehouse:
            raise TallyMappingError(f"No warehouse configured for company '{company.name}'.")

        if not product.is_storable:
            # A Tally Stock Item is a physically tracked inventory item by
            # definition - a product that reached here linked to Tally but
            # was never marked storable (e.g. created before this connector
            # started setting it on import) can't hold a quant otherwise.
            product.is_storable = True
            _logger.info(
                f"Marked '{product.display_name}' as storable (was not, despite being Tally-linked).",
                extra={"product_id": product.id},
            )

        old_qty = product.qty_available
        location = warehouse.lot_stock_id

        quant = self.env["stock.quant"].search(
            [("product_id", "=", product.id), ("location_id", "=", location.id)], limit=1
        )
        if not quant:
            quant = self.env["stock.quant"].create(
                {"product_id": product.id, "location_id": location.id, "company_id": company.id}
            )

        quant.inventory_quantity = new_qty
        quant.action_apply_inventory()

        _logger.info(
            f"Odoo stock manually adjusted for '{product.display_name}': {old_qty:g} -> {new_qty:g}",
            extra={"product_id": product.id, "old_qty": old_qty, "new_qty": new_qty},
        )

        return {
            "success": True,
            "message": f"Odoo on-hand quantity for '{product.display_name}' set to {new_qty:g}.",
            "old_qty": old_qty,
            "new_qty": new_qty,
        }

    def adjust_tally_quantity(self, connection, product, new_qty):
        """
        Push a Tally "Physical Stock" voucher recording product's counted
        quantity as new_qty (Tally computes the resulting adjustment
        itself, the same way it would if this were entered by hand in
        Tally's own Physical Stock voucher screen).

        Args:
            connection (tally.connection): target connection
            product (product.product): product to adjust (must already be
                synced to Tally)
            new_qty (float): the counted/target quantity

        Returns:
            dict: raw result from TallyClient.upsert_stock_adjustment()

        Raises:
            TallyConfigurationError: connection not usable
            TallyMappingError: product not synced to Tally
        """
        if not connection.active or not connection.enabled_for_sync:
            raise TallyConfigurationError(
                f"Connection '{connection.name}' is not active/enabled for sync."
            )

        if product.tally_sync_status != "success" or not product.tally_guid:
            raise TallyMappingError(
                f"Product '{product.display_name}' has not been synced to Tally yet. "
                f"Sync it first (Inventory > Products > Product Variants > Tally tab)."
            )

        company = connection.company_id
        voucher_date = fields.Date.to_string(fields.Date.context_today(self.env.user)).replace("-", "")
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, self.env.cr.dbname)
        guid = str(uuid.uuid5(namespace, f"odoo:stock_adjustment:{product.id}:{voucher_date}"))

        client = connection._get_tally_client()
        result = client.upsert_stock_adjustment(
            company=connection.tally_company_name or company.name,
            voucher_number=f"ADJ-{voucher_date}-{product.id}",
            voucher_date=voucher_date,
            guid=guid,
            stock_item_name=product.tally_synced_name or product.display_name,
            quantity=new_qty,
            unit=product.uom_id.name,
            action="Create",
            narration=f"Manual stock reconciliation adjustment via Odoo (product: {product.display_name}).",
        )

        if result.get("queued"):
            _logger.info(
                f"Tally stock adjustment queued for async Agent processing: '{product.display_name}' to {new_qty:g}",
                extra={"product_id": product.id, "connection_id": connection.id, "job_id": result.get("job_id")},
            )
        elif result["success"]:
            _logger.info(
                f"Tally stock manually adjusted for '{product.display_name}' to {new_qty:g}",
                extra={"product_id": product.id, "connection_id": connection.id, "new_qty": new_qty},
            )

        return result
