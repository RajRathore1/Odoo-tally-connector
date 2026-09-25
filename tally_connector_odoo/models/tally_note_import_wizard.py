"""
Wizard to import Credit Note or Debit Note vouchers from Tally into Odoo
for a date range. Thin UI layer only - delegates to
TallyCreditDebitNoteImportService via the connection's
action_import_notes_from_tally(). A single wizard serves both note types
(selected via note_type) since the only difference is which VCHTYPE is
fetched and which move_type is created - see
tally_credit_debit_note_import_service.py.
"""

from odoo import fields, models
from odoo.exceptions import ValidationError


class TallyNoteImportWizard(models.TransientModel):
    _name = "tally.note.import.wizard"
    _description = "Import Credit/Debit Notes from Tally"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
    )

    note_type = fields.Selection(
        [("credit_note", "Credit Note (Customer)"), ("debit_note", "Debit Note (Vendor)")],
        string="Note Type",
        required=True,
        default="credit_note",
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
        return self.connection_id.action_import_notes_from_tally(
            date_from=self.date_from, date_to=self.date_to, note_type=self.note_type
        )
