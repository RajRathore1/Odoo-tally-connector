"""
Tests for Tally -> Odoo product import (reverse sync direction).

Covers:
- XML builder for the native stock item collection request
- Response parser for the native stock item collection response
- Import service matching strategy (GUID match, name-linking, ambiguous
  name detection, missing UOM mapping) using a mocked TallyClient.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase


class TestTallyXmlBuilderStockItemList(TransactionCase):
    def test_build_stock_item_list_request(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_stock_item_list_request(company="Digi")
        self.assertIn("TALLYREQUEST>EXPORT<", xml)
        self.assertIn("<TYPE>StockItem</TYPE>", xml)
        self.assertIn("Name, GUID, BaseUnits", xml)
        self.assertIn("<SVCURRENTCOMPANY>Digi</SVCURRENTCOMPANY>", xml)

    def test_build_stock_item_list_request_no_company(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_stock_item_list_request()
        self.assertNotIn("SVCURRENTCOMPANY", xml)


class TestTallyStockItemListParser(TransactionCase):
    def test_parse_stock_items(self):
        from ..services import TallyResponseParser

        xml = """<ENVELOPE>
            <BODY><DATA><COLLECTION>
                <STOCKITEM NAME="Screw"><NAME>Screw</NAME><GUID>abc-1</GUID><BASEUNITS>Units</BASEUNITS></STOCKITEM>
                <STOCKITEM NAME="Bolt"><NAME>Bolt</NAME><GUID>abc-2</GUID><BASEUNITS>Nos</BASEUNITS></STOCKITEM>
            </COLLECTION></DATA></BODY>
        </ENVELOPE>"""
        result = TallyResponseParser.parse_stock_item_list_response(xml)
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["data"][0]["StockItemName"], "Screw")
        self.assertEqual(result["data"][0]["StockItemGuid"], "abc-1")
        self.assertEqual(result["data"][0]["StockItemUnit"], "Units")

    def test_parse_error_response(self):
        from ..services import TallyResponseParser

        xml = "<ENVELOPE><LINEERROR>Company does not exist</LINEERROR></ENVELOPE>"
        result = TallyResponseParser.parse_stock_item_list_response(xml)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Company does not exist")

    def test_parse_empty_collection(self):
        from ..services import TallyResponseParser

        xml = "<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"
        result = TallyResponseParser.parse_stock_item_list_response(xml)
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)


class TestTallyProductImportService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Import Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )

    def _mock_fetch(self, items):
        return {"success": True, "items": items, "message": "ok", "error": None}

    def test_import_creates_new_product(self):
        from ..services import TallyProductImportService

        service = TallyProductImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_items"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"StockItemName": "New Import Product", "StockItemGuid": "guid-1", "StockItemUnit": self.uom_units.name}]
            )
            result = service.import_products(self.connection)

        self.assertTrue(result["success"])
        self.assertIn("New Import Product", result["created"])
        product = self.env["product.product"].search([("name", "=", "New Import Product")], limit=1)
        self.assertTrue(product)
        self.assertEqual(product.tally_guid, "guid-1")
        self.assertEqual(product.tally_sync_status, "success")
        # A Tally Stock Item is a physically tracked inventory item by
        # definition - Odoo must track its quantity, or reconciliation
        # against Tally's closing balance is meaningless (Odoo side
        # always reads 0 for a non-storable product).
        self.assertTrue(product.is_storable)

    def test_import_updates_existing_by_guid(self):
        from ..services import TallyProductImportService

        existing = self.env["product.product"].create(
            {"name": "Old Name", "uom_id": self.uom_units.id, "tally_guid": "guid-2"}
        )
        service = TallyProductImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_items"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"StockItemName": "Renamed In Tally", "StockItemGuid": "guid-2", "StockItemUnit": self.uom_units.name}]
            )
            result = service.import_products(self.connection)

        self.assertIn("Renamed In Tally", result["updated"])
        self.assertEqual(existing.name, "Renamed In Tally")

    def test_import_links_unlinked_product_by_name(self):
        from ..services import TallyProductImportService

        existing = self.env["product.product"].create(
            {"name": "Already In Odoo", "uom_id": self.uom_units.id}
        )
        self.assertFalse(existing.tally_guid)

        service = TallyProductImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_items"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"StockItemName": "Already In Odoo", "StockItemGuid": "guid-3", "StockItemUnit": self.uom_units.name}]
            )
            result = service.import_products(self.connection)

        self.assertIn("Already In Odoo", result["updated"])
        self.assertEqual(existing.tally_guid, "guid-3")

    def test_import_missing_uom_reports_error_not_silent_skip(self):
        from ..services import TallyProductImportService

        service = TallyProductImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_items"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"StockItemName": "No Uom Product", "StockItemGuid": "guid-4", "StockItemUnit": "NonExistentUnitXYZ"}]
            )
            result = service.import_products(self.connection)

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("NonExistentUnitXYZ", result["errors"][0]["error"])
        product = self.env["product.product"].search([("name", "=", "No Uom Product")], limit=1)
        self.assertFalse(product)

    def test_import_ambiguous_name_match_reports_error(self):
        from ..services import TallyProductImportService

        self.env["product.product"].create({"name": "Duplicate Name", "uom_id": self.uom_units.id})
        self.env["product.product"].create({"name": "Duplicate Name", "uom_id": self.uom_units.id})

        service = TallyProductImportService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_items"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [{"StockItemName": "Duplicate Name", "StockItemGuid": "guid-5", "StockItemUnit": self.uom_units.name}]
            )
            result = service.import_products(self.connection)

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Ambiguous", result["errors"][0]["error"])
