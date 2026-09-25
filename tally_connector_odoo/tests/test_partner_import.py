"""
Tests for Tally -> Odoo customer import (reverse sync direction).

Mirrors test_product_import.py's structure. Covers the scope-limiting
filter (only "Sundry Debtors" group ledgers are imported).
"""

from unittest.mock import patch

from odoo.tests import TransactionCase


class TestTallyPartnerImportService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Partner Import Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

    def _mock_fetch(self, ledgers):
        return {"success": True, "ledgers": ledgers, "message": "ok", "error": None}

    def test_import_creates_new_customer(self):
        from ..services import TallyPartnerImportService

        service = TallyPartnerImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"LedgerName": "New Customer Ltd", "LedgerGuid": "g-1", "LedgerParent": "Sundry Debtors"}]
            )
            result = service.import_partners(self.connection)

        self.assertTrue(result["success"])
        self.assertIn("New Customer Ltd", result["created"])
        partner = self.env["res.partner"].search([("name", "=", "New Customer Ltd")], limit=1)
        self.assertTrue(partner)
        self.assertEqual(partner.tally_guid, "g-1")
        self.assertGreater(partner.customer_rank, 0)

    def test_import_skips_non_customer_ledgers(self):
        """System ledgers (Cash, Bank, P&L) must never be imported as contacts."""
        from ..services import TallyPartnerImportService

        service = TallyPartnerImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [
                    {"LedgerName": "Cash", "LedgerGuid": "g-2", "LedgerParent": "Cash-in-Hand"},
                    {"LedgerName": "Profit & Loss A/c", "LedgerGuid": "g-3", "LedgerParent": "Primary"},
                ]
            )
            result = service.import_partners(self.connection)

        self.assertEqual(result["created"], [])
        self.assertEqual(result["updated"], [])
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Cash")]))

    def test_import_updates_existing_by_guid(self):
        from ..services import TallyPartnerImportService

        existing = self.env["res.partner"].create(
            {"name": "Old Customer Name", "customer_rank": 1, "tally_guid": "g-4"}
        )
        service = TallyPartnerImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"LedgerName": "Renamed Customer", "LedgerGuid": "g-4", "LedgerParent": "Sundry Debtors"}]
            )
            result = service.import_partners(self.connection)

        self.assertIn("Renamed Customer", result["updated"])
        self.assertEqual(existing.name, "Renamed Customer")

    def test_import_name_filter_imports_only_matching(self):
        from ..services import TallyPartnerImportService

        service = TallyPartnerImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [
                    {"LedgerName": "Alpha Corp", "LedgerGuid": "g-5", "LedgerParent": "Sundry Debtors"},
                    {"LedgerName": "Beta Corp", "LedgerGuid": "g-6", "LedgerParent": "Sundry Debtors"},
                ]
            )
            result = service.import_partners(self.connection, name_filter="Alpha Corp")

        self.assertEqual(result["created"], ["Alpha Corp"])

    def test_import_ambiguous_name_match_reports_error(self):
        from ..services import TallyPartnerImportService

        self.env["res.partner"].create({"name": "Dup Customer", "customer_rank": 1})
        self.env["res.partner"].create({"name": "Dup Customer", "customer_rank": 1})

        service = TallyPartnerImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"LedgerName": "Dup Customer", "LedgerGuid": "g-7", "LedgerParent": "Sundry Debtors"}]
            )
            result = service.import_partners(self.connection)

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Ambiguous", result["errors"][0]["error"])

    def test_import_creates_new_vendor_from_sundry_creditors(self):
        from ..services import TallyPartnerImportService

        service = TallyPartnerImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_ledgers"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"LedgerName": "New Vendor Ltd", "LedgerGuid": "g-8", "LedgerParent": "Sundry Creditors"}]
            )
            result = service.import_partners(self.connection)

        self.assertIn("New Vendor Ltd", result["created"])
        partner = self.env["res.partner"].search([("name", "=", "New Vendor Ltd")], limit=1)
        self.assertTrue(partner)
        self.assertGreater(partner.supplier_rank, 0)
        self.assertEqual(partner.customer_rank, 0)
