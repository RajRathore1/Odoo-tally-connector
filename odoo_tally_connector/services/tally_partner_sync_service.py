"""
Partner (Customer/Vendor <-> Ledger) synchronization service.

Orchestrates Odoo res.partner -> Tally Ledger upsert. Mirrors
tally_product_sync_service.py's design (deterministic client-supplied GUID
for idempotency, name-change tracking for Alter/OLDNAME).

Handles both customers (Sundry Debtors) and vendors (Sundry Creditors).
A partner marked as both (customer_rank > 0 AND supplier_rank > 0) is
treated as a customer by default - this is a deliberate, documented choice,
not an oversight: a partner cannot be filed under two Ledger Groups at once
in Tally, and most such partners are customers first.
"""

import logging
import uuid

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError

_logger = logging.getLogger(__name__)

CUSTOMER_LEDGER_GROUP = "Sundry Debtors"
VENDOR_LEDGER_GROUP = "Sundry Creditors"


class TallyPartnerSyncService:
    """Synchronizes Odoo customers/vendors to Tally Ledgers."""

    def __init__(self, env):
        self.env = env

    def _get_connection(self, company):
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

    def _compute_sync_key(self, partner):
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, self.env.cr.dbname)
        return str(uuid.uuid5(namespace, f"odoo:res.partner:{partner.id}"))

    def _resolve_ledger_group(self, partner):
        """
        Decide which Tally Ledger Group this partner belongs to.

        Raises:
            TallyMappingError: Partner is neither a customer nor a vendor
        """
        if partner.customer_rank > 0:
            return CUSTOMER_LEDGER_GROUP
        if partner.supplier_rank > 0:
            return VENDOR_LEDGER_GROUP
        raise TallyMappingError(
            f"'{partner.display_name}' is not marked as a customer or vendor "
            f"(customer_rank = 0, supplier_rank = 0). Nothing to sync."
        )

    def sync_partner(self, partner):
        """
        Sync a single customer or vendor to Tally as a Ledger.

        Args:
            partner (res.partner): record to sync (single record expected)

        Returns:
            dict: raw result from TallyClient.upsert_ledger()

        Raises:
            TallyConfigurationError: No usable connection for the partner's company
            TallyMappingError: Partner is neither a customer nor a vendor
        """
        parent_group = self._resolve_ledger_group(partner)

        company = partner.company_id or self.env.company
        connection = self._get_connection(company)

        if not partner.tally_sync_key:
            partner.tally_sync_key = self._compute_sync_key(partner)

        already_synced = partner.tally_sync_status == "success" and bool(partner.tally_guid)
        action = "Alter" if already_synced else "Create"
        old_name = partner.tally_synced_name if action == "Alter" else None

        address_lines = [
            line
            for line in [partner.street, partner.street2, partner.city, partner.state_id.name, partner.zip]
            if line
        ]

        client = connection._get_tally_client()
        result = client.upsert_ledger(
            company=connection.tally_company_name or company.name,
            name=partner.display_name,
            parent_group=parent_group,
            guid=partner.tally_sync_key,
            action=action,
            old_name=old_name,
            address_lines=address_lines,
            phone=partner.phone,
            email=partner.email,
            res_model="res.partner",
            res_id=partner.id,
        )

        vals = {
            "tally_last_sync_at": fields.Datetime.now(),
            "tally_sync_attempts": partner.tally_sync_attempts + 1,
        }

        if result.get("queued"):
            vals["tally_sync_status"] = "queued"
            partner.write(vals)
            _logger.info(
                f"Partner queued for async Tally Agent processing: {partner.display_name}",
                extra={"partner_id": partner.id, "connection_id": connection.id, "job_id": result.get("job_id")},
            )
            return result

        if result["success"]:
            vals.update(
                {
                    "tally_sync_status": "success",
                    "tally_guid": partner.tally_sync_key,
                    "tally_synced_name": partner.display_name,
                    "tally_last_sync_error": False,
                }
            )
            _logger.info(
                f"Partner synced to Tally: {partner.display_name} ({action})",
                extra={"partner_id": partner.id, "connection_id": connection.id, "action": action},
            )
        else:
            vals.update(
                {
                    "tally_sync_status": "failed",
                    "tally_last_sync_error": result.get("error") or result.get("message"),
                }
            )
            _logger.warning(
                f"Partner sync to Tally failed: {partner.display_name}",
                extra={"partner_id": partner.id, "connection_id": connection.id, "error": result.get("error")},
            )

        partner.write(vals)
        return result
