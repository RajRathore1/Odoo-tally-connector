"""
Tests for Phase 3: Customer (Ledger) synchronization - push direction.

Mirrors test_product_sync.py's structure and coverage.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase

from ..services import TallyMappingError, TallyConfigurationError


class TestTallyXmlBuilderLedger(TransactionCase):
    def test_build_create_request(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_ledger_upsert_request(
            company="Digi",
            name="Test Customer",
            parent_group="Sundry Debtors",
            guid="abc-123",
            action="Create",
        )
        self.assertIn('ACTION="Create"', xml)
        self.assertIn("<GUID>abc-123</GUID>", xml)
        self.assertIn("<PARENT>Sundry Debtors</PARENT>", xml)
        self.assertNotIn("OLDNAME", xml)

    def test_build_alter_request_with_rename(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_ledger_upsert_request(
            company="Digi",
            name="New Name",
            parent_group="Sundry Debtors",
            guid="abc-123",
            action="Alter",
            old_name="Old Name",
        )
        self.assertIn("<OLDNAME>Old Name</OLDNAME>", xml)

    def test_build_with_address_and_contact(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_ledger_upsert_request(
            company="Digi",
            name="Test Customer",
            parent_group="Sundry Debtors",
            guid="abc-123",
            action="Create",
            address_lines=["123 Main St", "Suite 4"],
            phone="1234567890",
            email="test@example.com",
        )
        self.assertIn("123 Main St", xml)
        self.assertIn("Suite 4", xml)
        self.assertIn("<LEDGERPHONE>1234567890</LEDGERPHONE>", xml)
        self.assertIn("<EMAIL>test@example.com</EMAIL>", xml)

    def test_escapes_special_characters(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_ledger_upsert_request(
            company="Digi",
            name='Customer & "Special" <Corp>',
            parent_group="Sundry Debtors",
            guid="abc-123",
            action="Create",
        )
        self.assertNotIn("<Corp>", xml)
        self.assertIn("&amp;", xml)


class TestTallyLedgerListParser(TransactionCase):
    def test_parse_ledgers(self):
        from ..services import TallyResponseParser

        xml = """<ENVELOPE>
            <BODY><DATA><COLLECTION>
                <LEDGER NAME="ABC Corp"><NAME>ABC Corp</NAME><GUID>l-1</GUID><PARENT>Sundry Debtors</PARENT></LEDGER>
            </COLLECTION></DATA></BODY>
        </ENVELOPE>"""
        result = TallyResponseParser.parse_ledger_list_response(xml)
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["data"][0]["LedgerName"], "ABC Corp")
        self.assertEqual(result["data"][0]["LedgerParent"], "Sundry Debtors")

    def test_parse_error_response(self):
        from ..services import TallyResponseParser

        xml = "<ENVELOPE><LINEERROR>Company does not exist</LINEERROR></ENVELOPE>"
        result = TallyResponseParser.parse_ledger_list_response(xml)
        self.assertFalse(result["success"])


class TestTallyPartnerSyncService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.customer = self.env["res.partner"].create(
            {"name": "Sync Test Customer", "customer_rank": 1}
        )

    def test_sync_key_is_deterministic(self):
        from ..services import TallyPartnerSyncService

        service = TallyPartnerSyncService(self.env)
        key1 = service._compute_sync_key(self.customer)
        key2 = service._compute_sync_key(self.customer)
        self.assertEqual(key1, key2)

    def test_sync_non_customer_raises_mapping_error(self):
        from ..services import TallyPartnerSyncService

        non_customer = self.env["res.partner"].create({"name": "Not A Customer"})
        service = TallyPartnerSyncService(self.env)
        with self.assertRaises(TallyMappingError):
            service.sync_partner(non_customer)

    def test_sync_without_connection_raises_configuration_error(self):
        from ..services import TallyPartnerSyncService

        service = TallyPartnerSyncService(self.env)
        with self.assertRaises(TallyConfigurationError):
            service.sync_partner(self.customer)

    def test_sync_create_then_alter(self):
        from ..services import TallyPartnerSyncService

        self.env["tally.connection"].create(
            {
                "name": "Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )
        service = TallyPartnerSyncService(self.env)

        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_ledger"
        ) as mock_upsert:
            mock_upsert.return_value = {
                "success": True,
                "created": True,
                "altered": False,
                "message": "Created in Tally",
                "error": None,
            }
            result = service.sync_partner(self.customer)

            self.assertTrue(result["success"])
            self.assertEqual(self.customer.tally_sync_status, "success")
            self.assertTrue(self.customer.tally_guid)
            self.assertEqual(mock_upsert.call_args.kwargs["action"], "Create")
            self.assertEqual(mock_upsert.call_args.kwargs["parent_group"], "Sundry Debtors")

            mock_upsert.return_value = {
                "success": True,
                "created": False,
                "altered": True,
                "message": "Altered in Tally",
                "error": None,
            }
            first_key = self.customer.tally_sync_key
            service.sync_partner(self.customer)
            self.assertEqual(mock_upsert.call_args.kwargs["action"], "Alter")
            self.assertEqual(self.customer.tally_sync_key, first_key)

    def test_sync_failure_records_error_without_marking_success(self):
        from ..services import TallyPartnerSyncService

        self.env["tally.connection"].create(
            {
                "name": "Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )
        service = TallyPartnerSyncService(self.env)

        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_ledger"
        ) as mock_upsert:
            mock_upsert.return_value = {
                "success": False,
                "created": False,
                "altered": False,
                "message": "Tally import failed: Group does not exist",
                "error": "Group does not exist",
            }
            result = service.sync_partner(self.customer)

            self.assertFalse(result["success"])
            self.assertEqual(self.customer.tally_sync_status, "failed")
            self.assertFalse(self.customer.tally_guid)


class TestTallyPartnerSyncServiceVendor(TransactionCase):
    """Vendor sync uses the same service, resolved to a different Ledger Group."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.vendor = self.env["res.partner"].create(
            {"name": "Sync Test Vendor", "supplier_rank": 1}
        )
        self.env["tally.connection"].create(
            {
                "name": "Vendor Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

    def test_vendor_resolves_to_sundry_creditors(self):
        from ..services import TallyPartnerSyncService
        from ..services.tally_partner_sync_service import VENDOR_LEDGER_GROUP

        service = TallyPartnerSyncService(self.env)
        self.assertEqual(service._resolve_ledger_group(self.vendor), VENDOR_LEDGER_GROUP)

    def test_customer_and_vendor_both_set_defaults_to_customer_group(self):
        """Documented default: a partner marked as both is treated as a customer."""
        from ..services import TallyPartnerSyncService
        from ..services.tally_partner_sync_service import CUSTOMER_LEDGER_GROUP

        both = self.env["res.partner"].create(
            {"name": "Both Customer And Vendor", "customer_rank": 1, "supplier_rank": 1}
        )
        service = TallyPartnerSyncService(self.env)
        self.assertEqual(service._resolve_ledger_group(both), CUSTOMER_LEDGER_GROUP)

    def test_sync_vendor_uses_creditors_group_in_request(self):
        from ..services import TallyPartnerSyncService
        from ..services.tally_partner_sync_service import VENDOR_LEDGER_GROUP

        service = TallyPartnerSyncService(self.env)

        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_ledger"
        ) as mock_upsert:
            mock_upsert.return_value = {
                "success": True,
                "created": True,
                "altered": False,
                "message": "Created in Tally",
                "error": None,
            }
            result = service.sync_partner(self.vendor)

            self.assertTrue(result["success"])
            self.assertEqual(mock_upsert.call_args.kwargs["parent_group"], VENDOR_LEDGER_GROUP)
            self.assertEqual(self.vendor.tally_sync_status, "success")
