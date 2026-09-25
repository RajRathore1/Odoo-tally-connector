"""
Tests for Tally connection model and transport layer.

Phase 1 covers:
- Connection CRUD operations
- Connection validation
- Transport layer (mock Tally)
- XML building and parsing
- Test connection action
"""

from odoo.tests import TransactionCase
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class TestTallyConnection(TransactionCase):
    """Test Tally connection model."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company

    def test_create_connection(self):
        """Test creating a valid Tally connection."""
        connection = self.env["tally.connection"].create(
            {
                "name": "Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "timeout": 30,
                "tally_company_name": "My Company",
            }
        )

        self.assertIsNotNone(connection.id)
        self.assertEqual(connection.name, "Test Connection")
        self.assertEqual(connection.host, "localhost")
        self.assertEqual(connection.port, 9000)
        self.assertEqual(connection.timeout, 30)
        self.assertEqual(connection.connection_status, "never_tested")
        self.assertFalse(connection.enabled_for_sync)
        self.assertTrue(connection.active)

    def test_connection_company_unique(self):
        """Test that only one connection per company is allowed."""
        self.env["tally.connection"].create(
            {
                "name": "Connection 1",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
            }
        )

        # Attempt to create another for same company should fail
        with self.assertRaises(Exception):  # Integrity error
            self.env["tally.connection"].create(
                {
                    "name": "Connection 2",
                    "company_id": self.company.id,
                    "host": "localhost",
                    "port": 9001,
                }
            )

    def test_validate_host(self):
        """Test host validation."""
        # Empty host should fail
        with self.assertRaises(ValidationError):
            self.env["tally.connection"].create(
                {
                    "name": "Invalid Connection",
                    "company_id": self.company.id,
                    "host": "",
                    "port": 9000,
                }
            )

    def test_validate_port(self):
        """Test port validation."""
        # Invalid port numbers should fail
        with self.assertRaises(ValidationError):
            self.env["tally.connection"].create(
                {
                    "name": "Invalid Port - Too Low",
                    "company_id": self.company.id,
                    "host": "localhost",
                    "port": 0,
                }
            )

        with self.assertRaises(ValidationError):
            self.env["tally.connection"].create(
                {
                    "name": "Invalid Port - Too High",
                    "company_id": self.company.id,
                    "host": "localhost",
                    "port": 99999,
                }
            )

    def test_validate_timeout(self):
        """Test timeout validation."""
        # Timeout must be between 1-300
        with self.assertRaises(ValidationError):
            self.env["tally.connection"].create(
                {
                    "name": "Invalid Timeout",
                    "company_id": self.company.id,
                    "host": "localhost",
                    "port": 9000,
                    "timeout": 0,
                }
            )

        with self.assertRaises(ValidationError):
            self.env["tally.connection"].create(
                {
                    "name": "Invalid Timeout",
                    "company_id": self.company.id,
                    "host": "localhost",
                    "port": 9000,
                    "timeout": 500,
                }
            )

    def test_connection_params_change_resets_sync_flag(self):
        """Test that changing connection params clears the sync flag."""
        connection = self.env["tally.connection"].create(
            {
                "name": "Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "enabled_for_sync": True,
                "connection_status": "success",
            }
        )

        self.assertTrue(connection.enabled_for_sync)
        self.assertEqual(connection.connection_status, "success")

        # Change host
        connection.write({"host": "192.168.1.10"})

        # Sync flag should be cleared and status reset
        self.assertFalse(connection.enabled_for_sync)
        self.assertEqual(connection.connection_status, "unknown")

    def test_get_tally_client(self):
        """Test getting a Tally client from a connection."""
        from ..services import TallyClient

        connection = self.env["tally.connection"].create(
            {
                "name": "Test Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "success",
                "active": True,
            }
        )

        client = connection._get_tally_client()
        self.assertIsInstance(client, TallyClient)
        self.assertEqual(client.host, "localhost")
        self.assertEqual(client.port, 9000)

    def test_get_tally_client_inactive(self):
        """Test that getting client from inactive connection raises error."""
        from odoo.exceptions import UserError

        connection = self.env["tally.connection"].create(
            {
                "name": "Inactive Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "active": False,
            }
        )

        with self.assertRaises(UserError):
            connection._get_tally_client()

    def test_get_tally_client_untested(self):
        """Test that getting client from untested connection raises error."""
        from odoo.exceptions import UserError

        connection = self.env["tally.connection"].create(
            {
                "name": "Untested Connection",
                "company_id": self.company.id,
                "host": "localhost",
                "port": 9000,
                "connection_status": "never_tested",
                "active": True,
            }
        )

        with self.assertRaises(UserError):
            connection._get_tally_client()


class TestTallyTransport(TransactionCase):
    """Test Tally HTTP/XML transport layer."""

    def test_http_transport_init(self):
        """Test HttpXmlTransport initialization."""
        from ..services import HttpXmlTransport

        transport = HttpXmlTransport("localhost", 9000, 30)
        self.assertEqual(transport.host, "localhost")
        self.assertEqual(transport.port, 9000)
        self.assertEqual(transport.timeout, 30)

    def test_http_transport_invalid_host(self):
        """Test transport with invalid host raises error."""
        from ..services import HttpXmlTransport

        with self.assertRaises(ValueError):
            HttpXmlTransport("", 9000)

    def test_xml_builder_escape(self):
        """Test XML escaping."""
        from ..services import TallyXmlBuilder

        # Test special character escaping
        test_cases = [
            ("simple", "simple"),
            ("with & ampersand", "with &amp; ampersand"),
            ("with < bracket", "with &lt; bracket"),
            ("with > bracket", "with &gt; bracket"),
            ('with "quote"', 'with &quot;quote&quot;'),
        ]

        for input_val, expected in test_cases:
            result = TallyXmlBuilder.escape_xml(input_val)
            self.assertEqual(result, expected, f"Failed for {input_val}")

    def test_xml_builder_gateway_request(self):
        """Test Gateway request XML generation."""
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_gateway_request()
        self.assertIn("<?xml", xml)
        self.assertIn("RequestType", xml)
        self.assertIn("Gateway", xml)

    def test_xml_builder_company_list_request(self):
        """Test company list request XML generation."""
        from ..services import TallyXmlBuilder

        xml = TallyXmlBuilder.build_company_list_request()
        self.assertIn("<?xml", xml)
        self.assertIn("Export", xml)
        self.assertIn("Company", xml)

    def test_response_parser_safe_parse(self):
        """Test safe XML parsing."""
        from ..services import TallyResponseParser, TallyXmlError

        # Valid XML
        valid_xml = '<?xml version="1.0"?><ENVELOPE></ENVELOPE>'
        result = TallyResponseParser.safe_parse_xml(valid_xml)
        self.assertEqual(result.tag, "ENVELOPE")

        # Invalid XML should raise exception
        with self.assertRaises(TallyXmlError):
            TallyResponseParser.safe_parse_xml("<invalid>")

        # Empty XML should raise exception
        with self.assertRaises(TallyXmlError):
            TallyResponseParser.safe_parse_xml("")

    def test_response_parser_strips_invalid_control_characters(self):
        """
        Real Tally quirk: the exporter occasionally emits raw control characters
        (outside the XML 1.0 valid character set) inside field text. These must
        be stripped before parsing, not treated as a fatal error.
        """
        from ..services import TallyResponseParser

        invalid_char = chr(2)  # STX - not valid in XML 1.0
        xml_with_bad_char = f"<ENVELOPE><NAME>Test{invalid_char}Name</NAME></ENVELOPE>"

        root = TallyResponseParser.safe_parse_xml(xml_with_bad_char)
        self.assertEqual(root.find("NAME").text, "TestName")

    def test_response_parser_strips_invalid_numeric_char_references(self):
        """
        Real Tally quirk (distinct from raw control chars): Tally sometimes
        encodes a stray control character as a numeric XML entity (e.g. &#2;)
        rather than emitting it raw. Expat rejects this with "reference to
        invalid character number" even though it's syntactically well-formed -
        the reference itself must be stripped, not just raw bytes.
        """
        from ..services import TallyResponseParser

        xml_with_bad_ref = "<ENVELOPE><NAME>Test&#2;Name</NAME></ENVELOPE>"
        root = TallyResponseParser.safe_parse_xml(xml_with_bad_ref)
        self.assertEqual(root.find("NAME").text, "TestName")

    def test_response_parser_preserves_valid_char_references(self):
        """A legitimate character reference (e.g. accented characters) must survive."""
        from ..services import TallyResponseParser

        xml_with_valid_ref = "<ENVELOPE><NAME>Caf&#233;</NAME></ENVELOPE>"
        root = TallyResponseParser.safe_parse_xml(xml_with_valid_ref)
        self.assertEqual(root.find("NAME").text, "Café")

    def test_response_parser_gateway_response(self):
        """Test parsing Gateway response."""
        from ..services import TallyResponseParser

        xml = """<?xml version="1.0"?>
<ENVELOPE>
    <RESPONSE RequestType="Gateway">
        <GATEWAYTIME>2024-01-01 12:00:00</GATEWAYTIME>
    </RESPONSE>
</ENVELOPE>"""

        result = TallyResponseParser.parse_gateway_response(xml)
        self.assertTrue(result["success"])
        self.assertIsNone(result["error"])

    def test_response_parser_export_response(self):
        """Test parsing Export response."""
        from ..services import TallyResponseParser

        xml = """<?xml version="1.0"?>
<ENVELOPE>
    <RESPONSE RequestType="Export">
        <LINEITEMS>
            <LINEITEM>
                <FIELD Name="CompanyName">Test Company</FIELD>
                <FIELD Name="CompanyGuid">abc123</FIELD>
            </LINEITEM>
        </LINEITEMS>
    </RESPONSE>
</ENVELOPE>"""

        result = TallyResponseParser.parse_export_response(xml)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["CompanyName"], "Test Company")
        self.assertEqual(result["data"][0]["CompanyGuid"], "abc123")

    def test_tally_client_init(self):
        """Test TallyClient initialization."""
        from ..services import TallyClient

        client = TallyClient("localhost", 9000, 30)
        self.assertEqual(client.host, "localhost")
        self.assertEqual(client.port, 9000)
        self.assertEqual(client.timeout, 30)
