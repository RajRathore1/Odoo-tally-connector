"""
Partner (Ledger) import service - Tally -> Odoo direction.

Mirrors tally_product_import_service.py's design and matching strategy.

Only imports Ledgers under the "Sundry Debtors" (customer) or "Sundry
Creditors" (vendor) groups - Tally companies always contain many other
system ledgers (Cash, Bank, Profit & Loss A/c, tax ledgers, etc.) that must
NOT be imported as contacts. This is a deliberate scope limit, not an
oversight.
"""

import logging

from odoo import fields

from .tally_exceptions import TallyConfigurationError
from .tally_partner_sync_service import CUSTOMER_LEDGER_GROUP, VENDOR_LEDGER_GROUP

_logger = logging.getLogger(__name__)

# Maps the Tally Ledger Group to the res.partner rank field that marks a
# contact as belonging to that group.
_GROUP_TO_RANK_FIELD = {
    CUSTOMER_LEDGER_GROUP: "customer_rank",
    VENDOR_LEDGER_GROUP: "supplier_rank",
}


class TallyPartnerImportService:
    """Imports Tally Ledgers (Sundry Debtors/Creditors groups) into Odoo contacts."""

    def __init__(self, env):
        self.env = env

    def import_partners(self, connection, name_filter=None):
        """
        Fetch Ledgers from Tally and sync customer/vendor ledgers into Odoo.

        Args:
            connection (tally.connection): source connection (must be active
                and enabled for sync)
            name_filter (str): if given, only the Tally ledger whose name
                matches exactly (case-insensitive) is imported.

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
        result = client.fetch_ledgers(company=connection.tally_company_name)

        if not result["success"]:
            return {
                "success": False,
                "created": [],
                "updated": [],
                "errors": [],
                "message": result.get("message", "Failed to fetch ledgers from Tally"),
            }

        created, updated, errors = [], [], []
        Partner = self.env["res.partner"]
        company = connection.company_id

        for item in result["ledgers"]:
            name = (item.get("LedgerName") or "").strip()
            guid = (item.get("LedgerGuid") or "").strip()
            parent = (item.get("LedgerParent") or "").strip()

            if not name:
                continue

            rank_field = _GROUP_TO_RANK_FIELD.get(parent)
            if rank_field is None:
                continue

            if name_filter and name.strip().lower() != name_filter.strip().lower():
                continue

            try:
                partner = Partner
                if guid:
                    partner = Partner.search([("tally_guid", "=", guid)], limit=1)

                if not partner:
                    candidate = Partner.search(
                        [("name", "=", name), ("tally_guid", "=", False)], limit=2
                    )
                    if len(candidate) > 1:
                        errors.append(
                            {
                                "name": name,
                                "error": f"Ambiguous match: {len(candidate)} unlinked Odoo contacts "
                                f"named '{name}' - link one manually before importing.",
                            }
                        )
                        continue
                    partner = candidate

                if partner:
                    vals = {
                        "name": name,
                        rank_field: max(getattr(partner, rank_field), 1),
                        "tally_guid": guid or partner.tally_guid,
                        "tally_synced_name": name,
                        "tally_sync_status": "success",
                        "tally_last_sync_at": fields.Datetime.now(),
                    }
                    partner.write(vals)
                    updated.append(name)
                    _logger.info(
                        f"Partner updated from Tally import: {name}",
                        extra={"partner_id": partner.id, "connection_id": connection.id},
                    )
                else:
                    new_partner = Partner.create(
                        {
                            "name": name,
                            rank_field: 1,
                            "company_id": company.id if company else False,
                            "tally_guid": guid,
                            "tally_synced_name": name,
                            "tally_sync_status": "success",
                            "tally_last_sync_at": fields.Datetime.now(),
                        }
                    )
                    created.append(name)
                    _logger.info(
                        f"Partner created from Tally import: {name}",
                        extra={"partner_id": new_partner.id, "connection_id": connection.id},
                    )

            except Exception as e:
                errors.append({"name": name, "error": str(e)})
                _logger.warning(
                    f"Error importing Tally ledger '{name}': {str(e)}",
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
