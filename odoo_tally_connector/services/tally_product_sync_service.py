"""
Product (Stock Item) synchronization service.

Orchestrates Odoo product.product -> Tally Stock Item upsert.

Responsibilities:
- Resolve which Tally connection applies to the product's company
- Resolve UOM mapping (Odoo unit -> Tally unit name)
- Decide Create vs Alter based on prior sync state
- Generate/track a deterministic idempotency key (client-supplied Tally GUID)
- Call the Tally client and translate the result into product field updates

Does NOT handle:
- HTTP transport or XML generation/parsing (delegated to TallyClient)
- UI presentation (delegated to the model's action method)

Phase 11 additions (best-effort, never block a product sync on their own
failure - see _ensure_unit_exists's docstring):
- Passes the product's category's tally_stock_group_name (if mapped) as the
  Stock Item's Tally Stock Group parent, instead of always landing at
  Tally's default top level.
- Ensures the product's Unit of Measure exists as a Tally Unit master
  before creating the Stock Item, instead of just assuming it already does.

Neither the Stock Group nor Unit upsert XML shapes have been verified
against a real Tally instance yet - flagged the same way every other
first-use-of-a-new-master-type in this module is.
"""

import logging
import uuid

from odoo import fields

from .tally_exceptions import TallyConnectorError, TallyConfigurationError, TallyMappingError

_logger = logging.getLogger(__name__)


class TallyProductSyncService:
    """Synchronizes Odoo products to Tally Stock Items."""

    def __init__(self, env):
        self.env = env

    def _get_connection(self, company):
        """
        Resolve the active, sync-enabled Tally connection for a company.

        Raises:
            TallyConfigurationError: If no usable connection exists.
        """
        connection = self.env["tally.connection"].search(
            [
                ("company_id", "=", company.id),
                ("active", "=", True),
                ("enabled_for_sync", "=", True),
            ],
            limit=1,
        )
        if not connection:
            raise TallyConfigurationError(
                f"No active, sync-enabled Tally connection found for company '{company.name}'. "
                f"Configure one under Tally > Configuration > Connections and test it first."
            )
        return connection

    def _compute_sync_key(self, product):
        """
        Deterministic idempotency key: same Odoo DB + record always yields the
        same value, so retries never generate a second Tally record.
        """
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, self.env.cr.dbname)
        return str(uuid.uuid5(namespace, f"odoo:product.product:{product.id}"))

    def _ensure_unit_exists(self, client, company, unit_name):
        """
        Best-effort: push the unit as a Tally Unit master before the Stock
        Item that references it. Deliberately non-fatal - this connector has
        no per-unit sync-state tracking (no GUID, no "already ensured this
        session" cache), so it re-attempts a Create every single product
        sync; Tally's own handling of a duplicate Unit Create is what keeps
        this cheap and safe to repeat, not anything on this side. A failure
        here (including "already exists") is logged and swallowed rather
        than raised, so a hiccup pushing the Unit master never blocks the
        Stock Item sync that actually matters.
        """
        try:
            result = client.upsert_unit(company=company, name=unit_name, action="Create")
            if not result["success"]:
                _logger.info(
                    f"Tally Unit master push for '{unit_name}' did not report success (likely already "
                    f"exists) - continuing with Stock Item sync regardless: {result.get('error')}",
                    extra={"unit_name": unit_name},
                )
        except Exception:
            _logger.exception(
                f"Unexpected error ensuring Tally Unit master '{unit_name}' exists - continuing with "
                f"Stock Item sync regardless",
                extra={"unit_name": unit_name},
            )

    def sync_product(self, product):
        """
        Sync a single product to Tally as a Stock Item.

        Args:
            product (product.product): record to sync (single record expected)

        Returns:
            dict: raw result from TallyClient.upsert_stock_item()

        Raises:
            TallyConfigurationError: No usable connection for the product's company
            TallyMappingError: Product has no Unit of Measure
        """
        company = product.company_id or self.env.company
        connection = self._get_connection(company)

        base_unit = product.uom_id.name
        if not base_unit:
            raise TallyMappingError(
                f"Product '{product.display_name}' has no Unit of Measure - cannot determine Tally BASEUNITS."
            )

        if not product.tally_sync_key:
            product.tally_sync_key = self._compute_sync_key(product)

        already_synced = product.tally_sync_status == "success" and bool(product.tally_guid)
        action = "Alter" if already_synced else "Create"
        old_name = product.tally_synced_name if action == "Alter" else None

        client = connection._get_tally_client()
        tally_company = connection.tally_company_name or company.name

        self._ensure_unit_exists(client, tally_company, base_unit)

        result = client.upsert_stock_item(
            company=tally_company,
            name=product.display_name,
            base_unit=base_unit,
            guid=product.tally_sync_key,
            action=action,
            old_name=old_name,
            parent_group=product.categ_id.tally_stock_group_name or None,
            res_model="product.product",
            res_id=product.id,
        )

        vals = {
            "tally_last_sync_at": fields.Datetime.now(),
            "tally_sync_attempts": product.tally_sync_attempts + 1,
        }

        if result.get("queued"):
            vals["tally_sync_status"] = "queued"
            product.write(vals)
            _logger.info(
                f"Product queued for async Tally Agent processing: {product.display_name}",
                extra={"product_id": product.id, "connection_id": connection.id, "job_id": result.get("job_id")},
            )
            return result

        if result["success"]:
            vals.update(
                {
                    "tally_sync_status": "success",
                    "tally_guid": product.tally_sync_key,
                    "tally_synced_name": product.display_name,
                    "tally_last_sync_error": False,
                }
            )
            _logger.info(
                f"Product synced to Tally: {product.display_name} ({action})",
                extra={"product_id": product.id, "connection_id": connection.id, "action": action},
            )
        else:
            vals.update(
                {
                    "tally_sync_status": "failed",
                    "tally_last_sync_error": result.get("error") or result.get("message"),
                }
            )
            _logger.warning(
                f"Product sync to Tally failed: {product.display_name}",
                extra={"product_id": product.id, "connection_id": connection.id, "error": result.get("error")},
            )

        product.write(vals)
        return result
