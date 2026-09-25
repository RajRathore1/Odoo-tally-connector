"""
Tax -> Tally Ledger mapping.

Minimal, explicit mapping field (no auto-guessing, no hard-coded GST ledger
names - see spec section 15). A tax used on an invoice line must have this
set before the invoice can sync to Tally.
"""

from odoo import fields, models


class AccountTax(models.Model):
    _inherit = "account.tax"

    tally_ledger_name = fields.Char(
        string="Tally Ledger Name",
        help="Exact name of the corresponding Tax Ledger in Tally (e.g. 'Output CGST', "
        "must already exist there). Required for this tax to be usable on a "
        "Tally-synced invoice line.",
    )
