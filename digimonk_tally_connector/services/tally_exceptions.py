"""
Custom exception hierarchy for Tally Connector.

Exceptions are categorized to support intelligent error handling:
- Transient errors (retry eligible): timeout, connection refused, temporary unavailability
- Permanent errors (no retry): missing mapping, validation failure, configuration error
- Unknown errors: unexpected Tally response, protocol violations
"""


class TallyConnectorError(Exception):
    """Base exception for all Tally connector errors."""

    def __init__(self, message, error_code=None, tally_error=None, retry_eligible=False):
        self.message = message
        self.error_code = error_code
        self.tally_error = tally_error
        self.retry_eligible = retry_eligible
        super().__init__(self.message)


class TallyTransportError(TallyConnectorError):
    """HTTP transport layer error (status code, network)."""

    pass


class TallyConnectionError(TallyConnectorError):
    """Connection refused, host unreachable, or network unavailable."""

    def __init__(self, message, **kwargs):
        kwargs["retry_eligible"] = True
        super().__init__(message, **kwargs)


class TallyTimeoutError(TallyConnectorError):
    """Request timeout waiting for Tally response."""

    def __init__(self, message, **kwargs):
        kwargs["retry_eligible"] = True
        super().__init__(message, **kwargs)


class TallyXmlError(TallyConnectorError):
    """XML parsing or generation error."""

    pass


class TallyValidationError(TallyConnectorError):
    """Tally validation failure (missing field, invalid value, business rule violation)."""

    pass


class TallyMappingError(TallyConnectorError):
    """Required mapping (account, tax, product) is missing or invalid."""

    pass


class TallyDuplicateError(TallyConnectorError):
    """Duplicate record detection or constraint violation in Tally."""

    pass


class TallyCompanyError(TallyConnectorError):
    """Company mismatch, missing company, or company configuration error."""

    pass


class TallyRemoteError(TallyConnectorError):
    """Unexpected error from Tally server (not validation, not parsing)."""

    def __init__(self, message, http_status=None, **kwargs):
        self.http_status = http_status
        super().__init__(message, **kwargs)


class TallyNotFoundError(TallyConnectorError):
    """Resource not found in Tally (ledger, stock item, voucher, etc.)."""

    pass


class TallyConfigurationError(TallyConnectorError):
    """Invalid Tally connector configuration (missing host, invalid port, etc.)."""

    pass
