"""
Tests for Phase 7 of the Agent architecture - real sync integration (see
services/tally_agent_transport.py's module docstring) - and for its Phase
7.1/7.2 async rewrite, which removed send_request()'s AND test_connection()'s
blocking wait loops entirely (root-cause fix for a guaranteed, not
occasional, false-FAILED race - see that module's docstring for the full
writeup, including the production evidence: a real Test Connection click
showed "Failed" even though the agent completed that exact job in under
ten seconds).

Covers: _build_tally_client()/_get_tally_client() wiring the right transport
for each mode; and both AgentQueueTransport.send_request() and
test_connection() failing fast when no agent is online, otherwise enqueuing
and returning "queued" IMMEDIATELY (no wait, no blocking) - each tagging its
job for reconciliation onto the right target (a business record for
send_request(), the connection itself for test_connection()).

A genuine concurrent agent (a separate process claiming and completing the
job via /poll + /submit_result while a test's own transaction is still
open) isn't practically testable under TransactionCase (its data is
uncommitted, so a truly separate DB connection can't see it at all - see
test_agent_job.py's claim_next race-condition test docstring for the same
limitation, and test_agent_job.py's reconciliation tests for coverage of
what happens once a result does arrive).
"""

import time

from odoo.tests import TransactionCase

from ..services import AgentQueueTransport, TallyClient, TallyConnectionError


class TestBuildTallyClientTransportWiring(TransactionCase):
    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Agent Sync Integration Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
            }
        )

    def test_direct_mode_uses_default_http_transport(self):
        client = self.connection._build_tally_client()
        self.assertEqual(type(client.transport).__name__, "HttpXmlTransport")

    def test_agent_mode_uses_agent_queue_transport(self):
        self.connection.connection_mode = "agent"
        client = self.connection._build_tally_client()
        self.assertIsInstance(client.transport, AgentQueueTransport)
        self.assertEqual(client.transport.connection, self.connection)

    def test_get_tally_client_still_enforces_active_and_tested_guards_for_agent_mode(self):
        self.connection.connection_mode = "agent"
        with self.assertRaises(Exception):
            self.connection._get_tally_client()  # never_tested by default

        self.connection.write({"connection_status": "success"})
        client = self.connection._get_tally_client()
        self.assertIsInstance(client.transport, AgentQueueTransport)


class TestAgentQueueTransportSendRequest(TransactionCase):
    """
    send_request() - the path every real sync service uses - no longer
    waits at all. See this module's docstring and
    tally_agent_transport.py's for why: a same-transaction wait could never
    observe the agent's genuinely separate claim/result, so it was a
    guaranteed timeout, not an occasional one.
    """

    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Agent Queue Transport Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )

    def _make_online_device(self):
        self.env["tally.agent.device"].generate(self.connection)
        device = self.connection.agent_device_ids
        device.mark_seen()
        return device

    def test_send_request_fails_fast_when_no_device_ever_enrolled(self):
        transport = AgentQueueTransport(self.connection, self.env)
        with self.assertRaises(TallyConnectionError):
            transport.send_request("<ENVELOPE>req</ENVELOPE>")

        self.assertFalse(
            self.env["tally.agent.job"].search([("connection_id", "=", self.connection.id)])
        )

    def test_send_request_fails_fast_when_device_enrolled_but_offline(self):
        self.env["tally.agent.device"].generate(self.connection)
        # Never called mark_seen() - last_seen_at is unset, so is_online is False.

        transport = AgentQueueTransport(self.connection, self.env)
        with self.assertRaises(TallyConnectionError):
            transport.send_request("<ENVELOPE>req</ENVELOPE>")

    def test_send_request_returns_queued_immediately_without_blocking(self):
        """
        The core of the fix: no wait loop at all. Proven here by an
        elapsed-time assertion, not just by checking the result shape - a
        regression that reintroduced even a short poll loop would still
        pass a result-shape-only test.
        """
        self._make_online_device()
        transport = AgentQueueTransport(self.connection, self.env)

        started = time.monotonic()
        result = transport.send_request("<ENVELOPE>req</ENVELOPE>")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertTrue(result["success"])
        self.assertTrue(result["queued"])
        self.assertIsNone(result["response_xml"])
        self.assertTrue(result["job_id"])
        self.assertTrue(result["correlation_id"])

    def test_send_request_creates_a_pending_job_visible_to_a_fresh_query(self):
        """
        Simulates "the agent's own separate transaction commits and then
        looks" - once THIS transaction's flush_all() has run, a plain query
        for the job (standing in for the agent's own claim_next() call) sees
        it as 'pending', not stuck invisible - the state the old code could
        never reach without an explicit out-of-band commit.
        """
        self._make_online_device()
        transport = AgentQueueTransport(self.connection, self.env)

        result = transport.send_request("<ENVELOPE>req</ENVELOPE>")

        job = self.env["tally.agent.job"].browse(result["job_id"])
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.connection_id, self.connection)

    def test_send_request_passes_res_model_res_id_and_idempotency_key_onto_the_job(self):
        self._make_online_device()
        transport = AgentQueueTransport(self.connection, self.env)

        result = transport.send_request(
            "<ENVELOPE>req</ENVELOPE>",
            res_model="res.partner", res_id=999, operation="upsert_ledger",
            idempotency_key="partner-999-key",
        )

        job = self.env["tally.agent.job"].browse(result["job_id"])
        self.assertEqual(job.res_model, "res.partner")
        self.assertEqual(job.res_id, 999)
        self.assertEqual(job.operation, "upsert_ledger")
        self.assertEqual(job.idempotency_key, "partner-999-key")

    def test_send_request_does_not_create_a_second_job_for_a_retry_with_the_same_key(self):
        """
        "Timeout/lost response must not create a duplicate Tally voucher" -
        if the caller (a sync service) retries with the same deterministic
        idempotency key while the first job is still unresolved, only one
        job - and therefore only one voucher - is ever queued.
        """
        self._make_online_device()
        transport = AgentQueueTransport(self.connection, self.env)

        first = transport.send_request("<ENVELOPE>req</ENVELOPE>", idempotency_key="move-77-key")
        retry = transport.send_request("<ENVELOPE>req</ENVELOPE>", idempotency_key="move-77-key")

        self.assertEqual(first["job_id"], retry["job_id"])
        self.assertEqual(
            self.env["tally.agent.job"].search_count(
                [("connection_id", "=", self.connection.id), ("idempotency_key", "=", "move-77-key")]
            ),
            1,
        )


class TestAgentQueueTransportTestConnection(TransactionCase):
    """
    test_connection() no longer waits either - same fix, same reason as
    send_request() (see this module's docstring): a real production Test
    Connection click showed "Failed" even though the agent completed that
    exact job in under ten seconds, because the click's own request could
    never observe a genuinely separate transaction's commit. Fixed by
    enqueuing and returning "queued" immediately, same as send_request().
    """

    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Agent Test Connection Wait Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )

    def _make_online_device(self):
        self.env["tally.agent.device"].generate(self.connection)
        device = self.connection.agent_device_ids
        device.mark_seen()
        return device

    def test_test_connection_reports_failure_without_raising_when_no_device_online(self):
        transport = AgentQueueTransport(self.connection, self.env)
        result = transport.test_connection()
        self.assertFalse(result["success"])
        self.assertTrue(result["error"])

    def test_test_connection_returns_queued_immediately_without_blocking(self):
        self._make_online_device()
        transport = AgentQueueTransport(self.connection, self.env)

        started = time.monotonic()
        result = transport.test_connection()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertTrue(result["success"])
        self.assertTrue(result["queued"])
        self.assertTrue(result["job_id"])

    def test_test_connection_job_is_tagged_for_the_connection_itself(self):
        self._make_online_device()
        transport = AgentQueueTransport(self.connection, self.env)

        result = transport.test_connection()

        job = self.env["tally.agent.job"].browse(result["job_id"])
        self.assertEqual(job.res_model, "tally.connection")
        self.assertEqual(job.res_id, self.connection.id)
        self.assertEqual(job.operation, "test_connection")


class TestAsyncSyncEndToEnd(TransactionCase):
    """
    Full path: a real sync service -> TallyClient.upsert_ledger ->
    AgentQueueTransport (queued, no blocking) -> tally_agent_job's
    submit_result() -> reconciled back onto the ORIGINAL res.partner -
    proving the async fix works end-to-end, not just at the transport layer.
    """

    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Async E2E Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
                "enabled_for_sync": True,
                "connection_status": "success",
            }
        )
        self.env["tally.agent.device"].generate(self.connection)
        self.connection.agent_device_ids.mark_seen()
        self.partner = self.env["res.partner"].create(
            {"name": "Async E2E Customer", "customer_rank": 1, "company_id": self.env.company.id}
        )

    def test_sync_partner_queues_immediately_then_reconciles_to_success(self):
        from ..services import TallyPartnerSyncService

        result = TallyPartnerSyncService(self.env).sync_partner(self.partner)

        self.assertTrue(result["queued"])
        self.assertEqual(self.partner.tally_sync_status, "queued")
        self.assertFalse(self.partner.tally_guid)

        job = self.env["tally.agent.job"].browse(result["job_id"])
        self.assertEqual(job.res_model, "res.partner")
        self.assertEqual(job.res_id, self.partner.id)
        self.assertEqual(job.idempotency_key, self.partner.tally_sync_key)

        self.env["tally.agent.job"].claim_next(self.connection.agent_device_ids)
        job.submit_result(True, response_xml="<RESPONSE><CREATED>1</CREATED></RESPONSE>")

        self.assertEqual(self.partner.tally_sync_status, "success")
        self.assertEqual(self.partner.tally_guid, self.partner.tally_sync_key)
        self.assertFalse(self.partner.tally_last_sync_error)

    def test_sync_partner_queues_then_reconciles_to_genuine_tally_failure(self):
        """Not FAILED-because-of-a-timeout - FAILED because Tally itself
        rejected the ledger, with the real reason from its own ack XML."""
        from ..services import TallyPartnerSyncService

        result = TallyPartnerSyncService(self.env).sync_partner(self.partner)
        job = self.env["tally.agent.job"].browse(result["job_id"])
        self.env["tally.agent.job"].claim_next(self.connection.agent_device_ids)

        error_xml = (
            "<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED><ERRORS>1</ERRORS>"
            "<LINEERROR>Ledger group Sundry Debtors does not exist</LINEERROR></RESPONSE>"
        )
        job.submit_result(True, response_xml=error_xml)

        self.assertEqual(self.partner.tally_sync_status, "failed")
        self.assertIn("does not exist", self.partner.tally_last_sync_error)

    def test_retry_while_still_queued_does_not_create_a_second_job(self):
        """
        "Timeout/lost response must not create a duplicate Tally voucher":
        a caller retrying sync_partner() while the first attempt is still
        unresolved must reuse the same job (and therefore the same REMOTEID
        already in flight), never queue a second one.
        """
        from ..services import TallyPartnerSyncService

        service = TallyPartnerSyncService(self.env)
        first_result = service.sync_partner(self.partner)
        retry_result = service.sync_partner(self.partner)

        self.assertEqual(first_result["job_id"], retry_result["job_id"])
        self.assertEqual(
            self.env["tally.agent.job"].search_count(
                [("res_model", "=", "res.partner"), ("res_id", "=", self.partner.id)]
            ),
            1,
        )


class TestFetchOperationsDoNotCrashWhenQueued(TransactionCase):
    """
    fetch_*/list-style TallyClient methods (Fetch Companies, Fetch Ledgers,
    Import ... from Tally, Suggest Mappings, Stock Reconciliation) were never
    part of the async rewrite's own scope (they need a real list back
    immediately, unlike a sync write) - but they share the same
    AgentQueueTransport.send_request(), so once it started returning
    "queued" instead of blocking, every one of these calls tried to parse a
    None response_xml and crashed with a raw XML-parse error instead of a
    clean message.

    Fixed in two layers: TallyClient.fetch_raw_request() (used by all 8
    fetch_* methods below, instead of send_raw_request()) never crashes on
    a queued result - the tests in this class cover that. And
    AgentQueueTransport.fetch_or_queue() makes a SECOND identical call (the
    user re-clicking the same button after the agent has had time to
    answer) return the real result instead of "queued" forever - covered
    separately in TestFetchOperationsReuseResolvedJobs below.
    """

    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Fetch Queued Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )
        self.env["tally.agent.device"].generate(self.connection)
        self.connection.agent_device_ids.mark_seen()
        self.client = TallyClient(
            host=self.connection.host, port=self.connection.port,
            transport=AgentQueueTransport(self.connection, self.env),
        )

    def test_fetch_companies_reports_clean_failure_instead_of_crashing(self):
        result = self.client.fetch_companies()
        self.assertFalse(result["success"])
        self.assertEqual(result["companies"], [])
        self.assertIn("try again", result["message"])

    def test_fetch_ledgers_reports_clean_failure_instead_of_crashing(self):
        result = self.client.fetch_ledgers(company="Digi")
        self.assertFalse(result["success"])
        self.assertEqual(result["ledgers"], [])

    def test_fetch_stock_items_reports_clean_failure_instead_of_crashing(self):
        result = self.client.fetch_stock_items(company="Digi")
        self.assertFalse(result["success"])
        self.assertEqual(result["items"], [])

    def test_fetch_stock_groups_reports_clean_failure_instead_of_crashing(self):
        result = self.client.fetch_stock_groups(company="Digi")
        self.assertFalse(result["success"])
        self.assertEqual(result["stock_groups"], [])

    def test_fetch_sales_vouchers_reports_clean_failure_instead_of_crashing(self):
        result = self.client.fetch_sales_vouchers(company="Digi", from_date="20260101", to_date="20260131")
        self.assertFalse(result["success"])
        self.assertEqual(result["vouchers"], [])


class TestFetchOperationsReuseResolvedJobs(TransactionCase):
    """
    The actual fix that makes fetch/import buttons usable again in Agent
    mode: a second identical call (the user re-clicking the same button)
    finds the first call's now-completed job and returns ITS real result,
    instead of enqueuing yet another job and saying "queued" forever - see
    AgentQueueTransport.fetch_or_queue()'s docstring.
    """

    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Fetch Reuse Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )
        self.env["tally.agent.device"].generate(self.connection)
        self.device = self.connection.agent_device_ids
        self.device.mark_seen()
        self.client = TallyClient(
            host=self.connection.host, port=self.connection.port,
            transport=AgentQueueTransport(self.connection, self.env),
        )

    def test_second_call_returns_the_first_jobs_real_result(self):
        first = self.client.fetch_companies()
        self.assertTrue(first["queued"])

        job = self.env["tally.agent.job"].search([("connection_id", "=", self.connection.id)], limit=1)
        self.env["tally.agent.job"].claim_next(self.device)
        ack_xml = """<ENVELOPE><BODY><DATA><COLLECTION>
            <COMPANY><NAME>Digi</NAME></COMPANY>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        job.submit_result(True, response_xml=ack_xml)

        second = self.client.fetch_companies()
        self.assertTrue(second["success"])
        self.assertEqual(second["companies"], ["Digi"])
        self.assertFalse(second.get("queued"))

        # Reusing the result must not have created a second job.
        self.assertEqual(
            self.env["tally.agent.job"].search_count([("connection_id", "=", self.connection.id)]), 1
        )

    def test_second_call_surfaces_a_genuine_agent_failure_too(self):
        first = self.client.fetch_ledgers(company="Digi")
        self.assertTrue(first["queued"])

        job = self.env["tally.agent.job"].search([("connection_id", "=", self.connection.id)], limit=1)
        self.env["tally.agent.job"].claim_next(self.device)
        job.submit_result(False, error_message="Local Tally not reachable from agent")

        second = self.client.fetch_ledgers(company="Digi")
        self.assertFalse(second["success"])
        self.assertIn("Local Tally not reachable from agent", second["error"])

    def test_a_different_query_does_not_reuse_an_unrelated_result(self):
        """Fetching Sales vouchers for one date range must never be
        answered with a stale result from a different date range."""
        first = self.client.fetch_sales_vouchers(company="Digi", from_date="20260101", to_date="20260131")
        job = self.env["tally.agent.job"].search([("connection_id", "=", self.connection.id)], limit=1)
        self.env["tally.agent.job"].claim_next(self.device)
        job.submit_result(True, response_xml="<ENVELOPE><BODY><DATA></DATA></BODY></ENVELOPE>")

        second = self.client.fetch_sales_vouchers(company="Digi", from_date="20260201", to_date="20260228")
        self.assertTrue(second["queued"])
        self.assertEqual(
            self.env["tally.agent.job"].search_count([("connection_id", "=", self.connection.id)]), 2
        )
