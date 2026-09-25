"""
One-time display for a freshly-generated Agent enrollment token. Deliberately
a separate transient record (not a plain notification) so the token stays on
screen until the user closes it, rather than auto-dismissing - this is the
only moment the plaintext token exists anywhere outside tally.agent.device's
local variable during generation (see that model for why it's never stored).
"""

from odoo import fields, models


class TallyAgentTokenRevealWizard(models.TransientModel):
    _name = "tally.agent.token.reveal.wizard"
    _description = "Tally Agent Enrollment Token"

    connection_id = fields.Many2one("tally.connection", required=True)
    token = fields.Char(readonly=True)
