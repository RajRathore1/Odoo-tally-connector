"""
XML request builder for Tally.

Responsibilities:
- Safely generate XML for various Tally operations
- Proper escaping of special characters, Unicode, etc.
- Correct namespace and element structure

Does NOT handle:
- Business logic validation
- Ledger/stock item mapping
- Field selection logic

Future operations (Phase 2+):
- Ledger creation/alteration
- Stock item operations
- Voucher generation
- Invoice/payment handling

Phase 1: Test connection only
"""

import xml.sax.saxutils as saxutils
import logging

_logger = logging.getLogger(__name__)


class TallyXmlBuilder:
    """Generate properly formatted Tally XML requests."""

    # Tally XML protocol version
    TALLY_VERSION = "14.1"

    @staticmethod
    def escape_xml(value):
        """
        Safely escape XML special characters.

        Handles:
        - & -> &amp;
        - < -> &lt;
        - > -> &gt;
        - " -> &quot;
        - ' -> &apos;

        Preserves Unicode correctly.
        """
        if value is None:
            return ""
        # Convert to string if needed
        value = str(value).strip()
        return saxutils.escape(value, {'"': "&quot;"})

    @classmethod
    def build_gateway_request(cls):
        """
        Build a Gateway request to test connectivity and fetch Tally info.

        Response will contain Tally version, available companies, etc.

        Returns:
            str: XML request ready to send to Tally
        """
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE RequestType="Gateway">
</ENVELOPE>"""
        return xml

    @classmethod
    def build_company_list_request(cls):
        """
        Build request to fetch list of companies in Tally.

        Returns:
            str: XML request
        """
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>EXPORT</TALLYREQUEST>
        <TYPE>COLLECTION</TYPE>
        <ID>Odoo Company List</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Odoo Company List">
                        <TYPE>Company</TYPE>
                        <FETCH>Name, GUID</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_stock_item_upsert_request(
        cls, company, name, base_unit, guid, action="Create", old_name=None, parent_group=None,
    ):
        """
        Build a Tally master-import request to create or alter a Stock Item.

        Uses Tally's documented native XML import format (TALLYREQUEST=Import Data,
        REPORTNAME=All Masters, TALLYMESSAGE/STOCKITEM). GUID is client-supplied so
        idempotent re-sync can be tracked deterministically without depending on a
        GUID being echoed back by Tally.

        Args:
            company (str): Tally company name to import into (SVCURRENTCOMPANY)
            name (str): Stock item name (Odoo product display name)
            base_unit (str): Tally unit name - MUST already exist as a Unit master
                in Tally. This is a genuine Tally constraint: unit mapping is not
                auto-created here (see TallyMappingError in the sync service).
            guid (str): Client-generated deterministic GUID for idempotency
            action (str): "Create" or "Alter"
            old_name (str): Previous synced name, required by Tally when altering
                a master whose NAME is changing (rename). Omit/None if unchanged.
            parent_group (str): optional Tally Stock Group name (Phase 11) - MUST
                already exist in Tally. Omitted (None) reproduces the exact prior
                XML shape (no PARENT tag), so a product whose category has no
                Tally Stock Group mapping still syncs exactly as before, just
                landing at Tally's default top level.

        Returns:
            str: XML request ready to send to Tally
        """
        old_name_tag = ""
        if action == "Alter" and old_name and old_name != name:
            old_name_tag = f"<OLDNAME>{cls.escape_xml(old_name)}</OLDNAME>"

        parent_tag = f"<PARENT>{cls.escape_xml(parent_group)}</PARENT>" if parent_group else ""

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>All Masters</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <STOCKITEM NAME="{cls.escape_xml(name)}" ACTION="{action}">
                        {old_name_tag}
                        <NAME>{cls.escape_xml(name)}</NAME>
                        <GUID>{cls.escape_xml(guid)}</GUID>
                        <BASEUNITS>{cls.escape_xml(base_unit)}</BASEUNITS>
                        {parent_tag}
                    </STOCKITEM>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_stock_group_upsert_request(cls, company, name, parent_group=None, action="Create", old_name=None):
        """
        Build a Tally master-import request to create or alter a Stock Group
        (Phase 11 - product.category hierarchy). Same native XML import shape
        as build_ledger_upsert_request, just a different master tag and an
        OPTIONAL PARENT (a Stock Group may sit at Tally's top level).

        Never verified against a real Tally instance yet - flagged the same
        way every other first-use-of-a-new-master-type in this module is.

        Args:
            company (str): Tally company name to import into
            name (str): Stock Group name (Odoo product.category name)
            parent_group (str): optional parent Stock Group name - MUST
                already exist in Tally if given (mirrors a category's own
                parent_id chain; the caller is responsible for creating
                parents before children)
            action (str): "Create" or "Alter"
            old_name (str): Previous synced name, for rename handling on Alter

        Returns:
            str: XML request ready to send to Tally
        """
        old_name_tag = ""
        if action == "Alter" and old_name and old_name != name:
            old_name_tag = f"<OLDNAME>{cls.escape_xml(old_name)}</OLDNAME>"

        parent_tag = f"<PARENT>{cls.escape_xml(parent_group)}</PARENT>" if parent_group else ""

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>All Masters</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <STOCKGROUP NAME="{cls.escape_xml(name)}" ACTION="{action}">
                        {old_name_tag}
                        <NAME>{cls.escape_xml(name)}</NAME>
                        {parent_tag}
                    </STOCKGROUP>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_stock_group_list_request(cls, company=None):
        """
        Build request to fetch Stock Groups from Tally using the native TDL
        Collection export format (same proven pattern as build_ledger_list_request).

        Args:
            company (str): Tally company name (optional - uses current if None)

        Returns:
            str: XML request
        """
        company_tag = ""
        if company:
            company_tag = f"<SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>"

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>EXPORT</TALLYREQUEST>
        <TYPE>COLLECTION</TYPE>
        <ID>Odoo Stock Group List</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                {company_tag}
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Odoo Stock Group List">
                        <TYPE>StockGroup</TYPE>
                        <FETCH>Name, Parent</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_unit_upsert_request(cls, company, name, action="Create"):
        """
        Build a minimal Tally master-import request to create a simple Unit
        of Measure master (Phase 11), so a Stock Item's BASEUNITS reference
        is guaranteed to exist before the item itself is created/altered -
        previously this connector only ever assumed the unit already existed
        in Tally (see build_stock_item_upsert_request's base_unit docstring).

        Deliberately minimal: ISSIMPLEUNIT=Yes, no conversion/compound-unit
        fields - this connector has no equivalent of Tally's compound units
        (e.g. "Box of 12 Pcs") to map from Odoo's own uom.uom model.

        Never verified against a real Tally instance yet - see
        TallyProductSyncService's module docstring for the live-test note.

        Args:
            company (str): Tally company name to import into
            name (str): Unit name (Odoo uom.uom name, e.g. "Units", "kg")
            action (str): "Create" or "Alter"

        Returns:
            str: XML request ready to send to Tally
        """
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>All Masters</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <UNIT NAME="{cls.escape_xml(name)}" ACTION="{action}">
                        <NAME>{cls.escape_xml(name)}</NAME>
                        <ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
                    </UNIT>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_ledger_upsert_request(
        cls, company, name, parent_group, guid, action="Create", old_name=None,
        address_lines=None, phone=None, email=None,
    ):
        """
        Build a Tally master-import request to create or alter a Ledger.

        Same native XML import format proven for Stock Items (TALLYREQUEST=
        Import Data, REPORTNAME=All Masters, TALLYMESSAGE/LEDGER) - just a
        different master tag and a required PARENT (Ledger Group).

        Args:
            company (str): Tally company name to import into
            name (str): Ledger name (Odoo partner display name)
            parent_group (str): Tally Ledger Group name - MUST already exist
                (e.g. "Sundry Debtors", "Sundry Creditors"). Not auto-created.
            guid (str): Client-generated deterministic GUID for idempotency
            action (str): "Create" or "Alter"
            old_name (str): Previous synced name, for rename handling on Alter
            address_lines (list[str]): optional address lines
            phone (str): optional phone number
            email (str): optional email address

        Returns:
            str: XML request ready to send to Tally
        """
        old_name_tag = ""
        if action == "Alter" and old_name and old_name != name:
            old_name_tag = f"<OLDNAME>{cls.escape_xml(old_name)}</OLDNAME>"

        address_tag = ""
        if address_lines:
            lines = "".join(f"<ADDRESS>{cls.escape_xml(line)}</ADDRESS>" for line in address_lines if line)
            if lines:
                address_tag = f'<ADDRESS.LIST TYPE="String">{lines}</ADDRESS.LIST>'

        phone_tag = f"<LEDGERPHONE>{cls.escape_xml(phone)}</LEDGERPHONE>" if phone else ""
        email_tag = f"<EMAIL>{cls.escape_xml(email)}</EMAIL>" if email else ""

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>All Masters</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <LEDGER NAME="{cls.escape_xml(name)}" ACTION="{action}">
                        {old_name_tag}
                        <NAME>{cls.escape_xml(name)}</NAME>
                        <GUID>{cls.escape_xml(guid)}</GUID>
                        <PARENT>{cls.escape_xml(parent_group)}</PARENT>
                        {address_tag}
                        {phone_tag}
                        {email_tag}
                    </LEDGER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_ledger_opening_balance_alter_request(cls, company, ledger_name, opening_balance):
        """
        Build a minimal Tally master-import request that alters ONLY the
        OPENINGBALANCE of an already-existing ledger, identified by name
        (Phase 10 - one-time onboarding). Deliberately does NOT resend
        GUID/PARENT/address/phone/email the way build_ledger_upsert_request
        does for a full ledger create/rename - this ledger already exists
        in Tally (it's an account ledger this connector only ever
        references by name, never creates), and this connector has no
        record of its Tally parent group to safely resend. Sending a
        minimal Alter payload relies on Tally's own partial-update
        behavior (unspecified fields are left as-is) - the same assumption
        every voucher Alter in this module already makes for fields it
        doesn't resend.

        Never verified against a real Tally instance yet - see
        TallyOpeningBalanceService's module docstring.

        Args:
            company (str): Tally company name to import into
            ledger_name (str): Exact existing Tally ledger name to alter
            opening_balance (float): follows Odoo's own account.account
                "balance" sign convention (debit - credit): positive = net
                debit balance, negative = net credit balance

        Returns:
            str: XML request ready to send to Tally
        """
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>All Masters</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <LEDGER NAME="{cls.escape_xml(ledger_name)}" ACTION="Alter">
                        <NAME>{cls.escape_xml(ledger_name)}</NAME>
                        <OPENINGBALANCE>{opening_balance:.2f}</OPENINGBALANCE>
                    </LEDGER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_ledger_list_request(cls, company=None):
        """
        Build request to fetch ledgers from Tally using the native TDL
        Collection export format (same proven pattern as companies/stock
        items) - replaces the old, unverified generic Export/REPORT format.

        Args:
            company (str): Tally company name (optional - uses current if None)

        Returns:
            str: XML request
        """
        company_tag = ""
        if company:
            company_tag = f"<SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>"

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>EXPORT</TALLYREQUEST>
        <TYPE>COLLECTION</TYPE>
        <ID>Odoo Ledger List</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                {company_tag}
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Odoo Ledger List">
                        <TYPE>Ledger</TYPE>
                        <FETCH>Name, GUID, Parent</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_sales_voucher_upsert_request(
        cls, company, voucher_number, voucher_date, party_ledger, guid, ledger_entries,
        inventory_entries, action="Create", narration=None, reference=None,
    ):
        """
        Build a Tally voucher-import request for a Sales Voucher.

        Uses Tally's documented native XML voucher format (TALLYREQUEST=Import
        Data, REPORTNAME=Vouchers, TALLYMESSAGE/VOUCHER). REMOTEID is Tally's
        documented mechanism for external systems to tag a voucher with their
        own identifier, used here the same way GUID is used for masters -
        deterministic, so re-sync (Alter) targets the same voucher instead of
        creating a duplicate.

        Structure confirmed against a real Tally-exported "Item Invoice"
        Sales Voucher (exported from Tally's own Day Book after manually
        creating and saving a test voucher): the voucher must carry
        OBJVIEW="Invoice Voucher View" and VCHENTRYMODE=Item Invoice, the
        party (and tax) ledgers are top-level LEDGERENTRIES.LIST entries
        (not ALLLEDGERENTRIES.LIST), and each stock item's own sales/income
        ledger allocation is nested INSIDE that item's
        ALLINVENTORYENTRIES.LIST as an ACCOUNTINGALLOCATIONS.LIST - it is
        not a separate top-level ledger entry. Without this exact shape,
        Tally's importer misparses the voucher and reports a confusing
        unrelated error ("Voucher date is missing") instead of a structural
        one.

        Args:
            company (str): Tally company name to import into
            voucher_number (str): Invoice/voucher number (e.g. Odoo invoice name)
            voucher_date (str): Date in YYYYMMDD format
            party_ledger (str): Customer's Tally Ledger name
            guid (str): Client-generated deterministic ID (sent as REMOTEID)
            ledger_entries (list[dict]): top-level ledger entries - the party
                entry (with "is_party_ledger": True and optional
                "bill_allocation") and any tax entries. [{"ledger_name": str,
                "amount": Decimal/float, "is_deemed_positive": bool,
                "is_party_ledger": bool (optional)}, ...] - must sum to zero
                together with each inventory entry's own accounting_allocation.
            inventory_entries (list[dict]): [{"stock_item_name": str, "quantity": float,
                "rate": float, "amount": float, "unit": str, "accounting_allocation":
                {"ledger_name": str, "amount": float} (optional)}, ...]
            action (str): "Create" or "Alter"
            narration (str): optional invoice narration
            reference (str): optional reference number

        Returns:
            str: XML request ready to send to Tally
        """
        narration_tag = f"<NARRATION>{cls.escape_xml(narration)}</NARRATION>" if narration else ""
        reference_tag = f"<REFERENCE>{cls.escape_xml(reference)}</REFERENCE>" if reference else ""

        def _bill_allocation_xml(entry):
            bill = entry.get("bill_allocation")
            if not bill:
                return ""
            return f"""<BILLALLOCATIONS.LIST>
                <NAME>{cls.escape_xml(bill['name'])}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>{bill['amount']:.2f}</AMOUNT>
            </BILLALLOCATIONS.LIST>"""

        ledger_xml = "".join(
            f"""<LEDGERENTRIES.LIST>
                <LEDGERNAME>{cls.escape_xml(entry['ledger_name'])}</LEDGERNAME>
                <ISPARTYLEDGER>{"Yes" if entry.get('is_party_ledger') else "No"}</ISPARTYLEDGER>
                <ISDEEMEDPOSITIVE>{"Yes" if entry['is_deemed_positive'] else "No"}</ISDEEMEDPOSITIVE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
                {_bill_allocation_xml(entry)}
            </LEDGERENTRIES.LIST>"""
            for entry in ledger_entries
        )

        def _accounting_allocation_xml(entry):
            alloc = entry.get("accounting_allocation")
            if not alloc:
                return ""
            return f"""<ACCOUNTINGALLOCATIONS.LIST>
                <LEDGERNAME>{cls.escape_xml(alloc['ledger_name'])}</LEDGERNAME>
                <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                <AMOUNT>{alloc['amount']:.2f}</AMOUNT>
            </ACCOUNTINGALLOCATIONS.LIST>"""

        inventory_xml = "".join(
            f"""<ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>{cls.escape_xml(entry['stock_item_name'])}</STOCKITEMNAME>
                <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                <RATE>{entry['rate']:.2f}/{cls.escape_xml(entry['unit'])}</RATE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
                <ACTUALQTY>{entry['quantity']:g} {cls.escape_xml(entry['unit'])}</ACTUALQTY>
                <BILLEDQTY>{entry['quantity']:g} {cls.escape_xml(entry['unit'])}</BILLEDQTY>
                {_accounting_allocation_xml(entry)}
            </ALLINVENTORYENTRIES.LIST>"""
            for entry in inventory_entries
        )

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="Sales" ACTION="{action}" REMOTEID="{cls.escape_xml(guid)}" OBJVIEW="Invoice Voucher View">
                        <DATE>{cls.escape_xml(voucher_date)}</DATE>
                        <EFFECTIVEDATE>{cls.escape_xml(voucher_date)}</EFFECTIVEDATE>
                        <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
                        <VCHENTRYMODE>Item Invoice</VCHENTRYMODE>
                        <VOUCHERNUMBER>{cls.escape_xml(voucher_number)}</VOUCHERNUMBER>
                        <PARTYLEDGERNAME>{cls.escape_xml(party_ledger)}</PARTYLEDGERNAME>
                        {reference_tag}
                        {narration_tag}
                        {inventory_xml}
                        {ledger_xml}
                    </VOUCHER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_voucher_export_request(cls, company, from_date, to_date):
        """
        Build a request to export raw vouchers (all types) for a date range,
        using Tally's built-in "Day Book" report - the same TALLYMESSAGE/
        VOUCHER shape Tally itself produces when exporting the Day Book
        manually (confirmed by exporting a real voucher from Tally and
        reading its XML - see build_sales_voucher_upsert_request's
        docstring for the structural notes that export uncovered).

        Uses the REPORTNAME-based "Export Data" request shape - the read
        counterpart to the "Import Data"/REPORTNAME shape already proven
        for ledger/stock item/voucher upserts (build_*_upsert_request).
        An earlier version of this method used TALLYREQUEST=EXPORT with
        TYPE=DATA/ID=Vouchers (the shape that works for TDL Collections,
        e.g. build_company_list_request) - that is NOT the same mechanism
        as exporting a built-in report and Tally responded with its
        "Import Data" screen prompts (File Format/File Path/etc.) instead
        of voucher data. Day Book is a built-in report, not a TDL
        collection, so it must be exported the "Export Data" way.

        Tally does not offer a voucher-type filter for this export - it
        returns every voucher type in the date range. Callers must filter
        the parsed results by VCHTYPE themselves (see
        TallyResponseParser.parse_voucher_export_response).

        Args:
            company (str): Tally company name
            from_date (str): Start date, YYYYMMDD format (inclusive)
            to_date (str): End date, YYYYMMDD format (inclusive)

        Returns:
            str: XML request ready to send to Tally
        """
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <EXPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Day Book</REPORTNAME>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVFROMDATE>{cls.escape_xml(from_date)}</SVFROMDATE>
                    <SVTODATE>{cls.escape_xml(to_date)}</SVTODATE>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
        </EXPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_purchase_voucher_upsert_request(
        cls, company, voucher_number, voucher_date, party_ledger, guid, ledger_entries,
        inventory_entries, action="Create", narration=None, reference=None,
    ):
        """
        Build a Tally voucher-import request for a Purchase Voucher.

        Same proven Item Invoice structure as build_sales_voucher_upsert_request
        (OBJVIEW="Invoice Voucher View", VCHENTRYMODE=Item Invoice, party as a
        top-level LEDGERENTRIES.LIST entry, each item's expense/purchase
        ledger nested in its own ALLINVENTORYENTRIES.LIST/ACCOUNTINGALLOCATIONS.LIST)
        - only VCHTYPE differs, and the Dr/Cr roles are mirrored: the vendor
        (creditor) is the increasing (credit) leg here instead of the
        decreasing (debit) leg a customer is on a Sales voucher. Callers
        (see TallyBillSyncService) are expected to pass ledger_entries/
        inventory_entries with amounts and is_deemed_positive already
        reflecting that mirrored convention - this method does not flip
        anything itself, it only renders what it is given.

        Args:
            (see build_sales_voucher_upsert_request - identical shape)

        Returns:
            str: XML request ready to send to Tally
        """
        narration_tag = f"<NARRATION>{cls.escape_xml(narration)}</NARRATION>" if narration else ""
        reference_tag = f"<REFERENCE>{cls.escape_xml(reference)}</REFERENCE>" if reference else ""

        def _bill_allocation_xml(entry):
            bill = entry.get("bill_allocation")
            if not bill:
                return ""
            return f"""<BILLALLOCATIONS.LIST>
                <NAME>{cls.escape_xml(bill['name'])}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>{bill['amount']:.2f}</AMOUNT>
            </BILLALLOCATIONS.LIST>"""

        ledger_xml = "".join(
            f"""<LEDGERENTRIES.LIST>
                <LEDGERNAME>{cls.escape_xml(entry['ledger_name'])}</LEDGERNAME>
                <ISPARTYLEDGER>{"Yes" if entry.get('is_party_ledger') else "No"}</ISPARTYLEDGER>
                <ISDEEMEDPOSITIVE>{"Yes" if entry['is_deemed_positive'] else "No"}</ISDEEMEDPOSITIVE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
                {_bill_allocation_xml(entry)}
            </LEDGERENTRIES.LIST>"""
            for entry in ledger_entries
        )

        def _accounting_allocation_xml(entry):
            alloc = entry.get("accounting_allocation")
            if not alloc:
                return ""
            return f"""<ACCOUNTINGALLOCATIONS.LIST>
                <LEDGERNAME>{cls.escape_xml(alloc['ledger_name'])}</LEDGERNAME>
                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                <AMOUNT>{alloc['amount']:.2f}</AMOUNT>
            </ACCOUNTINGALLOCATIONS.LIST>"""

        inventory_xml = "".join(
            f"""<ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>{cls.escape_xml(entry['stock_item_name'])}</STOCKITEMNAME>
                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                <RATE>{entry['rate']:.2f}/{cls.escape_xml(entry['unit'])}</RATE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
                <ACTUALQTY>{entry['quantity']:g} {cls.escape_xml(entry['unit'])}</ACTUALQTY>
                <BILLEDQTY>{entry['quantity']:g} {cls.escape_xml(entry['unit'])}</BILLEDQTY>
                {_accounting_allocation_xml(entry)}
            </ALLINVENTORYENTRIES.LIST>"""
            for entry in inventory_entries
        )

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="Purchase" ACTION="{action}" REMOTEID="{cls.escape_xml(guid)}" OBJVIEW="Invoice Voucher View">
                        <DATE>{cls.escape_xml(voucher_date)}</DATE>
                        <EFFECTIVEDATE>{cls.escape_xml(voucher_date)}</EFFECTIVEDATE>
                        <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
                        <VCHENTRYMODE>Item Invoice</VCHENTRYMODE>
                        <VOUCHERNUMBER>{cls.escape_xml(voucher_number)}</VOUCHERNUMBER>
                        <PARTYLEDGERNAME>{cls.escape_xml(party_ledger)}</PARTYLEDGERNAME>
                        {reference_tag}
                        {narration_tag}
                        {inventory_xml}
                        {ledger_xml}
                    </VOUCHER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_credit_debit_note_upsert_request(
        cls, vch_type, company, voucher_number, voucher_date, party_ledger, guid, ledger_entries,
        inventory_entries, action="Create", narration=None, reference=None,
    ):
        """
        Build a Tally voucher-import request for a Credit Note or Debit Note.

        Same proven Item Invoice structure as build_sales_voucher_upsert_request
        / build_purchase_voucher_upsert_request - only VCHTYPE differs here
        (caller passes "Credit Note" or "Debit Note"). Unlike those two
        dedicated methods, this one is shared between both note types
        because their XML shape is identical either way - only the Dr/Cr
        sign convention differs, and that lives entirely in the
        ledger_entries/inventory_entries values the caller supplies (see
        TallyCreditDebitNoteSyncService), not in this method.

        Args:
            vch_type (str): "Credit Note" or "Debit Note"
            (all other args: see build_sales_voucher_upsert_request - identical shape)

        Returns:
            str: XML request ready to send to Tally
        """
        narration_tag = f"<NARRATION>{cls.escape_xml(narration)}</NARRATION>" if narration else ""
        reference_tag = f"<REFERENCE>{cls.escape_xml(reference)}</REFERENCE>" if reference else ""

        def _bill_allocation_xml(entry):
            bill = entry.get("bill_allocation")
            if not bill:
                return ""
            return f"""<BILLALLOCATIONS.LIST>
                <NAME>{cls.escape_xml(bill['name'])}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>{bill['amount']:.2f}</AMOUNT>
            </BILLALLOCATIONS.LIST>"""

        ledger_xml = "".join(
            f"""<LEDGERENTRIES.LIST>
                <LEDGERNAME>{cls.escape_xml(entry['ledger_name'])}</LEDGERNAME>
                <ISPARTYLEDGER>{"Yes" if entry.get('is_party_ledger') else "No"}</ISPARTYLEDGER>
                <ISDEEMEDPOSITIVE>{"Yes" if entry['is_deemed_positive'] else "No"}</ISDEEMEDPOSITIVE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
                {_bill_allocation_xml(entry)}
            </LEDGERENTRIES.LIST>"""
            for entry in ledger_entries
        )

        def _accounting_allocation_xml(entry):
            alloc = entry.get("accounting_allocation")
            if not alloc:
                return ""
            return f"""<ACCOUNTINGALLOCATIONS.LIST>
                <LEDGERNAME>{cls.escape_xml(alloc['ledger_name'])}</LEDGERNAME>
                <ISDEEMEDPOSITIVE>{"Yes" if alloc.get('is_deemed_positive') else "No"}</ISDEEMEDPOSITIVE>
                <AMOUNT>{alloc['amount']:.2f}</AMOUNT>
            </ACCOUNTINGALLOCATIONS.LIST>"""

        inventory_xml = "".join(
            f"""<ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>{cls.escape_xml(entry['stock_item_name'])}</STOCKITEMNAME>
                <ISDEEMEDPOSITIVE>{"Yes" if entry.get('is_deemed_positive') else "No"}</ISDEEMEDPOSITIVE>
                <RATE>{entry['rate']:.2f}/{cls.escape_xml(entry['unit'])}</RATE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
                <ACTUALQTY>{entry['quantity']:g} {cls.escape_xml(entry['unit'])}</ACTUALQTY>
                <BILLEDQTY>{entry['quantity']:g} {cls.escape_xml(entry['unit'])}</BILLEDQTY>
                {_accounting_allocation_xml(entry)}
            </ALLINVENTORYENTRIES.LIST>"""
            for entry in inventory_entries
        )

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="{cls.escape_xml(vch_type)}" ACTION="{action}" REMOTEID="{cls.escape_xml(guid)}" OBJVIEW="Invoice Voucher View">
                        <DATE>{cls.escape_xml(voucher_date)}</DATE>
                        <EFFECTIVEDATE>{cls.escape_xml(voucher_date)}</EFFECTIVEDATE>
                        <VOUCHERTYPENAME>{cls.escape_xml(vch_type)}</VOUCHERTYPENAME>
                        <VCHENTRYMODE>Item Invoice</VCHENTRYMODE>
                        <VOUCHERNUMBER>{cls.escape_xml(voucher_number)}</VOUCHERNUMBER>
                        <PARTYLEDGERNAME>{cls.escape_xml(party_ledger)}</PARTYLEDGERNAME>
                        {reference_tag}
                        {narration_tag}
                        {inventory_xml}
                        {ledger_xml}
                    </VOUCHER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_receipt_payment_voucher_upsert_request(
        cls, vch_type, company, voucher_number, voucher_date, party_ledger, guid, ledger_entries,
        action="Create", narration=None, reference=None,
    ):
        """
        Build a Tally voucher-import request for a Receipt or Payment
        voucher - a plain 2-leg accounting voucher (party ledger and a
        Cash/Bank ledger), with no stock items at all. Unlike
        build_sales_voucher_upsert_request and friends, this uses
        OBJVIEW="Accounting Voucher View" (not "Invoice Voucher View") and
        no VCHENTRYMODE - confirmed by the real plain-accounting-voucher XML
        captured during the Sales Voucher investigation (see that method's
        docstring). An earlier version of this method omitted OBJVIEW
        entirely on the assumption that Tally would default to the same
        thing - it does not: Tally silently rejected the voucher with
        EXCEPTIONS=1 and no LINEERROR text at all, which is how this was
        caught (a real, confirmed Odoo-form-and-Tally-EDU-restriction-ruled-
        out test still failed until OBJVIEW was made explicit).

        Args:
            vch_type (str): "Receipt" or "Payment"
            company (str): Tally company name to import into
            voucher_number (str): Payment/Receipt number (e.g. Odoo payment name)
            voucher_date (str): Date in YYYYMMDD format
            party_ledger (str): Customer/Vendor Tally Ledger name
            guid (str): Client-generated deterministic ID (sent as REMOTEID)
            ledger_entries (list[dict]): exactly 2 entries - the party ledger
                and the Cash/Bank ledger - [{"ledger_name": str, "amount":
                float, "is_deemed_positive": bool, "is_party_ledger": bool
                (optional), "bill_allocation": dict (optional)}, ...] -
                must sum to zero
            action (str): "Create" or "Alter"
            narration (str): optional narration
            reference (str): optional reference number

        Returns:
            str: XML request ready to send to Tally
        """
        narration_tag = f"<NARRATION>{cls.escape_xml(narration)}</NARRATION>" if narration else ""
        reference_tag = f"<REFERENCE>{cls.escape_xml(reference)}</REFERENCE>" if reference else ""

        def _bill_allocation_xml(entry):
            bill = entry.get("bill_allocation")
            if not bill:
                return ""
            return f"""<BILLALLOCATIONS.LIST>
                <NAME>{cls.escape_xml(bill['name'])}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>{bill['amount']:.2f}</AMOUNT>
            </BILLALLOCATIONS.LIST>"""

        ledger_xml = "".join(
            f"""<LEDGERENTRIES.LIST>
                <LEDGERNAME>{cls.escape_xml(entry['ledger_name'])}</LEDGERNAME>
                <ISPARTYLEDGER>{"Yes" if entry.get('is_party_ledger') else "No"}</ISPARTYLEDGER>
                <ISDEEMEDPOSITIVE>{"Yes" if entry['is_deemed_positive'] else "No"}</ISDEEMEDPOSITIVE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
                {_bill_allocation_xml(entry)}
            </LEDGERENTRIES.LIST>"""
            for entry in ledger_entries
        )

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="{cls.escape_xml(vch_type)}" ACTION="{action}" REMOTEID="{cls.escape_xml(guid)}" OBJVIEW="Accounting Voucher View">
                        <DATE>{cls.escape_xml(voucher_date)}</DATE>
                        <EFFECTIVEDATE>{cls.escape_xml(voucher_date)}</EFFECTIVEDATE>
                        <VOUCHERTYPENAME>{cls.escape_xml(vch_type)}</VOUCHERTYPENAME>
                        <VOUCHERNUMBER>{cls.escape_xml(voucher_number)}</VOUCHERNUMBER>
                        <PARTYLEDGERNAME>{cls.escape_xml(party_ledger)}</PARTYLEDGERNAME>
                        {reference_tag}
                        {narration_tag}
                        {ledger_xml}
                    </VOUCHER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_journal_voucher_upsert_request(
        cls, company, voucher_number, voucher_date, guid, ledger_entries,
        action="Create", narration=None, reference=None,
    ):
        """
        Build a Tally voucher-import request for a Journal voucher (Phase 8
        - month-end adjustments, depreciation, provisions: general-purpose
        multi-line entries with no party ledger and no stock items at all).

        Modeled directly on build_receipt_payment_voucher_upsert_request -
        same OBJVIEW="Accounting Voucher View", no VCHENTRYMODE, same
        DATE-immediately-before-VOUCHERTYPENAME ordering (Tally's parser is
        positionally sensitive here - narration/reference must not sit
        between them, or Tally reports a misleading "Voucher date is
        missing" error even though DATE is present and valid; see that
        method's docstring for how this was originally discovered). The one
        structural difference: no PARTYLEDGERNAME at all - a Journal voucher
        has no designated party, just N ledger legs that net to zero.

        This is the first brand-new voucher type in this module since
        Physical Stock, and like that one, has never been verified against
        a real Tally instance yet - expect it may need a structural
        correction on first live test, the same way Sales/Receipt/Payment
        each needed one round of fixing after comparing against real Tally
        output.

        Args:
            company (str): Tally company name to import into
            voucher_number (str): Journal voucher number (e.g. Odoo move name)
            voucher_date (str): Date in YYYYMMDD format
            guid (str): Client-generated deterministic ID (sent as REMOTEID)
            ledger_entries (list[dict]): N entries, one per account.move.line
                - [{"ledger_name": str, "amount": float, "is_deemed_positive":
                bool}, ...] - must sum to zero (Odoo already guarantees this
                for any move that reached "posted", so callers don't need to
                re-check it)
            action (str): "Create" or "Alter"
            narration (str): optional narration
            reference (str): optional reference number

        Returns:
            str: XML request ready to send to Tally
        """
        narration_tag = f"<NARRATION>{cls.escape_xml(narration)}</NARRATION>" if narration else ""
        reference_tag = f"<REFERENCE>{cls.escape_xml(reference)}</REFERENCE>" if reference else ""

        ledger_xml = "".join(
            f"""<LEDGERENTRIES.LIST>
                <LEDGERNAME>{cls.escape_xml(entry['ledger_name'])}</LEDGERNAME>
                <ISPARTYLEDGER>No</ISPARTYLEDGER>
                <ISDEEMEDPOSITIVE>{"Yes" if entry['is_deemed_positive'] else "No"}</ISDEEMEDPOSITIVE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
            </LEDGERENTRIES.LIST>"""
            for entry in ledger_entries
        )

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="Journal" ACTION="{action}" REMOTEID="{cls.escape_xml(guid)}" OBJVIEW="Accounting Voucher View">
                        <DATE>{cls.escape_xml(voucher_date)}</DATE>
                        <EFFECTIVEDATE>{cls.escape_xml(voucher_date)}</EFFECTIVEDATE>
                        <VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
                        <VOUCHERNUMBER>{cls.escape_xml(voucher_number)}</VOUCHERNUMBER>
                        {reference_tag}
                        {narration_tag}
                        {ledger_xml}
                    </VOUCHER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_contra_voucher_upsert_request(
        cls, company, voucher_number, voucher_date, guid, ledger_entries,
        action="Create", narration=None, reference=None,
    ):
        """
        Build a Tally voucher-import request for a Contra voucher (Phase 9
        - internal fund transfers between the business's own Cash/Bank
        ledgers, e.g. cash deposited into a bank account). Structurally
        identical to build_journal_voucher_upsert_request (same OBJVIEW,
        same DATE-before-VOUCHERTYPENAME ordering, no party ledger, N
        ledger legs that net to zero) - copied rather than shared, matching
        this file's established convention of one dedicated builder method
        per voucher type even when nearly identical (see
        build_receipt_payment_voucher_upsert_request's docstring note on
        the same pattern for Receipt vs Payment). The only difference from
        Journal is VCHTYPE/VOUCHERTYPENAME="Contra" - Tally itself is what
        actually enforces that a Contra voucher's ledgers must be Cash/Bank
        type; this module's own guard for that lives in
        TallyContraSyncService, not here.

        Verified against a real Tally instance the same way Journal was -
        see TallyContraSyncService's module docstring for the live-test note.

        Args:
            company (str): Tally company name to import into
            voucher_number (str): Contra voucher number (e.g. Odoo move name)
            voucher_date (str): Date in YYYYMMDD format
            guid (str): Client-generated deterministic ID (sent as REMOTEID)
            ledger_entries (list[dict]): N entries, one per account.move.line
                - [{"ledger_name": str, "amount": float, "is_deemed_positive":
                bool}, ...] - must sum to zero
            action (str): "Create" or "Alter"
            narration (str): optional narration
            reference (str): optional reference number

        Returns:
            str: XML request ready to send to Tally
        """
        narration_tag = f"<NARRATION>{cls.escape_xml(narration)}</NARRATION>" if narration else ""
        reference_tag = f"<REFERENCE>{cls.escape_xml(reference)}</REFERENCE>" if reference else ""

        ledger_xml = "".join(
            f"""<LEDGERENTRIES.LIST>
                <LEDGERNAME>{cls.escape_xml(entry['ledger_name'])}</LEDGERNAME>
                <ISPARTYLEDGER>No</ISPARTYLEDGER>
                <ISDEEMEDPOSITIVE>{"Yes" if entry['is_deemed_positive'] else "No"}</ISDEEMEDPOSITIVE>
                <AMOUNT>{entry['amount']:.2f}</AMOUNT>
            </LEDGERENTRIES.LIST>"""
            for entry in ledger_entries
        )

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="Contra" ACTION="{action}" REMOTEID="{cls.escape_xml(guid)}" OBJVIEW="Accounting Voucher View">
                        <DATE>{cls.escape_xml(voucher_date)}</DATE>
                        <EFFECTIVEDATE>{cls.escape_xml(voucher_date)}</EFFECTIVEDATE>
                        <VOUCHERTYPENAME>Contra</VOUCHERTYPENAME>
                        <VOUCHERNUMBER>{cls.escape_xml(voucher_number)}</VOUCHERNUMBER>
                        {reference_tag}
                        {narration_tag}
                        {ledger_xml}
                    </VOUCHER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_stock_adjustment_voucher_upsert_request(
        cls, company, voucher_number, voucher_date, guid, stock_item_name, quantity, unit,
        action="Create", narration=None,
    ):
        """
        Build a Tally voucher-import request for a "Physical Stock" voucher
        - Tally's dedicated voucher type for recording a physical stock
          count, used here to correct a detected quantity mismatch (see
          services/tally_stock_adjustment_service.py's docstring).

        Unlike every other voucher this module generates, this one carries
        no ledger entries at all (a Physical Stock voucher only records
        the counted quantity - Tally computes the resulting value/ledger
        effect itself, the same way it would for a manual entry). This is
        the first brand-new voucher type in this module never verified
        against real Tally - expect it may need a structural correction on
        first live test, the same way Sales/Receipt/Payment vouchers each
        needed one round of fixing after comparing against real Tally
        output.

        Args:
            company (str): Tally company name to import into
            voucher_number (str): Adjustment voucher number
            voucher_date (str): Date in YYYYMMDD format
            guid (str): Client-generated deterministic ID (sent as REMOTEID)
            stock_item_name (str): Tally Stock Item name being corrected
            quantity (float): the counted/target quantity (not a delta)
            unit (str): Tally unit name
            action (str): "Create" or "Alter"
            narration (str): optional narration

        Returns:
            str: XML request ready to send to Tally
        """
        narration_tag = f"<NARRATION>{cls.escape_xml(narration)}</NARRATION>" if narration else ""

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="Physical Stock" ACTION="{action}" REMOTEID="{cls.escape_xml(guid)}">
                        <DATE>{cls.escape_xml(voucher_date)}</DATE>
                        <EFFECTIVEDATE>{cls.escape_xml(voucher_date)}</EFFECTIVEDATE>
                        <VOUCHERTYPENAME>Physical Stock</VOUCHERTYPENAME>
                        <VOUCHERNUMBER>{cls.escape_xml(voucher_number)}</VOUCHERNUMBER>
                        {narration_tag}
                        <ALLINVENTORYENTRIES.LIST>
                            <STOCKITEMNAME>{cls.escape_xml(stock_item_name)}</STOCKITEMNAME>
                            <ACTUALQTY>{quantity:g} {cls.escape_xml(unit)}</ACTUALQTY>
                            <BILLEDQTY>{quantity:g} {cls.escape_xml(unit)}</BILLEDQTY>
                        </ALLINVENTORYENTRIES.LIST>
                    </VOUCHER>
                </TALLYMESSAGE>
            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>"""
        return xml

    @classmethod
    def build_stock_item_list_request(cls, company=None):
        """
        Build request to fetch stock items (products) from Tally.

        Uses the same native TDL Collection export format proven to work for
        build_company_list_request() - not the generic Export/REPORT format
        (which was unverified against real Tally and has been replaced here).

        Args:
            company (str): Tally company name (optional - uses current if None)

        Returns:
            str: XML request
        """
        company_tag = ""
        if company:
            company_tag = f"<SVCURRENTCOMPANY>{cls.escape_xml(company)}</SVCURRENTCOMPANY>"

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>EXPORT</TALLYREQUEST>
        <TYPE>COLLECTION</TYPE>
        <ID>Odoo Stock Item List</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                {company_tag}
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Odoo Stock Item List">
                        <TYPE>StockItem</TYPE>
                        <FETCH>Name, GUID, BaseUnits, ClosingBalance</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""
        return xml
