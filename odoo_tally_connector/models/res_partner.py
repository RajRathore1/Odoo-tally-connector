"""
Partner synchronization fields and actions.

Thin ORM layer only - actual sync orchestration lives in
services/tally_partner_sync_service.py. This model never talks HTTP/XML
directly. Mirrors product_product.py's design.
"""

import logging

from odoo import fields, models

from ..services.tally_exceptions import TallyConnectorError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    tally_sync_key = fields.Char(
        string="Tally Sync Key",
        readonly=True,
        copy=False,
        help="Deterministic idempotency key sent to Tally as the Ledger's GUID on creation. "
        "Stable across retries so re-sync never creates a duplicate.",
    )

    tally_guid = fields.Char(
        string="Tally GUID",
        readonly=True,
        copy=False,
        help="Set once Tally has confirmed this partner exists as a Ledger.",
    )

    tally_synced_name = fields.Char(
        string="Last Synced Name",
        readonly=True,
        copy=False,
        help="Partner name as of the last successful sync. Used to detect renames "
        "so Tally's ALTER request can include OLDNAME correctly.",
    )

    tally_sync_status = fields.Selection(
        [
            ("draft", "Not Synced"),
            ("queued", "Queued"),
            ("success", "Synced"),
            ("failed", "Failed"),
        ],
        string="Tally Sync Status",
        default="draft",
        copy=False,
    )

    tally_last_sync_at = fields.Datetime(
        string="Last Sync Time",
        readonly=True,
        copy=False,
    )

    tally_last_sync_error = fields.Text(
        string="Last Sync Error",
        readonly=True,
        copy=False,
    )

    tally_sync_attempts = fields.Integer(
        string="Sync Attempts",
        default=0,
        readonly=True,
        copy=False,
    )

    def action_sync_to_tally(self):
        """Sync this customer to Tally as a Ledger (create or alter)."""
        self.ensure_one()

        from ..services import TallyPartnerSyncService

        try:
            service = TallyPartnerSyncService(self.env)
            result = service.sync_partner(self)

            if result.get("queued"):
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Queued",
                        "message": f"'{self.display_name}' queued for the Tally Agent - status will "
                        f"update once it's processed.",
                        "type": "info",
                        "sticky": False,
                    },
                }

            if result["success"]:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Success",
                        "message": f"'{self.display_name}' synced to Tally: {result['message']}",
                        "type": "success",
                        "sticky": False,
                    },
                }

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Sync Failed",
                    "message": result.get("message", "Unknown error"),
                    "type": "danger",
                    "sticky": True,
                },
            }

        except TallyConnectorError as e:
            self.write(
                {
                    "tally_sync_status": "failed",
                    "tally_last_sync_error": e.message,
                    "tally_last_sync_at": fields.Datetime.now(),
                }
            )
            _logger.warning(
                f"Tally partner sync blocked: {self.display_name}",
                extra={"partner_id": self.id, "error": e.message},
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Sync Failed",
                    "message": e.message,
                    "type": "danger",
                    "sticky": True,
                },
            }
