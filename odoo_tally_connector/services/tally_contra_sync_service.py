"""
Contra Voucher synchronization service (Phase 9).

Orchestrates Odoo account.move (move_type="entry") -> Tally Contra voucher
upsert, for the specific case of an internal fund transfer between the
business's own Cash/Bank ledgers (e.g. cash deposited into a bank account,
or a transfer between two bank accounts).

Why this reuses account.move rather than account.payment: Odoo's own
"internal transfer" feature (account.payment with a transfer-like flow) in
this version represents a transfer as two independent, NOT reliably linked
account.payment records (see paired_internal_transfer_payment_id - declared
but never populated anywhere in core account/account_accountant), each
carrying the company's own contact as partner_id rather than a real
customer/vendor. Building a Contra voucher - which Tally represents as ONE
voucher with two Cash/Bank legs and no party at all - out of two unlinked
payment records would be fragile. A manual journal entry between two
Cash/Bank-type accounts is a well-defined, already-reliable way to record
the same transaction in Odoo, and is what this service targets instead.

Detection: account_move.py's action_sync_to_tally() decides whether a
move_type="entry" move is a Contra (every line's account.account_type ==
"asset_cash") or a plain Journal (anything else) - see that file's
_is_contra_entry(). This service re-validates the same condition itself
(defensive, matching every other sync service's own-validation convention)
rather than trusting the caller blindly.

Sign convention and N-leg structure: identical to
tally_journal_sync_service.py - see that module's docstring. A Contra
voucher is structurally just a Journal voucher restricted to Cash/Bank
ledgers only; only the Tally VCHTYPE differs.
"""

import logging
import uuid

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError, TallyValidationError

_logger = logging.getLogger(__name__)

_NON_LEDGER_DISPLAY_TYPES = ("line_section", "line_note")

# The one Odoo account_type Tally's Contra voucher can legally use - "Bank
# and Cash" ledgers only. A journal entry with even one line on any other
# account type is not a Contra by Tally's own definition and must go
# through TallyJournalSyncService instead.
_CASH_ACCOUNT_TYPE = "asset_cash"


class TallyContraSyncService:
    """Synchronizes posted Odoo Cash/Bank-only journal entries to Tally Contra vouchers."""

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

    def _compute_sync_key(self, move):
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, self.env.cr.dbname)
        return str(uuid.uuid5(namespace, f"odoo:account.move:{move.id}"))

    def _validate_and_build_entries(self, move):
        """
        Validate this is genuinely a Cash/Bank-only entry and every line's
        account has a Tally Ledger Name mapping, then build the N-leg
        ledger entry list.

        Returns:
            list[dict]: ledger_entries
        """
        lines = move.line_ids.filtered(lambda l: l.display_type not in _NON_LEDGER_DISPLAY_TYPES)
        if not lines:
            raise TallyValidationError(f"Journal entry '{move.name}' has no lines to sync.")

        non_cash = lines.filtered(lambda l: l.account_id.account_type != _CASH_ACCOUNT_TYPE)
        if non_cash:
            account = non_cash[0].account_id
            raise TallyValidationError(
                f"'{move.name}' is not a Contra entry - account '{account.display_name}' on line "
                f"'{non_cash[0].name or non_cash[0].id}' is not a Bank/Cash account. A Contra voucher "
                f"can only move funds between the business's own Cash/Bank ledgers."
            )

        ledger_entries = []
        for line in lines:
            account = line.account_id
            if not account.tally_ledger_name:
                raise TallyMappingError(
                    f"Account '{account.display_name}' on Contra entry '{move.name}' line "
                    f"'{line.name or line.id}' has no Tally Ledger Name mapping. "
                    f"Set it on the account (Accounting > Chart of Accounts)."
                )
            if line.debit:
                ledger_entries.append(
                    {"ledger_name": account.tally_ledger_name, "amount": -line.debit, "is_deemed_positive": True}
                )
            elif line.credit:
                ledger_entries.append(
                    {"ledger_name": account.tally_ledger_name, "amount": line.credit, "is_deemed_positive": False}
                )
        return ledger_entries

    def sync_contra_entry(self, move):
        """
        Sync a single posted Odoo Cash/Bank-only journal entry to Tally as
        a Contra voucher.

        Args:
            move (account.move): record to sync (move_type must be "entry",
                every line's account must be account_type "asset_cash")

        Returns:
            dict: raw result from TallyClient.upsert_contra_voucher()

        Raises:
            TallyConfigurationError: No usable connection for the move's company
            TallyValidationError: Not a posted journal entry, no lines, or
                not every line is on a Cash/Bank account
            TallyMappingError: An account on a line has no Tally ledger mapping
        """
        if move.move_type != "entry":
            raise TallyValidationError(
                f"'{move.name}' is a {move.move_type} - only miscellaneous journal entries "
                f"can sync to Tally as a Contra voucher."
            )
        if move.state != "posted":
            raise TallyValidationError(
                f"'{move.name}' is not posted (state={move.state}). Only posted entries "
                f"sync to Tally."
            )

        company = move.company_id or self.env.company
        connection = self._get_connection(company)

        ledger_entries = self._validate_and_build_entries(move)

        if not move.tally_sync_key:
            move.tally_sync_key = self._compute_sync_key(move)

        already_synced = move.tally_sync_status == "success" and bool(move.tally_guid)
        action = "Alter" if already_synced else "Create"

        voucher_date = fields.Date.to_string(move.date).replace("-", "")

        client = connection._get_tally_client()
        result = client.upsert_contra_voucher(
            company=connection.tally_company_name or company.name,
            voucher_number=move.name,
            voucher_date=voucher_date,
            guid=move.tally_sync_key,
            ledger_entries=ledger_entries,
            action=action,
            narration=move.narration and move.narration[:500] or None,
            reference=move.ref,
            res_model="account.move",
            res_id=move.id,
        )

        vals = {
            "tally_last_sync_at": fields.Datetime.now(),
            "tally_sync_attempts": move.tally_sync_attempts + 1,
        }

        if result.get("queued"):
            vals["tally_sync_status"] = "queued"
            move.write(vals)
            _logger.info(
                f"Contra entry queued for async Tally Agent processing: {move.name}",
                extra={"move_id": move.id, "connection_id": connection.id, "job_id": result.get("job_id")},
            )
            return result

        if result["success"]:
            vals.update(
                {
                    "tally_sync_status": "success",
                    "tally_guid": move.tally_sync_key,
                    "tally_last_sync_error": False,
                }
            )
            _logger.info(
                f"Contra entry synced to Tally: {move.name} ({action})",
                extra={"move_id": move.id, "connection_id": connection.id, "action": action},
            )
        else:
            vals.update(
                {
                    "tally_sync_status": "failed",
                    "tally_last_sync_error": result.get("error") or result.get("message"),
                }
            )
            _logger.warning(
                f"Contra entry sync to Tally failed: {move.name}",
                extra={"move_id": move.id, "connection_id": connection.id, "error": result.get("error")},
            )

        move.write(vals)
        return result
