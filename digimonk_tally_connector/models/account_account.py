"""
Account -> Tally Ledger mapping.

Minimal, explicit mapping field (no auto-guessing, no hard-coded ledger
names - see spec section 15). An invoice line's account must have this set
before it can sync to Tally; if missing, the sync fails clearly rather than
silently picking a ledger name.
"""

from odoo import fields, models


class AccountAccount(models.Model):
    _inherit = "account.account"

    tally_ledger_name = fields.Char(
        string="Tally Ledger Name",
        help="Exact name of the corresponding Ledger in Tally (must already exist there). "
        "Required for this account to be usable on a Tally-synced invoice line.",
    )
