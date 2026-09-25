"""
Audit trail for manual stock adjustments (Option 4 of the stock-mismatch
pair - see services/tally_stock_adjustment_service.py). A persistent
model, not transient: every correction a human makes to reconcile a
detected mismatch is recorded here permanently - who, when, which side was
adjusted, and the before/after quantities - so a later "why does this
number look different" question has an answer.
"""

from odoo import fields, models


class TallyStockAdjustmentLog(models.Model):
    _name = "tally.stock.adjustment.log"
    _description = "Tally Stock Adjustment Log"
    _order = "create_date desc"
    _rec_name = "product_id"

    connection_id = fields.Many2one("tally.connection", string="Tally Connection", required=True)
    product_id = fields.Many2one("product.product", string="Product", required=True)
    direction = fields.Selection(
        [("odoo_to_tally", "Set Tally = Odoo Qty"), ("tally_to_odoo", "Set Odoo = Tally Qty")],
        string="Direction",
        required=True,
    )
    odoo_qty_before = fields.Float(string="Odoo Qty (Before)")
    tally_qty_before = fields.Float(string="Tally Qty (Before)")
    target_qty = fields.Float(string="Target Qty Applied")
    user_id = fields.Many2one("res.users", string="Adjusted By", default=lambda self: self.env.user)
    success = fields.Boolean(string="Succeeded")
    error_message = fields.Text(string="Error")
