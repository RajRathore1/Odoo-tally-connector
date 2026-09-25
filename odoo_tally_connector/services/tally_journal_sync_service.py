"""
Journal Voucher synchronization service (Phase 8).

Orchestrates Odoo account.move (move_type="entry", i.e. a Miscellaneous
Operations / general journal entry - month-end adjustments, depreciation,
provisions, accruals) -> Tally Journal voucher upsert. Like Receipt/Payment,
a Journal voucher is a plain accounting voucher with no stock items - but
unlike every other voucher this module syncs, it also has no party ledger
at all: just N ledger legs (one per account.move.line) that net to zero
(see TallyXmlBuilder.build_journal_voucher_upsert_request).

Sign convention - this maps directly onto Odoo's own debit/credit fields,
following the same Dr/Cr-vs-amount-sign pattern verified against real Tally
for every other voucher type in this module (see
tally_credit_debit_note_sync_service.py's module docstring):
- A line with a debit balance is the debit leg (amount negative,
  is_deemed_positive=True).
- A line with a credit balance is the credit leg (amount positive,
  is_deemed_positive=False).

Odoo already guarantees a posted account.move's lines net to zero (core
ORM constraint) - this service trusts that rather than re-deriving balance
math itself, the same way _distribute_line_taxes trusts compute_all instead
of reimplementing tax math.
"""

import logging
import uuid

from odoo import fields

from .tally_exceptions import TallyConfigurationError, TallyMappingError, TallyValidationError

_logger = logging.getLogger(__name__)

# display_type values that are presentation-only rows, never real ledger
# lines - a section/note heading has no account_id and nothing to sync.
_NON_LEDGER_DISPLAY_TYPES = ("line_section", "line_note")


class TallyJournalSyncService:
    """Synchronizes posted Odoo journal entries to Tally Journal vouchers."""

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
        Validate every line's account has a Tally Ledger Name mapping and
        build the N-leg ledger entry list.

        Returns:
            list[dict]: ledger_entries
        """
        lines = move.line_ids.filtered(lambda l: l.display_type not in _NON_LEDGER_DISPLAY_TYPES)
        if not lines:
            raise TallyValidationError(f"Journal entry '{move.name}' has no lines to sync.")

        ledger_entries = []
        for line in lines:
            account = line.account_id
            if not account or not account.tally_ledger_name:
                raise TallyMappingError(
                    f"Account '{account.display_name if account else '(none)'}' on journal entry "
                    f"'{move.name}' line '{line.name or line.id}' has no Tally Ledger Name mapping. "
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

    def sync_journal_entry(self, move):
        """
        Sync a single posted Odoo journal entry to Tally as a Journal voucher.

        Args:
            move (account.move): record to sync (move_type must be "entry")

        Returns:
            dict: raw result from TallyClient.upsert_journal_voucher()

        Raises:
            TallyConfigurationError: No usable connection for the move's company
            TallyValidationError: Not a posted journal entry, or no lines to sync
            TallyMappingError: An account on a line has no Tally ledger mapping
        """
        if move.move_type != "entry":
            raise TallyValidationError(
                f"'{move.name}' is a {move.move_type} - only miscellaneous journal entries "
                f"can sync to Tally through this action."
            )
        if move.state != "posted":
            raise TallyValidationError(
                f"'{move.name}' is not posted (state={move.state}). Only posted journal entries "
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
        result = client.upsert_journal_voucher(
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
                f"Journal entry queued for async Tally Agent processing: {move.name}",
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
                f"Journal entry synced to Tally: {move.name} ({action})",
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
                f"Journal entry sync to Tally failed: {move.name}",
                extra={"move_id": move.id, "connection_id": connection.id, "error": result.get("error")},
            )

        move.write(vals)
        return result
