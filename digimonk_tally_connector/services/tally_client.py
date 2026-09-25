"""
Tally client - orchestrates communication with Tally.

Responsibilities:
- manage transport
- handle request/response flow
- distinguish transport errors from business errors
- return structured results

Does NOT handle:
- Business logic
- Field mapping
- Sync orchestration
"""

import logging
from .tally_transport import HttpXmlTransport
from .tally_xml_builder import TallyXmlBuilder
from .tally_response_parser import TallyResponseParser
from .tally_exceptions import (
    TallyConnectorError,
    TallyTransportError,
    TallyConnectionError,
    TallyTimeoutError,
    TallyXmlError,
    TallyRemoteError,
)

_logger = logging.getLogger(__name__)


def _queued_fetch_result(list_key):
    """
    Shared "no result yet" return shape for every fetch_* method below, used
    when the transport (AgentQueueTransport, agent-mode connections only)
    enqueues a fresh job instead of returning Tally's real response inline -
    either the first call for this exact query, or a repeat call too soon
    after it (see AgentQueueTransport.fetch_or_queue()). Unlike the write
    path's _queued_result(), fetch_* callers (import services, mapping
    suggestions, stock reconciliation) already expect a plain success/
    failure result and show `message` on failure - so this reports
    success=False with a clear, actionable message rather than crashing
    trying to parse a response that doesn't exist yet.

    Carries queued=True (checked by TestFetchOperationsReuseResolvedJobs'
    tests, and by anything wanting to distinguish "no result yet" from a
    genuine Tally-side failure) - a later identical call, once the agent
    has answered, gets the real result back via fetch_or_queue() instead of
    this same "please retry" message forever.
    """
    msg = "Queued for the Tally Agent - wait a few seconds and try again to see the result."
    return {
        "success": False,
        list_key: [],
        "message": msg,
        "error": msg,
        "queued": True,
    }


def _queued_result(transport_response):
    """
    Shared "no result yet" return shape for every upsert_*/alter_* method
    below, used when the transport (AgentQueueTransport, agent-mode
    connections only) reports a job was queued instead of returning Tally's
    real response inline. Callers (sync services) must treat this as
    pending, not success or failure - the real outcome is written back onto
    the originating record later; see tally_agent_job.py's
    submit_result()/_reconcile_business_record().
    """
    return {
        "success": True,
        "queued": True,
        "created": False,
        "altered": False,
        "job_id": transport_response.get("job_id"),
        "correlation_id": transport_response.get("correlation_id"),
        "message": "Job queued for the Tally Agent - result will be applied once processed.",
        "error": None,
    }


class TallyClient:
    """
    HTTP/XML Tally client.

    Handles:
    - connection establishment
    - request/response communication
    - error classification
    - structured result return

    Does not assume:
    - business context
    - synchronization direction
    - idempotency strategy
    """

    def __init__(self, host, port=9000, timeout=None, verify_ssl=False, transport=None):
        """
        Initialize Tally client.

        Args:
            host (str): Tally host (localhost or private IP)
            port (int): Tally HTTP port (default 9000)
            timeout (int): Request timeout in seconds (default 30)
            verify_ssl (bool): Verify SSL (default False)
            transport (BaseTransport): transport to use instead of the default
                HttpXmlTransport - e.g. AgentQueueTransport for Agent-mode
                connections, which have no direct network path to Tally.
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verify_ssl = verify_ssl

        # Initialize transport
        self.transport = transport or HttpXmlTransport(
            host=host, port=port, timeout=timeout, verify_ssl=verify_ssl
        )

        # Initialize builders
        self.xml_builder = TallyXmlBuilder()
        self.xml_parser = TallyResponseParser()

        _logger.info(
            f"Tally client initialized for {host}:{port}",
            extra={"host": host, "port": port, "timeout": timeout},
        )

    def test_connection(self):
        """
        Test connectivity to Tally.

        Returns:
            dict: {
                "success": bool,
                "message": str,
                "error": str or None,
            }
        """
        return self.transport.test_connection(timeout=self.timeout)

    def send_raw_request(self, request_xml, timeout=None, res_model=None, res_id=None,
                          operation=None, idempotency_key=None):
        """
        Send raw XML request to Tally.

        Low-level method - returns transport response only.

        Args:
            request_xml (str): XML request body
            timeout (int): Override timeout
            res_model, res_id (str, int): Odoo record this request is for -
                meaningful only to AgentQueueTransport (agent-mode
                connections): lets a later async result be written back onto
                the right record. Ignored by HttpXmlTransport (direct mode),
                which already returns the real result inline.
            operation (str): descriptive name of the calling upsert_*/
                alter_* method, for logs/tracing only.
            idempotency_key (str): stable per-record key (the same
                deterministic REMOTEID/GUID passed to the XML builder) - lets
                AgentQueueTransport reuse an already-in-flight job instead of
                creating a duplicate on retry.

        Returns:
            dict: {
                "success": bool,
                "status_code": int,
                "response_xml": str,
                "error": str or None,
                "raw_response": requests.Response,
                "queued": bool (agent-mode only - True means no result yet),
            }

        Raises:
            TallyConnectionError: Network unreachable
            TallyTimeoutError: Request timeout
            TallyTransportError: HTTP or protocol error
        """
        return self.transport.send_request(
            request_xml, timeout=timeout or self.timeout,
            res_model=res_model, res_id=res_id, operation=operation, idempotency_key=idempotency_key,
        )

    def fetch_raw_request(self, request_xml, operation=None):
        """
        Send a raw XML request to Tally for a read/fetch operation - Fetch
        Companies/Ledgers, every Import ... from Tally button, Suggest
        Mappings, Stock Reconciliation. Unlike send_raw_request() (a
        fire-and-forget write), these need an actual result to do anything
        useful.

        On a direct connection this is identical to send_raw_request() - a
        direct HTTP call already returns the real result inline. On an
        agent-mode connection, a first call queues a job and returns
        {"queued": True, ...} same as send_raw_request(); a LATER call
        asking the same question (same request_xml) returns that job's
        real result once the agent has answered, instead of always saying
        "queued" - see AgentQueueTransport.fetch_or_queue()'s docstring for
        why re-clicking the same button is what makes that happen.

        Args:
            request_xml (str): XML request body
            operation (str): descriptive name of the calling fetch_* method,
                used both for logs/tracing and to key the reusable result.

        Returns:
            dict: same shape as send_raw_request()

        Raises:
            TallyConnectionError: Network unreachable
            TallyTimeoutError: Request timeout
            TallyTransportError: HTTP or protocol error
        """
        return self.transport.fetch_or_queue(request_xml, operation=operation)

    def fetch_companies(self):
        """
        Fetch list of companies from Tally.

        Returns:
            dict: {
                "success": bool,
                "companies": [list of company names],
                "raw_data": [raw response dicts],
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_company_list_request()
            transport_response = self.fetch_raw_request(xml_request, operation="fetch_companies")

            if transport_response.get("queued"):
                return _queued_fetch_result("companies")

            if not transport_response["success"]:
                return {
                    "success": False,
                    "companies": [],
                    "raw_data": [],
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            # Parse response
            parsed = self.xml_parser.parse_company_list_response(transport_response["response_xml"])

            if not parsed["success"]:
                return {
                    "success": False,
                    "companies": [],
                    "raw_data": parsed["data"],
                    "message": f"Tally error: {parsed['error']}",
                    "error": parsed["error"],
                }

            # Extract company names
            company_names = []
            for record in parsed["data"]:
                name = record.get("CompanyName", "").strip()
                if name:
                    company_names.append(name)

            return {
                "success": True,
                "companies": company_names,
                "raw_data": parsed["data"],
                "message": f"Retrieved {len(company_names)} companies",
                "error": None,
            }

        except TallyXmlError as e:
            msg = f"XML parsing error: {e.message}"
            _logger.error(msg, extra={"error": e.message})
            return {
                "success": False,
                "companies": [],
                "raw_data": [],
                "message": msg,
                "error": msg,
            }
        except (TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Connection error: {e.message}"
            _logger.error(msg, extra={"error": e.message})
            return {
                "success": False,
                "companies": [],
                "raw_data": [],
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error fetching companies: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "companies": [],
                "raw_data": [],
                "message": msg,
                "error": msg,
            }

    def fetch_ledgers(self, company=None):
        """
        Fetch list of ledgers from Tally company.

        Args:
            company (str): Tally company name (optional - uses current if None)

        Returns:
            dict: {
                "success": bool,
                "ledgers": [list of ledger dicts],
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_ledger_list_request(company=company)
            transport_response = self.fetch_raw_request(xml_request, operation="fetch_ledgers")

            if transport_response.get("queued"):
                return _queued_fetch_result("ledgers")

            if not transport_response["success"]:
                return {
                    "success": False,
                    "ledgers": [],
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            parsed = self.xml_parser.parse_ledger_list_response(transport_response["response_xml"])

            if not parsed["success"]:
                return {
                    "success": False,
                    "ledgers": [],
                    "message": f"Tally error: {parsed['error']}",
                    "error": parsed["error"],
                }

            return {
                "success": True,
                "ledgers": parsed["data"],
                "message": f"Retrieved {len(parsed['data'])} ledgers",
                "error": None,
            }

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error fetching ledgers: {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "ledgers": [],
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error fetching ledgers: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "ledgers": [],
                "message": msg,
                "error": msg,
            }

    def fetch_stock_items(self, company=None):
        """
        Fetch list of stock items (products) from Tally.

        Args:
            company (str): Tally company name (optional)

        Returns:
            dict: {
                "success": bool,
                "items": [list of stock item dicts],
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_stock_item_list_request(company=company)
            transport_response = self.fetch_raw_request(xml_request, operation="fetch_stock_items")

            if transport_response.get("queued"):
                return _queued_fetch_result("items")

            if not transport_response["success"]:
                return {
                    "success": False,
                    "items": [],
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            parsed = self.xml_parser.parse_stock_item_list_response(transport_response["response_xml"])

            if not parsed["success"]:
                return {
                    "success": False,
                    "items": [],
                    "message": f"Tally error: {parsed['error']}",
                    "error": parsed["error"],
                }

            return {
                "success": True,
                "items": parsed["data"],
                "message": f"Retrieved {len(parsed['data'])} stock items",
                "error": None,
            }

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error fetching stock items: {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "items": [],
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error fetching stock items: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "items": [],
                "message": msg,
                "error": msg,
            }

    def fetch_sales_vouchers(self, company, from_date, to_date):
        """
        Fetch Sales vouchers from Tally within a date range (Tally -> Odoo
        reverse sync direction).

        Args:
            company (str): Tally company name
            from_date (str): Start date, YYYYMMDD format (inclusive)
            to_date (str): End date, YYYYMMDD format (inclusive)

        Returns:
            dict: {
                "success": bool,
                "vouchers": [list of voucher dicts],
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_voucher_export_request(
                company=company, from_date=from_date, to_date=to_date
            )
            transport_response = self.fetch_raw_request(xml_request, operation="fetch_sales_vouchers")

            if transport_response.get("queued"):
                return _queued_fetch_result("vouchers")

            if not transport_response["success"]:
                return {
                    "success": False,
                    "vouchers": [],
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            parsed = self.xml_parser.parse_voucher_export_response(
                transport_response["response_xml"], vch_type_filter="Sales"
            )

            if not parsed["success"]:
                return {
                    "success": False,
                    "vouchers": [],
                    "message": f"Tally error: {parsed['error']}",
                    "error": parsed["error"],
                }

            return {
                "success": True,
                "vouchers": parsed["data"],
                "message": f"Retrieved {len(parsed['data'])} Sales voucher(s)",
                "error": None,
            }

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error fetching Sales vouchers: {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "vouchers": [],
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error fetching Sales vouchers: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "vouchers": [],
                "message": msg,
                "error": msg,
            }

    def fetch_purchase_vouchers(self, company, from_date, to_date):
        """
        Fetch Purchase vouchers from Tally within a date range (Tally -> Odoo
        reverse sync direction). Same Day Book export as fetch_sales_vouchers,
        filtered to VCHTYPE=Purchase instead of Sales.

        Args:
            company (str): Tally company name
            from_date (str): Start date, YYYYMMDD format (inclusive)
            to_date (str): End date, YYYYMMDD format (inclusive)

        Returns:
            dict: {
                "success": bool,
                "vouchers": [list of voucher dicts],
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_voucher_export_request(
                company=company, from_date=from_date, to_date=to_date
            )
            transport_response = self.fetch_raw_request(xml_request, operation="fetch_purchase_vouchers")

            if transport_response.get("queued"):
                return _queued_fetch_result("vouchers")

            if not transport_response["success"]:
                return {
                    "success": False,
                    "vouchers": [],
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            parsed = self.xml_parser.parse_voucher_export_response(
                transport_response["response_xml"], vch_type_filter="Purchase"
            )

            if not parsed["success"]:
                return {
                    "success": False,
                    "vouchers": [],
                    "message": f"Tally error: {parsed['error']}",
                    "error": parsed["error"],
                }

            return {
                "success": True,
                "vouchers": parsed["data"],
                "message": f"Retrieved {len(parsed['data'])} Purchase voucher(s)",
                "error": None,
            }

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error fetching Purchase vouchers: {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "vouchers": [],
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error fetching Purchase vouchers: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "vouchers": [],
                "message": msg,
                "error": msg,
            }

    def upsert_purchase_voucher(
        self, company, voucher_number, voucher_date, party_ledger, guid, ledger_entries,
        inventory_entries, action="Create", narration=None, reference=None,
        res_model=None, res_id=None,
    ):
        """
        Create or alter a Purchase Voucher in Tally.

        Args:
            (see TallyXmlBuilder.build_purchase_voucher_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_purchase_voucher_upsert_request(
                company=company,
                voucher_number=voucher_number,
                voucher_date=voucher_date,
                party_ledger=party_ledger,
                guid=guid,
                ledger_entries=ledger_entries,
                inventory_entries=inventory_entries,
                action=action,
                narration=narration,
                reference=reference,
            )
            transport_response = self.send_raw_request(
                xml_request, res_model=res_model, res_id=res_id,
                operation="upsert_purchase_voucher", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting purchase voucher '{voucher_number}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting purchase voucher '{voucher_number}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def fetch_credit_debit_notes(self, vch_type, company, from_date, to_date):
        """
        Fetch Credit Note or Debit Note vouchers from Tally within a date
        range (Tally -> Odoo reverse sync direction). Same Day Book export
        as fetch_sales_vouchers/fetch_purchase_vouchers, filtered by vch_type.

        Args:
            vch_type (str): "Credit Note" or "Debit Note"
            company (str): Tally company name
            from_date (str): Start date, YYYYMMDD format (inclusive)
            to_date (str): End date, YYYYMMDD format (inclusive)

        Returns:
            dict: {
                "success": bool,
                "vouchers": [list of voucher dicts],
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_voucher_export_request(
                company=company, from_date=from_date, to_date=to_date
            )
            transport_response = self.fetch_raw_request(xml_request, operation="fetch_credit_debit_notes")

            if transport_response.get("queued"):
                return _queued_fetch_result("vouchers")

            if not transport_response["success"]:
                return {
                    "success": False,
                    "vouchers": [],
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            parsed = self.xml_parser.parse_voucher_export_response(
                transport_response["response_xml"], vch_type_filter=vch_type
            )

            if not parsed["success"]:
                return {
                    "success": False,
                    "vouchers": [],
                    "message": f"Tally error: {parsed['error']}",
                    "error": parsed["error"],
                }

            return {
                "success": True,
                "vouchers": parsed["data"],
                "message": f"Retrieved {len(parsed['data'])} {vch_type} voucher(s)",
                "error": None,
            }

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error fetching {vch_type} vouchers: {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "vouchers": [],
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error fetching {vch_type} vouchers: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "vouchers": [],
                "message": msg,
                "error": msg,
            }

    def upsert_credit_debit_note(
        self, vch_type, company, voucher_number, voucher_date, party_ledger, guid, ledger_entries,
        inventory_entries, action="Create", narration=None, reference=None,
        res_model=None, res_id=None,
    ):
        """
        Create or alter a Credit Note or Debit Note in Tally.

        Args:
            vch_type (str): "Credit Note" or "Debit Note"
            (all other args: see TallyXmlBuilder.build_credit_debit_note_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_credit_debit_note_upsert_request(
                vch_type=vch_type,
                company=company,
                voucher_number=voucher_number,
                voucher_date=voucher_date,
                party_ledger=party_ledger,
                guid=guid,
                ledger_entries=ledger_entries,
                inventory_entries=inventory_entries,
                action=action,
                narration=narration,
                reference=reference,
            )
            transport_response = self.send_raw_request(
                xml_request, res_model=res_model, res_id=res_id,
                operation="upsert_credit_debit_note", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting {vch_type} '{voucher_number}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting {vch_type} '{voucher_number}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def fetch_receipt_payment_vouchers(self, vch_type, company, from_date, to_date):
        """
        Fetch Receipt or Payment vouchers from Tally within a date range
        (Tally -> Odoo reverse sync direction). Same Day Book export as
        fetch_sales_vouchers/fetch_purchase_vouchers, filtered by vch_type.

        Args:
            vch_type (str): "Receipt" or "Payment"
            company (str): Tally company name
            from_date (str): Start date, YYYYMMDD format (inclusive)
            to_date (str): End date, YYYYMMDD format (inclusive)

        Returns:
            dict: {
                "success": bool,
                "vouchers": [list of voucher dicts],
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_voucher_export_request(
                company=company, from_date=from_date, to_date=to_date
            )
            transport_response = self.fetch_raw_request(xml_request, operation="fetch_receipt_payment_vouchers")

            if transport_response.get("queued"):
                return _queued_fetch_result("vouchers")

            if not transport_response["success"]:
                return {
                    "success": False,
                    "vouchers": [],
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            parsed = self.xml_parser.parse_voucher_export_response(
                transport_response["response_xml"], vch_type_filter=vch_type
            )

            if not parsed["success"]:
                return {
                    "success": False,
                    "vouchers": [],
                    "message": f"Tally error: {parsed['error']}",
                    "error": parsed["error"],
                }

            return {
                "success": True,
                "vouchers": parsed["data"],
                "message": f"Retrieved {len(parsed['data'])} {vch_type} voucher(s)",
                "error": None,
            }

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error fetching {vch_type} vouchers: {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "vouchers": [],
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error fetching {vch_type} vouchers: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "vouchers": [],
                "message": msg,
                "error": msg,
            }

    def upsert_receipt_payment(
        self, vch_type, company, voucher_number, voucher_date, party_ledger, guid, ledger_entries,
        action="Create", narration=None, reference=None,
        res_model=None, res_id=None,
    ):
        """
        Create or alter a Receipt or Payment voucher in Tally.

        Args:
            vch_type (str): "Receipt" or "Payment"
            (all other args: see TallyXmlBuilder.build_receipt_payment_voucher_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_receipt_payment_voucher_upsert_request(
                vch_type=vch_type,
                company=company,
                voucher_number=voucher_number,
                voucher_date=voucher_date,
                party_ledger=party_ledger,
                guid=guid,
                ledger_entries=ledger_entries,
                action=action,
                narration=narration,
                reference=reference,
            )
            transport_response = self.send_raw_request(
                xml_request, res_model=res_model, res_id=res_id,
                operation="upsert_receipt_payment", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting {vch_type} '{voucher_number}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting {vch_type} '{voucher_number}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def upsert_journal_voucher(
        self, company, voucher_number, voucher_date, guid, ledger_entries,
        action="Create", narration=None, reference=None,
        res_model=None, res_id=None,
    ):
        """
        Create or alter a Journal voucher in Tally (Phase 8).

        Args:
            (see TallyXmlBuilder.build_journal_voucher_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_journal_voucher_upsert_request(
                company=company,
                voucher_number=voucher_number,
                voucher_date=voucher_date,
                guid=guid,
                ledger_entries=ledger_entries,
                action=action,
                narration=narration,
                reference=reference,
            )
            transport_response = self.send_raw_request(
                xml_request, res_model=res_model, res_id=res_id,
                operation="upsert_journal_voucher", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting journal voucher '{voucher_number}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting journal voucher '{voucher_number}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def upsert_contra_voucher(
        self, company, voucher_number, voucher_date, guid, ledger_entries,
        action="Create", narration=None, reference=None,
        res_model=None, res_id=None,
    ):
        """
        Create or alter a Contra voucher in Tally (Phase 9).

        Args:
            (see TallyXmlBuilder.build_contra_voucher_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_contra_voucher_upsert_request(
                company=company,
                voucher_number=voucher_number,
                voucher_date=voucher_date,
                guid=guid,
                ledger_entries=ledger_entries,
                action=action,
                narration=narration,
                reference=reference,
            )
            transport_response = self.send_raw_request(
                xml_request, res_model=res_model, res_id=res_id,
                operation="upsert_contra_voucher", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting contra voucher '{voucher_number}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting contra voucher '{voucher_number}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def upsert_stock_adjustment(
        self, company, voucher_number, voucher_date, guid, stock_item_name, quantity, unit,
        action="Create", narration=None,
    ):
        """
        Create or alter a Physical Stock voucher in Tally, recording a
        manually-decided quantity correction.

        Args:
            (see TallyXmlBuilder.build_stock_adjustment_voucher_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_stock_adjustment_voucher_upsert_request(
                company=company,
                voucher_number=voucher_number,
                voucher_date=voucher_date,
                guid=guid,
                stock_item_name=stock_item_name,
                quantity=quantity,
                unit=unit,
                action=action,
                narration=narration,
            )
            transport_response = self.send_raw_request(
                xml_request, operation="upsert_stock_adjustment", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting stock adjustment '{voucher_number}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting stock adjustment '{voucher_number}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def upsert_stock_item(self, company, name, base_unit, guid, action="Create", old_name=None, parent_group=None,
                           res_model=None, res_id=None):
        """
        Create or alter a Stock Item (product) master in Tally.

        Args:
            company (str): Tally company name to import into
            name (str): Stock item name
            base_unit (str): Tally unit name (must already exist in Tally)
            guid (str): Client-generated deterministic GUID for idempotency
            action (str): "Create" or "Alter"
            old_name (str): Previous synced name (for rename handling on Alter)
            parent_group (str): optional Tally Stock Group name (Phase 11)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_stock_item_upsert_request(
                company=company,
                name=name,
                base_unit=base_unit,
                guid=guid,
                action=action,
                old_name=old_name,
                parent_group=parent_group,
            )
            transport_response = self.send_raw_request(
                xml_request, res_model=res_model, res_id=res_id,
                operation="upsert_stock_item", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting stock item '{name}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting stock item '{name}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def upsert_stock_group(self, company, name, parent_group=None, action="Create", old_name=None):
        """
        Create or alter a Stock Group master in Tally (Phase 11).

        Args:
            (see TallyXmlBuilder.build_stock_group_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_stock_group_upsert_request(
                company=company,
                name=name,
                parent_group=parent_group,
                action=action,
                old_name=old_name,
            )
            transport_response = self.send_raw_request(xml_request, operation="upsert_stock_group")

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting stock group '{name}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting stock group '{name}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def fetch_stock_groups(self, company=None):
        """
        Fetch list of Stock Groups from Tally (Phase 11).

        Returns:
            dict: {
                "success": bool,
                "stock_groups": [list of stock group dicts],
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_stock_group_list_request(company=company)
            transport_response = self.fetch_raw_request(xml_request, operation="fetch_stock_groups")

            if transport_response.get("queued"):
                return _queued_fetch_result("stock_groups")

            if not transport_response["success"]:
                return {
                    "success": False,
                    "stock_groups": [],
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            parsed = self.xml_parser.parse_stock_group_list_response(transport_response["response_xml"])

            if not parsed["success"]:
                return {
                    "success": False,
                    "stock_groups": [],
                    "message": f"Tally error: {parsed['error']}",
                    "error": parsed["error"],
                }

            return {
                "success": True,
                "stock_groups": parsed["data"],
                "message": f"Retrieved {len(parsed['data'])} stock groups",
                "error": None,
            }

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error fetching stock groups: {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "stock_groups": [],
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error fetching stock groups: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "stock_groups": [],
                "message": msg,
                "error": msg,
            }

    def upsert_unit(self, company, name, action="Create"):
        """
        Create or alter a simple Unit of Measure master in Tally (Phase 11).

        Args:
            (see TallyXmlBuilder.build_unit_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_unit_upsert_request(company=company, name=name, action=action)
            transport_response = self.send_raw_request(xml_request, operation="upsert_unit")

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting unit '{name}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting unit '{name}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def upsert_ledger(
        self, company, name, parent_group, guid, action="Create", old_name=None,
        address_lines=None, phone=None, email=None,
        res_model=None, res_id=None,
    ):
        """
        Create or alter a Ledger (customer/vendor) master in Tally.

        Args:
            company (str): Tally company name to import into
            name (str): Ledger name
            parent_group (str): Tally Ledger Group (must already exist)
            guid (str): Client-generated deterministic GUID for idempotency
            action (str): "Create" or "Alter"
            old_name (str): Previous synced name (for rename handling on Alter)
            address_lines (list[str]): optional address lines
            phone (str): optional phone number
            email (str): optional email address

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_ledger_upsert_request(
                company=company,
                name=name,
                parent_group=parent_group,
                guid=guid,
                action=action,
                old_name=old_name,
                address_lines=address_lines,
                phone=phone,
                email=email,
            )
            transport_response = self.send_raw_request(
                xml_request, res_model=res_model, res_id=res_id,
                operation="upsert_ledger", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting ledger '{name}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting ledger '{name}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def alter_ledger_opening_balance(self, company, ledger_name, opening_balance):
        """
        Alter just the OPENINGBALANCE of an already-existing Tally ledger (Phase 10).

        Args:
            (see TallyXmlBuilder.build_ledger_opening_balance_alter_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_ledger_opening_balance_alter_request(
                company=company,
                ledger_name=ledger_name,
                opening_balance=opening_balance,
            )
            transport_response = self.send_raw_request(xml_request, operation="alter_ledger_opening_balance")

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error altering opening balance for ledger '{ledger_name}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error altering opening balance for ledger '{ledger_name}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }

    def upsert_sales_voucher(
        self, company, voucher_number, voucher_date, party_ledger, guid, ledger_entries,
        inventory_entries, action="Create", narration=None, reference=None,
        res_model=None, res_id=None,
    ):
        """
        Create or alter a Sales Voucher in Tally.

        Args:
            (see TallyXmlBuilder.build_sales_voucher_upsert_request)

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "message": str,
                "error": str or None,
            }
        """
        try:
            xml_request = self.xml_builder.build_sales_voucher_upsert_request(
                company=company,
                voucher_number=voucher_number,
                voucher_date=voucher_date,
                party_ledger=party_ledger,
                guid=guid,
                ledger_entries=ledger_entries,
                inventory_entries=inventory_entries,
                action=action,
                narration=narration,
                reference=reference,
            )
            transport_response = self.send_raw_request(
                xml_request, res_model=res_model, res_id=res_id,
                operation="upsert_sales_voucher", idempotency_key=guid,
            )

            if transport_response.get("queued"):
                return _queued_result(transport_response)

            if not transport_response["success"]:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "message": f"Transport error: {transport_response['error']}",
                    "error": transport_response["error"],
                }

            return self.xml_parser.parse_master_import_response(transport_response["response_xml"])

        except (TallyXmlError, TallyConnectionError, TallyTimeoutError, TallyTransportError) as e:
            msg = f"Error upserting sales voucher '{voucher_number}': {e.message if hasattr(e, 'message') else str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
        except Exception as e:
            msg = f"Unexpected error upserting sales voucher '{voucher_number}': {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            return {
                "success": False,
                "created": False,
                "altered": False,
                "message": msg,
                "error": msg,
            }
