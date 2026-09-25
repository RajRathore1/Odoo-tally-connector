"""
Invoice/Bill/Journal-entry synchronization fields and actions.

Thin ORM layer only - actual sync orchestration lives in
services/tally_invoice_sync_service.py (customer invoices),
services/tally_bill_sync_service.py (vendor bills),
services/tally_credit_debit_note_sync_service.py (credit/debit notes),
services/tally_journal_sync_service.py (move_type="entry" journal entries),
and services/tally_contra_sync_service.py (move_type="entry" entries whose
every line is on a Cash/Bank account - see _is_contra_entry() below).
Mirrors product_product.py's design. action_sync_to_tally() dispatches to
the right service by move_type.
"""

import logging

from odoo import fields, models

from ..services.tally_exceptions import TallyConnectorError, TallyValidationError

_logger = logging.getLogger(__name__)

# The one Odoo account_type Tally's Contra voucher can legally use - see
# tally_contra_sync_service.py's module docstring for why a plain journal
# entry (rather than account.payment's unreliable "transfer" pairing) is
# what this connector treats as the source of a Contra voucher.
_CASH_ACCOUNT_TYPE = "asset_cash"
_NON_LEDGER_DISPLAY_TYPES = ("line_section", "line_note")


class AccountMove(models.Model):
    _inherit = "account.move"

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
        help="Set once Tally has confirmed this invoice/bill exists as a Sales/Purchase Voucher.",
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

    def _is_contra_entry(self):
        """
        True if every real ledger line on this move is a Bank/Cash account -
        Tally's own definition of what a Contra voucher may contain. An
        empty line set is not a Contra (falls through to Journal, which
        will raise its own "no lines to sync" error).
        """
        self.ensure_one()
        lines = self.line_ids.filtered(lambda l: l.display_type not in _NON_LEDGER_DISPLAY_TYPES)
        return bool(lines) and all(line.account_id.account_type == _CASH_ACCOUNT_TYPE for line in lines)

    def action_sync_to_tally(self):
        """
        Sync this posted customer invoice or vendor bill to Tally, as a
        Sales Voucher or Purchase Voucher respectively.
        """
        self.ensure_one()

        try:
            if self.move_type == "out_invoice":
                from ..services import TallyInvoiceSyncService

                result = TallyInvoiceSyncService(self.env).sync_invoice(self)
            elif self.move_type == "in_invoice":
                from ..services import TallyBillSyncService

                result = TallyBillSyncService(self.env).sync_bill(self)
            elif self.move_type in ("out_refund", "in_refund"):
                from ..services import TallyCreditDebitNoteSyncService

                result = TallyCreditDebitNoteSyncService(self.env).sync_note(self)
            elif self.move_type == "entry" and self._is_contra_entry():
                from ..services import TallyContraSyncService

                result = TallyContraSyncService(self.env).sync_contra_entry(self)
            elif self.move_type == "entry":
                from ..services import TallyJournalSyncService

                result = TallyJournalSyncService(self.env).sync_journal_entry(self)
            else:
                raise TallyValidationError(
                    f"'{self.name}' is a {self.move_type} - only customer invoices, vendor "
                    f"bills, credit notes, debit notes, and journal entries can sync to Tally."
                )

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
                f"Tally invoice sync blocked: {self.name}",
                extra={"move_id": self.id, "error": e.message},
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
