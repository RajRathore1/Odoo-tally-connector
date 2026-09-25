"""
Tests for Phase 10: Opening Balance push.

Covers the XML builder, the compute_balances() aggregation (pure Odoo-side
SQL, no Tally client needed - safe to test directly against real posted
moves), and push_opening_balance()'s early validation gate (missing
mapping). Deep Tally-dependent paths are documented in the service's
docstring and exercised manually against a real Tally instance, matching
every other sync service's testing philosophy in this module.
"""

from odoo.tests import TransactionCase

from ..services import TallyMappingError


class TestTallyLedgerOpeningBalanceXmlBuilder(TransactionCase):
    def test_build_alter_request_structure(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_ledger_opening_balance_alter_request(
            company="Digi",
            ledger_name="Bank",
            opening_balance=1500.0,
        )
        self.assertIn('ACTION="Alter"', xml)
        self.assertIn('NAME="Bank"', xml)
        self.assertIn("<OPENINGBALANCE>1500.00</OPENINGBALANCE>", xml)
        # Deliberately minimal - no GUID/PARENT resent for an existing ledger.
        self.assertNotIn("<GUID>", xml)
        self.assertNotIn("<PARENT>", xml)

    def test_negative_balance_formats_correctly(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_ledger_opening_balance_alter_request(
            company="Digi",
            ledger_name="Creditor Corp",
            opening_balance=-250.5,
        )
        self.assertIn("<OPENINGBALANCE>-250.50</OPENINGBALANCE>", xml)

    def test_escapes_special_characters_in_ledger_name(self):
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_ledger_opening_balance_alter_request(
            company="Digi",
            ledger_name='X & "Y" <Z>',
            opening_balance=0.0,
        )
        self.assertNotIn("<Z>", xml)
        self.assertIn("&amp;", xml)


class TestTallyOpeningBalanceServiceComputeBalances(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Opening Balance Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
            }
        )
        self.mapped_account = self.env["account.account"].create(
            {
                "name": "Opening Balance Test Account",
                "code": "OBT001",
                "account_type": "asset_cash",
                "tally_ledger_name": "Test Bank",
            }
        )
        self.unmapped_account = self.env["account.account"].create(
            {"name": "Opening Balance Test Unmapped", "code": "OBT002", "account_type": "asset_cash"}
        )

    def _post_move(self, date, debit_account, credit_account, amount):
        uom = self.env.ref("uom.product_uom_unit")
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "date": date,
                "line_ids": [
                    (0, 0, {
                        "account_id": debit_account.id, "debit": amount, "credit": 0.0,
                        "name": "debit", "quantity": 1.0, "price_unit": amount, "product_uom_id": uom.id,
                    }),
                    (0, 0, {
                        "account_id": credit_account.id, "debit": 0.0, "credit": amount,
                        "name": "credit", "quantity": 1.0, "price_unit": amount, "product_uom_id": uom.id,
                    }),
                ],
            }
        )
        move.action_post()
        return move

    def test_compute_balances_only_includes_mapped_accounts_with_nonzero_balance(self):
        from ..services import TallyOpeningBalanceService

        other_side = self.env["account.account"].create(
            {"name": "Opening Balance Test Other Side", "code": "OBT003", "account_type": "expense"}
        )
        self._post_move("2026-01-15", self.mapped_account, other_side, 500.0)

        service = TallyOpeningBalanceService(self.env)
        rows = service.compute_balances(self.connection, "2026-12-31")

        accounts_in_result = {row["account"].id for row in rows}
        self.assertIn(self.mapped_account.id, accounts_in_result)
        self.assertNotIn(self.unmapped_account.id, accounts_in_result)

        row = next(r for r in rows if r["account"].id == self.mapped_account.id)
        self.assertAlmostEqual(row["balance"], 500.0)

    def test_compute_balances_respects_as_of_date_cutoff(self):
        from ..services import TallyOpeningBalanceService

        other_side = self.env["account.account"].create(
            {"name": "Opening Balance Test Other Side 2", "code": "OBT004", "account_type": "expense"}
        )
        self._post_move("2026-06-01", self.mapped_account, other_side, 300.0)

        service = TallyOpeningBalanceService(self.env)
        rows_before = service.compute_balances(self.connection, "2026-01-01")
        rows_after = service.compute_balances(self.connection, "2026-12-31")

        self.assertNotIn(self.mapped_account.id, {r["account"].id for r in rows_before})
        self.assertIn(self.mapped_account.id, {r["account"].id for r in rows_after})

    def test_push_opening_balance_without_mapping_raises(self):
        from ..services import TallyOpeningBalanceService

        service = TallyOpeningBalanceService(self.env)
        with self.assertRaises(TallyMappingError):
            service.push_opening_balance(self.unmapped_account, 100.0, company=self.company)


class TestTallyOpeningBalanceWizard(TransactionCase):
    def test_refresh_populates_lines(self):
        connection = self.env["tally.connection"].create(
            {
                "name": "Opening Balance Wizard Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
            }
        )
        account = self.env["account.account"].create(
            {
                "name": "Opening Balance Wizard Test Account",
                "code": "OBW001",
                "account_type": "asset_cash",
                "tally_ledger_name": "Wizard Test Bank",
            }
        )
        other_side = self.env["account.account"].create(
            {"name": "Opening Balance Wizard Other Side", "code": "OBW002", "account_type": "expense"}
        )
        uom = self.env.ref("uom.product_uom_unit")
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "date": "2026-01-01",
                "line_ids": [
                    (0, 0, {
                        "account_id": account.id, "debit": 200.0, "credit": 0.0,
                        "name": "debit", "quantity": 1.0, "price_unit": 200.0, "product_uom_id": uom.id,
                    }),
                    (0, 0, {
                        "account_id": other_side.id, "debit": 0.0, "credit": 200.0,
                        "name": "credit", "quantity": 1.0, "price_unit": 200.0, "product_uom_id": uom.id,
                    }),
                ],
            }
        )
        move.action_post()

        wizard = self.env["tally.opening.balance.wizard"].create(
            {"connection_id": connection.id, "as_of_date": "2026-12-31"}
        )
        wizard.action_refresh()

        self.assertEqual(len(wizard.line_ids), 1)
        self.assertEqual(wizard.line_ids.account_id, account)
        self.assertAlmostEqual(wizard.line_ids.balance, 200.0)
