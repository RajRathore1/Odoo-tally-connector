"""
Tests for the manual stock adjustment workflow (Option 4 of the
stock-mismatch pair - see tally_stock_adjustment_service.py's module
docstring for why every adjustment here is a deliberate, one-click human
decision rather than anything automatic).

Covers: TallyStockAdjustmentService.adjust_odoo_quantity (Inventory
Adjustment on the company's main warehouse), adjust_tally_quantity
(guards for inactive connection / unsynced product, and the happy path
calling TallyClient.upsert_stock_adjustment), and the two reconciliation
wizard line buttons that drive them end-to-end, including that every
attempt - success or failure - is written to tally.stock.adjustment.log.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase


class TestTallyStockAdjustmentService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Stock Adjustment Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
            }
        )
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

    def test_adjust_odoo_quantity_sets_qty_available(self):
        from ..services import TallyStockAdjustmentService

        product = self.env["product.product"].create(
            {"name": "Adjust Me", "type": "consu", "is_storable": True, "uom_id": self.uom_units.id}
        )

        service = TallyStockAdjustmentService(self.env)
        result = service.adjust_odoo_quantity(product, 42.0, self.company)

        self.assertTrue(result["success"])
        self.assertEqual(result["new_qty"], 42.0)
        self.assertAlmostEqual(product.qty_available, 42.0)

    def test_adjust_odoo_quantity_auto_fixes_non_storable_product(self):
        """
        Regression test: a Tally-linked product created before this connector
        started setting is_storable=True on import (real case hit live -
        Tally's "Carry" item) must not dead-end with Odoo's raw
        "Quants cannot be created for consumables or services." error.
        """
        from ..services import TallyStockAdjustmentService

        product = self.env["product.product"].create(
            {"name": "Legacy Non-Storable Item", "uom_id": self.uom_units.id, "tally_guid": "guid-legacy"}
        )
        self.assertFalse(product.is_storable)

        service = TallyStockAdjustmentService(self.env)
        result = service.adjust_odoo_quantity(product, 5.0, self.company)

        self.assertTrue(result["success"])
        self.assertTrue(product.is_storable)
        self.assertAlmostEqual(product.qty_available, 5.0)

    def test_adjust_odoo_quantity_no_warehouse_raises_mapping_error(self):
        from ..services import TallyStockAdjustmentService, TallyMappingError

        product = self.env["product.product"].create(
            {"name": "No Warehouse Product", "uom_id": self.uom_units.id}
        )
        other_company = self.env["res.company"].create({"name": "No Warehouse Co"})
        # res.company creation auto-provisions a default warehouse via the
        # stock module; archiving it (rather than unlinking, which cascades
        # into stock rules/picking types) is enough to make search() find
        # none, exercising the "no warehouse configured" guard.
        self.env["stock.warehouse"].search([("company_id", "=", other_company.id)]).write({"active": False})

        service = TallyStockAdjustmentService(self.env)
        with self.assertRaises(TallyMappingError):
            service.adjust_odoo_quantity(product, 10.0, other_company)

    def test_adjust_tally_quantity_requires_active_connection(self):
        from ..services import TallyStockAdjustmentService, TallyConfigurationError

        self.connection.active = False
        product = self.env["product.product"].create(
            {
                "name": "Synced Product",
                "uom_id": self.uom_units.id,
                "tally_guid": "guid-1",
                "tally_sync_status": "success",
            }
        )

        service = TallyStockAdjustmentService(self.env)
        with self.assertRaises(TallyConfigurationError):
            service.adjust_tally_quantity(self.connection, product, 10.0)

    def test_adjust_tally_quantity_requires_product_already_synced(self):
        from ..services import TallyStockAdjustmentService, TallyMappingError

        product = self.env["product.product"].create({"name": "Unsynced Product", "uom_id": self.uom_units.id})

        service = TallyStockAdjustmentService(self.env)
        with self.assertRaises(TallyMappingError):
            service.adjust_tally_quantity(self.connection, product, 10.0)

    def test_adjust_tally_quantity_calls_client_upsert(self):
        from ..services import TallyStockAdjustmentService

        product = self.env["product.product"].create(
            {
                "name": "Ready Product",
                "uom_id": self.uom_units.id,
                "tally_guid": "guid-1",
                "tally_sync_status": "success",
            }
        )

        service = TallyStockAdjustmentService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.upsert_stock_adjustment"
        ) as mock_upsert:
            mock_upsert.return_value = {"success": True, "message": "Created in Tally", "error": None}
            result = service.adjust_tally_quantity(self.connection, product, 25.0)

        self.assertTrue(result["success"])
        mock_upsert.assert_called_once()
        call_kwargs = mock_upsert.call_args.kwargs
        self.assertEqual(call_kwargs["stock_item_name"], "Ready Product")
        self.assertEqual(call_kwargs["quantity"], 25.0)
        self.assertEqual(call_kwargs["unit"], self.uom_units.name)


class TestTallyStockReconciliationWizardAdjustmentButtons(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Wizard Adjustment Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
                "stock_mismatch_threshold": 1.0,
            }
        )
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")
        self.product = self.env["product.product"].create(
            {
                "name": "Reconciled Product",
                "type": "consu",
                "is_storable": True,
                "uom_id": self.uom_units.id,
                "tally_guid": "guid-1",
                "tally_sync_status": "success",
            }
        )
        self.wizard = self.env["tally.stock.reconciliation.wizard"].create({"connection_id": self.connection.id})
        self.line = self.env["tally.stock.reconciliation.line"].create(
            {
                "wizard_id": self.wizard.id,
                "product_id": self.product.id,
                "odoo_qty": 100.0,
                "tally_qty": 90.0,
                "difference": 10.0,
                "is_mismatch": True,
            }
        )

    def _logs_for(self, product):
        return self.env["tally.stock.adjustment.log"].search([("product_id", "=", product.id)])

    def test_set_odoo_to_tally_qty_updates_stock_and_logs_success(self):
        self.line.action_set_odoo_to_tally_qty()

        self.assertAlmostEqual(self.product.qty_available, 90.0)
        self.assertAlmostEqual(self.line.odoo_qty, 90.0)
        self.assertAlmostEqual(self.line.difference, 0.0)
        self.assertFalse(self.line.is_mismatch)

        logs = self._logs_for(self.product)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs.direction, "tally_to_odoo")
        self.assertTrue(logs.success)

    def test_set_tally_to_odoo_qty_calls_service_and_logs_success(self):
        from ..services import TallyStockAdjustmentService

        with patch.object(TallyStockAdjustmentService, "adjust_tally_quantity") as mock_adjust:
            mock_adjust.return_value = {"success": True, "message": "Altered in Tally", "error": None}
            self.line.action_set_tally_to_odoo_qty()

        mock_adjust.assert_called_once_with(self.connection, self.product, 100.0)
        self.assertAlmostEqual(self.line.tally_qty, 100.0)
        self.assertAlmostEqual(self.line.difference, 0.0)
        self.assertFalse(self.line.is_mismatch)

        logs = self._logs_for(self.product)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs.direction, "odoo_to_tally")
        self.assertTrue(logs.success)

    def test_set_tally_to_odoo_qty_logs_failure_on_result_failure(self):
        from ..services import TallyStockAdjustmentService

        with patch.object(TallyStockAdjustmentService, "adjust_tally_quantity") as mock_adjust:
            mock_adjust.return_value = {"success": False, "message": "Tally rejected voucher", "error": "EXCEPTIONS=1"}
            self.line.action_set_tally_to_odoo_qty()

        # Tally-side qty must not be updated locally when the push failed.
        self.assertAlmostEqual(self.line.tally_qty, 90.0)
        self.assertTrue(self.line.is_mismatch)

        logs = self._logs_for(self.product)
        self.assertEqual(len(logs), 1)
        self.assertFalse(logs.success)
        self.assertEqual(logs.error_message, "EXCEPTIONS=1")

    def test_set_tally_to_odoo_qty_logs_failure_on_raised_error(self):
        from ..services import TallyStockAdjustmentService, TallyConfigurationError

        with patch.object(TallyStockAdjustmentService, "adjust_tally_quantity") as mock_adjust:
            mock_adjust.side_effect = TallyConfigurationError("Connection not usable.")
            self.line.action_set_tally_to_odoo_qty()

        self.assertAlmostEqual(self.line.tally_qty, 90.0)
        logs = self._logs_for(self.product)
        self.assertEqual(len(logs), 1)
        self.assertFalse(logs.success)
        self.assertEqual(logs.error_message, "Connection not usable.")
