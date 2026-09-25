"""
Wizard to import customer/vendor Ledgers from Tally into Odoo, optionally
filtered to a single named ledger. Mirrors tally_product_import_wizard.py.
"""

from odoo import fields, models


class TallyPartnerImportWizard(models.TransientModel):
    _name = "tally.partner.import.wizard"
    _description = "Import Contacts from Tally"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
    )

    partner_name = fields.Char(
        string="Contact Name",
        help="Import only the Tally ledger with this exact name. Leave empty to import all "
        "customer (Sundry Debtors) and vendor (Sundry Creditors) ledgers.",
    )

    def action_import(self):
        self.ensure_one()
        return self.connection_id.action_import_partners_from_tally(
            name_filter=self.partner_name or None
        )
