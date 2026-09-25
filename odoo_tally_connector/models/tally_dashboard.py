"""
Tally Sync Dashboard - a single at-a-glance screen for what previously
required checking several different places: each connection's status
(Tally > Configuration > Connections), per-model sync queues (product/
partner/invoice/bill/note/payment list views filtered by sync status),
and the stock mismatch alert (My Activities).

Read-only + one "fix it" action ("Retry Failed Now"): this dashboard
never invents new sync logic - it only surfaces what
tally.connection._sync_pending_queue()/_check_stock_mismatch() already
track, and on request re-runs the exact same retry path the 15-minute
cron already uses (see models/tally_connection.py).
"""

import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# (category key, display label, model, extra domain to scope that model to this category)
_CATEGORIES = [
    ("product", "Products", "product.product", []),
    ("partner", "Contacts", "res.partner", []),
    ("invoice", "Sales Invoices", "account.move", [("move_type", "=", "out_invoice")]),
    ("bill", "Purchase Bills", "account.move", [("move_type", "=", "in_invoice")]),
    ("credit_note", "Credit Notes", "account.move", [("move_type", "=", "out_refund")]),
    ("debit_note", "Debit Notes", "account.move", [("move_type", "=", "in_refund")]),
    ("receipt", "Receipts", "account.payment", [("payment_type", "=", "inbound")]),
    ("payment", "Payments", "account.payment", [("payment_type", "=", "outbound")]),
]


class TallyDashboardWizard(models.TransientModel):
    _name = "tally.dashboard.wizard"
    _description = "Tally Sync Dashboard"
    _rec_name = "name"

    name = fields.Char(default="Tally Sync Dashboard")

    connection_line_ids = fields.One2many("tally.dashboard.connection.line", "wizard_id", string="Connections")
    category_line_ids = fields.One2many("tally.dashboard.category.line", "wizard_id", string="Sync Status by Type")
    error_line_ids = fields.One2many("tally.dashboard.error.line", "wizard_id", string="Recent Errors")

    total_pending = fields.Integer(compute="_compute_totals")
    total_failed = fields.Integer(compute="_compute_totals")
    open_mismatch_alerts = fields.Integer(compute="_compute_totals")

    def _compute_totals(self):
        mismatch_count = self.env["mail.activity"].search_count(
            [("res_model", "=", "tally.connection"), ("summary", "ilike", "Tally stock mismatch")]
        )
        for wizard in self:
            wizard.total_pending = sum(wizard.category_line_ids.mapped("pending_count"))
            wizard.total_failed = sum(wizard.category_line_ids.mapped("failed_count"))
            wizard.open_mismatch_alerts = mismatch_count

    @api.model
    def action_open_dashboard(self):
        """
        Entry point from the Dashboard menu (a server action, not a plain
        act_window): the Odoo web client only calls create() on a wizard
        once the user saves or clicks a button, so opening a bare
        act_window with no res_id would show an empty, unsaved-looking
        form until the user clicked Refresh. Creating and populating it
        here first means the menu opens straight into a filled-in,
        already-saved dashboard.
        """
        wizard = self.create({})
        wizard._populate()
        return wizard._reload_action()

    def _reload_action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "tally.dashboard.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_refresh(self):
        """Re-scan every connection and every synced model, replacing the current lines."""
        self.ensure_one()
        self._populate()
        return self._reload_action()

    def _populate(self):
        """Re-scan every connection and every synced model, replacing the current lines."""
        self.ensure_one()
        self.connection_line_ids.unlink()
        self.category_line_ids.unlink()
        self.error_line_ids.unlink()

        connections = self.env["tally.connection"].search([])

        self.env["tally.dashboard.connection.line"].create(
            [{"wizard_id": self.id, "connection_id": conn.id} for conn in connections]
        )

        category_vals = []
        for conn in connections:
            for key, label, model_name, extra_domain in _CATEGORIES:
                # Include records with no company set (shared across
                # companies) alongside this connection's own company -
                # most tally-synced records do carry a company_id, but
                # nothing enforces it.
                base_domain = [("company_id", "in", [conn.company_id.id, False])] + extra_domain
                Model = self.env[model_name]
                pending = Model.search_count(base_domain + [("tally_sync_status", "=", "draft")])
                failed = Model.search_count(base_domain + [("tally_sync_status", "=", "failed")])
                if pending or failed:
                    category_vals.append(
                        {
                            "wizard_id": self.id,
                            "connection_id": conn.id,
                            "category": key,
                            "category_label": label,
                            "pending_count": pending,
                            "failed_count": failed,
                        }
                    )
        self.env["tally.dashboard.category.line"].create(category_vals)

        error_vals = []
        for key, label, model_name, extra_domain in _CATEGORIES:
            failed_records = self.env[model_name].search(
                [("tally_sync_status", "=", "failed"), ("tally_last_sync_error", "!=", False)] + extra_domain,
                order="tally_last_sync_at desc",
                limit=10,
            )
            for rec in failed_records:
                error_vals.append(
                    {
                        "wizard_id": self.id,
                        "category_label": label,
                        "record_name": rec.display_name,
                        "error_message": rec.tally_last_sync_error,
                        "occurred_at": rec.tally_last_sync_at,
                        "res_model": model_name,
                        "res_id": rec.id,
                    }
                )
        _epoch = fields.Datetime.from_string("1970-01-01 00:00:00")
        error_vals.sort(key=lambda v: v["occurred_at"] or _epoch, reverse=True)
        self.env["tally.dashboard.error.line"].create(error_vals[:20])

    def action_retry_all_failed(self):
        """Re-run the same pending/failed retry path the 15-minute cron uses, right now."""
        self.ensure_one()
        for connection in self.env["tally.connection"].search(
            [("active", "=", True), ("enabled_for_sync", "=", True)]
        ):
            try:
                connection._sync_pending_queue(auto_commit=False)
            except Exception:
                _logger.exception(
                    f"Dashboard 'Retry Failed Now' raised for connection {connection.id}",
                    extra={"connection_id": connection.id},
                )
        return self.action_refresh()


class TallyDashboardConnectionLine(models.TransientModel):
    _name = "tally.dashboard.connection.line"
    _description = "Tally Dashboard - Connection Status"

    wizard_id = fields.Many2one("tally.dashboard.wizard", required=True, ondelete="cascade")
    connection_id = fields.Many2one("tally.connection", required=True)
    company_id = fields.Many2one(related="connection_id.company_id")
    connection_status = fields.Selection(related="connection_id.connection_status")
    enabled_for_sync = fields.Boolean(related="connection_id.enabled_for_sync")
    last_tested_at = fields.Datetime(related="connection_id.last_tested_at")


class TallyDashboardCategoryLine(models.TransientModel):
    _name = "tally.dashboard.category.line"
    _description = "Tally Dashboard - Sync Status by Record Type"

    wizard_id = fields.Many2one("tally.dashboard.wizard", required=True, ondelete="cascade")
    connection_id = fields.Many2one("tally.connection", required=True)
    category = fields.Char()
    category_label = fields.Char()
    pending_count = fields.Integer()
    failed_count = fields.Integer()

    def _category_model_and_domain(self):
        self.ensure_one()
        for key, _label, model_name, extra_domain in _CATEGORIES:
            if key == self.category:
                return model_name, extra_domain
        return None, []

    def _open_records(self, status):
        self.ensure_one()
        model_name, extra_domain = self._category_model_and_domain()
        domain = [
            ("company_id", "in", [self.connection_id.company_id.id, False]),
            ("tally_sync_status", "=", status),
        ] + extra_domain
        return {
            "type": "ir.actions.act_window",
            "name": f"{self.category_label} - {'Pending' if status == 'draft' else 'Failed'}",
            "res_model": model_name,
            "view_mode": "list,form",
            "domain": domain,
        }

    def action_view_pending(self):
        return self._open_records("draft")

    def action_view_failed(self):
        return self._open_records("failed")


class TallyDashboardErrorLine(models.TransientModel):
    _name = "tally.dashboard.error.line"
    _description = "Tally Dashboard - Recent Sync Errors"

    wizard_id = fields.Many2one("tally.dashboard.wizard", required=True, ondelete="cascade")
    category_label = fields.Char()
    record_name = fields.Char()
    error_message = fields.Text()
    occurred_at = fields.Datetime()
    res_model = fields.Char()
    res_id = fields.Integer()

    def action_open_record(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self.res_model,
            "res_id": self.res_id,
            "view_mode": "form",
        }
