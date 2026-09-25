"""
Opening Balance push service (Phase 10).

One-time onboarding tool: sets the OPENINGBALANCE of already-mapped Tally
ledgers (account.account.tally_ledger_name) to match Odoo's own account
balance as of a chosen cutover date, so historical figures agree between
the two systems from day one instead of Tally's ledgers silently starting
at zero.

Deliberately NOT part of the regular sync queue (_sync_pending_queue) -
this only makes sense to run once, right after a connection first goes
live (or after a chart-of-accounts correction), triggered manually via
tally.opening.balance.wizard, never automatically or per-transaction.

Scope: accounts only, not partners - a customer/vendor's opening balance
is already carried by their own historical invoices/bills/payments once
those sync (see tally_invoice_sync_service.py etc.), so there is no
separate "partner opening balance" concept needed here the way there is
for a bank/expense/asset account that predates this connector entirely.

Sign convention: follows Odoo's own account.account "balance" semantics
(debit - credit) - positive means a net debit balance, negative a net
credit balance - passed straight through as Tally's OPENINGBALANCE. This
is the correct convention for an asset-side ledger (e.g. Bank, Cash) but
has never been verified against a real Tally instance for a liability or
equity-side ledger; expect it may need a sign correction there after the
first live test, the same way every other new Tally feature in this
module has needed one round of fixing against real Tally output.
"""

import logging

from .tally_exceptions import TallyConfigurationError, TallyMappingError

_logger = logging.getLogger(__name__)

# Ignore balances smaller than this - float rounding noise, not a real
# opening balance worth pushing.
_ZERO_THRESHOLD = 0.005


class TallyOpeningBalanceService:
    """Computes and pushes opening balances for already-mapped accounts."""

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

    def compute_balances(self, connection, as_of_date):
        """
        For every account.account mapped to a Tally ledger with at least
        one posted move line on or before as_of_date, compute its balance
        (debit - credit) as of that date.

        Args:
            connection (tally.connection): scopes which company's accounts to consider
            as_of_date (date): cutoff date (inclusive)

        Returns:
            list[dict]: [{"account": account.account, "balance": float}, ...] -
                only accounts with a non-zero balance, ordered by account code
        """
        company = connection.company_id
        accounts = self.env["account.account"].search(
            [
                ("company_ids", "in", company.id),
                ("tally_ledger_name", "!=", False),
            ]
        )
        if not accounts:
            return []

        self.env.cr.execute(
            """
            SELECT aml.account_id, SUM(aml.debit) - SUM(aml.credit) AS balance
            FROM account_move_line aml
            JOIN account_move am ON am.id = aml.move_id
            WHERE aml.account_id = ANY(%s)
              AND am.company_id = %s
              AND am.state = 'posted'
              AND aml.date <= %s
            GROUP BY aml.account_id
            """,
            (accounts.ids, company.id, as_of_date),
        )
        balances = dict(self.env.cr.fetchall())

        rows = [
            {"account": account, "balance": balances[account.id]}
            for account in accounts
            if account.id in balances and abs(balances[account.id]) > _ZERO_THRESHOLD
        ]
        rows.sort(key=lambda row: row["account"].code or "")
        return rows

    def push_opening_balance(self, account, balance, company=None):
        """
        Push one account's opening balance to its mapped Tally ledger.

        Args:
            account (account.account): must have tally_ledger_name set
            balance (float): value to push (debit - credit convention - see
                module docstring)
            company (res.company): defaults to account's own company if not given

        Returns:
            dict: raw result from TallyClient.alter_ledger_opening_balance()

        Raises:
            TallyConfigurationError: No usable connection for the company
            TallyMappingError: account has no Tally ledger mapping
        """
        if not account.tally_ledger_name:
            raise TallyMappingError(
                f"Account '{account.display_name}' has no Tally Ledger Name mapping. "
                f"Set it on the account (Accounting > Chart of Accounts)."
            )

        company = company or account.company_ids[:1] or self.env.company
        connection = self._get_connection(company)
        client = connection._get_tally_client()

        result = client.alter_ledger_opening_balance(
            company=connection.tally_company_name or company.name,
            ledger_name=account.tally_ledger_name,
            opening_balance=balance,
        )

        if result.get("queued"):
            _logger.info(
                f"Opening balance push queued for async Tally Agent processing: account "
                f"'{account.display_name}'",
                extra={"account_id": account.id, "connection_id": connection.id, "job_id": result.get("job_id")},
            )
        elif result["success"]:
            _logger.info(
                f"Opening balance pushed to Tally for account '{account.display_name}': {balance:.2f}",
                extra={"account_id": account.id, "connection_id": connection.id},
            )
        else:
            _logger.warning(
                f"Opening balance push to Tally failed for account '{account.display_name}'",
                extra={"account_id": account.id, "connection_id": connection.id, "error": result.get("error")},
            )

        return result
