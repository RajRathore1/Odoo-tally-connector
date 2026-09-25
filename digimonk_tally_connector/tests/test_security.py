"""
Security review regression tests (see the module's security review findings):

1. tally.stock.adjustment.log must be company-isolated via its connection -
   a user restricted to one company must not see another company's log rows,
   the same way tally.connection itself is already isolated.
2. Tally Sync Operator must actually be able to open/use the sync wizards
   its own group description promises ("execute synchronization jobs").
3. Tally Admin must not be able to delete stock adjustment log rows - the
   audit trail must survive the same role whose actions it records.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase


class TestTallyMultiCompanyIsolation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

        self.company_a = self.env["res.company"].create({"name": "Tally Security Test Co A"})
        self.company_b = self.env["res.company"].create({"name": "Tally Security Test Co B"})

        self.connection_a = self.env["tally.connection"].create(
            {"name": "Conn A", "company_id": self.company_a.id, "host": "localhost", "port": 9001}
        )
        self.connection_b = self.env["tally.connection"].create(
            {"name": "Conn B", "company_id": self.company_b.id, "host": "localhost", "port": 9002}
        )

        self.product = self.env["product.product"].create({"name": "Cross-Company Item", "uom_id": self.uom_units.id})

        self.log_a = self.env["tally.stock.adjustment.log"].create(
            {
                "connection_id": self.connection_a.id,
                "product_id": self.product.id,
                "direction": "odoo_to_tally",
                "target_qty": 5.0,
                "success": True,
            }
        )
        self.log_b = self.env["tally.stock.adjustment.log"].create(
            {
                "connection_id": self.connection_b.id,
                "product_id": self.product.id,
                "direction": "odoo_to_tally",
                "target_qty": 9.0,
                "success": True,
            }
        )

        self.user_a = self.env["res.users"].create(
            {
                "name": "Company A Tally Admin",
                "login": "tally_security_test_user_a",
                "company_id": self.company_a.id,
                "company_ids": [(6, 0, [self.company_a.id])],
                "group_ids": [(4, self.env.ref("odoo_tally_connector.group_tally_admin").id)],
            }
        )

    def test_user_cannot_read_other_companys_adjustment_log(self):
        Log = self.env["tally.stock.adjustment.log"].with_user(self.user_a)
        visible = Log.search([("id", "in", [self.log_a.id, self.log_b.id])])

        self.assertIn(self.log_a, visible)
        self.assertNotIn(self.log_b, visible)

    def test_user_cannot_browse_other_companys_log_by_id(self):
        with self.assertRaises(AccessError):
            self.env["tally.stock.adjustment.log"].with_user(self.user_a).browse(self.log_b.id).success


class TestTallyAgentMultiCompanyIsolation(TransactionCase):
    """Phase 6 security review: tally.agent.device and tally.agent.job need
    the same company isolation as tally.connection/tally.stock.adjustment.log -
    initially shipped (Phases 1-4) without it."""

    def setUp(self):
        super().setUp()
        self.company_a = self.env["res.company"].create({"name": "Agent Security Test Co A"})
        self.company_b = self.env["res.company"].create({"name": "Agent Security Test Co B"})

        self.connection_a = self.env["tally.connection"].create(
            {
                "name": "Agent Conn A",
                "company_id": self.company_a.id,
                "host": "localhost",
                "port": 9001,
                "connection_mode": "agent",
            }
        )
        self.connection_b = self.env["tally.connection"].create(
            {
                "name": "Agent Conn B",
                "company_id": self.company_b.id,
                "host": "localhost",
                "port": 9002,
                "connection_mode": "agent",
            }
        )

        self.env["tally.agent.device"].generate(self.connection_a)
        self.env["tally.agent.device"].generate(self.connection_b)
        self.device_a = self.connection_a.agent_device_ids
        self.device_b = self.connection_b.agent_device_ids

        self.job_a = self.env["tally.agent.job"].enqueue(self.connection_a, "<ENVELOPE>a</ENVELOPE>")
        self.job_b = self.env["tally.agent.job"].enqueue(self.connection_b, "<ENVELOPE>b</ENVELOPE>")

        self.user_a = self.env["res.users"].create(
            {
                "name": "Agent Security Company A Admin",
                "login": "agent_security_test_user_a",
                "company_id": self.company_a.id,
                "company_ids": [(6, 0, [self.company_a.id])],
                "group_ids": [(4, self.env.ref("odoo_tally_connector.group_tally_admin").id)],
            }
        )

    def test_user_cannot_read_other_companys_agent_device(self):
        Device = self.env["tally.agent.device"].with_user(self.user_a)
        visible = Device.search([("id", "in", [self.device_a.id, self.device_b.id])])

        self.assertIn(self.device_a, visible)
        self.assertNotIn(self.device_b, visible)

    def test_user_cannot_read_other_companys_agent_job(self):
        Job = self.env["tally.agent.job"].with_user(self.user_a)
        visible = Job.search([("id", "in", [self.job_a.id, self.job_b.id])])

        self.assertIn(self.job_a, visible)
        self.assertNotIn(self.job_b, visible)


class TestTallySyncOperatorWizardAccess(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.operator = self.env["res.users"].create(
            {
                "name": "Tally Sync Operator Test User",
                "login": "tally_security_test_operator",
                "company_id": self.company.id,
                "company_ids": [(6, 0, [self.company.id])],
                "group_ids": [(4, self.env.ref("odoo_tally_connector.group_tally_operator").id)],
            }
        )
        self.connection = self.env["tally.connection"].create(
            {"name": "Operator Access Test Connection", "company_id": self.company.id, "host": "localhost", "port": 9000}
        )

    def test_operator_can_create_import_wizards(self):
        # Each of these must not raise AccessError for a Sync Operator -
        # before the fix, none of them had any group_tally_operator ACL row.
        wizard_models = [
            "tally.product.import.wizard",
            "tally.partner.import.wizard",
            "tally.invoice.import.wizard",
            "tally.bill.import.wizard",
            "tally.note.import.wizard",
            "tally.payment.import.wizard",
            "tally.stock.reconciliation.wizard",
        ]
        for model_name in wizard_models:
            wizard = self.env[model_name].with_user(self.operator).create({"connection_id": self.connection.id})
            self.assertTrue(wizard, f"Operator could not create {model_name}")

    def test_operator_can_read_but_not_write_connections(self):
        Connection = self.env["tally.connection"].with_user(self.operator)
        self.assertTrue(Connection.search([("id", "=", self.connection.id)]))
        with self.assertRaises(AccessError):
            self.connection.with_user(self.operator).write({"host": "elsewhere"})


class TestTallyStockAdjustmentLogAuditIntegrity(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.uom_units = self.env["uom.uom"].search([("name", "=ilike", "Units")], limit=1)
        if not self.uom_units:
            self.uom_units = self.env.ref("uom.product_uom_unit")

        self.admin = self.env["res.users"].create(
            {
                "name": "Tally Admin Test User",
                "login": "tally_security_test_admin",
                "company_id": self.company.id,
                "company_ids": [(6, 0, [self.company.id])],
                "group_ids": [(4, self.env.ref("odoo_tally_connector.group_tally_admin").id)],
            }
        )
        self.connection = self.env["tally.connection"].create(
            {"name": "Audit Integrity Test Connection", "company_id": self.company.id, "host": "localhost", "port": 9000}
        )
        self.product = self.env["product.product"].create({"name": "Audited Item", "uom_id": self.uom_units.id})
        self.log = self.env["tally.stock.adjustment.log"].create(
            {
                "connection_id": self.connection.id,
                "product_id": self.product.id,
                "direction": "odoo_to_tally",
                "target_qty": 3.0,
                "success": True,
            }
        )

    def test_tally_admin_cannot_delete_adjustment_log(self):
        with self.assertRaises(AccessError):
            self.log.with_user(self.admin).unlink()
