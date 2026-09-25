"""
Agent-mode transport (Phase 7 - real sync integration for the Agent
architecture; see the "Tally Connector - Agent Mode" internal proposal and
models/tally_agent_job.py's docstring).

Every sync/import service in this module talks to Tally exclusively through
`tally.connection._get_tally_client()` -> a TallyClient -> its `transport`
(see tally_client.py, tally_transport.py). Direct mode wires that transport
to HttpXmlTransport, which calls Tally over HTTP itself. Before this file
existed, Agent mode wired the exact same HttpXmlTransport - meaning
"selecting Agent mode" changed nothing about how a sync actually happened,
and every real sync attempt on an agent-mode connection quietly tried (and
failed) to reach Tally directly instead of going through the agent.

This transport implements the same BaseTransport contract (send_request/
test_connection) but backs it with the job queue instead of a direct HTTP
call.

Async rewrite (root-cause fix for a guaranteed-not-occasional false-FAILED
bug): both methods used to enqueue a job and then BLOCK the calling Odoo
transaction for up to a minute, re-reading that job's own row until the
assigned agent device claimed it (via /poll) and reported a result (via
/submit_result). That wait could never succeed on a real request: the
agent's /poll and /submit_result calls run in a genuinely separate DB
transaction/connection, and under Postgres READ COMMITTED isolation they
cannot see a row this transaction inserted until THIS transaction commits -
which never happens until AFTER the method returns (whether by success or
by timeout), since Odoo only commits at the end of the whole request/cron
tick that called it. So the wait was a guaranteed timeout on every single
attempt, not an occasional race - confirmed live on production: a real
Test Connection click showed "Failed" even though the agent picked up and
completed that exact job in under ten seconds (see job's own dispatched_at/
completed_at), because the click's own request could never observe that
completion from inside its still-open transaction.

Fixed by not waiting at all, for either method: enqueue the job and return
a "queued" result immediately, in the SAME transaction the caller is
already in - which then commits normally at the end of that request/cron
tick, at which point the job becomes visible to the agent like any other
committed row. Callers treat "queued" as its own outcome (not success, not
failure), and the REAL outcome is written back later by tally_agent_job.py's
submit_result()/_reconcile_business_record(), when the agent reports a
result from a genuinely separate, already-committing request:
- send_request()'s callers (sync services) get it written onto the
  originating business record (res_model/res_id) - see
  tally_invoice_sync_service.py etc.
- test_connection()'s caller (tally.connection.action_test_connection())
  gets it written onto the connection's own connection_status/last_error,
  via the same reconciliation method (res_model="tally.connection").
Business logic (building request XML, parsing responses) stays exactly
where it was - this only changes when the result is known and who writes
it back.
"""

import hashlib
import logging

from .tally_transport import BaseTransport
from .tally_exceptions import TallyConnectionError

_logger = logging.getLogger(__name__)

_GATEWAY_TEST_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<ENVELOPE RequestType="Gateway"/>'

# How long a fetch/list operation's completed job stays reusable by a later
# click of the same button - long enough to cover "queued, wait a moment,
# click again", short enough that a stale result is never silently served
# as if it were fresh. See fetch_or_queue()'s docstring.
_FETCH_RESULT_MAX_AGE_SECONDS = 300


class AgentQueueTransport(BaseTransport):
    """
    BaseTransport implementation that routes through tally.agent.job instead
    of calling Tally directly. One instance is built per _get_tally_client()
    call (see tally_connection.py's _build_tally_client()), scoped to a
    single connection.
    """

    def __init__(self, connection, env, wait_seconds=None):
        self.connection = connection
        self.env = env
        # Kept for backward compatibility with existing callers/tests that
        # pass it - no longer used to size a wait, since neither method
        # waits anymore.
        self.wait_seconds = wait_seconds

    def _require_online_device(self):
        # Fail fast rather than queuing a job nobody will ever pick up -
        # e.g. the client's PC is off overnight.
        device = self.connection.active_agent_device_id
        if not device or not device.is_online:
            raise TallyConnectionError(
                f"No agent is currently online for connection '{self.connection.name}' - "
                f"the job was not sent."
            )
        return device

    def _enqueue_and_return_queued(self, request_xml, res_model=None, res_id=None,
                                    operation=None, idempotency_key=None):
        self._require_online_device()

        job = self.env["tally.agent.job"].enqueue(
            self.connection, request_xml,
            res_model=res_model, res_id=res_id, operation=operation, idempotency_key=idempotency_key,
        )
        # Flushes THIS transaction's own write to the DB so it's part of
        # what gets committed when the calling request/cron tick ends - not
        # a commit itself (see tally_agent_job.py's claim_next() for the
        # same gotcha this guards against: an unflushed ORM write is
        # invisible even to a raw SQL read on this same cursor).
        self.env.flush_all()

        _logger.info(
            f"Tally agent job {job.id} queued (async) for connection '{self.connection.name}'",
            extra={
                "connection_id": self.connection.id,
                "job_id": job.id,
                "correlation_id": job.correlation_id,
                "operation": operation,
            },
        )
        return {
            "success": True,
            "status_code": 202,
            "response_xml": None,
            "error": None,
            "raw_response": None,
            "queued": True,
            "job_id": job.id,
            "correlation_id": job.correlation_id,
        }

    def send_request(self, request_xml, timeout=None, res_model=None, res_id=None,
                      operation=None, idempotency_key=None):
        """
        Enqueue a job for the agent and return immediately - see this
        module's docstring for why this no longer waits for a result.
        Callers get {"queued": True, "job_id":..., "correlation_id":...}
        back and must treat that as pending, not success or failure; the
        real outcome is written back onto res_model/res_id (when given)
        once the agent calls /submit_result - see tally_agent_job.py's
        submit_result()/_reconcile_business_record().
        """
        return self._enqueue_and_return_queued(
            request_xml, res_model=res_model, res_id=res_id,
            operation=operation, idempotency_key=idempotency_key,
        )

    def test_connection(self, timeout=None):
        """
        Enqueue a gateway probe and return "queued" immediately, exactly
        like send_request() - see this module's docstring for why a
        synchronous wait can never work here either. The caller
        (tally.connection.action_test_connection()) treats "queued" as its
        own outcome (writes connection_status="testing"), and the real
        result lands on connection_status/last_error moments later via
        _reconcile_business_record(), once the agent actually reaches (or
        fails to reach) Tally.
        """
        try:
            return self._enqueue_and_return_queued(
                _GATEWAY_TEST_XML,
                res_model="tally.connection", res_id=self.connection.id,
                operation="test_connection",
            )
        except TallyConnectionError as e:
            return {"success": False, "message": e.message, "error": e.message}

    def fetch_or_queue(self, request_xml, operation=None):
        """
        For read/fetch operations (Fetch Companies/Ledgers, every Import
        ... from Tally button, Suggest Mappings, Stock Reconciliation) -
        these need a real result to do anything useful, unlike a sync
        write's fire-and-forget, so plain send_request()'s "queued, and
        the caller moves on" contract doesn't fit them on its own.

        There is still no way to hand back a real result on the SAME click
        that enqueues the job (that's the exact same transaction-visibility
        problem documented on send_request() - it cannot change just
        because this caller wants data back). What CAN work: recognize that
        a later click - by which point the agent has had time to answer -
        is asking for the same thing, and hand back what the agent already
        said instead of enqueuing a fresh job and saying "queued" forever.

        The idempotency key is a hash of the request XML itself (not
        supplied by the caller, unlike send_request()'s per-record key) -
        two calls asking the exact same question (same company, same date
        range, etc.) get the same key automatically, with no per-operation
        bookkeeping needed from TallyClient's fetch_* methods.
        """
        idempotency_key = f"{operation or 'fetch'}:{hashlib.sha1(request_xml.encode('utf-8')).hexdigest()}"

        existing = self.env["tally.agent.job"].find_reusable_result(
            self.connection, idempotency_key, max_age_seconds=_FETCH_RESULT_MAX_AGE_SECONDS
        )
        if existing:
            done = existing.state == "done"
            _logger.info(
                f"Tally agent job {existing.id} result reused for connection '{self.connection.name}' "
                f"(operation={operation})",
                extra={"connection_id": self.connection.id, "job_id": existing.id, "operation": operation},
            )
            return {
                "success": done,
                "status_code": 200 if done else 502,
                "response_xml": existing.response_xml,
                "error": None if done else (existing.error_message or "Agent reported a failure."),
                "raw_response": None,
                "queued": False,
                "job_id": existing.id,
                "correlation_id": existing.correlation_id,
            }

        return self._enqueue_and_return_queued(request_xml, operation=operation, idempotency_key=idempotency_key)
