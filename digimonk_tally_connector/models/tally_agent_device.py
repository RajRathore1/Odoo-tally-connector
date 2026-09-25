"""
Local Tally Agent device registry - Phase 1 of the Agent architecture (see
the "Tally Connector - Agent Mode" internal proposal for the full plan this
is the foundation of: a future mode where a lightweight program on the
client's own PC talks to Odoo over outbound HTTPS instead of Odoo reaching
into the client's network directly).

Only the SHA-256 hash of the enrollment token is ever stored. The plaintext
token is generated once (tally.connection.action_generate_agent_token),
shown to the user exactly once via a one-time-reveal wizard, and is never
recoverable afterward - only re-generatable, which revokes whatever token
was issued before it. This is the same handling convention as any API
key/webhook secret: the database is not a safe place to keep a secret in
cleartext, since anyone with read access to the table would otherwise see it.

A connection has at most one ACTIVE device token at a time, to keep "which
token is actually live" unambiguous - generating a new one revokes the
previous device record rather than leaving two active side by side.
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_TOKEN_BYTES = 32  # 256 bits of entropy

# An agent polls every few seconds (see tally_local_agent/agent.py's
# poll_interval_seconds, typically 3-5s) - if we haven't heard from it in
# this long, treat it as offline rather than trusting a stale last_seen_at
# forever. Generous relative to the poll interval so one slow/dropped
# request doesn't flap the status.
_ONLINE_THRESHOLD = timedelta(seconds=60)


class TallyAgentDevice(models.Model):
    _name = "tally.agent.device"
    _description = "Tally Local Agent Device"
    _order = "create_date desc"

    connection_id = fields.Many2one(
        "tally.connection",
        string="Tally Connection",
        required=True,
        ondelete="cascade",
    )

    name = fields.Char(
        string="Device Label",
        help="Optional human-readable label (e.g. the client's PC name). Set once the agent registers, "
        "or editable manually.",
    )

    token_hash = fields.Char(
        string="Token Hash",
        required=True,
        readonly=True,
        copy=False,
        help="SHA-256 hash of the enrollment token. The plaintext token itself is never stored anywhere.",
    )

    device_id = fields.Char(
        string="Device ID",
        readonly=True,
        copy=False,
        help="Set once the agent successfully registers using this token.",
    )

    agent_version = fields.Char(
        string="Agent Version",
        readonly=True,
        copy=False,
        help="Version string the agent reported on its last registration - lets an admin tell "
        "which PCs are running an outdated build.",
    )

    active = fields.Boolean(default=True)

    activated_at = fields.Datetime(readonly=True, copy=False)
    last_seen_at = fields.Datetime(readonly=True, copy=False)
    revoked_at = fields.Datetime(readonly=True, copy=False)

    tally_reachable = fields.Boolean(
        readonly=True,
        copy=False,
        help="Whether the agent's own last check of its LOCAL Tally succeeded - reported by the "
        "agent itself, not checked by Odoo (Odoo has no way to reach it directly, which is the "
        "whole point of Agent mode).",
    )

    is_online = fields.Boolean(
        string="Agent Online",
        compute="_compute_is_online",
        help="True if this device has been seen within the last %d seconds. An agent that stops "
        "polling (PC off, network down, process crashed) shows as offline rather than trusting a "
        "stale last-seen timestamp forever." % _ONLINE_THRESHOLD.total_seconds(),
    )

    @api.depends("last_seen_at")
    def _compute_is_online(self):
        cutoff = fields.Datetime.now() - _ONLINE_THRESHOLD
        for device in self:
            device.is_online = bool(device.last_seen_at and device.last_seen_at >= cutoff)

    @staticmethod
    def _hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @api.model
    def generate(self, connection):
        """
        Revoke any existing active device for `connection` and create a new
        one. Returns the plaintext token - the ONLY time it ever exists
        outside this method's local variable.
        """
        self.search([("connection_id", "=", connection.id), ("active", "=", True)]).write(
            {"active": False, "revoked_at": fields.Datetime.now()}
        )

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        self.create(
            {
                "connection_id": connection.id,
                "token_hash": self._hash_token(token),
            }
        )
        return token

    @api.model
    def authenticate(self, token):
        """
        Look up the active device matching `token`.

        Returns:
            tally.agent.device: the matching record, or an empty recordset
            if the token is missing, malformed, or doesn't match any active
            device.
        """
        if not token:
            return self.browse()
        return self.sudo().search(
            [("token_hash", "=", self._hash_token(token)), ("active", "=", True)], limit=1
        )

    def action_revoke(self):
        self.write({"active": False, "revoked_at": fields.Datetime.now()})

    def mark_activated(self, device_id, agent_version=None):
        self.ensure_one()
        vals = {"last_seen_at": fields.Datetime.now()}
        if not self.activated_at:
            vals["activated_at"] = fields.Datetime.now()
            vals["device_id"] = device_id
        if agent_version:
            vals["agent_version"] = agent_version
        self.write(vals)

    def mark_seen(self, tally_reachable=None):
        vals = {"last_seen_at": fields.Datetime.now()}
        if tally_reachable is not None:
            vals["tally_reachable"] = tally_reachable
        self.write(vals)
