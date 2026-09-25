"""
Wizard to import Purchase vouchers from Tally into Odoo for a date range.
Thin UI layer only - delegates to TallyBillImportService via the
connection's action_import_bills_from_tally().
"""

from odoo import fields, models
from odoo.exceptions import ValidationError


class TallyBillImportWizard(models.TransientModel):
    _name = "tally.bill.import.wizard"
    _description = "Import Purchase Vouchers from Tally"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
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
        return self.connection_id.action_import_bills_from_tally(
            date_from=self.date_from, date_to=self.date_to
        )
