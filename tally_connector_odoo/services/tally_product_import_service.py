"""
Product (Stock Item) import service - Tally -> Odoo direction.

Orchestrates fetching Stock Items from Tally and creating/updating matching
Odoo product.product records.

Matching strategy (stable identity first, name fallback explicit and logged -
never silently pick among ambiguous candidates):
1. product.product.tally_guid == Tally's GUID for the item -> update it
2. Else an unlinked product.product with the exact same name -> link it
   (sets tally_guid so it won't be re-matched-by-name next time)
3. Else create a new product.product

Requires a UOM mapping: the Tally item's BASEUNITS name must match an
existing odoo uom.uom name exactly (case-insensitive). Missing mapping is
reported as a per-item error, not silently skipped or guessed.
"""

import logging

from odoo import fields

from .tally_exceptions import TallyConfigurationError

_logger = logging.getLogger(__name__)


class TallyProductImportService:
    """Imports Tally Stock Items into Odoo as product.product records."""

    def __init__(self, env):
        self.env = env

    def import_products(self, connection, name_filter=None):
        """
        Fetch Stock Items from Tally and sync them into Odoo.

        Args:
            connection (tally.connection): source connection (must be active
                and enabled for sync)
            name_filter (str): if given, only the Tally stock item whose name
                matches exactly (case-insensitive) is imported - everything
                else is fetched but skipped (not reported as an error, since
                this is an intentional user-chosen filter, not a data problem).

        Returns:
            dict: {
                "success": bool,
                "created": [names],
                "updated": [names],
                "errors": [{"name": str, "error": str}],
                "message": str,
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
                "created": [],
                "updated": [],
                "errors": [],
                "message": result.get("message", "Failed to fetch stock items from Tally"),
            }

        created, updated, errors = [], [], []
        Product = self.env["product.product"]
        Uom = self.env["uom.uom"]
        company = connection.company_id

        for item in result["items"]:
            name = (item.get("StockItemName") or "").strip()
            guid = (item.get("StockItemGuid") or "").strip()
            unit_name = (item.get("StockItemUnit") or "").strip()

            if not name:
                continue

            if name_filter and name.strip().lower() != name_filter.strip().lower():
                continue

            try:
                product = Product
                if guid:
                    product = Product.search([("tally_guid", "=", guid)], limit=1)

                if not product:
                    candidate = Product.search(
                        [("name", "=", name), ("tally_guid", "=", False)], limit=2
                    )
                    if len(candidate) > 1:
                        errors.append(
                            {
                                "name": name,
                                "error": f"Ambiguous match: {len(candidate)} unlinked Odoo products "
                                f"named '{name}' - link one manually before importing.",
                            }
                        )
                        continue
                    product = candidate

                uom = Uom.search([("name", "=ilike", unit_name)], limit=1) if unit_name else Uom
                if not uom:
                    errors.append(
                        {
                            "name": name,
                            "error": f"No Odoo Unit of Measure found matching Tally unit '{unit_name}'.",
                        }
                    )
                    continue

                if product:
                    vals = {
                        "name": name,
                        "tally_guid": guid or product.tally_guid,
                        "tally_synced_name": name,
                        "tally_sync_status": "success",
                        "tally_last_sync_at": fields.Datetime.now(),
                    }
                    product.write(vals)
                    updated.append(name)
                    _logger.info(
                        f"Product updated from Tally import: {name}",
                        extra={"product_id": product.id, "connection_id": connection.id},
                    )
                else:
                    new_product = Product.create(
                        {
                            "name": name,
                            "uom_id": uom.id,
                            "company_id": company.id if company else False,
                            # Every Tally Stock Item is, by definition, a
                            # physically tracked inventory item - without
                            # this, Odoo never tracks qty_available for it
                            # and stock reconciliation against Tally is
                            # meaningless (Odoo side always reads 0).
                            "is_storable": True,
                            "tally_guid": guid,
                            "tally_synced_name": name,
                            "tally_sync_status": "success",
                            "tally_last_sync_at": fields.Datetime.now(),
                        }
                    )
                    created.append(name)
                    _logger.info(
                        f"Product created from Tally import: {name}",
                        extra={"product_id": new_product.id, "connection_id": connection.id},
                    )

            except Exception as e:
                errors.append({"name": name, "error": str(e)})
                _logger.warning(
                    f"Error importing Tally stock item '{name}': {str(e)}",
                    extra={"connection_id": connection.id},
                )

        message = f"Created {len(created)}, updated {len(updated)}, {len(errors)} error(s)"
        return {
            "success": True,
            "created": created,
            "updated": updated,
            "errors": errors,
            "message": message,
        }
