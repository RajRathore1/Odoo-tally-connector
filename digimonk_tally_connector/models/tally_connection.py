"""
Tally Connection model.

Represents a connection configuration to a Tally instance.

Core responsibilities:
- Store connection details (host, port, company, etc.)
- Enforce multi-company isolation
- Manage connection state and last test results
- Provide test connection action
- Track connection health
- Drive the scheduled auto-sync queue (_cron_process_sync_queue)

Does NOT handle:
- Synchronization orchestration (delegated to the services/ layer)
- Mapping logic
"""

from datetime import timedelta

from odoo import fields, models, api
from odoo.exceptions import UserError, ValidationError
import logging

_logger = logging.getLogger(__name__)

# After this many sync attempts on a record, the scheduled queue stops
# auto-retrying it (to avoid an endlessly-failing record being hammered
# forever) - a manual "Sync to Tally" click is not subject to this cap and
# always retries regardless of how many attempts have already been made.
MAX_AUTO_SYNC_ATTEMPTS = 5

# The scheduled pull (Tally -> Odoo) re-scans a rolling date window rather
# than full history each run, for performance - this is how far back it
# looks the very first time a connection has no last_auto_pull_date yet.
# Safe regardless of the exact number: re-scanning an already-imported
# voucher is a no-op (GUID match -> skipped), never a duplicate.
INITIAL_PULL_LOOKBACK_DAYS = 7


class TallyConnection(models.Model):
    _name = "tally.connection"
    _description = "Tally ERP Connection Configuration"
    # mail.thread + mail.activity.mixin: activity_schedule() (used by
    # _check_stock_mismatch) needs BOTH - its internal notification step
    # calls message_notify(), which only mail.thread provides;
    # mail.activity.mixin alone raises AttributeError (confirmed by a
    # failing test before this was added). mail.thread's chatter/followers
    # UI was deliberately removed from this model's form earlier for
    # clutter - re-inheriting the mixin does NOT bring that back on its
    # own, only adding <chatter/> or an oe_chatter div to the view would,
    # and the view is deliberately left alone here.
    _inherit = ["mail.thread", "mail.activity.mixin"]

    # Identity
    name = fields.Char(
        string="Connection Name",
        required=True,
        tracking=True,
        help="Descriptive name for this Tally connection (e.g., 'India Branch - Main').",
    )

    company_id = fields.Many2one(
        "res.company",
        string="Odoo Company",
        required=True,
        tracking=True,
        help="Odoo company for which this Tally connection applies. One company per connection.",
    )

    # Connection parameters
    host = fields.Char(
        string="Tally Host",
        required=True,
        tracking=True,
        help="Tally server hostname or IP address (e.g., localhost, 192.168.1.10).",
    )

    port = fields.Integer(
        string="Tally Port",
        required=True,
        default=9000,
        tracking=True,
        help="Tally HTTP listening port (default 9000).",
    )

    timeout = fields.Integer(
        string="Request Timeout (seconds)",
        required=True,
        default=30,
        tracking=True,
        help="Maximum seconds to wait for Tally response before timing out.",
    )

    tally_company_name = fields.Char(
        string="Tally Company Name",
        tracking=True,
        help="Name of the company in Tally (e.g., 'My Company'). Used for multi-company filtering.",
    )

    # Connection state
    active = fields.Boolean(
        string="Active",
        default=True,
        tracking=True,
        help="Disable to prevent synchronization operations.",
    )

    connection_status = fields.Selection(
        [
            ("unknown", "Unknown"),
            ("testing", "Testing"),
            ("success", "Connected"),
            ("failed", "Failed"),
            ("timeout", "Timeout"),
            ("never_tested", "Never Tested"),
        ],
        string="Connection Status",
        default="never_tested",
        tracking=True,
        help="Status of the last test connection attempt. 'Testing' is transient - only "
        "possible on Agent-mode connections, while the async gateway probe is in flight; it "
        "settles to Connected/Failed within moments, once the agent reports back (see "
        "tally_agent_job.py's _reconcile_connection()).",
    )

    last_tested_at = fields.Datetime(
        string="Last Test Time",
        tracking=True,
        help="Timestamp of the last test connection attempt.",
    )

    last_error = fields.Text(
        string="Last Error",
        help="Error message from last failed connection attempt.",
    )

    # Sync control
    enabled_for_sync = fields.Boolean(
        string="Enabled for Sync",
        default=False,
        tracking=True,
        help="Only active, successfully tested connections with this flag enabled can sync.",
    )

    last_auto_pull_date = fields.Date(
        string="Last Auto-Pull Date",
        readonly=True,
        copy=False,
        help="Cutoff date used by the last scheduled Tally -> Odoo pull (see "
        "_cron_process_sync_queue). Advances automatically after each run; not user-editable.",
    )

    # Stock reconciliation (detection only - see services/tally_stock_reconciliation_service.py)
    stock_mismatch_threshold = fields.Float(
        string="Stock Mismatch Alert Threshold",
        default=1.0,
        help="Minimum absolute difference between Odoo's on-hand quantity and Tally's stock "
        "closing balance (for the same linked product) before the scheduled check raises an "
        "alert. Keeps small rounding differences from generating noise.",
    )

    stock_alert_user_id = fields.Many2one(
        "res.users",
        string="Stock Mismatch Alert Responsible",
        help="User notified (via an Activity on this connection) when the scheduled stock "
        "reconciliation check finds a mismatch past the threshold above. Leave empty to disable "
        "the scheduled alert (the on-demand reconciliation wizard still works either way).",
    )

    # Agent mode (see models/tally_agent_device.py, models/tally_agent_job.py,
    # and services/tally_agent_transport.py). Every sync/import service below
    # goes through _get_tally_client() -> _build_tally_client(), which wires
    # the right transport for whichever mode is selected here - no service
    # needs to know or care which one is active.
    connection_mode = fields.Selection(
        [("direct", "Direct"), ("agent", "Agent (preview)")],
        string="Connection Mode",
        default="direct",
        required=True,
        tracking=True,
        help="Direct: Odoo connects to Tally itself (VPN/network access required). "
        "Agent: a local program on the Tally PC connects out to Odoo instead, and every sync "
        "request queues for it to pick up on its next poll - requires the agent to be running "
        "and online (see the Agent Devices list below).",
    )

    agent_device_ids = fields.One2many(
        "tally.agent.device",
        "connection_id",
        string="Agent Devices",
    )

    active_agent_device_id = fields.Many2one(
        "tally.agent.device",
        string="Active Agent Device",
        compute="_compute_active_agent_device_id",
        help="The current live agent device for this connection, if a token has been generated.",
    )

    @api.depends("agent_device_ids.active")
    def _compute_active_agent_device_id(self):
        for connection in self:
            connection.active_agent_device_id = connection.agent_device_ids.filtered("active")[:1]

    def action_generate_agent_token(self):
        """
        Generate a new Agent enrollment token for this connection, revoking
        any previous one, and show it exactly once via a reveal wizard.
        """
        self.ensure_one()
        token = self.env["tally.agent.device"].generate(self)
        wizard = self.env["tally.agent.token.reveal.wizard"].create(
            {"connection_id": self.id, "token": token}
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "tally.agent.token.reveal.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_view_agent_jobs(self):
        """Open the Agent Sync Timeline filtered to this connection (Phase 4 - Observability)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Agent Sync Timeline - {self.name}",
            "res_model": "tally.agent.job",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
            "context": {"search_default_filter_failed": 0},
        }

    # Metadata
    created_by = fields.Char(
        string="Created By",
        help="User/context that created this connection record.",
    )

    notes = fields.Text(
        string="Notes",
        help="Internal notes about this connection setup or special considerations.",
    )

    _company_unique = models.Constraint(
        "UNIQUE(company_id)",
        "One Tally connection per Odoo company.",
    )

    @api.model
    def create(self, vals):
        """Create connection record."""
        record = super().create(vals)
        _logger.info(
            f"Tally connection created: {record.name} -> {record.host}:{record.port}",
            extra={"connection_id": record.id, "company_id": record.company_id.id},
        )
        return record

    def write(self, vals):
        """Update connection record."""
        result = super().write(vals)

        # Clear sync flag if host/port/timeout changed (requires re-test)
        if any(field in vals for field in ["host", "port", "timeout"]):
            self.write(
                {
                    "enabled_for_sync": False,
                    "connection_status": "unknown",
                    "last_error": "Connection parameters changed - requires re-test",
                }
            )

        return result

    @api.constrains("host", "port", "timeout")
    def _validate_connection_params(self):
        """Validate connection parameters."""
        for record in self:
            if not record.host or not record.host.strip():
                raise ValidationError("Tally host cannot be empty.")

            if record.port < 1 or record.port > 65535:
                raise ValidationError("Tally port must be between 1 and 65535.")

            if record.timeout < 1 or record.timeout > 300:
                raise ValidationError("Request timeout must be between 1 and 300 seconds.")

    def action_test_connection(self):
        """
        Test connection to Tally. On an Agent-mode connection, the actual
        probe is async (see AgentQueueTransport.test_connection()'s
        docstring for why a synchronous wait can never reliably work here) -
        this shows a "Testing" notification and connection_status, and the
        real Connected/Failed result lands moments later via
        tally_agent_job.py's submit_result()/_reconcile_connection(), once
        the agent actually reports back. Direct-mode connections are
        unaffected - HttpXmlTransport still answers inline, same as always.
        """
        self.ensure_one()

        try:
            client = self._build_tally_client()

            result = client.test_connection()

            if result.get("queued"):
                self.write(
                    {
                        "connection_status": "testing",
                        "last_tested_at": fields.Datetime.now(),
                        "last_error": None,
                    }
                )
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Testing",
                        "message": "Test queued for the Tally Agent - status will update in a "
                        "few seconds. Refresh to see the result.",
                        "type": "info",
                        "sticky": False,
                    },
                }

            # Update connection state
            if result["success"]:
                self.write(
                    {
                        "connection_status": "success",
                        "last_tested_at": fields.Datetime.now(),
                        "last_error": None,
                    }
                )
                _logger.info(
                    f"Tally connection test successful: {self.name}",
                    extra={"connection_id": self.id},
                )

                # Show success notification
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Success",
                        "message": f"Connection to Tally at {self.host}:{self.port} successful!",
                        "type": "success",
                        "sticky": False,
                    },
                }
            else:
                self.write(
                    {
                        "connection_status": "failed",
                        "last_tested_at": fields.Datetime.now(),
                        "last_error": result.get("error", "Unknown error"),
                    }
                )
                _logger.warning(
                    f"Tally connection test failed: {self.name}",
                    extra={"connection_id": self.id, "error": result.get("error")},
                )

                # Show error notification
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Connection Failed",
                        "message": f"Cannot connect to Tally: {result.get('message', 'Unknown error')}",
                        "type": "danger",
                        "sticky": True,
                    },
                }

        except Exception as e:
            error_msg = str(e)
            self.write(
                {
                    "connection_status": "failed",
                    "last_tested_at": fields.Datetime.now(),
                    "last_error": error_msg,
                }
            )
            _logger.error(
                f"Exception during Tally connection test: {self.name}",
                extra={"connection_id": self.id, "error": error_msg},
                exc_info=True,
            )

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error testing connection: {error_msg}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_fetch_companies(self):
        """Fetch companies from Tally and show in dialog."""
        self.ensure_one()

        try:
            client = self._build_tally_client()

            result = client.fetch_companies()

            if result["success"]:
                company_list = "\n".join([f"• {name}" for name in result["companies"]])
                message = f"Companies available in Tally:\n\n{company_list}"

                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Tally Companies",
                        "message": message,
                        "type": "info",
                        "sticky": True,
                    },
                }
            else:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Error",
                        "message": f"Cannot fetch companies: {result.get('error', 'Unknown error')}",
                        "type": "danger",
                        "sticky": True,
                    },
                }

        except Exception as e:
            _logger.error(f"Exception fetching Tally companies: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_fetch_ledgers(self):
        """
        Fetch Ledgers (Name + GUID) from Tally and show in dialog.

        Used to manually resolve an ambiguous-name import conflict: when two
        distinct Odoo contacts share a name, the admin looks up the correct
        GUID here and pastes it into the intended contact's Tally GUID field
        (editable while not yet successfully synced) before re-running import.
        """
        self.ensure_one()

        try:
            client = self._build_tally_client()

            result = client.fetch_ledgers(company=self.tally_company_name)

            if result["success"]:
                ledger_list = "\n".join(
                    f"• {l.get('LedgerName')} (Group: {l.get('LedgerParent')}) - GUID: {l.get('LedgerGuid')}"
                    for l in result["ledgers"]
                )
                message = f"Ledgers available in Tally:\n\n{ledger_list}" if ledger_list else "No ledgers found."

                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Tally Ledgers",
                        "message": message,
                        "type": "info",
                        "sticky": True,
                    },
                }
            else:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Error",
                        "message": f"Cannot fetch ledgers: {result.get('error', 'Unknown error')}",
                        "type": "danger",
                        "sticky": True,
                    },
                }

        except Exception as e:
            _logger.error(f"Exception fetching Tally ledgers: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_import_products_from_tally(self, name_filter=None):
        """Import Stock Items from Tally as Odoo products (Tally -> Odoo direction).

        Args:
            name_filter (str): if given, only the Tally stock item with this
                exact name (case-insensitive) is imported.
        """
        self.ensure_one()

        from ..services import TallyProductImportService, TallyConnectorError

        try:
            service = TallyProductImportService(self.env)
            result = service.import_products(self, name_filter=name_filter)

            if not result["success"]:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Import Failed",
                        "message": result.get("message", "Unknown error"),
                        "type": "danger",
                        "sticky": True,
                    },
                }

            details = ""
            if result["errors"]:
                error_lines = "\n".join(f"• {e['name']}: {e['error']}" for e in result["errors"])
                details = f"\n\nErrors:\n{error_lines}"

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import from Tally Complete",
                    "message": f"{result['message']}{details}",
                    "type": "warning" if result["errors"] else "success",
                    "sticky": bool(result["errors"]),
                },
            }

        except TallyConnectorError as e:
            _logger.warning(
                f"Tally product import blocked: {self.name}",
                extra={"connection_id": self.id, "error": e.message},
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import Failed",
                    "message": e.message,
                    "type": "danger",
                    "sticky": True,
                },
            }
        except Exception as e:
            _logger.error(f"Exception importing Tally products: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_import_partners_from_tally(self, name_filter=None):
        """Import customer Ledgers from Tally as Odoo contacts (Tally -> Odoo direction).

        Args:
            name_filter (str): if given, only the Tally ledger with this
                exact name (case-insensitive) is imported.
        """
        self.ensure_one()

        from ..services import TallyPartnerImportService, TallyConnectorError

        try:
            service = TallyPartnerImportService(self.env)
            result = service.import_partners(self, name_filter=name_filter)

            if not result["success"]:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Import Failed",
                        "message": result.get("message", "Unknown error"),
                        "type": "danger",
                        "sticky": True,
                    },
                }

            details = ""
            if result["errors"]:
                error_lines = "\n".join(f"• {e['name']}: {e['error']}" for e in result["errors"])
                details = f"\n\nErrors:\n{error_lines}"

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import from Tally Complete",
                    "message": f"{result['message']}{details}",
                    "type": "warning" if result["errors"] else "success",
                    "sticky": bool(result["errors"]),
                },
            }

        except TallyConnectorError as e:
            _logger.warning(
                f"Tally partner import blocked: {self.name}",
                extra={"connection_id": self.id, "error": e.message},
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import Failed",
                    "message": e.message,
                    "type": "danger",
                    "sticky": True,
                },
            }
        except Exception as e:
            _logger.error(f"Exception importing Tally partners: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_import_invoices_from_tally(self, date_from, date_to):
        """Import Sales vouchers from Tally as draft Odoo invoices (Tally -> Odoo direction).

        Args:
            date_from (date): start date (inclusive)
            date_to (date): end date (inclusive)
        """
        self.ensure_one()

        from ..services import TallyInvoiceImportService, TallyConnectorError

        try:
            service = TallyInvoiceImportService(self.env)
            result = service.import_invoices(self, date_from=date_from, date_to=date_to)

            if not result["success"]:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Import Failed",
                        "message": result.get("message", "Unknown error"),
                        "type": "danger",
                        "sticky": True,
                    },
                }

            details = ""
            if result["errors"]:
                error_lines = "\n".join(f"• {e['name']}: {e['error']}" for e in result["errors"])
                details = f"\n\nErrors:\n{error_lines}"

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import from Tally Complete",
                    "message": f"{result['message']}{details}",
                    "type": "warning" if result["errors"] else "success",
                    "sticky": bool(result["errors"]),
                },
            }

        except TallyConnectorError as e:
            _logger.warning(
                f"Tally invoice import blocked: {self.name}",
                extra={"connection_id": self.id, "error": e.message},
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import Failed",
                    "message": e.message,
                    "type": "danger",
                    "sticky": True,
                },
            }
        except Exception as e:
            _logger.error(f"Exception importing Tally invoices: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_import_bills_from_tally(self, date_from, date_to):
        """Import Purchase vouchers from Tally as draft Odoo vendor bills (Tally -> Odoo direction).

        Args:
            date_from (date): start date (inclusive)
            date_to (date): end date (inclusive)
        """
        self.ensure_one()

        from ..services import TallyBillImportService, TallyConnectorError

        try:
            service = TallyBillImportService(self.env)
            result = service.import_bills(self, date_from=date_from, date_to=date_to)

            if not result["success"]:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Import Failed",
                        "message": result.get("message", "Unknown error"),
                        "type": "danger",
                        "sticky": True,
                    },
                }

            details = ""
            if result["errors"]:
                error_lines = "\n".join(f"• {e['name']}: {e['error']}" for e in result["errors"])
                details = f"\n\nErrors:\n{error_lines}"

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import from Tally Complete",
                    "message": f"{result['message']}{details}",
                    "type": "warning" if result["errors"] else "success",
                    "sticky": bool(result["errors"]),
                },
            }

        except TallyConnectorError as e:
            _logger.warning(
                f"Tally bill import blocked: {self.name}",
                extra={"connection_id": self.id, "error": e.message},
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import Failed",
                    "message": e.message,
                    "type": "danger",
                    "sticky": True,
                },
            }
        except Exception as e:
            _logger.error(f"Exception importing Tally bills: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_import_notes_from_tally(self, date_from, date_to, note_type):
        """Import Credit/Debit Note vouchers from Tally as draft Odoo records (Tally -> Odoo direction).

        Args:
            date_from (date): start date (inclusive)
            date_to (date): end date (inclusive)
            note_type (str): "credit_note" or "debit_note"
        """
        self.ensure_one()

        from ..services import TallyCreditDebitNoteImportService, TallyConnectorError

        try:
            service = TallyCreditDebitNoteImportService(self.env)
            result = service.import_notes(self, date_from=date_from, date_to=date_to, note_type=note_type)

            if not result["success"]:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Import Failed",
                        "message": result.get("message", "Unknown error"),
                        "type": "danger",
                        "sticky": True,
                    },
                }

            details = ""
            if result["errors"]:
                error_lines = "\n".join(f"• {e['name']}: {e['error']}" for e in result["errors"])
                details = f"\n\nErrors:\n{error_lines}"

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import from Tally Complete",
                    "message": f"{result['message']}{details}",
                    "type": "warning" if result["errors"] else "success",
                    "sticky": bool(result["errors"]),
                },
            }

        except TallyConnectorError as e:
            _logger.warning(
                f"Tally note import blocked: {self.name}",
                extra={"connection_id": self.id, "error": e.message},
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import Failed",
                    "message": e.message,
                    "type": "danger",
                    "sticky": True,
                },
            }
        except Exception as e:
            _logger.error(f"Exception importing Tally notes: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_import_payments_from_tally(self, date_from, date_to, voucher_kind):
        """Import Receipt/Payment vouchers from Tally as draft Odoo payments (Tally -> Odoo direction).

        Args:
            date_from (date): start date (inclusive)
            date_to (date): end date (inclusive)
            voucher_kind (str): "receipt" or "payment"
        """
        self.ensure_one()

        from ..services import TallyPaymentImportService, TallyConnectorError

        try:
            service = TallyPaymentImportService(self.env)
            result = service.import_payments(
                self, date_from=date_from, date_to=date_to, voucher_kind=voucher_kind
            )

            if not result["success"]:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Import Failed",
                        "message": result.get("message", "Unknown error"),
                        "type": "danger",
                        "sticky": True,
                    },
                }

            details = ""
            if result["errors"]:
                error_lines = "\n".join(f"• {e['name']}: {e['error']}" for e in result["errors"])
                details = f"\n\nErrors:\n{error_lines}"

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import from Tally Complete",
                    "message": f"{result['message']}{details}",
                    "type": "warning" if result["errors"] else "success",
                    "sticky": bool(result["errors"]),
                },
            }

        except TallyConnectorError as e:
            _logger.warning(
                f"Tally payment import blocked: {self.name}",
                extra={"connection_id": self.id, "error": e.message},
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Import Failed",
                    "message": e.message,
                    "type": "danger",
                    "sticky": True,
                },
            }
        except Exception as e:
            _logger.error(f"Exception importing Tally payments: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    def action_suggest_mappings_from_tally(self):
        """
        Auto-fill unambiguous Tally Ledger Name mappings on accounts, taxes,
        and journals that don't have one yet, by exact-matching their Odoo
        name against Tally's own ledger list.
        """
        self.ensure_one()

        from ..services import TallyMappingSuggestionService, TallyConnectorError

        try:
            service = TallyMappingSuggestionService(self.env)
            result = service.suggest_mappings(self)

            if not result["success"]:
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Suggestion Failed",
                        "message": result.get("message", "Unknown error"),
                        "type": "danger",
                        "sticky": True,
                    },
                }

            details = ""
            for model, names in result["applied"].items():
                if names:
                    details += f"\n\n{model} ({len(names)}):\n" + "\n".join(f"• {n}" for n in names)
            if result["skipped_ambiguous"]:
                details += "\n\nAmbiguous (skipped):\n" + "\n".join(
                    f"• {s}" for s in result["skipped_ambiguous"]
                )

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Mapping Suggestions Applied",
                    "message": f"{result['message']}{details}",
                    "type": "warning" if result["skipped_ambiguous"] else "success",
                    "sticky": True,
                },
            }

        except TallyConnectorError as e:
            _logger.warning(
                f"Tally mapping suggestion blocked: {self.name}",
                extra={"connection_id": self.id, "error": e.message},
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Suggestion Failed",
                    "message": e.message,
                    "type": "danger",
                    "sticky": True,
                },
            }
        except Exception as e:
            _logger.error(f"Exception suggesting Tally mappings: {str(e)}", exc_info=True)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Error",
                    "message": f"Unexpected error: {str(e)}",
                    "type": "danger",
                    "sticky": True,
                },
            }

    @api.model
    def _cron_process_sync_queue(self):
        """
        Scheduled entry point (see data/tally_sync_queue_cron.xml): for
        every active, sync-enabled connection, automatically process BOTH
        directions without requiring any manual button click:

        1. Push (Odoo -> Tally): sync whatever is pending - never-synced
           (draft) or previously-failed records, capped at
           MAX_AUTO_SYNC_ATTEMPTS attempts each.
        2. Pull (Tally -> Odoo): re-run every import service (products,
           contacts, invoices, bills, credit/debit notes, receipts/
           payments) so records created directly in Tally - e.g. while
           this connection was unreachable - get picked up automatically,
           the same as clicking each "Import ... from Tally" button.

        Products and partners are pushed before the transactional
        documents that depend on them, in the same run, so a newly-created
        invoice whose customer wasn't synced yet gets a chance to succeed
        later in this very run, once that customer's own sync (also
        picked up here) has just completed.

        Concurrency note: this deliberately does not use any extra
        record-level locking beyond what each sync already does. A manual
        "Sync to Tally" click racing this cron on the same record is safe
        by construction, not by locking - every push is idempotent via a
        deterministic Tally GUID/REMOTEID (see each sync service's
        _compute_sync_key), and every pull matches by that same GUID before
        creating anything, so the worst case is a redundant Alter call or a
        harmless re-check of an already-imported voucher, never a
        duplicate.
        """
        connections = self.search([("active", "=", True), ("enabled_for_sync", "=", True)])
        for connection in connections:
            connection._sync_pending_queue()
            connection._pull_pending_from_tally()

    def _sync_pending_queue(self, auto_commit=True):
        """
        Sync this connection's company's pending queue, in dependency order.

        Args:
            auto_commit (bool): commit after each record (see
                _sync_pending_records) - True for the real scheduled cron;
                tests pass False since Odoo's TransactionCase forbids
                committing the test cursor.
        """
        self.ensure_one()
        company = self.company_id

        pending_products = self.env["product.product"].search(
            [
                ("tally_sync_status", "in", ("draft", "failed")),
                ("tally_sync_attempts", "<", MAX_AUTO_SYNC_ATTEMPTS),
                "|", ("company_id", "=", company.id), ("company_id", "=", False),
            ]
        )
        self._sync_pending_records(pending_products, auto_commit=auto_commit)

        pending_partners = self.env["res.partner"].search(
            [
                ("tally_sync_status", "in", ("draft", "failed")),
                ("tally_sync_attempts", "<", MAX_AUTO_SYNC_ATTEMPTS),
                "|", ("company_id", "=", company.id), ("company_id", "=", False),
                "|", ("customer_rank", ">", 0), ("supplier_rank", ">", 0),
            ]
        )
        self._sync_pending_records(pending_partners, auto_commit=auto_commit)

        pending_moves = self.env["account.move"].search(
            [
                ("move_type", "in", ("out_invoice", "in_invoice", "out_refund", "in_refund", "entry")),
                ("state", "=", "posted"),
                ("company_id", "=", company.id),
                ("tally_sync_status", "in", ("draft", "failed")),
                ("tally_sync_attempts", "<", MAX_AUTO_SYNC_ATTEMPTS),
            ]
        )
        self._sync_pending_records(pending_moves, auto_commit=auto_commit)

        pending_payments = self.env["account.payment"].search(
            [
                ("state", "in", ("paid", "reconciled")),
                ("company_id", "=", company.id),
                ("tally_sync_status", "in", ("draft", "failed")),
                ("tally_sync_attempts", "<", MAX_AUTO_SYNC_ATTEMPTS),
            ]
        )
        self._sync_pending_records(pending_payments, auto_commit=auto_commit)

    def _sync_pending_records(self, records, auto_commit=True):
        """
        Call action_sync_to_tally() on each record, one at a time, isolating
        failures so one bad record can't abort the rest of the queue - and
        (when auto_commit) committing after each so a crash partway through
        this cron run doesn't lose progress already made.
        """
        for record in records:
            try:
                record.action_sync_to_tally()
            except Exception:
                _logger.exception(
                    f"Tally auto-sync (scheduled queue) failed for {record._name}({record.id})",
                    extra={"model": record._name, "record_id": record.id},
                )
            if auto_commit:
                self.env.cr.commit()

    def _pull_pending_from_tally(self, auto_commit=True):
        """
        Re-run every Tally -> Odoo import service for this connection, the
        same way clicking each "Import ... from Tally" button would.
        Masters (products, contacts) are re-scanned in full each run - a
        full Tally ledger/stock-item list is cheap and already-imported
        records are skipped instantly by GUID match. Transactional vouchers
        (invoices, bills, notes, receipts/payments) use a rolling date
        window instead of full history, for performance: from
        last_auto_pull_date (with a 1-day overlap buffer, in case a
        voucher landed right at a previous run's boundary) through today.
        """
        self.ensure_one()

        from ..services import (
            TallyProductImportService,
            TallyPartnerImportService,
            TallyInvoiceImportService,
            TallyBillImportService,
            TallyCreditDebitNoteImportService,
            TallyPaymentImportService,
        )

        date_to = fields.Date.context_today(self)
        watermark = self.last_auto_pull_date or (date_to - timedelta(days=INITIAL_PULL_LOOKBACK_DAYS))
        date_from = watermark - timedelta(days=1)

        self._run_pull("products", lambda: TallyProductImportService(self.env).import_products(self), auto_commit)
        self._run_pull("contacts", lambda: TallyPartnerImportService(self.env).import_partners(self), auto_commit)
        self._run_pull(
            "sales vouchers",
            lambda: TallyInvoiceImportService(self.env).import_invoices(self, date_from, date_to),
            auto_commit,
        )
        self._run_pull(
            "purchase vouchers",
            lambda: TallyBillImportService(self.env).import_bills(self, date_from, date_to),
            auto_commit,
        )
        for note_type in ("credit_note", "debit_note"):
            self._run_pull(
                f"{note_type} vouchers",
                lambda note_type=note_type: TallyCreditDebitNoteImportService(self.env).import_notes(
                    self, date_from, date_to, note_type
                ),
                auto_commit,
            )
        for voucher_kind in ("receipt", "payment"):
            self._run_pull(
                f"{voucher_kind} vouchers",
                lambda voucher_kind=voucher_kind: TallyPaymentImportService(self.env).import_payments(
                    self, date_from, date_to, voucher_kind
                ),
                auto_commit,
            )

        self.last_auto_pull_date = date_to
        if auto_commit:
            self.env.cr.commit()

    def _run_pull(self, label, call, auto_commit=True):
        """
        Run one import service call, isolating failures (raised exceptions
        as well as a returned {"success": False, ...} result) so one
        import type failing - e.g. an unmapped voucher, or Tally being
        briefly unreachable mid-run - doesn't stop the rest of the pull.
        """
        try:
            result = call()
            if isinstance(result, dict) and not result.get("success", True):
                _logger.warning(
                    f"Tally auto-pull ({label}) reported failure for connection {self.id}: "
                    f"{result.get('message')}",
                    extra={"connection_id": self.id, "pull_type": label},
                )
        except Exception:
            _logger.exception(
                f"Tally auto-pull ({label}) raised for connection {self.id}",
                extra={"connection_id": self.id, "pull_type": label},
            )
        if auto_commit:
            self.env.cr.commit()

    @api.model
    def _cron_check_stock_mismatch(self):
        """
        Scheduled entry point (see data/tally_stock_reconciliation_cron.xml):
        for every active, sync-enabled connection with a
        stock_alert_user_id configured, compare Odoo's on-hand quantity
        against Tally's stock closing balance for every linked product,
        and raise ONE activity on the connection (not one per product -
        threshold alerts that flood the inbox get ignored) summarizing
        every product past stock_mismatch_threshold.

        This is detection only - see
        services/tally_stock_reconciliation_service.py's docstring for why
        automatically "fixing" the mismatch is out of scope on purpose.
        """
        connections = self.search(
            [
                ("active", "=", True),
                ("enabled_for_sync", "=", True),
                ("stock_alert_user_id", "!=", False),
            ]
        )
        for connection in connections:
            connection._check_stock_mismatch()
            self.env.cr.commit()

    def _check_stock_mismatch(self):
        self.ensure_one()

        from ..services import TallyStockReconciliationService

        try:
            service = TallyStockReconciliationService(self.env)
            result = service.reconcile(self)
        except Exception:
            _logger.exception(
                f"Tally stock mismatch check failed for connection {self.id}",
                extra={"connection_id": self.id},
            )
            return

        if not result["success"]:
            _logger.warning(
                f"Tally stock mismatch check could not fetch Tally data for connection "
                f"{self.id}: {result.get('message')}",
                extra={"connection_id": self.id},
            )
            return

        threshold = self.stock_mismatch_threshold
        flagged = [row for row in result["rows"] if abs(row["difference"]) > threshold]
        if not flagged:
            return

        lines = "\n".join(
            f"- {row['product'].display_name}: Odoo {row['odoo_qty']:g}, Tally "
            f"{row['tally_qty']:g} (diff {row['difference']:+g})"
            for row in flagged
        )
        self.activity_schedule(
            "mail.mail_activity_data_todo",
            summary=f"Tally stock mismatch: {len(flagged)} product(s) past threshold",
            note=(
                f"Scheduled stock reconciliation found {len(flagged)} product(s) where Odoo's "
                f"on-hand quantity differs from Tally's by more than {threshold:g}:\n\n{lines}\n\n"
                f"This can happen when a sale/purchase is recorded in one system while the "
                f"connection to the other is down. Review manually - neither Odoo nor Tally is "
                f"assumed correct automatically."
            ),
            user_id=self.stock_alert_user_id.id,
        )

    def action_open_stock_reconciliation(self):
        """Open the on-demand stock reconciliation wizard for this connection."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Stock Reconciliation",
            "res_model": "tally.stock.reconciliation.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_connection_id": self.id},
        }

    def action_open_opening_balance_wizard(self):
        """Open the one-time opening balance push wizard for this connection (Phase 10)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Push Opening Balances",
            "res_model": "tally.opening.balance.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_connection_id": self.id},
        }

    def _get_tally_client(self):
        """
        Get an initialized Tally client for this connection.

        Returns:
            TallyClient: Client instance

        Raises:
            UserError: If connection is not properly configured
        """
        if not self.active:
            raise UserError(f"Connection '{self.name}' is not active.")

        if self.connection_status != "success":
            raise UserError(f"Connection '{self.name}' has not been tested successfully.")

        return self._build_tally_client()

    def _build_tally_client(self):
        """
        Build a TallyClient wired to the right transport for connection_mode -
        direct mode talks to Tally over HTTP itself (HttpXmlTransport,
        TallyClient's own default); agent mode routes through the job queue
        instead (AgentQueueTransport - see services/tally_agent_transport.py),
        since Odoo has no direct network path to an agent-mode Tally.

        Deliberately does NOT check active/connection_status - action_test_connection()
        calls this directly (that's the action that sets connection_status in
        the first place), while _get_tally_client() checks those before delegating here.
        """
        self.ensure_one()
        from ..services import TallyClient, AgentQueueTransport

        transport = AgentQueueTransport(self, self.env) if self.connection_mode == "agent" else None
        return TallyClient(
            host=self.host,
            port=self.port,
            timeout=self.timeout,
            verify_ssl=False,
            transport=transport,
        )
