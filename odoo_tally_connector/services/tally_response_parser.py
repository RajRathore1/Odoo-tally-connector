"""
Tally XML response parser.

Converts Tally XML responses into structured Python objects.

Never requires business models to inspect raw XML.

Returns structured results with:
- success status
- parsed data
- error details
- warnings
"""

import re
import xml.etree.ElementTree as ET
import logging
from .tally_exceptions import TallyXmlError, TallyValidationError, TallyRemoteError

_logger = logging.getLogger(__name__)


def _build_invalid_xml_chars_regex():
    # XML 1.0 only allows codepoints 9, 10, 13, 32-0xD7FF, 0xE000-0xFFFD, 0x10000-0x10FFFF.
    # Tally is known to occasionally emit raw control characters that violate this
    # (a real, observed quirk) - stripping them is the pragmatic fix.
    ranges = [(9, 9), (10, 10), (13, 13), (32, 0xD7FF), (0xE000, 0xFFFD), (0x10000, 0x10FFFF)]
    parts = []
    for lo, hi in ranges:
        if lo == hi:
            parts.append(chr(lo))
        else:
            parts.append(chr(lo) + "-" + chr(hi))
    return re.compile("[^" + "".join(parts) + "]")


_INVALID_XML_CHARS_RE = _build_invalid_xml_chars_regex()

# Matches numeric character references: &#123; or &#x7B;
_CHAR_REF_RE = re.compile(r"&#(x[0-9A-Fa-f]+|[0-9]+);")


def _is_valid_xml_codepoint(codepoint):
    if codepoint in (9, 10, 13):
        return True
    if 32 <= codepoint <= 0xD7FF:
        return True
    if 0xE000 <= codepoint <= 0xFFFD:
        return True
    if 0x10000 <= codepoint <= 0x10FFFF:
        return True
    return False


def _strip_invalid_char_references(text):
    """
    Drop numeric character references (e.g. &#2; or &#x2;) that point to a
    codepoint outside the XML 1.0 valid character set.

    Real Tally quirk: Tally sometimes encodes a stray control character from
    source data as a numeric entity rather than emitting it raw. Expat
    correctly rejects these at parse time ("reference to invalid character
    number") even though the reference is syntactically well-formed - this
    is a semantic XML validity rule, not a syntax error. Stripping the
    reference (not the whole field) is the pragmatic fix.
    """

    def _replace(match):
        ref = match.group(1)
        try:
            codepoint = int(ref[1:], 16) if ref[0] in ("x", "X") else int(ref)
        except ValueError:
            return match.group(0)
        return match.group(0) if _is_valid_xml_codepoint(codepoint) else ""

    return _CHAR_REF_RE.sub(_replace, text)


class TallyResponseParser:
    """Parse and interpret Tally XML responses."""

    # Namespaces used in Tally responses (may vary)
    TALLY_NS = {}

    @staticmethod
    def safe_parse_xml(response_xml):
        """
        Safely parse XML response.

        Args:
            response_xml (str): XML response body

        Returns:
            ET.Element: Root element

        Raises:
            TallyXmlError: If XML is malformed
        """
        if not response_xml or not response_xml.strip():
            raise TallyXmlError("Empty XML response from Tally", error_code="EMPTY_RESPONSE")

        cleaned = _INVALID_XML_CHARS_RE.sub("", response_xml)
        cleaned = _strip_invalid_char_references(cleaned)
        if len(cleaned) != len(response_xml):
            _logger.warning(
                "Stripped invalid XML character(s)/character-reference(s) from Tally response before parsing",
                extra={"removed_count": len(response_xml) - len(cleaned)},
            )

        try:
            root = ET.fromstring(cleaned)
            return root
        except ET.ParseError as e:
            msg = f"Failed to parse Tally XML response: {str(e)}"
            _logger.error(msg, extra={"error": str(e), "response_length": len(response_xml)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")
        except Exception as e:
            msg = f"Unexpected error parsing Tally response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="UNKNOWN")

    @staticmethod
    def find_element_text(element, path, default=None):
        """
        Safely find and extract text from an XML element.

        Args:
            element (ET.Element): XML element to search
            path (str): XPath expression (e.g., "RESPONSE/STATUS")
            default: Default value if element not found

        Returns:
            str or default: Text content of element
        """
        if element is None:
            return default
        try:
            found = element.find(path)
            if found is not None and found.text:
                return found.text.strip()
            return default
        except Exception as e:
            _logger.warning(f"Error finding element {path}: {str(e)}")
            return default

    @classmethod
    def parse_gateway_response(cls, response_xml):
        """
        Parse Gateway response.

        Response structure:
        <ENVELOPE>
            <RESPONSE RequestType="Gateway">
                <GATEWAYTIME>...</GATEWAYTIME>
                ...
            </RESPONSE>
        </ENVELOPE>

        Returns:
            dict: {
                "success": bool,
                "message": str,
                "tally_time": str or None,
                "error": str or None,
                "warnings": []
            }
        """
        try:
            root = cls.safe_parse_xml(response_xml)

            # Try to find response element
            response_elem = root.find("RESPONSE")
            if response_elem is None:
                response_elem = root

            # Check for status/error indicators
            status = cls.find_element_text(response_elem, "STATUS", "").lower()
            error_text = cls.find_element_text(response_elem, "ERRORDESCRIPTION")
            tally_time = cls.find_element_text(response_elem, "GATEWAYTIME")

            # Tally Gateway responses typically indicate success by presence of data
            if error_text:
                return {
                    "success": False,
                    "message": f"Tally error: {error_text}",
                    "tally_time": tally_time,
                    "error": error_text,
                    "warnings": [],
                }

            return {
                "success": True,
                "message": "Gateway response received successfully",
                "tally_time": tally_time,
                "error": None,
                "warnings": [],
            }

        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing gateway response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")

    @classmethod
    def parse_export_response(cls, response_xml, expected_report=None):
        """
        Parse Export response (used for queries like company list, ledgers, products).

        Response structure:
        <ENVELOPE>
            <RESPONSE RequestType="Export">
                <LINEITEMS>
                    <LINEITEM>
                        <FIELD Name="CompanyName">...</FIELD>
                        <FIELD Name="CompanyGuid">...</FIELD>
                        ...
                    </LINEITEM>
                </LINEITEMS>
            </RESPONSE>
        </ENVELOPE>

        Args:
            response_xml (str): XML response
            expected_report (str): Expected report name (for validation)

        Returns:
            dict: {
                "success": bool,
                "data": [list of dicts],
                "count": int,
                "message": str,
                "error": str or None,
                "warnings": []
            }
        """
        try:
            root = cls.safe_parse_xml(response_xml)

            response_elem = root.find("RESPONSE")
            if response_elem is None:
                response_elem = root

            # Check for errors
            error_text = cls.find_element_text(response_elem, "ERRORDESCRIPTION")
            if error_text:
                return {
                    "success": False,
                    "data": [],
                    "count": 0,
                    "message": f"Tally error: {error_text}",
                    "error": error_text,
                    "warnings": [],
                }

            # Extract line items
            data = []
            lineitems = response_elem.find("LINEITEMS")

            if lineitems is not None:
                for lineitem in lineitems.findall("LINEITEM"):
                    row = {}
                    for field in lineitem.findall("FIELD"):
                        field_name = field.get("Name", "")
                        field_value = field.text or ""
                        row[field_name] = field_value.strip() if field_value else ""
                    if row:
                        data.append(row)

            return {
                "success": True,
                "data": data,
                "count": len(data),
                "message": f"Retrieved {len(data)} records",
                "error": None,
                "warnings": [],
            }

        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing export response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")

    @classmethod
    def parse_company_list_response(cls, response_xml):
        """Parse the native Tally company collection response."""
        try:
            root = cls.safe_parse_xml(response_xml)

            error_text = next(
                (
                    (element.text or "").strip()
                    for element in root.findall(".//LINEERROR") + root.findall(".//ERRORDESCRIPTION")
                    if (element.text or "").strip()
                ),
                None,
            )
            if error_text:
                return {
                    "success": False,
                    "data": [],
                    "count": 0,
                    "message": f"Tally error: {error_text}",
                    "error": error_text,
                    "warnings": [],
                }

            data = []
            for company in root.findall(".//DATA/COLLECTION/COMPANY"):
                name = (company.findtext("NAME") or company.get("NAME") or "").strip()
                guid = (company.findtext("GUID") or "").strip()
                if name:
                    data.append({"CompanyName": name, "CompanyGuid": guid})

            return {
                "success": True,
                "data": data,
                "count": len(data),
                "message": f"Retrieved {len(data)} companies",
                "error": None,
                "warnings": [],
            }
        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing company list response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")

    @classmethod
    def parse_stock_item_list_response(cls, response_xml):
        """
        Parse the native Tally stock item collection response.

        Same DATA/COLLECTION/<TAG> shape proven working for companies -
        Tally native TDL collection export, not the generic Export/REPORT
        format (unverified, since replaced).
        """
        try:
            root = cls.safe_parse_xml(response_xml)

            error_text = next(
                (
                    (element.text or "").strip()
                    for element in root.findall(".//LINEERROR") + root.findall(".//ERRORDESCRIPTION")
                    if (element.text or "").strip()
                ),
                None,
            )
            if error_text:
                return {
                    "success": False,
                    "data": [],
                    "count": 0,
                    "message": f"Tally error: {error_text}",
                    "error": error_text,
                    "warnings": [],
                }

            data = []
            for item in root.findall(".//DATA/COLLECTION/STOCKITEM"):
                name = (item.findtext("NAME") or item.get("NAME") or "").strip()
                guid = (item.findtext("GUID") or "").strip()
                base_unit = (item.findtext("BASEUNITS") or "").strip()
                closing_qty, _closing_unit = cls._parse_qty(item.findtext("CLOSINGBALANCE"))
                if name:
                    data.append(
                        {
                            "StockItemName": name,
                            "StockItemGuid": guid,
                            "StockItemUnit": base_unit,
                            "StockItemClosingQty": closing_qty,
                        }
                    )

            return {
                "success": True,
                "data": data,
                "count": len(data),
                "message": f"Retrieved {len(data)} stock items",
                "error": None,
                "warnings": [],
            }
        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing stock item list response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")

    @classmethod
    def parse_ledger_list_response(cls, response_xml):
        """
        Parse the native Tally ledger collection response.

        Same DATA/COLLECTION/<TAG> shape proven working for companies and
        stock items - genuine Tally native TDL collection export.
        """
        try:
            root = cls.safe_parse_xml(response_xml)

            error_text = next(
                (
                    (element.text or "").strip()
                    for element in root.findall(".//LINEERROR") + root.findall(".//ERRORDESCRIPTION")
                    if (element.text or "").strip()
                ),
                None,
            )
            if error_text:
                return {
                    "success": False,
                    "data": [],
                    "count": 0,
                    "message": f"Tally error: {error_text}",
                    "error": error_text,
                    "warnings": [],
                }

            data = []
            for item in root.findall(".//DATA/COLLECTION/LEDGER"):
                name = (item.findtext("NAME") or item.get("NAME") or "").strip()
                guid = (item.findtext("GUID") or "").strip()
                parent = (item.findtext("PARENT") or "").strip()
                if name:
                    data.append({"LedgerName": name, "LedgerGuid": guid, "LedgerParent": parent})

            return {
                "success": True,
                "data": data,
                "count": len(data),
                "message": f"Retrieved {len(data)} ledgers",
                "error": None,
                "warnings": [],
            }
        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing ledger list response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")

    @classmethod
    def parse_stock_group_list_response(cls, response_xml):
        """
        Parse the native Tally Stock Group collection response (Phase 11).

        Same DATA/COLLECTION/<TAG> shape as parse_ledger_list_response, just
        the STOCKGROUP tag instead of LEDGER.
        """
        try:
            root = cls.safe_parse_xml(response_xml)

            error_text = next(
                (
                    (element.text or "").strip()
                    for element in root.findall(".//LINEERROR") + root.findall(".//ERRORDESCRIPTION")
                    if (element.text or "").strip()
                ),
                None,
            )
            if error_text:
                return {
                    "success": False,
                    "data": [],
                    "count": 0,
                    "message": f"Tally error: {error_text}",
                    "error": error_text,
                    "warnings": [],
                }

            data = []
            for item in root.findall(".//DATA/COLLECTION/STOCKGROUP"):
                name = (item.findtext("NAME") or item.get("NAME") or "").strip()
                parent = (item.findtext("PARENT") or "").strip()
                if name:
                    data.append({"StockGroupName": name, "StockGroupParent": parent})

            return {
                "success": True,
                "data": data,
                "count": len(data),
                "message": f"Retrieved {len(data)} stock groups",
                "error": None,
                "warnings": [],
            }
        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing stock group list response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")

    @staticmethod
    def _parse_amount(text):
        """Parse a Tally amount like '1,234.00' or '-118.00' into a float."""
        if not text:
            return 0.0
        try:
            return float(text.replace(",", "").strip())
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_qty(text):
        """Parse a Tally quantity like '10 Units' or '1 Nos' -> (10.0, 'Units')."""
        if not text:
            return 0.0, ""
        parts = text.strip().split(None, 1)
        try:
            qty = float(parts[0].replace(",", ""))
        except (ValueError, IndexError):
            qty = 0.0
        unit = parts[1].strip() if len(parts) > 1 else ""
        return qty, unit

    @staticmethod
    def _parse_rate(text):
        """Parse a Tally rate like '10.00/Units' -> 10.0 (unit discarded, already known)."""
        if not text:
            return 0.0
        rate_part = text.split("/", 1)[0]
        try:
            return float(rate_part.replace(",", "").strip())
        except ValueError:
            return 0.0

    @classmethod
    def _parse_voucher_element(cls, voucher_elem):
        """Parse one <VOUCHER> element (from a Day Book style raw export) into a dict."""
        guid = (voucher_elem.get("REMOTEID") or cls.find_element_text(voucher_elem, "GUID") or "").strip()
        vch_type = (voucher_elem.get("VCHTYPE") or cls.find_element_text(voucher_elem, "VOUCHERTYPENAME") or "").strip()

        ledger_entries = []
        for entry in voucher_elem.findall("LEDGERENTRIES.LIST") + voucher_elem.findall("ALLLEDGERENTRIES.LIST"):
            ledger_entries.append(
                {
                    "ledger_name": cls.find_element_text(entry, "LEDGERNAME", "").strip(),
                    "amount": cls._parse_amount(cls.find_element_text(entry, "AMOUNT")),
                    "is_deemed_positive": cls.find_element_text(entry, "ISDEEMEDPOSITIVE", "").lower() == "yes",
                    "is_party_ledger": cls.find_element_text(entry, "ISPARTYLEDGER", "").lower() == "yes",
                }
            )

        inventory_entries = []
        for entry in voucher_elem.findall("ALLINVENTORYENTRIES.LIST"):
            quantity, unit = cls._parse_qty(cls.find_element_text(entry, "ACTUALQTY"))
            allocations = []
            for alloc in entry.findall("ACCOUNTINGALLOCATIONS.LIST"):
                allocations.append(
                    {
                        "ledger_name": cls.find_element_text(alloc, "LEDGERNAME", "").strip(),
                        "amount": cls._parse_amount(cls.find_element_text(alloc, "AMOUNT")),
                    }
                )
            inventory_entries.append(
                {
                    "stock_item_name": cls.find_element_text(entry, "STOCKITEMNAME", "").strip(),
                    "quantity": quantity,
                    "unit": unit,
                    "rate": cls._parse_rate(cls.find_element_text(entry, "RATE")),
                    "amount": cls._parse_amount(cls.find_element_text(entry, "AMOUNT")),
                    "accounting_allocations": allocations,
                }
            )

        return {
            "guid": guid,
            "vch_type": vch_type,
            "date": cls.find_element_text(voucher_elem, "DATE", "").strip(),
            "voucher_number": cls.find_element_text(voucher_elem, "VOUCHERNUMBER", "").strip(),
            "party_ledger_name": cls.find_element_text(voucher_elem, "PARTYLEDGERNAME", "").strip(),
            "narration": cls.find_element_text(voucher_elem, "NARRATION"),
            "reference": cls.find_element_text(voucher_elem, "REFERENCE"),
            "ledger_entries": ledger_entries,
            "inventory_entries": inventory_entries,
        }

    @classmethod
    def parse_voucher_export_response(cls, response_xml, vch_type_filter=None):
        """
        Parse a Day Book style raw voucher export response into a list of
        voucher dicts (see build_voucher_export_request - this is the same
        TALLYMESSAGE/VOUCHER shape used for import, just read the other way).

        Args:
            response_xml (str): XML response
            vch_type_filter (str): if given, only vouchers whose VCHTYPE
                matches exactly (case-insensitive) are returned - Tally's
                raw Day Book export has no server-side type filter.

        Returns:
            dict: {
                "success": bool,
                "data": [list of voucher dicts, see _parse_voucher_element],
                "count": int,
                "message": str,
                "error": str or None,
                "warnings": []
            }
        """
        try:
            root = cls.safe_parse_xml(response_xml)

            error_text = next(
                (
                    (element.text or "").strip()
                    for element in root.findall(".//LINEERROR") + root.findall(".//ERRORDESCRIPTION")
                    if (element.text or "").strip()
                ),
                None,
            )
            if error_text:
                return {
                    "success": False,
                    "data": [],
                    "count": 0,
                    "message": f"Tally error: {error_text}",
                    "error": error_text,
                    "warnings": [],
                }

            data = []
            for voucher_elem in root.findall(".//TALLYMESSAGE/VOUCHER"):
                parsed = cls._parse_voucher_element(voucher_elem)
                if vch_type_filter and parsed["vch_type"].strip().lower() != vch_type_filter.strip().lower():
                    continue
                data.append(parsed)

            return {
                "success": True,
                "data": data,
                "count": len(data),
                "message": f"Retrieved {len(data)} voucher(s)",
                "error": None,
                "warnings": [],
            }
        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing voucher export response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")

    @classmethod
    def parse_master_import_response(cls, response_xml):
        """
        Parse Tally's native master-import acknowledgment (used for Stock Item /
        Ledger create-alter via TALLYREQUEST=Import Data).

        Genuine Tally response shape:
        <RESPONSE>
            <CREATED>1</CREATED>
            <ALTERED>0</ALTERED>
            <DELETED>0</DELETED>
            <COMBINED>1</COMBINED>
            <IGNORED>0</IGNORED>
            <ERRORS>0</ERRORS>
            <CANCELLED>0</CANCELLED>
            <LINEERROR>optional error text</LINEERROR>
        </RESPONSE>

        Note: this ack does NOT echo back a GUID - idempotency relies on the
        client-supplied GUID sent in the request (see build_stock_item_upsert_request).

        Returns:
            dict: {
                "success": bool,
                "created": bool,
                "altered": bool,
                "errors_count": int,
                "message": str,
                "error": str or None,
            }
        """
        try:
            root = cls.safe_parse_xml(response_xml)

            response_elem = root.find(".//RESPONSE")
            if response_elem is None:
                response_elem = root

            def _int(tag):
                try:
                    return int(cls.find_element_text(response_elem, tag, "0"))
                except (TypeError, ValueError):
                    return 0

            created = _int("CREATED")
            altered = _int("ALTERED")
            errors_count = _int("ERRORS")
            line_error = cls.find_element_text(response_elem, "LINEERROR")

            if errors_count > 0 or line_error:
                error_msg = line_error or f"Tally reported {errors_count} error(s) during import."
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "errors_count": errors_count,
                    "message": f"Tally import failed: {error_msg}",
                    "error": error_msg,
                }

            if created == 0 and altered == 0:
                return {
                    "success": False,
                    "created": False,
                    "altered": False,
                    "errors_count": 0,
                    "message": "Tally did not report a create or alter - response was ambiguous.",
                    "error": "Ambiguous Tally import result (no CREATED/ALTERED count).",
                }

            return {
                "success": True,
                "created": created > 0,
                "altered": altered > 0,
                "errors_count": 0,
                "message": "Created in Tally" if created > 0 else "Altered in Tally",
                "error": None,
            }

        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing master import response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")

    @classmethod
    def parse_import_response(cls, response_xml):
        """
        Parse Import response (used for creating/altering ledgers, stock items, vouchers).

        Response structure:
        <ENVELOPE>
            <RESPONSE RequestType="Import">
                <IMPORTRESULT>
                    <LINEITEMS>
                        <LINEITEM>
                            <RESULT>Success|Failure</RESULT>
                            <GUID>...</GUID>
                            <ERRORDESCRIPTION>...</ERRORDESCRIPTION>
                        </LINEITEM>
                    </LINEITEMS>
                </IMPORTRESULT>
            </RESPONSE>
        </ENVELOPE>

        Returns:
            dict: {
                "success": bool,
                "status": "created" | "altered" | "failed" | "unknown",
                "tally_guid": str or None,
                "message": str,
                "error": str or None,
                "warnings": [],
                "raw_results": [list of LINEITEM results]
            }
        """
        try:
            root = cls.safe_parse_xml(response_xml)

            response_elem = root.find("RESPONSE")
            if response_elem is None:
                response_elem = root

            # Check for top-level error
            error_text = cls.find_element_text(response_elem, "ERRORDESCRIPTION")
            if error_text:
                return {
                    "success": False,
                    "status": "failed",
                    "tally_guid": None,
                    "message": f"Tally import error: {error_text}",
                    "error": error_text,
                    "warnings": [],
                    "raw_results": [],
                }

            # Extract import results
            importresult = response_elem.find("IMPORTRESULT")
            if importresult is None:
                return {
                    "success": False,
                    "status": "unknown",
                    "tally_guid": None,
                    "message": "No IMPORTRESULT element found in Tally response",
                    "error": "Missing IMPORTRESULT",
                    "warnings": [],
                    "raw_results": [],
                }

            lineitems = importresult.find("LINEITEMS")
            raw_results = []
            all_success = True
            tally_guid = None

            if lineitems is not None:
                for lineitem in lineitems.findall("LINEITEM"):
                    result = cls.find_element_text(lineitem, "RESULT", "").lower()
                    guid = cls.find_element_text(lineitem, "GUID")
                    error = cls.find_element_text(lineitem, "ERRORDESCRIPTION")

                    result_dict = {
                        "result": result,
                        "guid": guid,
                        "error": error,
                    }
                    raw_results.append(result_dict)

                    if result not in ("success", "created", "altered"):
                        all_success = False
                    else:
                        if guid and not tally_guid:
                            tally_guid = guid

            status = "created"  # Default assumption for new records
            if all_success:
                return {
                    "success": True,
                    "status": status,
                    "tally_guid": tally_guid,
                    "message": f"Import successful (GUID: {tally_guid})" if tally_guid else "Import successful",
                    "error": None,
                    "warnings": [],
                    "raw_results": raw_results,
                }
            else:
                error_msg = "; ".join([r["error"] for r in raw_results if r["error"]])
                return {
                    "success": False,
                    "status": "failed",
                    "tally_guid": tally_guid,
                    "message": f"Import failed: {error_msg}",
                    "error": error_msg,
                    "warnings": [],
                    "raw_results": raw_results,
                }

        except TallyXmlError:
            raise
        except Exception as e:
            msg = f"Unexpected error parsing import response: {str(e)}"
            _logger.error(msg, extra={"error": str(e)})
            raise TallyXmlError(msg, error_code="PARSE_ERROR")
