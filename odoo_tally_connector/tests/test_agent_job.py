"""
Tests for Phase 2 of the Agent architecture - job delivery (see
models/tally_agent_job.py's module docstring, and the "Tally Connector -
Agent Mode" internal proposal). Phase 1 (device auth) is covered in
test_agent_device.py; this covers the actual work-delivery mechanism it
was built to support: enqueue -> poll (claim) -> submit_result.

Covers: claim_next's one-job-at-a-time, oldest-first, exactly-once-claim
semantics at the model level, and the /poll + /submit_result endpoints
end-to-end via HttpCase - including that a device can't submit a result
for a job it never claimed (or that another device claimed), and that an
empty queue polls cleanly rather than erroring.
"""

from odoo.tests import HttpCase, TransactionCase


class TestTallyAgentJobModel(TransactionCase):
    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Agent Job Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )
        self.env["tally.agent.device"].generate(self.connection)
        self.device = self.connection.agent_device_ids

    def test_enqueue_creates_pending_job(self):
        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>test</ENVELOPE>")

        self.assertEqual(job.state, "pending")
        self.assertEqual(job.connection_id, self.connection)
        self.assertTrue(job.correlation_id)
        self.assertFalse(job.device_id)

    def test_view_agent_jobs_action_scoped_to_this_connection(self):
        """Phase 4 - Observability: the Sync Timeline button on a connection
        must only show that connection's own jobs, not every connection's."""
        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>mine</ENVELOPE>")
        other_connection = self.env["tally.connection"].create(
            {
                "name": "Other Agent Job Timeline Connection",
                "company_id": self.env["res.company"].create({"name": "Other Co Timeline Test"}).id,
                "host": "localhost",
                "port": 9001,
                "connection_mode": "agent",
            }
        )
        self.env["tally.agent.job"].enqueue(other_connection, "<ENVELOPE>not-mine</ENVELOPE>")

        action = self.connection.action_view_agent_jobs()

        self.assertEqual(action["res_model"], "tally.agent.job")
        matching = self.env["tally.agent.job"].search(action["domain"])
        self.assertEqual(matching, job)

    def test_claim_next_returns_oldest_pending_first(self):
        first = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>first</ENVELOPE>")
        self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>second</ENVELOPE>")

        claimed = self.env["tally.agent.job"].claim_next(self.device)

        self.assertEqual(claimed, first)
        self.assertEqual(claimed.state, "in_progress")
        self.assertEqual(claimed.device_id, self.device)
        self.assertTrue(claimed.dispatched_at)

    def test_claim_next_does_not_reclaim_in_progress_job(self):
        self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>only</ENVELOPE>")
        first_claim = self.env["tally.agent.job"].claim_next(self.device)
        self.assertTrue(first_claim)

        second_claim = self.env["tally.agent.job"].claim_next(self.device)
        self.assertFalse(second_claim)

    def test_claim_next_empty_queue_returns_empty_recordset(self):
        self.assertFalse(self.env["tally.agent.job"].claim_next(self.device))

    def test_claim_next_uses_row_locking_sql_and_still_returns_the_right_job(self):
        """
        Security review (Phase 6) regression test: claim_next() was rewritten
        from a plain ORM search-then-write to raw SQL using
        SELECT ... FOR UPDATE SKIP LOCKED, specifically so two concurrent
        /poll calls can't both claim the same job and send it to Tally
        twice. A true cross-transaction race isn't practically testable
        under TransactionCase (its data is uncommitted, so a genuinely
        separate DB connection can't see it to contend for the same lock -
        confirmed by trying exactly that and finding the second connection
        sees no row at all, not a locked one). What IS tested here: the
        rewritten query still behaves correctly for the ordinary case
        (respects the pending filter, still picks oldest-first, still
        returns a real job) - i.e. the concurrency fix didn't break
        correctness for the common single-caller path.
        """
        older = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>older</ENVELOPE>")
        self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>newer</ENVELOPE>")

        claimed = self.env["tally.agent.job"].claim_next(self.device)

        self.assertEqual(claimed, older)
        self.assertEqual(claimed.state, "in_progress")

    def test_submit_result_success_marks_done(self):
        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>x</ENVELOPE>")
        self.env["tally.agent.job"].claim_next(self.device)

        job.submit_result(True, response_xml="<ENVELOPE>ok</ENVELOPE>")

        self.assertEqual(job.state, "done")
        self.assertEqual(job.response_xml, "<ENVELOPE>ok</ENVELOPE>")
        self.assertTrue(job.completed_at)

    def test_submit_result_failure_marks_failed_with_error(self):
        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>x</ENVELOPE>")
        self.env["tally.agent.job"].claim_next(self.device)

        job.submit_result(False, error_message="Tally unreachable")

        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_message, "Tally unreachable")

    def test_enqueue_reuses_in_flight_job_with_same_idempotency_key(self):
        """
        Core of the "timeout/lost response must not create a duplicate
        Tally voucher" fix: if a caller retries while an earlier attempt for
        the SAME record is still pending/in_progress, enqueue() must hand
        back that same job instead of creating a second one that would send
        the same voucher to Tally twice.
        """
        first = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE>first</ENVELOPE>", idempotency_key="move-42"
        )
        retry = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE>retry</ENVELOPE>", idempotency_key="move-42"
        )

        self.assertEqual(first, retry)
        self.assertEqual(
            self.env["tally.agent.job"].search_count(
                [("connection_id", "=", self.connection.id), ("idempotency_key", "=", "move-42")]
            ),
            1,
        )

    def test_enqueue_does_not_reuse_a_resolved_job_with_same_key(self):
        """Once a job is done/failed, a genuinely new sync attempt (e.g. an
        Alter after a later edit) must be able to queue its own new job."""
        first = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE>first</ENVELOPE>", idempotency_key="move-42"
        )
        first.submit_result(True, response_xml="<ENVELOPE>ok</ENVELOPE>")

        second = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE>second</ENVELOPE>", idempotency_key="move-42"
        )

        self.assertNotEqual(first, second)

    def test_submit_result_reconciles_success_onto_originating_record(self):
        partner = self.env["res.partner"].create({"name": "Async Reconcile Success Partner"})
        job = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE>x</ENVELOPE>",
            res_model="res.partner", res_id=partner.id, idempotency_key="partner-key-1",
        )
        partner.tally_sync_key = "partner-key-1"
        self.env["tally.agent.job"].claim_next(self.device)

        ack_xml = "<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS></RESPONSE>"
        job.submit_result(True, response_xml=ack_xml)

        self.assertEqual(partner.tally_sync_status, "success")
        self.assertEqual(partner.tally_guid, "partner-key-1")
        self.assertFalse(partner.tally_last_sync_error)

    def test_submit_result_reconciles_genuine_tally_error_onto_originating_record(self):
        """A genuine Tally-side rejection (parsed from the ack XML, e.g. a
        missing ledger) must land as FAILED with the real error - not as a
        silent success just because the agent reached Tally at all."""
        partner = self.env["res.partner"].create({"name": "Async Reconcile Error Partner"})
        job = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE>x</ENVELOPE>",
            res_model="res.partner", res_id=partner.id, idempotency_key="partner-key-2",
        )
        self.env["tally.agent.job"].claim_next(self.device)

        error_xml = (
            "<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED><ERRORS>1</ERRORS>"
            "<LINEERROR>Could not create Ledger: Group does not exist</LINEERROR></RESPONSE>"
        )
        job.submit_result(True, response_xml=error_xml)

        self.assertEqual(partner.tally_sync_status, "failed")
        self.assertIn("Group does not exist", partner.tally_last_sync_error)

    def test_submit_result_reconciles_agent_level_failure_onto_originating_record(self):
        """An agent-level failure (couldn't reach its own local Tally at
        all - no response_xml to parse) must also land as FAILED, using the
        agent's own error_message."""
        partner = self.env["res.partner"].create({"name": "Async Reconcile Agent Failure Partner"})
        job = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE>x</ENVELOPE>",
            res_model="res.partner", res_id=partner.id, idempotency_key="partner-key-3",
        )
        self.env["tally.agent.job"].claim_next(self.device)

        job.submit_result(False, error_message="Local Tally not reachable from agent")

        self.assertEqual(partner.tally_sync_status, "failed")
        self.assertEqual(partner.tally_last_sync_error, "Local Tally not reachable from agent")

    def test_submit_result_reconciles_test_connection_success_onto_connection(self):
        """
        A Test Connection gateway probe isn't a master-import ack - reaching
        Tally at all (state 'done') is success, whatever the response body
        says, matching the pre-async test_connection()'s own semantics.
        """
        job = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE RequestType=\"Gateway\"/>",
            res_model="tally.connection", res_id=self.connection.id, operation="test_connection",
        )
        self.env["tally.agent.job"].claim_next(self.device)

        job.submit_result(True, response_xml="<ENVELOPE><HEADER><VERSION>1</VERSION></HEADER></ENVELOPE>")

        self.assertEqual(self.connection.connection_status, "success")
        self.assertFalse(self.connection.last_error)
        self.assertTrue(self.connection.last_tested_at)

    def test_submit_result_reconciles_test_connection_agent_failure_onto_connection(self):
        job = self.env["tally.agent.job"].enqueue(
            self.connection, "<ENVELOPE RequestType=\"Gateway\"/>",
            res_model="tally.connection", res_id=self.connection.id, operation="test_connection",
        )
        self.env["tally.agent.job"].claim_next(self.device)

        job.submit_result(False, error_message="Local Tally not reachable from agent")

        self.assertEqual(self.connection.connection_status, "failed")
        self.assertEqual(self.connection.last_error, "Local Tally not reachable from agent")

    def test_reconcile_is_a_noop_without_res_model(self):
        """A job with no owning record (e.g. a Test Connection gateway
        check) must reconcile as a pure no-op, never raise."""
        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>x</ENVELOPE>")
        self.env["tally.agent.job"].claim_next(self.device)

        job.submit_result(True, response_xml="<RESPONSE><CREATED>1</CREATED></RESPONSE>")

        self.assertEqual(job.state, "done")


class TestTallyAgentJobEndpoints(HttpCase):
    def setUp(self):
        super().setUp()
        self.connection = self.env["tally.connection"].create(
            {
                "name": "Agent Job Endpoint Test Connection",
                "company_id": self.env.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_mode": "agent",
            }
        )
        self.token = self.env["tally.agent.device"].generate(self.connection)

    def test_poll_with_no_jobs_returns_null_job(self):
        result = self.make_jsonrpc_request("/tally_agent/v1/poll", {"token": self.token})

        self.assertTrue(result["success"])
        self.assertIsNone(result["job"])

    def test_poll_returns_and_claims_pending_job(self):
        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>hello</ENVELOPE>")

        result = self.make_jsonrpc_request("/tally_agent/v1/poll", {"token": self.token})

        self.assertTrue(result["success"])
        self.assertEqual(result["job"]["id"], job.id)
        self.assertEqual(result["job"]["request_xml"], "<ENVELOPE>hello</ENVELOPE>")
        self.assertEqual(result["job"]["correlation_id"], job.correlation_id)

        job.invalidate_recordset()
        self.assertEqual(job.state, "in_progress")

    def test_poll_with_invalid_token_rejected(self):
        result = self.make_jsonrpc_request("/tally_agent/v1/poll", {"token": "garbage"})
        self.assertFalse(result["success"])

    def test_submit_result_completes_claimed_job(self):
        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>hello</ENVELOPE>")
        self.make_jsonrpc_request("/tally_agent/v1/poll", {"token": self.token})

        result = self.make_jsonrpc_request(
            "/tally_agent/v1/submit_result",
            {"token": self.token, "job_id": job.id, "success": True, "response_xml": "<ENVELOPE>done</ENVELOPE>"},
        )

        self.assertTrue(result["success"])
        job.invalidate_recordset()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.response_xml, "<ENVELOPE>done</ENVELOPE>")

    def test_submit_result_rejects_job_never_claimed_by_this_device(self):
        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>hello</ENVELOPE>")
        # Never polled, so still pending - not claimed by anyone.

        result = self.make_jsonrpc_request(
            "/tally_agent/v1/submit_result", {"token": self.token, "job_id": job.id, "success": True}
        )

        self.assertFalse(result["success"])
        job.invalidate_recordset()
        self.assertEqual(job.state, "pending")

    def test_submit_result_rejects_job_claimed_by_a_different_device(self):
        other_connection = self.env["tally.connection"].create(
            {
                "name": "Other Agent Job Endpoint Connection",
                "company_id": self.env["res.company"].create({"name": "Other Co Agent Job Test"}).id,
                "host": "localhost",
                "port": 9001,
                "connection_mode": "agent",
            }
        )
        other_token = self.env["tally.agent.device"].generate(other_connection)

        job = self.env["tally.agent.job"].enqueue(self.connection, "<ENVELOPE>hello</ENVELOPE>")
        self.make_jsonrpc_request("/tally_agent/v1/poll", {"token": self.token})  # claimed by self.token's device

        result = self.make_jsonrpc_request(
            "/tally_agent/v1/submit_result", {"token": other_token, "job_id": job.id, "success": True}
        )

        self.assertFalse(result["success"])
        job.invalidate_recordset()
        self.assertEqual(job.state, "in_progress")
