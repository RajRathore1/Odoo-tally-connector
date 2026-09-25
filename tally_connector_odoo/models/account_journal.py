"""
Journal <-> Tally Cash/Bank ledger mapping.

Used by Payment/Receipt sync to know which Tally ledger represents this
journal's Cash/Bank account - see services/tally_payment_sync_service.py
and services/tally_payment_import_service.py.
"""

from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    tally_ledger_name = fields.Char(
        string="Tally Ledger Name",
        help="Exact name of the corresponding Cash/Bank Ledger in Tally for this journal "
        "(e.g. 'Cash', 'HDFC Bank'). Required for Receipt/Payment sync.",
    )
