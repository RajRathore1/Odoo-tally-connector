"""
Stock quantity reconciliation service (detection only - never auto-corrects).

Compares each Tally-linked Odoo product's on-hand quantity against Tally's
own stock item closing balance, for a given connection's company. This is
a genuine, structural gap in any periodic (not real-time) two-system
integration: each side can record independent stock-affecting transactions
while disconnected from the other, so quantities can silently diverge.
Automatically "fixing" a mismatch is out of scope on purpose - only a
human, usually after a physical stock count, can say which number (if
either) is actually correct. This service's job is only to surface the
mismatch clearly and quickly (see TallyStockReconciliationWizard for the
on-demand report, and tally_connection.py's _cron_check_stock_mismatch for
the scheduled alert).
"""

import logging

from .tally_exceptions import TallyConfigurationError

_logger = logging.getLogger(__name__)


class TallyStockReconciliationService:
    """Compares Odoo product quantities against Tally's stock item closing balances."""

    def __init__(self, env):
        self.env = env

    def reconcile(self, connection):
        """
        Fetch Tally's stock item list (with closing balances) and compare
        against the on-hand quantity of every Odoo product already linked
        to a Tally stock item (via tally_guid).

        Args:
            connection (tally.connection): source connection

        Returns:
            dict: {
                "success": bool,
                "rows": [{"product": product.product, "odoo_qty": float,
                    "tally_qty": float, "difference": float}, ...],
                "message": str,
                "error": str or None,
            }

        Raises:
            TallyConfigurationError: connection not usable
        """
        if not connection.active or not connection.enabled_for_sync:
            raise TallyConfigurationError(
                f"Connection '{connection.name}' is not active/enabled for sync."
            )

        client = connection._get_tally_client()
        result = client.fetch_stock_items(company=connection.tally_company_name)

        if not result["success"]:
            return {
                "success": False,
                "rows": [],
                "message": result.get("message", "Failed to fetch stock items from Tally"),
                "error": result.get("error"),
            }

        Product = self.env["product.product"]
        rows = []

        for item in result["items"]:
            guid = (item.get("StockItemGuid") or "").strip()
            if not guid:
                continue

            product = Product.search([("tally_guid", "=", guid)], limit=1)
            if not product:
                # Not linked to any Odoo product - nothing to compare.
                continue

            tally_qty = item.get("StockItemClosingQty") or 0.0
            odoo_qty = product.qty_available
            rows.append(
                {
                    "product": product,
                    "odoo_qty": odoo_qty,
                    "tally_qty": tally_qty,
                    "difference": odoo_qty - tally_qty,
                }
            )

        return {
            "success": True,
            "rows": rows,
            "message": f"Compared {len(rows)} Tally-linked product(s)",
            "error": None,
        }
