"""
Stock Group mapping field (Phase 11).

Thin field-only extension, mirroring account_account.py's tally_ledger_name
pattern exactly: maps an Odoo product.category to an already-existing Tally
Stock Group by name. Actual sync orchestration (passing this as a Stock
Item's PARENT) lives in services/tally_product_sync_service.py; auto-fill
of unambiguous matches lives in services/tally_mapping_suggestion_service.py.
"""

from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = "product.category"

    tally_stock_group_name = fields.Char(
        string="Tally Stock Group Name",
        help="Exact name of the corresponding Stock Group in Tally (must already exist there). "
        "Products in this category sync with this Stock Group as their parent, instead of "
        "landing at Tally's default top level.",
    )
