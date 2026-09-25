"""
Job queue for the Local Tally Agent - Phase 2 of the Agent architecture (see
the "Tally Connector - Agent Mode" internal proposal). This is the actual
work-delivery mechanism Phase 1's device auth was built to support: Odoo
never reaches into the client's network, so instead it drops a job here and
the agent picks it up on its next poll.

A job is deliberately opaque at this layer - `request_xml` is a complete,
ready-to-send Tally XML request (the same XML this connector already builds
for direct connections via TallyXmlBuilder), and `response_xml` is whatever
Tally returned. The agent's only responsibility is "send this XML to my
local Tally, give me back what it says" - all business logic (building the
right XML, parsing the response, deciding what that means) stays in Odoo,
exactly like the architecture principle in the proposal document says.

State machine: pending -> in_progress (claimed by exactly one device) ->
done|failed. A job can only be claimed once - claim_next() marks it
in_progress atomically as part of the same search+write, and submit_result()
only accepts a result from the SAME device that claimed it, so one device
can never complete or clobber another device's job.

Async race-condition fix (see AgentQueueTransport.send_request()'s
docstring for the full root-cause writeup): a job used to be waited on
synchronously, inside the SAME Odoo transaction that created it - a wait
that could never see the agent's own, genuinely separate transaction
commit its claim/result, so it always timed out and the caller always
(wrongly) marked the record FAILED. Fixed by not waiting at all: the
caller gets an immediate "queued" result, and this model reconciles the
REAL outcome back onto the originating record itself (via res_model/
res_id) the moment the agent calls submit_result() - by which point that
call is a genuinely separate, already-committing transaction, so the
write lands cleanly with no special-casing needed.
"""

import logging
import uuid
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class TallyAgentJob(models.Model):
    _name = "tally.agent.job"
    _description = "Tally Agent Job Queue"
    _order = "create_date asc"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
        ondelete="cascade",
    )

    device_id = fields.Many2one(
        "tally.agent.device",
        string="Claimed By",
        readonly=True,
        copy=False,
        help="Set once an agent device claims this job - only that device may submit its result.",
    )

    job_type = fields.Selection(
        [("raw_xml", "Raw Tally XML Request")],
        default="raw_xml",
        required=True,
        help="Only one kind of job exists so far: send an already-built Tally XML request and "
        "return whatever Tally responds. Business logic for what that XML means lives in Odoo, "
        "not here or in the agent.",
    )

    request_xml = fields.Text(
        string="Request XML",
        required=True,
        help="Complete Tally XML request, already built by the same XML builder used for direct "
        "connections - the agent sends this to its local Tally unmodified.",
    )

    response_xml = fields.Text(string="Response XML", readonly=True, copy=False)

    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("in_progress", "In Progress"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="pending",
        required=True,
        readonly=True,
        copy=False,
    )

    error_message = fields.Text(readonly=True, copy=False)

    correlation_id = fields.Char(
        default=lambda self: str(uuid.uuid4()),
        readonly=True,
        copy=False,
        help="Carried through Agent/network/Odoo logs so a single sync attempt can be traced "
        "end-to-end (Phase 4 - full timeline view - builds on this, not implemented yet).",
    )

    res_model = fields.Char(
        string="Related Document Model",
        readonly=True,
        copy=False,
        index=True,
        help="Model of the Odoo record this job's result should be written back onto, e.g. "
        "'account.move'. Blank for jobs with no single owning record (e.g. a Test Connection "
        "gateway check) - those are never reconciled back onto anything.",
    )

    res_id = fields.Integer(
        string="Related Document ID",
        readonly=True,
        copy=False,
        help="ID of the res_model record this job's result should be written back onto.",
    )

    operation = fields.Char(
        readonly=True,
        copy=False,
        help="Which TallyClient method built this request, e.g. 'upsert_sales_voucher' - "
        "purely descriptive, for logs/tracing.",
    )

    idempotency_key = fields.Char(
        readonly=True,
        copy=False,
        index=True,
        help="A stable key for what this job represents - for a sync write, the same "
        "deterministic REMOTEID/GUID the owning sync service computes from the Odoo record's "
        "own id (see each service's _compute_sync_key()); for a fetch/list operation, a hash "
        "of the request XML itself (see AgentQueueTransport.fetch_or_queue()). enqueue() reuses "
        "an existing pending/in_progress job with the same key instead of creating a second "
        "one, so a retry racing an already-in-flight attempt can never send the same voucher "
        "twice; find_reusable_result() separately reuses an already-RESOLVED job with the same "
        "key, letting a fetch operation's second click return the first click's real result "
        "instead of always saying 'queued'.",
    )

    dispatched_at = fields.Datetime(readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)

    @api.model
    def enqueue(self, connection, request_xml, job_type="raw_xml", res_model=None, res_id=None,
                operation=None, idempotency_key=None):
        """
        Queue a new job for the agent assigned to `connection` to pick up -
        or, when `idempotency_key` matches an unresolved job already in
        flight for this connection, return that one instead of creating a
        duplicate (see this field's help text: this is what stops a retry
        from ever causing the same voucher to be sent to Tally twice).
        """
        if idempotency_key:
            existing = self.sudo().search(
                [
                    ("connection_id", "=", connection.id),
                    ("idempotency_key", "=", idempotency_key),
                    ("state", "in", ("pending", "in_progress")),
                ],
                limit=1,
            )
            if existing:
                _logger.info(
                    f"Tally agent job reused (already in flight) for connection '{connection.name}'",
                    extra={
                        "connection_id": connection.id,
                        "job_id": existing.id,
                        "correlation_id": existing.correlation_id,
                        "idempotency_key": idempotency_key,
                    },
                )
                return existing

        job = self.sudo().create(
            {
                "connection_id": connection.id,
                "request_xml": request_xml,
                "job_type": job_type,
                "res_model": res_model,
                "res_id": res_id,
                "operation": operation,
                "idempotency_key": idempotency_key,
            }
        )
        _logger.info(
            f"Tally agent job queued for connection '{connection.name}'",
            extra={"connection_id": connection.id, "job_id": job.id, "correlation_id": job.correlation_id},
        )
        return job

    @api.model
    def find_reusable_result(self, connection, idempotency_key, max_age_seconds=300):
        """
        Find a job with this idempotency_key that has ALREADY been resolved
        (done or failed) recently enough to reuse its result directly,
        instead of enqueuing a new job and making the caller wait again.

        Used by fetch/list operations (Fetch Companies, every Import ...
        from Tally button, Suggest Mappings, Stock Reconciliation) - unlike
        a sync write's fire-and-forget, these need an actual result to do
        anything useful. Under this queue-based architecture there is no
        way to hand one back on the SAME click that enqueues the job (see
        AgentQueueTransport's module docstring for why); this is what makes
        the SECOND click - after the agent has had time to answer - return
        the real result instead of "queued" forever.

        max_age_seconds bounds how long a completed job stays reusable, so
        a fetch of "the last hour's Sales vouchers" issued yesterday can
        never be silently served as if it were fresh.

        Returns:
            tally.agent.job: the most recently completed matching job, or
                an empty recordset if none exists within the window.
        """
        if not idempotency_key:
            return self.browse()

        cutoff = fields.Datetime.now() - timedelta(seconds=max_age_seconds)
        return self.sudo().search(
            [
                ("connection_id", "=", connection.id),
                ("idempotency_key", "=", idempotency_key),
                ("state", "in", ("done", "failed")),
                ("completed_at", ">=", cutoff),
            ],
            order="completed_at desc",
            limit=1,
        )

    @api.model
    def claim_next(self, device):
        """
        Atomically claim the oldest pending job for `device`'s connection.

        Uses SELECT ... FOR UPDATE SKIP LOCKED (raw SQL, not a plain ORM
        search) deliberately - a search-then-write here would let two
        concurrent /poll requests (two agent processes sharing a token by
        mistake, or a retried request racing the original) both read the
        same pending row before either commits its claim, both send the
        same job to Tally, and create the same voucher twice. SKIP LOCKED
        makes a second concurrent caller see the row as unavailable and
        move on, rather than blocking on or re-reading it.

        Returns:
            tally.agent.job: the claimed job, or an empty recordset if none pending.
        """
        # Raw SQL below reads the actual table directly, bypassing the ORM's
        # write buffer - without this flush, a still-buffered write from an
        # earlier claim_next() in the same transaction (state -> in_progress)
        # wouldn't be visible yet, and this query could re-select that same
        # row as if it were still pending.
        self.env.flush_all()

        self.env.cr.execute(
            """
            SELECT id FROM tally_agent_job
            WHERE connection_id = %s AND state = 'pending'
            ORDER BY create_date ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (device.connection_id.id,),
        )
        row = self.env.cr.fetchone()
        if not row:
            return self.browse()

        job = self.sudo().browse(row[0])
        job.write({"state": "in_progress", "device_id": device.id, "dispatched_at": fields.Datetime.now()})
        return job

    def submit_result(self, success, response_xml=None, error_message=None):
        self.ensure_one()
        self.sudo().write(
            {
                "state": "done" if success else "failed",
                "response_xml": response_xml,
                "error_message": error_message,
                "completed_at": fields.Datetime.now(),
            }
        )
        _logger.info(
            f"Tally agent job {'completed' if success else 'failed'} for connection "
            f"'{self.connection_id.name}'",
            extra={"connection_id": self.connection_id.id, "job_id": self.id, "correlation_id": self.correlation_id},
        )

        try:
            self.sudo()._reconcile_business_record()
        except Exception:
            # Never let a reconciliation bug corrupt the agent's own HTTP
            # response - the job's own state above is already safely
            # written either way, and this is logged loudly for follow-up.
            _logger.exception(
                f"Failed to reconcile Tally agent job {self.id} back onto "
                f"{self.res_model}({self.res_id})",
                extra={"job_id": self.id, "res_model": self.res_model, "res_id": self.res_id},
            )

    def _reconcile_business_record(self):
        """
        Write the real, now-known outcome of an async job back onto the
        Odoo record that originally queued it (res_model/res_id) - the
        second half of the fix for the false-FAILED race described in
        AgentQueueTransport's module docstring. Called from submit_result(),
        which only ever runs inside the agent's own /submit_result request -
        a genuinely separate, real transaction that commits normally, so
        this write is visible immediately with no special handling.

        Two shapes, dispatched on the record's own fields:
        - tally.connection (a Test Connection gateway probe): writes
          connection_status/last_tested_at/last_error. A gateway probe's ack
          isn't a master-import response - reaching Tally at all (state
          "done") is success, regardless of what it said back.
        - Everything else (account.move, product.product, res.partner, ...):
          writes tally_sync_status/tally_guid/tally_last_sync_error, parsing
          the ack the same way TallyClient's upsert_* methods always have.

        A no-op for jobs with no owning record (res_model/res_id blank) and
        for records that carry neither shape's fields (defensive - every
        model that ever queues a job today has one or the other).
        """
        self.ensure_one()
        if not self.res_model or not self.res_id:
            return

        record = self.env[self.res_model].browse(self.res_id).exists()
        if not record:
            return

        if "connection_status" in record._fields:
            self._reconcile_connection(record)
        elif "tally_sync_status" in record._fields:
            self._reconcile_sync_record(record)

    def _reconcile_connection(self, connection):
        """Reconcile a Test Connection gateway probe onto tally.connection."""
        success = self.state == "done"
        connection.sudo().write(
            {
                "connection_status": "success" if success else "failed",
                "last_tested_at": fields.Datetime.now(),
                "last_error": False if success else (self.error_message or "Tally Agent reported a failure."),
            }
        )
        _logger.info(
            f"Tally agent job {self.id} reconciled onto connection '{connection.name}': "
            f"{'success' if success else 'failed'}",
            extra={"job_id": self.id, "connection_id": connection.id},
        )

    def _reconcile_sync_record(self, record):
        """Reconcile a business-record sync (invoice, product, partner, ...)."""
        from ..services.tally_response_parser import TallyResponseParser

        vals = {"tally_last_sync_at": fields.Datetime.now()}

        if self.state == "done":
            parsed = TallyResponseParser.parse_master_import_response(self.response_xml or "")
            if parsed["success"]:
                vals.update(
                    {
                        "tally_sync_status": "success",
                        "tally_last_sync_error": False,
                    }
                )
                if "tally_guid" in record._fields:
                    vals["tally_guid"] = getattr(record, "tally_sync_key", False) or self.idempotency_key
            else:
                vals.update(
                    {
                        "tally_sync_status": "failed",
                        "tally_last_sync_error": parsed.get("error") or parsed.get("message"),
                    }
                )
        else:
            vals.update(
                {
                    "tally_sync_status": "failed",
                    "tally_last_sync_error": self.error_message or "Tally Agent reported a failure.",
                }
            )

        record.sudo().write(vals)
        _logger.info(
            f"Tally agent job {self.id} reconciled onto {self.res_model}({self.res_id}): "
            f"{vals.get('tally_sync_status')}",
            extra={"job_id": self.id, "res_model": self.res_model, "res_id": self.res_id},
        )
