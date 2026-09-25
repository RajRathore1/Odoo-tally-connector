"""
Tests for Phase 1 of the Agent architecture (device registration/auth only -
see models/tally_agent_device.py's module docstring, and the "Tally
Connector - Agent Mode" internal proposal for the full plan this is the
foundation of). No sync job delivery exists yet - these tests only cover
that a device can enroll with a token, prove it later, and be revoked.

Covers: token generation/hashing/revocation semantics at the model level,
the one-time-reveal wizard action on tally.connection, and the actual HTTP
endpoints (register/heartbeat) end-to-end via HttpCase - including that a
wrong or revoked token is rejected without distinguishing why.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, TransactionCase


class TestTallyAgentDeviceModel(TransactionCase):
    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Agent Phase 1 Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )

    def test_generate_returns_plaintext_token_and_stores_only_hash(self):
        token = self.env["tally.agent.device"].generate(self.connection)

        self.assertTrue(token)
        device = self.connection.agent_device_ids
        self.assertEqual(len(device), 1)
        self.assertTrue(device.active)
        self.assertNotEqual(device.token_hash, token)
        self.assertEqual(len(device.token_hash), 64)  # sha256 hex digest length

    def test_generate_revokes_previous_active_device(self):
        first_token = self.env["tally.agent.device"].generate(self.connection)
        first_device = self.connection.agent_device_ids

        self.env["tally.agent.device"].generate(self.connection)

        first_device.invalidate_recordset()
        self.assertFalse(first_device.active)
        self.assertTrue(first_device.revoked_at)
        # The old token must no longer authenticate.
        self.assertFalse(self.env["tally.agent.device"].authenticate(first_token))

    def test_authenticate_matches_correct_token_only(self):
        token = self.env["tally.agent.device"].generate(self.connection)

        matched = self.env["tally.agent.device"].authenticate(token)
        self.assertEqual(matched, self.connection.agent_device_ids)

        self.assertFalse(self.env["tally.agent.device"].authenticate("wrong-token"))
        self.assertFalse(self.env["tally.agent.device"].authenticate(""))
        self.assertFalse(self.env["tally.agent.device"].authenticate(False))

    def test_authenticate_rejects_revoked_token(self):
        token = self.env["tally.agent.device"].generate(self.connection)
        self.connection.agent_device_ids.action_revoke()

        self.assertFalse(self.env["tally.agent.device"].authenticate(token))

    def test_mark_activated_sets_device_id_only_once(self):
        self.env["tally.agent.device"].generate(self.connection)
        device = self.connection.agent_device_ids

        device.mark_activated("device-abc")
        self.assertEqual(device.device_id, "device-abc")
        first_activated_at = device.activated_at

        device.mark_activated("device-xyz")
        self.assertEqual(device.device_id, "device-abc")  # unchanged
        self.assertEqual(device.activated_at, first_activated_at)  # unchanged

    def test_mark_activated_stores_agent_version(self):
        self.env["tally.agent.device"].generate(self.connection)
        device = self.connection.agent_device_ids

        device.mark_activated("device-abc", agent_version="1.0.0")

        self.assertEqual(device.agent_version, "1.0.0")

    def test_active_agent_device_id_computed_field(self):
        self.assertFalse(self.connection.active_agent_device_id)

        self.env["tally.agent.device"].generate(self.connection)
        self.assertEqual(self.connection.active_agent_device_id, self.connection.agent_device_ids)

        self.connection.agent_device_ids.action_revoke()
        self.assertFalse(self.connection.active_agent_device_id)

    def test_is_online_false_when_never_seen(self):
        self.env["tally.agent.device"].generate(self.connection)
        self.assertFalse(self.connection.agent_device_ids.last_seen_at)
        self.assertFalse(self.connection.agent_device_ids.is_online)

    def test_is_online_true_right_after_mark_seen(self):
        self.env["tally.agent.device"].generate(self.connection)
        device = self.connection.agent_device_ids

        device.mark_seen()

        self.assertTrue(device.is_online)

    def test_is_online_false_when_last_seen_is_stale(self):
        self.env["tally.agent.device"].generate(self.connection)
        device = self.connection.agent_device_ids
        device.mark_seen()
        # Simulate a device that stopped polling a while ago.
        device.write({"last_seen_at": fields.Datetime.now() - timedelta(minutes=5)})

        self.assertFalse(device.is_online)

    def test_mark_seen_records_tally_reachable_when_given(self):
        self.env["tally.agent.device"].generate(self.connection)
        device = self.connection.agent_device_ids
        self.assertFalse(device.tally_reachable)

        device.mark_seen(tally_reachable=True)
        self.assertTrue(device.tally_reachable)

        device.mark_seen(tally_reachable=False)
        self.assertFalse(device.tally_reachable)

    def test_mark_seen_leaves_tally_reachable_unchanged_when_not_given(self):
        self.env["tally.agent.device"].generate(self.connection)
        device = self.connection.agent_device_ids
        device.mark_seen(tally_reachable=True)

        device.mark_seen()  # plain heartbeat/poll call with no reachability info

        self.assertTrue(device.tally_reachable)


class TestTallyConnectionGenerateAgentToken(TransactionCase):
    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Token Action Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )

    def test_action_returns_reveal_wizard_with_token(self):
        action = self.connection.action_generate_agent_token()

        self.assertEqual(action["res_model"], "tally.agent.token.reveal.wizard")
        wizard = self.env["tally.agent.token.reveal.wizard"].browse(action["res_id"])
        self.assertEqual(wizard.connection_id, self.connection)
        self.assertTrue(wizard.token)

        # The wizard's token must actually authenticate against the device just created.
        matched = self.env["tally.agent.device"].authenticate(wizard.token)
        self.assertEqual(matched, self.connection.agent_device_ids)


class TestTallyAgentControllerEndpoints(HttpCase):
    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Agent Controller Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )
        self.token = self.env["tally.agent.device"].generate(self.connection)

    def test_register_with_valid_token_activates_device(self):
        result = self.make_jsonrpc_request(
            "/tally_agent/v1/register", {"token": self.token, "device_name": "Accounts PC"}
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["connection_name"], self.connection.name)

        device = self.connection.agent_device_ids
        device.invalidate_recordset()
        self.assertEqual(device.device_id, result["device_id"])
        self.assertEqual(device.name, "Accounts PC")
        self.assertTrue(device.activated_at)

    def test_register_with_invalid_token_is_rejected_generically(self):
        result = self.make_jsonrpc_request("/tally_agent/v1/register", {"token": "not-a-real-token"})

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Invalid or revoked token.")

    def test_register_with_revoked_token_gets_same_generic_error(self):
        self.connection.agent_device_ids.action_revoke()

        result = self.make_jsonrpc_request("/tally_agent/v1/register", {"token": self.token})

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Invalid or revoked token.")

    def test_heartbeat_updates_last_seen(self):
        device = self.connection.agent_device_ids
        self.assertFalse(device.last_seen_at)

        result = self.make_jsonrpc_request("/tally_agent/v1/heartbeat", {"token": self.token})

        self.assertTrue(result["success"])
        device.invalidate_recordset()
        self.assertTrue(device.last_seen_at)

    def test_heartbeat_with_invalid_token_fails(self):
        result = self.make_jsonrpc_request("/tally_agent/v1/heartbeat", {"token": "garbage"})
        self.assertFalse(result["success"])

    def test_register_stores_agent_version(self):
        result = self.make_jsonrpc_request(
            "/tally_agent/v1/register", {"token": self.token, "agent_version": "1.0.0"}
        )

        self.assertTrue(result["success"])
        device = self.connection.agent_device_ids
        device.invalidate_recordset()
        self.assertEqual(device.agent_version, "1.0.0")

    def test_register_returns_no_latest_version_when_param_unset(self):
        result = self.make_jsonrpc_request("/tally_agent/v1/register", {"token": self.token})
        self.assertIsNone(result.get("latest_version"))

    def test_register_returns_latest_version_when_param_set(self):
        self.env["ir.config_parameter"].sudo().set_str(
            "odoo_tally_connector.agent_latest_version", "2.0.0"
        )

        result = self.make_jsonrpc_request("/tally_agent/v1/register", {"token": self.token})

        self.assertEqual(result["latest_version"], "2.0.0")

    def test_heartbeat_records_tally_reachable(self):
        result = self.make_jsonrpc_request(
            "/tally_agent/v1/heartbeat", {"token": self.token, "tally_reachable": False}
        )

        self.assertTrue(result["success"])
        device = self.connection.agent_device_ids
        device.invalidate_recordset()
        self.assertFalse(device.tally_reachable)
        self.assertTrue(device.is_online)
