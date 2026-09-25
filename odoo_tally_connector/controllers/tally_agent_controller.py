"""
HTTP API for the Local Tally Agent (Phase 1: device auth - register/
heartbeat. Phase 2: job delivery - poll/submit_result. See the "Tally
Connector - Agent Mode" internal proposal for the full plan).

Wire format is standard Odoo JSON-RPC (type="jsonrpc"), the same convention
every other Odoo web API uses:

    POST /tally_agent/v1/register
    {"jsonrpc": "2.0", "method": "call",
     "params": {"token": "...", "device_name": "...", "agent_version": "1.0.0"}}
    -> {"jsonrpc": "2.0", "result": {"success": true, "device_id": "...", "connection_name": "...",
                                      "latest_version": "1.0.0"}}
       (latest_version comes from the odoo_tally_connector.agent_latest_version system parameter -
       purely informational, set manually by an admin when a new agent build ships; Phase 5 -
       there is no auto-update, see the internal proposal's decision matrix, item 02)

    POST /tally_agent/v1/poll
    {"jsonrpc": "2.0", "method": "call", "params": {"token": "...", "tally_reachable": true}}
    -> {"jsonrpc": "2.0", "result": {"success": true, "job": {"id": 1, "job_type": "raw_xml",
                                                                "request_xml": "...", "correlation_id": "..."}}}
       (job is null when nothing is pending)

    POST /tally_agent/v1/submit_result
    {"jsonrpc": "2.0", "method": "call",
     "params": {"token": "...", "job_id": 1, "success": true, "response_xml": "..."}}
    -> {"jsonrpc": "2.0", "result": {"success": true}}

auth="public" is required here (an external agent has no Odoo session) -
every request is instead authenticated by the enrollment token itself, via
TallyAgentDevice.authenticate(). Failure responses are deliberately generic
("Invalid or revoked token.") so a wrong guess can't be used to distinguish
"no such token" from "token exists but revoked". submit_result additionally
only accepts a result for a job the SAME device claimed via poll - one
device can never complete or overwrite another device's job.
"""

import logging
import uuid

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_INVALID_TOKEN_RESPONSE = {"success": False, "error": "Invalid or revoked token."}


class TallyAgentController(http.Controller):
    @http.route("/tally_agent/v1/register", type="jsonrpc", auth="public", methods=["POST"])
    def register(self, token=None, device_name=None, agent_version=None, **kwargs):
        device = request.env["tally.agent.device"].authenticate(token)
        if not device:
            _logger.warning("Tally agent registration rejected: invalid or revoked token.")
            return _INVALID_TOKEN_RESPONSE

        device_id = device.device_id or uuid.uuid4().hex
        device.mark_activated(device_id, agent_version=agent_version)
        if device_name and not device.name:
            device.write({"name": device_name})

        _logger.info(
            f"Tally agent registered for connection '{device.connection_id.name}'",
            extra={"connection_id": device.connection_id.id, "device_id": device_id},
        )
        latest_version = (
            request.env["ir.config_parameter"].sudo().get_str("odoo_tally_connector.agent_latest_version")
        )
        return {
            "success": True,
            "device_id": device_id,
            "connection_name": device.connection_id.name,
            "latest_version": latest_version or None,
        }

    @http.route("/tally_agent/v1/heartbeat", type="jsonrpc", auth="public", methods=["POST"])
    def heartbeat(self, token=None, tally_reachable=None, **kwargs):
        device = request.env["tally.agent.device"].authenticate(token)
        if not device:
            return _INVALID_TOKEN_RESPONSE

        device.mark_seen(tally_reachable=tally_reachable)
        return {"success": True}

    @http.route("/tally_agent/v1/poll", type="jsonrpc", auth="public", methods=["POST"])
    def poll(self, token=None, tally_reachable=None, **kwargs):
        device = request.env["tally.agent.device"].authenticate(token)
        if not device:
            return _INVALID_TOKEN_RESPONSE

        device.mark_seen(tally_reachable=tally_reachable)
        job = request.env["tally.agent.job"].claim_next(device)
        if not job:
            return {"success": True, "job": None}

        return {
            "success": True,
            "job": {
                "id": job.id,
                "job_type": job.job_type,
                "request_xml": job.request_xml,
                "correlation_id": job.correlation_id,
            },
        }

    @http.route("/tally_agent/v1/submit_result", type="jsonrpc", auth="public", methods=["POST"])
    def submit_result(self, token=None, job_id=None, success=None, response_xml=None, error_message=None, **kwargs):
        device = request.env["tally.agent.device"].authenticate(token)
        if not device:
            return _INVALID_TOKEN_RESPONSE

        job = request.env["tally.agent.job"].sudo().search(
            [("id", "=", job_id), ("device_id", "=", device.id), ("state", "=", "in_progress")], limit=1
        )
        if not job:
            return {"success": False, "error": "Job not found, already completed, or not claimed by this device."}

        job.submit_result(bool(success), response_xml=response_xml, error_message=error_message)
        return {"success": True}
