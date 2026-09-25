"""
Tests for stock quantity reconciliation (detection-only - see
tally_stock_reconciliation_service.py's module docstring for why this
never auto-corrects anything).

Covers: the comparison service itself (linked products matched by GUID,
unlinked Tally items skipped), the scheduled threshold-alert cron (only
flags products past the connection's threshold, creates exactly one
activity per connection rather than one per product, and skips
connections with no responsible user configured), and the on-demand
wizard's refresh action.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase


class TestTallyStockReconciliationService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Stock Reconciliation Test Connection",
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

    def _mock_fetch(self, items):
        return {"success": True, "items": items, "message": "ok", "error": None}

    def test_compares_linked_products_only(self):
        from ..services import TallyStockReconciliationService

        linked = self.env["product.product"].create(
            {"name": "Linked Product", "uom_id": self.uom_units.id, "tally_guid": "guid-1"}
        )

        service = TallyStockReconciliationService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_items"
        ) as mock_fetch:
            mock_fetch.return_value = self._mock_fetch(
                [
                    {"StockItemName": "Linked Product", "StockItemGuid": "guid-1", "StockItemClosingQty": 90.0},
                    {"StockItemName": "Unlinked Product", "StockItemGuid": "guid-2", "StockItemClosingQty": 10.0},
                ]
            )
            result = service.reconcile(self.connection)

        self.assertTrue(result["success"])
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["product"], linked)
        self.assertEqual(row["tally_qty"], 90.0)
        self.assertEqual(row["odoo_qty"], linked.qty_available)
        self.assertAlmostEqual(row["difference"], linked.qty_available - 90.0)

    def test_fetch_failure_reports_error_not_silent(self):
        from ..services import TallyStockReconciliationService

        service = TallyStockReconciliationService(self.env)
        with patch(
            "odoo.addons.odoo_tally_connector.services.tally_client.TallyClient.fetch_stock_items"
        ) as mock_fetch:
            mock_fetch.return_value = {
                "success": False,
                "items": [],
                "message": "Connection timed out",
                "error": "timeout",
            }
            result = service.reconcile(self.connection)

        self.assertFalse(result["success"])
        self.assertEqual(result["rows"], [])


class TestTallyStockMismatchCron(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.responsible = self.env.ref("base.user_admin")
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

        self.connection = self.env["tally.connection"].create(
            {
                "name": "Mismatch Alert Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
                "stock_mismatch_threshold": 5.0,
                "stock_alert_user_id": self.responsible.id,
            }
        )

    def _activities_for(self, connection):
        return self.env["mail.activity"].search(
            [("res_model", "=", "tally.connection"), ("res_id", "=", connection.id)]
        )

    def test_creates_one_activity_for_mismatches_past_threshold(self):
        from ..services import TallyStockReconciliationService

        with patch.object(TallyStockReconciliationService, "reconcile") as mock_reconcile:
            mock_reconcile.return_value = {
                "success": True,
                "rows": [
                    {"product": self._make_product("A"), "odoo_qty": 100.0, "tally_qty": 50.0, "difference": 50.0},
                    {"product": self._make_product("B"), "odoo_qty": 10.0, "tally_qty": 9.0, "difference": 1.0},
                ],
                "message": "ok",
                "error": None,
            }
            self.connection._check_stock_mismatch()

        activities = self._activities_for(self.connection)
        self.assertEqual(len(activities), 1)
        self.assertIn("Product A", activities.note)
        self.assertNotIn("Product B", activities.note)

    def test_no_activity_when_nothing_past_threshold(self):
        from ..services import TallyStockReconciliationService

        with patch.object(TallyStockReconciliationService, "reconcile") as mock_reconcile:
            mock_reconcile.return_value = {
                "success": True,
                "rows": [
                    {"product": self._make_product("C"), "odoo_qty": 10.0, "tally_qty": 9.0, "difference": 1.0},
                ],
                "message": "ok",
                "error": None,
            }
            self.connection._check_stock_mismatch()

        self.assertFalse(self._activities_for(self.connection))

    def test_cron_skips_connections_without_responsible_user(self):
        self.connection.stock_alert_user_id = False

        with patch(
            "odoo.addons.odoo_tally_connector.models.tally_connection.TallyConnection._check_stock_mismatch"
        ) as mock_check:
            self.env["tally.connection"]._cron_check_stock_mismatch()

        mock_check.assert_not_called()

    def test_reconcile_failure_does_not_raise(self):
        from ..services import TallyStockReconciliationService

        with patch.object(TallyStockReconciliationService, "reconcile") as mock_reconcile:
            mock_reconcile.return_value = {"success": False, "rows": [], "message": "timeout", "error": "timeout"}
            # Must not raise.
            self.connection._check_stock_mismatch()

        self.assertFalse(self._activities_for(self.connection))

    def _make_product(self, name):
        return self.env["product.product"].create({"name": f"Product {name}", "uom_id": self.uom_units.id})


class TestTallyStockReconciliationWizard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Wizard Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "enabled_for_sync": True,
                "active": True,
                "stock_mismatch_threshold": 5.0,
            }
        )
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

    def test_refresh_populates_lines_and_flags_mismatches(self):
        from ..services import TallyStockReconciliationService

        product = self.env["product.product"].create(
            {"name": "Wizard Product", "uom_id": self.uom_units.id, "tally_guid": "guid-1"}
        )
        wizard = self.env["tally.stock.reconciliation.wizard"].create({"connection_id": self.connection.id})

        with patch.object(TallyStockReconciliationService, "reconcile") as mock_reconcile:
            mock_reconcile.return_value = {
                "success": True,
                "rows": [
                    {"product": product, "odoo_qty": 100.0, "tally_qty": 50.0, "difference": 50.0},
                ],
                "message": "ok",
                "error": None,
            }
            wizard.action_refresh()

        self.assertEqual(len(wizard.line_ids), 1)
        self.assertTrue(wizard.line_ids.is_mismatch)
        self.assertEqual(wizard.mismatched_count, 1)
