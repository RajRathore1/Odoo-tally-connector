"""
Wizard to import Receipt or Payment vouchers from Tally into Odoo for a
date range. Thin UI layer only - delegates to TallyPaymentImportService via
the connection's action_import_payments_from_tally(). A single wizard
serves both voucher kinds (selected via voucher_kind), mirroring
tally_note_import_wizard.py.
"""

from odoo import fields, models
from odoo.exceptions import ValidationError


class TallyPaymentImportWizard(models.TransientModel):
    _name = "tally.payment.import.wizard"
    _description = "Import Receipt/Payment Vouchers from Tally"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
    )

    voucher_kind = fields.Selection(
        [("receipt", "Receipt (Customer)"), ("payment", "Payment (Vendor)")],
        string="Voucher Kind",
        required=True,
        default="receipt",
    )

    date_from = fields.Date(
        string="From Date",
        required=True,
        default=fields.Date.context_today,
    )

    date_to = fields.Date(
        string="To Date",
        required=True,
        default=fields.Date.context_today,
    )

    def action_import(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise ValidationError("'From Date' must not be after 'To Date'.")
        return self.connection_id.action_import_payments_from_tally(
            date_from=self.date_from, date_to=self.date_to, voucher_kind=self.voucher_kind
        )
