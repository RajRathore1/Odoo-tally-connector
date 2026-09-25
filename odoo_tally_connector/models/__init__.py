"""Tally Connector models."""

from . import tally_connection
from . import tally_agent_device
from . import tally_agent_job
from . import tally_agent_token_reveal_wizard
from . import product_product
from . import product_template
from . import product_category
from . import tally_product_import_wizard
from . import res_partner
from . import tally_partner_import_wizard
from . import account_account
from . import account_tax
from . import account_move
from . import tally_invoice_import_wizard
from . import tally_bill_import_wizard
from . import tally_note_import_wizard
from . import account_journal
from . import account_payment
from . import tally_payment_import_wizard
from . import tally_stock_reconciliation_wizard
from . import tally_stock_adjustment_log
from . import tally_dashboard
from . import tally_opening_balance_wizard

__all__ = [
    "tally_connection",
    "tally_agent_device",
    "tally_agent_job",
    "tally_agent_token_reveal_wizard",
    "product_product",
    "product_template",
    "product_category",
    "tally_product_import_wizard",
    "res_partner",
    "tally_partner_import_wizard",
    "account_account",
    "account_tax",
    "account_move",
    "tally_invoice_import_wizard",
    "tally_bill_import_wizard",
    "tally_note_import_wizard",
    "account_journal",
    "account_payment",
    "tally_payment_import_wizard",
    "tally_stock_reconciliation_wizard",
    "tally_stock_adjustment_log",
    "tally_dashboard",
    "tally_opening_balance_wizard",
]
