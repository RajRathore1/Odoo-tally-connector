"""
One-time Opening Balance push wizard (Phase 10).

Thin UI layer only - delegates the actual balance computation and push to
TallyOpeningBalanceService. Mirrors tally_stock_reconciliation_wizard.py's
shape: "Refresh" computes/previews, then each line is pushed individually
so a mapping error on one account doesn't block the rest.

Opened via the connection's action_open_opening_balance_wizard() - meant to
be run once, right after a connection first goes live (or after a
chart-of-accounts correction), not on any schedule.
"""

from odoo import fields, models


class TallyOpeningBalanceWizard(models.TransientModel):
    _name = "tally.opening.balance.wizard"
    _description = "Tally Opening Balance Push"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
    )

    as_of_date = fields.Date(
        string="As Of Date",
        required=True,
        default=fields.Date.context_today,
        help="Cutoff date (inclusive) - each account's balance is computed from all its posted "
        "entries on or before this date, then pushed as that Tally ledger's opening balance.",
    )

    line_ids = fields.One2many(
        "tally.opening.balance.line",
        "wizard_id",
        string="Lines",
    )

    def action_refresh(self):
        """Recompute balances as of as_of_date and repopulate the lines."""
        self.ensure_one()

        from ..services import TallyOpeningBalanceService, TallyConnectorError

        self.line_ids.unlink()

        try:
            service = TallyOpeningBalanceService(self.env)
            rows = service.compute_balances(self.connection_id, self.as_of_date)
        except TallyConnectorError as e:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "Refresh Failed", "message": e.message, "type": "danger", "sticky": True},
            }

        self.env["tally.opening.balance.line"].create(
            [
                {
                    "wizard_id": self.id,
                    "account_id": row["account"].id,
                    "balance": row["balance"],
                }
                for row in rows
            ]
        )

        return {
            "type": "ir.actions.act_window",
            "res_model": "tally.opening.balance.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }


class TallyOpeningBalanceLine(models.TransientModel):
    _name = "tally.opening.balance.line"
    _description = "Tally Opening Balance Push Line"

    wizard_id = fields.Many2one("tally.opening.balance.wizard", required=True, ondelete="cascade")
    account_id = fields.Many2one("account.account", string="Account", required=True)
    tally_ledger_name = fields.Char(related="account_id.tally_ledger_name")
    balance = fields.Float(string="Balance as of Date")

    push_status = fields.Selection(
        [("pending", "Pending"), ("success", "Pushed"), ("failed", "Failed")],
        default="pending",
    )
    push_error = fields.Char(readonly=True)

    def action_push(self):
        """Push this one account's opening balance to Tally."""
        self.ensure_one()

        from ..services import TallyOpeningBalanceService, TallyConnectorError

        service = TallyOpeningBalanceService(self.env)
        try:
            result = service.push_opening_balance(
                self.account_id, self.balance, company=self.wizard_id.connection_id.company_id
            )
        except TallyConnectorError as e:
            self.write({"push_status": "failed", "push_error": e.message})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "Push Failed", "message": e.message, "type": "danger", "sticky": True},
            }

        if result["success"]:
            self.write({"push_status": "success", "push_error": False})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"title": "Success", "message": result["message"], "type": "success"},
            }

        error = result.get("error") or result.get("message")
        self.write({"push_status": "failed", "push_error": error})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Push Failed",
                "message": result.get("message", "Unknown error"),
                "type": "danger",
                "sticky": True,
            },
        }
