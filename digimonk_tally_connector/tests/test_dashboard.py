"""
Tests for the Tally Sync Dashboard - a read-only aggregation screen (see
models/tally_dashboard.py's module docstring for why it never invents new
sync logic of its own).

Covers: action_open_dashboard (what the menu actually calls) creates and
immediately populates a wizard so the dashboard never opens blank,
action_refresh populates connection/category/error lines correctly
scoped by company and category, drill-down buttons return the right
model+domain, and "Retry Failed Now" re-runs _sync_pending_queue for
every active+enabled connection (and only those) without raising if one
connection's retry fails.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase

from ..models.tally_connection import TallyConnection


class TestTallyDashboard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Dashboard Test Connection",
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

    def test_action_open_dashboard_creates_and_populates(self):
        """This is what the Dashboard menu itself calls (a server action) -
        it must hand back an already-populated, already-saved wizard, not
        an empty unsaved one the user has to manually Refresh."""
        product = self.env["product.product"].create(
            {
                "name": "Dashboard Menu-Open Product",
                "uom_id": self.uom_units.id,
                "tally_sync_status": "failed",
                "tally_last_sync_error": "Connection refused",
            }
        )

        action = self.env["tally.dashboard.wizard"].action_open_dashboard()
        wizard = self.env["tally.dashboard.wizard"].browse(action["res_id"])

        self.assertTrue(isinstance(wizard.id, int))
        self.assertIn(self.connection, wizard.connection_line_ids.connection_id)
        self.assertIn(product.display_name, wizard.error_line_ids.mapped("record_name"))

    def test_action_refresh_populates_lines(self):
        product = self.env["product.product"].create(
            {
                "name": "Dashboard Failed Product",
                "uom_id": self.uom_units.id,
                "tally_sync_status": "failed",
                "tally_last_sync_error": "Connection refused",
            }
        )

        wizard = self.env["tally.dashboard.wizard"].create({})
        wizard.action_refresh()

        self.assertIn(self.connection, wizard.connection_line_ids.connection_id)
        product_lines = wizard.category_line_ids.filtered(lambda l: l.category == "product")
        self.assertTrue(product_lines)
        self.assertGreaterEqual(sum(product_lines.mapped("failed_count")), 1)
        self.assertIn(product.display_name, wizard.error_line_ids.mapped("record_name"))

    def test_totals_sum_category_lines(self):
        self.env["product.product"].create(
            {"name": "Dashboard Pending Product", "uom_id": self.uom_units.id, "tally_sync_status": "draft"}
        )
        self.env["res.partner"].create({"name": "Dashboard Failed Partner", "tally_sync_status": "failed"})

        wizard = self.env["tally.dashboard.wizard"].create({})
        wizard.action_refresh()

        self.assertEqual(wizard.total_pending, sum(wizard.category_line_ids.mapped("pending_count")))
        self.assertEqual(wizard.total_failed, sum(wizard.category_line_ids.mapped("failed_count")))
        self.assertGreaterEqual(wizard.total_pending, 1)
        self.assertGreaterEqual(wizard.total_failed, 1)

    def test_view_failed_returns_correct_model_and_domain(self):
        self.env["res.partner"].create({"name": "Dashboard Domain Test Partner", "tally_sync_status": "failed"})
        wizard = self.env["tally.dashboard.wizard"].create({})
        wizard.action_refresh()

        partner_line = wizard.category_line_ids.filtered(lambda l: l.category == "partner")
        self.assertTrue(partner_line)
        action = partner_line[0].action_view_failed()

        self.assertEqual(action["res_model"], "res.partner")
        self.assertIn(("tally_sync_status", "=", "failed"), action["domain"])
        self.assertIn(("company_id", "in", [self.company.id, False]), action["domain"])

    def test_view_pending_only_returns_pending_status(self):
        self.env["product.product"].create(
            {"name": "Dashboard Pending Only Product", "uom_id": self.uom_units.id, "tally_sync_status": "draft"}
        )
        wizard = self.env["tally.dashboard.wizard"].create({})
        wizard.action_refresh()

        product_line = wizard.category_line_ids.filtered(lambda l: l.category == "product")
        self.assertTrue(product_line)
        action = product_line[0].action_view_pending()

        self.assertIn(("tally_sync_status", "=", "draft"), action["domain"])

    def test_open_record_from_error_line(self):
        product = self.env["product.product"].create(
            {
                "name": "Dashboard Error Record Product",
                "uom_id": self.uom_units.id,
                "tally_sync_status": "failed",
                "tally_last_sync_error": "Timeout",
            }
        )
        wizard = self.env["tally.dashboard.wizard"].create({})
        wizard.action_refresh()

        error_line = wizard.error_line_ids.filtered(lambda l: l.record_name == product.display_name)
        self.assertTrue(error_line)
        action = error_line[0].action_open_record()

        self.assertEqual(action["res_model"], "product.product")
        self.assertEqual(action["res_id"], product.id)

    def test_retry_all_failed_calls_sync_queue_for_active_enabled_connections_only(self):
        disabled_connection = self.env["tally.connection"].create(
            {
                "name": "Disabled Dashboard Connection",
                "company_id": self.env["res.company"].create({"name": "Dashboard Retry Test Co"}).id,
                "host": "localhost",
                "port": 9001,
                "enabled_for_sync": False,
                "active": True,
            }
        )

        called_on = []

        def _recording_sync_pending_queue(self, auto_commit=True):
            called_on.append(self.id)

        with patch.object(TallyConnection, "_sync_pending_queue", new=_recording_sync_pending_queue):
            wizard = self.env["tally.dashboard.wizard"].create({})
            wizard.action_retry_all_failed()

        self.assertIn(self.connection.id, called_on)
        self.assertNotIn(disabled_connection.id, called_on)

    def test_retry_all_failed_does_not_raise_if_one_connection_errors(self):
        def _raising_sync_pending_queue(self, auto_commit=True):
            raise Exception("boom")

        with patch.object(TallyConnection, "_sync_pending_queue", new=_raising_sync_pending_queue):
            wizard = self.env["tally.dashboard.wizard"].create({})
            # Must not raise.
            wizard.action_retry_all_failed()
