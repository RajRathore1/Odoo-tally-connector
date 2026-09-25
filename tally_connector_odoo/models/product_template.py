"""
Product Template sync surface.

Tally Stock Items map to product.product (variants) - see product_product.py.
Odoo's default "Products" UI (Inventory/Sales > Products) shows product.template,
so this thin layer exposes read-only status + a sync action for the common
single-variant case, delegating to the variant's real sync logic.
"""

from odoo import fields, models
from odoo.exceptions import UserError


class ProductTemplate(models.Model):
    _inherit = "product.template"

    tally_sync_status = fields.Selection(
        related="product_variant_id.tally_sync_status",
        string="Tally Sync Status",
        readonly=True,
    )
    tally_guid = fields.Char(
        related="product_variant_id.tally_guid",
        string="Tally GUID",
        readonly=True,
    )
    tally_last_sync_at = fields.Datetime(
        related="product_variant_id.tally_last_sync_at",
        string="Last Sync Time",
        readonly=True,
    )
    tally_last_sync_error = fields.Text(
        related="product_variant_id.tally_last_sync_error",
        string="Last Sync Error",
        readonly=True,
    )
    tally_sync_attempts = fields.Integer(
        related="product_variant_id.tally_sync_attempts",
        string="Sync Attempts",
        readonly=True,
    )

    def action_sync_to_tally(self):
        """Sync this template's product to Tally. Requires exactly one variant -
        with multiple variants, sync each variant individually (Inventory >
        Products > Product Variants) since each variant is a distinct Tally
        Stock Item.
        """
        self.ensure_one()

        if len(self.product_variant_ids) != 1:
            raise UserError(
                f"'{self.display_name}' has {len(self.product_variant_ids)} variants. "
                f"Sync each variant individually from Inventory > Products > Product Variants, "
                f"since each variant maps to its own Tally Stock Item."
            )

        return self.product_variant_id.action_sync_to_tally()
