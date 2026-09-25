"""
Wizard to import Stock Items from Tally into Odoo, optionally filtered to a
single named item. Thin UI layer only - delegates to TallyProductImportService
via the connection's action_import_products_from_tally().
"""

from odoo import fields, models


class TallyProductImportWizard(models.TransientModel):
    _name = "tally.product.import.wizard"
    _description = "Import Products from Tally"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
    )

    product_name = fields.Char(
        string="Product Name",
        help="Import only the Tally stock item with this exact name. Leave empty to import all.",
    )

    def action_import(self):
        self.ensure_one()
        return self.connection_id.action_import_products_from_tally(
            name_filter=self.product_name or None
        )
