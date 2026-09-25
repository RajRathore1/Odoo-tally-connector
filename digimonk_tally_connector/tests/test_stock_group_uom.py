"""
Tests for Phase 11: Stock Group (product.category hierarchy) and Unit of
Measure master push.

Covers the two new XML builders (Stock Group upsert/list, Unit upsert), the
Stock Group list response parser, and the product sync service's two new
behaviors: passing a mapped category's tally_stock_group_name as the Stock
Item's PARENT, and best-effort (never-blocking) Unit master creation before
the Stock Item itself. Neither new master type has been verified against a
real Tally instance yet - see tally_product_sync_service.py's module
docstring.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase


class TestTallyStockGroupXmlBuilder(TransactionCase):
    def test_build_create_request_no_parent(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_stock_group_upsert_request(company="Digi", name="Electronics")
        self.assertIn('<STOCKGROUP NAME="Electronics" ACTION="Create">', xml)
        self.assertIn("<NAME>Electronics</NAME>", xml)
        self.assertNotIn("<PARENT>", xml)

    def test_build_create_request_with_parent(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_stock_group_upsert_request(
            company="Digi", name="Mobile Phones", parent_group="Electronics"
        )
        self.assertIn("<PARENT>Electronics</PARENT>", xml)

    def test_build_alter_request_with_rename(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_stock_group_upsert_request(
            company="Digi", name="New Name", action="Alter", old_name="Old Name"
        )
        self.assertIn("<OLDNAME>Old Name</OLDNAME>", xml)

    def test_escapes_special_characters(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_stock_group_upsert_request(company="Digi", name='X & "Y" <Z>')
        self.assertNotIn("<Z>", xml)
        self.assertIn("&amp;", xml)


class TestTallyStockGroupListResponseParser(TransactionCase):
    def test_parses_stock_groups(self):
        from ..services import TallyResponseParser

        xml = """<ENVELOPE><BODY><DATA><COLLECTION>
            <STOCKGROUP><NAME>Electronics</NAME><PARENT></PARENT></STOCKGROUP>
            <STOCKGROUP><NAME>Mobile Phones</NAME><PARENT>Electronics</PARENT></STOCKGROUP>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        result = TallyResponseParser.parse_stock_group_list_response(xml)
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 2)
        names = {row["StockGroupName"] for row in result["data"]}
        self.assertEqual(names, {"Electronics", "Mobile Phones"})


class TestTallyUnitXmlBuilder(TransactionCase):
    def test_build_create_request(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_unit_upsert_request(company="Digi", name="Units")
        self.assertIn('<UNIT NAME="Units" ACTION="Create">', xml)
        self.assertIn("<NAME>Units</NAME>", xml)
        self.assertIn("<ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>", xml)


class TestTallyStockItemWithParentGroup(TransactionCase):
    def test_parent_tag_present_when_given(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_stock_item_upsert_request(
            company="Digi", name="Widget", base_unit="Nos", guid="g-1", parent_group="Electronics",
        )
        self.assertIn("<PARENT>Electronics</PARENT>", xml)

    def test_parent_tag_absent_when_not_given(self):
        """Backward compatibility: omitting parent_group reproduces the exact prior XML shape."""
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_stock_item_upsert_request(
            company="Digi", name="Widget", base_unit="Nos", guid="g-1",
        )
        self.assertNotIn("<PARENT>", xml)


class TestTallyProductSyncServicePhase11(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Phase 11 Product Sync Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

    def test_passes_mapped_category_as_parent_group(self):
        from ..services import TallyProductSyncService

        category = self.env["product.category"].create(
            {"name": "Phase 11 Test Category", "tally_stock_group_name": "Electronics"}
        )
        product = self.env["product.product"].create(
            {"name": "Phase 11 Test Product", "type": "consu", "categ_id": category.id}
        )

        service = TallyProductSyncService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_stock_item"
        ) as mock_upsert, patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_unit"
        ) as mock_unit:
            mock_upsert.return_value = {"success": True, "created": True, "altered": False, "message": "ok", "error": None}
            mock_unit.return_value = {"success": True, "created": True, "altered": False, "message": "ok", "error": None}
            service.sync_product(product)

        self.assertEqual(mock_upsert.call_args.kwargs["parent_group"], "Electronics")
        mock_unit.assert_called_once()

    def test_unmapped_category_passes_no_parent_group(self):
        from ..services import TallyProductSyncService

        category = self.env["product.category"].create({"name": "Phase 11 Unmapped Category"})
        product = self.env["product.product"].create(
            {"name": "Phase 11 Unmapped Product", "type": "consu", "categ_id": category.id}
        )

        service = TallyProductSyncService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_stock_item"
        ) as mock_upsert, patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_unit"
        ):
            mock_upsert.return_value = {"success": True, "created": True, "altered": False, "message": "ok", "error": None}
            service.sync_product(product)

        self.assertIsNone(mock_upsert.call_args.kwargs["parent_group"])

    def test_unit_ensure_failure_does_not_block_stock_item_sync(self):
        """Best-effort Unit push: a failure/exception there must never block the Stock Item sync."""
        from ..services import TallyProductSyncService

        product = self.env["product.product"].create({"name": "Phase 11 Unit Failure Product", "type": "consu"})

        service = TallyProductSyncService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_stock_item"
        ) as mock_upsert, patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_unit"
        ) as mock_unit:
            mock_upsert.return_value = {"success": True, "created": True, "altered": False, "message": "ok", "error": None}
            mock_unit.side_effect = Exception("boom")
            result = service.sync_product(product)

        self.assertTrue(result["success"])
        self.assertEqual(product.tally_sync_status, "success")
