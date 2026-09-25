"""
Transport abstraction layer for Tally communication.

Current implementation: HTTP/XML over REST
Future implementations: LocalAgentTransport, VPNTransport, etc.

The sync orchestration layer must not care which transport is used.
"""

import requests
import logging
from abc import ABC, abstractmethod
from .tally_exceptions import (
    TallyTransportError,
    TallyConnectionError,
    TallyTimeoutError,
    TallyXmlError,
)

_logger = logging.getLogger(__name__)


class BaseTransport(ABC):
    """Abstract base transport for Tally communication."""

    @abstractmethod
    def send_request(self, request_xml, timeout=None, **kwargs):
        """
        Send XML request to Tally and return response.

        Args:
            request_xml (str): XML request body
            timeout (int): Request timeout in seconds
            **kwargs: transport-specific extras a caller may pass through
                (e.g. res_model/res_id/operation/idempotency_key - meaningful
                only to AgentQueueTransport's async job queue; HttpXmlTransport
                accepts and ignores them, since a direct HTTP call has no job
                to correlate).

        Returns:
            dict: {
                "success": bool,
                "status_code": int,
                "response_xml": str,
                "error": str or None,
                "raw_response": requests.Response or None
            }

        Raises:
            TallyConnectionError: Network unreachable, host unreachable
            TallyTimeoutError: Request timed out
            TallyTransportError: HTTP error or protocol violation
        """
        pass

    @abstractmethod
    def test_connection(self, timeout=None):
        """
        Test transport connectivity to Tally endpoint.

        Returns:
            dict: {
                "success": bool,
                "message": str,
                "error": str or None
            }
        """
        pass

    def fetch_or_queue(self, request_xml, operation=None):
        """
        For read/fetch operations (Fetch Companies/Ledgers, every Import
        ... from Tally button, Suggest Mappings, Stock Reconciliation) -
        unlike a sync write's fire-and-forget, these need a real result to
        do anything useful.

        Default (this class): identical to send_request() - a direct HTTP
        call already returns the real result inline, so there is nothing to
        queue or reuse. AgentQueueTransport overrides this to check for an
        already-resolved job from a recent identical request before
        enqueuing a new one - see its docstring for why that's the only way
        to get a real result back under a queue-based architecture.
        """
        return self.send_request(request_xml)


class HttpXmlTransport(BaseTransport):
    """
    HTTP/XML transport for Tally communication.

    Assumes Tally is reachable via HTTP on the configured host:port.
    Default port: 9000

    Current scope:
    - localhost
    - private LAN (192.168.x.x, 10.x.x.x, etc.)

    NOT suitable for:
    - public Internet Tally (dangerous and not typical)
    - firewall/NAT crossing without explicit setup
    """

    # Tally HTTP endpoint and request format
    # Tally's built-in HTTP/XML listener accepts requests at the server root.
    TALLY_ENDPOINT = "/"
    TALLY_CONTENT_TYPE = "application/xml"
    TALLY_REQUEST_TIMEOUT = 30  # seconds

    def __init__(self, host, port=9000, timeout=None, verify_ssl=False):
        """
        Initialize HTTP/XML transport.

        Args:
            host (str): Tally host (localhost, IP address, or FQDN on private network)
            port (int): Tally port (default 9000)
            timeout (int): Request timeout in seconds (default 30)
            verify_ssl (bool): Verify SSL certificate (default False - Tally typically doesn't use HTTPS)
        """
        if not host:
            raise ValueError("Host is required for HTTP transport")

        self.host = host.strip()
        self.port = int(port)
        self.timeout = timeout or self.TALLY_REQUEST_TIMEOUT
        self.verify_ssl = verify_ssl
        self.base_url = f"http://{self.host}:{self.port}"

    def test_connection(self, timeout=None):
        """
        Test connectivity to Tally endpoint.

        Sends a simple XML query to verify the endpoint is reachable.
        """
        timeout = timeout or self.timeout

        try:
            # Simple connectivity test - request Tally info/gateway
            test_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE RequestType="Gateway"/>"""

            response = self.send_request(test_xml, timeout=timeout)

            if response["success"]:
                return {
                    "success": True,
                    "message": f"Tally connection successful at {self.base_url}",
                    "error": None,
                }
            else:
                return {
                    "success": False,
                    "message": f"Tally returned error: {response.get('error', 'Unknown error')}",
                    "error": response.get("error"),
                }

        except TallyConnectionError as e:
            return {
                "success": False,
                "message": f"Cannot reach Tally at {self.base_url}: {e.message}",
                "error": e.message,
            }
        except TallyTimeoutError as e:
            return {
                "success": False,
                "message": f"Tally connection timeout after {timeout}s: {e.message}",
                "error": e.message,
            }
        except TallyTransportError as e:
            return {
                "success": False,
                "message": f"Tally transport error: {e.message}",
                "error": e.message,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Unexpected error testing Tally connection: {str(e)}",
                "error": str(e),
            }

    def send_request(self, request_xml, timeout=None, **kwargs):
        """
        Send XML request to Tally via HTTP.

        Handles:
        - Connection errors (host unreachable, refused)
        - Timeout errors
        - HTTP errors
        - Response parsing

        Does NOT handle:
        - XML parsing (left to caller)
        - Business logic interpretation (left to caller)

        Ignores **kwargs (res_model/res_id/operation/idempotency_key) -
        those only mean something to AgentQueueTransport's async job queue;
        a direct HTTP call to Tally already returns the real result inline,
        so there is nothing to correlate a later result back to.
        """
        timeout = timeout or self.timeout
        url = f"{self.base_url}{self.TALLY_ENDPOINT}"

        _logger.debug(
            f"Sending Tally request to {url}",
            extra={"host": self.host, "port": self.port, "timeout": timeout},
        )
        # SECURITY: logs the full request body (customer/vendor names, ledger
        # names, amounts, addresses) verbatim. Only enable this logger's
        # DEBUG level (--log-handler=...tally_transport:DEBUG) for short,
        # deliberate troubleshooting sessions - never leave it on in
        # production, since server log files are typically readable by more
        # people/tools than the Odoo application itself.
        _logger.debug("Tally request body: %s", request_xml)

        try:
            response = requests.post(
                url,
                data=request_xml.encode("utf-8"),
                headers={
                    "Content-Type": f"{self.TALLY_CONTENT_TYPE}; charset=utf-8",
                    "User-Agent": "Odoo-TallyConnector/19.0",
                },
                timeout=timeout,
                verify=self.verify_ssl,
            )

            # Log response status
            _logger.debug(
                f"Tally response status: {response.status_code}",
                extra={
                    "status_code": response.status_code,
                    "response_length": len(response.content),
                },
            )

            # Capture response regardless of status
            response_text = None
            try:
                response_text = response.text
            except Exception as e:
                _logger.warning(
                    f"Failed to decode Tally response: {str(e)}",
                    extra={"error": str(e)},
                )

            # SECURITY: same caveat as the request-body debug log above.
            _logger.debug("Tally response body: %s", response_text)

            # HTTP 200+ success
            if response.status_code >= 200 and response.status_code < 300:
                return {
                    "success": True,
                    "status_code": response.status_code,
                    "response_xml": response_text,
                    "error": None,
                    "raw_response": response,
                }

            # HTTP error
            error_msg = f"HTTP {response.status_code}: {response.reason}"
            if response_text:
                error_msg += f" - {response_text[:200]}"

            _logger.warning(
                f"Tally HTTP error: {error_msg}",
                extra={"status_code": response.status_code},
            )

            return {
                "success": False,
                "status_code": response.status_code,
                "response_xml": response_text,
                "error": error_msg,
                "raw_response": response,
            }

        except requests.exceptions.ConnectionError as e:
            msg = f"Cannot connect to Tally at {self.host}:{self.port}: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyConnectionError(msg, retry_eligible=True)

        except requests.exceptions.Timeout as e:
            msg = f"Tally request timeout after {timeout}s: {str(e)}"
            _logger.error(msg, extra={"error": str(e), "timeout": timeout})
            raise TallyTimeoutError(msg, retry_eligible=True)

        except requests.exceptions.RequestException as e:
            msg = f"Tally HTTP error: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyTransportError(msg, error_code="HTTP_ERROR")

        except Exception as e:
            msg = f"Unexpected error in Tally transport: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyTransportError(msg, error_code="UNKNOWN")
