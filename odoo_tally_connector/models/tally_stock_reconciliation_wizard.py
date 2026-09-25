"""
On-demand Stock Reconciliation report (Option 1 of the stock-mismatch
detection pair - see services/tally_stock_reconciliation_service.py for
why this is detection-only, never an automatic fix).

Thin UI layer only - delegates the actual comparison to
TallyStockReconciliationService. Opened via the connection's
action_open_stock_reconciliation(); "Refresh" re-runs the comparison
on demand (e.g. right after reconnecting, to see the current gap
immediately rather than waiting for the next scheduled check).
"""

from odoo import fields, models


class TallyStockReconciliationWizard(models.TransientModel):
    _name = "tally.stock.reconciliation.wizard"
    _description = "Tally Stock Reconciliation"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
    )

    line_ids = fields.One2many(
        "tally.stock.reconciliation.line",
        "wizard_id",
        string="Lines",
    )

    mismatched_count = fields.Integer(
        string="Mismatched Products",
        compute="_compute_mismatched_count",
    )

    def _compute_mismatched_count(self):
        for wizard in self:
            wizard.mismatched_count = len(wizard.line_ids.filtered("is_mismatch"))

    def action_refresh(self):
        """Re-run the comparison against Tally and repopulate the lines."""
        self.ensure_one()

        from ..services import TallyStockReconciliationService, TallyConnectorError

        self.line_ids.unlink()

        try:
            service = TallyStockReconciliationService(self.env)
            result = service.reconcile(self.connection_id)
        except TallyConnectorError as e:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "Reconciliation Failed", "message": e.message, "type": "danger", "sticky": True},
            }

        if not result["success"]:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Reconciliation Failed",
                    "message": result.get("message", "Unknown error"),
                    "type": "danger",
                    "sticky": True,
                },
            }

        threshold = self.connection_id.stock_mismatch_threshold
        self.env["tally.stock.reconciliation.line"].create(
            [
                {
                    "wizard_id": self.id,
                    "product_id": row["product"].id,
                    "odoo_qty": row["odoo_qty"],
                    "tally_qty": row["tally_qty"],
                    "difference": row["difference"],
                    "is_mismatch": abs(row["difference"]) > threshold,
                }
                for row in result["rows"]
            ]
        )

        return {
            "type": "ir.actions.act_window",
            "res_model": "tally.stock.reconciliation.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }


class TallyStockReconciliationLine(models.TransientModel):
    _name = "tally.stock.reconciliation.line"
    _description = "Tally Stock Reconciliation Line"

    wizard_id = fields.Many2one("tally.stock.reconciliation.wizard", required=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", string="Product", required=True)
    odoo_qty = fields.Float(string="Odoo On-Hand Qty")
    tally_qty = fields.Float(string="Tally Closing Qty")
    difference = fields.Float(string="Difference")
    is_mismatch = fields.Boolean(string="Past Threshold")

    def _log_adjustment(self, direction, target_qty, success, error_message=None):
        self.env["tally.stock.adjustment.log"].create(
            {
                "connection_id": self.wizard_id.connection_id.id,
                "product_id": self.product_id.id,
                "direction": direction,
                "odoo_qty_before": self.odoo_qty,
                "tally_qty_before": self.tally_qty,
                "target_qty": target_qty,
                "success": success,
                "error_message": error_message,
            }
        )

    def action_set_odoo_to_tally_qty(self):
        """Manual correction: set Odoo's on-hand quantity to match Tally's."""
        self.ensure_one()

        from ..services import TallyStockAdjustmentService, TallyConnectorError

        service = TallyStockAdjustmentService(self.env)
        try:
            result = service.adjust_odoo_quantity(
                self.product_id, self.tally_qty, self.wizard_id.connection_id.company_id
            )
        except TallyConnectorError as e:
            self._log_adjustment("tally_to_odoo", self.tally_qty, False, e.message)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "Adjustment Failed", "message": e.message, "type": "danger", "sticky": True},
            }

        self._log_adjustment("tally_to_odoo", self.tally_qty, True)
        self.odoo_qty = self.product_id.qty_available
        self.difference = self.odoo_qty - self.tally_qty
        self.is_mismatch = abs(self.difference) > self.wizard_id.connection_id.stock_mismatch_threshold

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Success", "message": result["message"], "type": "success"},
        }

    def action_set_tally_to_odoo_qty(self):
        """Manual correction: push a Tally Physical Stock voucher so Tally's quantity matches Odoo's."""
        self.ensure_one()

        from ..services import TallyStockAdjustmentService, TallyConnectorError

        service = TallyStockAdjustmentService(self.env)
        try:
            result = service.adjust_tally_quantity(self.wizard_id.connection_id, self.product_id, self.odoo_qty)
        except TallyConnectorError as e:
            self._log_adjustment("odoo_to_tally", self.odoo_qty, False, e.message)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "Adjustment Failed", "message": e.message, "type": "danger", "sticky": True},
            }

        if not result["success"]:
            error = result.get("error") or result.get("message")
            self._log_adjustment("odoo_to_tally", self.odoo_qty, False, error)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Adjustment Failed",
                    "message": result.get("message", "Unknown error"),
                    "type": "danger",
                    "sticky": True,
                },
            }

        self._log_adjustment("odoo_to_tally", self.odoo_qty, True)
        self.tally_qty = self.odoo_qty
        self.difference = self.odoo_qty - self.tally_qty
        self.is_mismatch = False

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Success", "message": f"Tally updated: {result['message']}", "type": "success"},
        }
