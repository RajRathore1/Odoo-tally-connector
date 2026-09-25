"""
Payment/Receipt synchronization fields and actions.

Thin ORM layer only - actual sync orchestration lives in
services/tally_payment_sync_service.py. Mirrors account_move.py's design.
"""

import logging

from odoo import fields, models

from ..services.tally_exceptions import TallyConnectorError

_logger = logging.getLogger(__name__)


class AccountPayment(models.Model):
    _inherit = "account.payment"

    tally_sync_key = fields.Char(
        string="Tally Sync Key",
        readonly=True,
        copy=False,
        help="Deterministic idempotency key sent to Tally as the voucher's REMOTEID. "
        "Stable across retries so re-sync never creates a duplicate voucher.",
    )

    tally_guid = fields.Char(
        string="Tally Voucher ID",
        readonly=True,
        copy=False,
        help="Set once Tally has confirmed this payment exists as a Receipt/Payment Voucher.",
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
        """Sync this confirmed payment to Tally as a Receipt/Payment Voucher."""
        self.ensure_one()

        from ..services import TallyPaymentSyncService

        try:
            service = TallyPaymentSyncService(self.env)
            result = service.sync_payment(self)

            if result.get("queued"):
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Queued",
                        "message": f"'{self.name}' queued for the Tally Agent - status will update "
                        f"once it's processed.",
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
                        "message": f"'{self.name}' synced to Tally: {result['message']}",
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
                f"Tally payment sync blocked: {self.name}",
                extra={"payment_id": self.id, "error": e.message},
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
