"""
Tests for the Account/Tax/Journal <-> Tally Ledger mapping suggestion
service. Covers: unambiguous exact-name matches get auto-filled, already-
mapped records are left untouched, and a Tally-side name collision (two
Tally ledgers whose names only differ by case) is reported as ambiguous
rather than guessed.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase


class TestTallyMappingSuggestionService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Mapping Suggestion Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

    def _mock_fetch(self, ledger_names):
        return {
            "success": True,
            "ledgers": [{"LedgerName": name, "LedgerGuid": "", "LedgerParent": ""} for name in ledger_names],
            "message": "ok",
            "error": None,
        }

    def test_applies_unambiguous_exact_match(self):
        from ..services import TallyMappingSuggestionService

        account = self.env["account.account"].create(
            {"code": "TMAP1", "name": "Sales Account", "account_type": "income"}
        )
        tax = self.env["account.tax"].create(
            {"name": "Output CGST", "amount": 9.0, "type_tax_use": "sale"}
        )

        service = TallyMappingSuggestionService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(["Sales Account", "Output CGST"])
            result = service.suggest_mappings(self.connection)

        self.assertTrue(result["success"])
        self.assertIn(account.display_name, result["applied"]["account.account"])
        self.assertIn(tax.display_name, result["applied"]["account.tax"])
        self.assertEqual(account.tally_ledger_name, "Sales Account")
        self.assertEqual(tax.tally_ledger_name, "Output CGST")
        self.assertEqual(result["skipped_ambiguous"], [])

    def test_leaves_already_mapped_records_untouched(self):
        from ..services import TallyMappingSuggestionService

        account = self.env["account.account"].create(
            {
                "code": "TMAP2",
                "name": "Purchase Account",
                "account_type": "expense",
                "tally_ledger_name": "Manually Mapped Ledger",
            }
        )

        service = TallyMappingSuggestionService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(["Purchase Account"])
            service.suggest_mappings(self.connection)

        self.assertEqual(account.tally_ledger_name, "Manually Mapped Ledger")

    def test_skips_case_ambiguous_tally_ledger_names(self):
        from ..services import TallyMappingSuggestionService

        account = self.env["account.account"].create(
            {"code": "TMAP3", "name": "Cash", "account_type": "asset_cash"}
        )

        service = TallyMappingSuggestionService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            # Two Tally ledgers differing only by case - genuinely ambiguous.
            mock_fetch.return_value = self._mock_fetch(["Cash", "CASH"])
            result = service.suggest_mappings(self.connection)

        self.assertFalse(account.tally_ledger_name)
        self.assertEqual(len(result["skipped_ambiguous"]), 1)

    def test_no_match_leaves_field_empty_without_error(self):
        from ..services import TallyMappingSuggestionService

        account = self.env["account.account"].create(
            {"code": "TMAP4", "name": "Unmatched Account", "account_type": "expense"}
        )

        service = TallyMappingSuggestionService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(["Some Other Ledger"])
            result = service.suggest_mappings(self.connection)

        self.assertTrue(result["success"])
        self.assertFalse(account.tally_ledger_name)
        self.assertNotIn(account.display_name, result["applied"]["account.account"])

    def _mock_fetch_stock_groups(self, group_names):
        return {
            "success": True,
            "stock_groups": [{"StockGroupName": name, "StockGroupParent": ""} for name in group_names],
            "message": "ok",
            "error": None,
        }

    def test_applies_unambiguous_stock_group_match(self):
        """Phase 11: product.category is matched against Tally's own Stock Group list."""
        from ..services import TallyMappingSuggestionService

        category = self.env["product.category"].create({"name": "Electronics"})

        service = TallyMappingSuggestionService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_ledgers, patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_groups"
        ) as mock_groups:
            mock_ledgers.return_value = self._mock_fetch([])
            mock_groups.return_value = self._mock_fetch_stock_groups(["Electronics"])
            result = service.suggest_mappings(self.connection)

        self.assertTrue(result["success"])
        self.assertEqual(category.tally_stock_group_name, "Electronics")
        self.assertIn(category.display_name, result["applied"]["product.category"])

    def test_stock_group_fetch_failure_does_not_block_ledger_suggestions(self):
        """A Stock Group fetch failure must not stop account/tax/journal suggestions from applying."""
        from ..services import TallyMappingSuggestionService

        account = self.env["account.account"].create(
            {"code": "TMAP5", "name": "Fallback Account", "account_type": "expense"}
        )

        service = TallyMappingSuggestionService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_ledgers, patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_groups"
        ) as mock_groups:
            mock_ledgers.return_value = self._mock_fetch(["Fallback Account"])
            mock_groups.return_value = {"success": False, "stock_groups": [], "message": "timeout", "error": "timeout"}
            result = service.suggest_mappings(self.connection)

        self.assertTrue(result["success"])
        self.assertEqual(account.tally_ledger_name, "Fallback Account")
