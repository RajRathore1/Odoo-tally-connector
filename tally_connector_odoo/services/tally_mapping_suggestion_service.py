"""
Account/Tax/Journal <-> Tally Ledger, and Stock Group <-> Tally Stock Group
mapping suggestion service.

Fetches Tally's ledger list once and, for every account.account, account.tax,
and account.journal record with no Tally Ledger Name set yet, fills it in
automatically IF exactly one Tally ledger matches that record's name
(case-insensitive, exact match) - a genuine, unambiguous suggestion, not a
guess. A name matching more than one Odoo record needing that same Tally
ledger, or a Tally ledger name that isn't unique among Tally's own ledgers,
is left unmapped and reported so the user maps it explicitly - mirrors this
project's established "never guess ambiguous matches" rule from every sync
service.

Phase 11: the same exact-match logic separately applies to product.category
<-> Tally's own Stock Group list - a distinct Tally collection from ledgers,
fetched and matched independently, but reported through the same
applied/skipped_ambiguous shape.
"""

import logging

_logger = logging.getLogger(__name__)

_MAPPABLE_MODELS = ("account.account", "account.tax", "account.journal")
_STOCK_GROUP_MODEL = "product.category"


class TallyMappingSuggestionService:
    """Suggests (auto-fills unambiguous) Tally Ledger/Stock Group Name mappings from Tally's own master lists."""

    def __init__(self, env):
        self.env = env

    @staticmethod
    def _build_name_index(names):
        index = {}
        for name in names:
            name = (name or "").strip()
            if not name:
                continue
            index.setdefault(name.lower(), set()).add(name)
        return index

    def _suggest_for_index(self, model, field_name, name_index, applied, skipped_ambiguous):
        records = self.env[model].search([(field_name, "=", False)])
        for record in records:
            record_name = (record.name or "").strip()
            if not record_name:
                continue
            matches = name_index.get(record_name.lower())
            if not matches:
                continue
            if len(matches) > 1:
                skipped_ambiguous.append(
                    f"{record.display_name} ({model}): {len(matches)} Tally entries named "
                    f"'{record_name}' with different casing - map manually."
                )
                continue
            record[field_name] = next(iter(matches))
            applied[model].append(record.display_name)

    def suggest_mappings(self, connection):
        """
        Fetch Tally's ledger list and Stock Group list, then auto-fill
        unambiguous name matches on unmapped accounts, taxes, journals, and
        product categories.

        Args:
            connection (tally.connection): source connection

        Returns:
            dict: {
                "success": bool,
                "applied": {"account.account": [names], "account.tax": [names],
                    "account.journal": [names], "product.category": [names]},
                "skipped_ambiguous": [str],
                "message": str,
                "error": str or None,
            }
        """
        client = connection._get_tally_client()
        result = client.fetch_ledgers(company=connection.tally_company_name)

        if not result["success"]:
            return {
                "success": False,
                "applied": {},
                "skipped_ambiguous": [],
                "message": result.get("message", "Failed to fetch ledgers from Tally"),
                "error": result.get("error"),
            }

        ledger_index = self._build_name_index(ledger.get("LedgerName") for ledger in result["ledgers"])

        applied = {model: [] for model in _MAPPABLE_MODELS}
        applied[_STOCK_GROUP_MODEL] = []
        skipped_ambiguous = []

        for model in _MAPPABLE_MODELS:
            self._suggest_for_index(model, "tally_ledger_name", ledger_index, applied, skipped_ambiguous)

        stock_group_result = client.fetch_stock_groups(company=connection.tally_company_name)
        if stock_group_result["success"]:
            stock_group_index = self._build_name_index(
                group.get("StockGroupName") for group in stock_group_result["stock_groups"]
            )
            self._suggest_for_index(
                _STOCK_GROUP_MODEL, "tally_stock_group_name", stock_group_index, applied, skipped_ambiguous
            )
        else:
            _logger.warning(
                "Could not fetch Tally's Stock Group list while suggesting mappings - "
                "account/tax/journal suggestions still applied, product.category skipped: %s",
                stock_group_result.get("error"),
            )

        total_applied = sum(len(v) for v in applied.values())
        message = f"Applied {total_applied} mapping(s), {len(skipped_ambiguous)} ambiguous match(es) skipped"
        return {
            "success": True,
            "applied": applied,
            "skipped_ambiguous": skipped_ambiguous,
            "message": message,
            "error": None,
        }
