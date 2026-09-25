{
    "name": "Tally Connector",
    # Let Odoo prefix the local release series (saas~19.3 here).
    "version": "19.0.1.1.0",
    "category": "Accounting/Integrations",
    "author": "DigiMonk Technologies",
    "website": "https://digimonk.in",
    "license": "AGPL-3",
    "images": ["static/description/banner.png"],
    "depends": [
        "base",
        "mail",
        "account",
        "sale",
        "purchase",
        "stock",
        "contacts",
        "product",
    ],
    "data": [
        # Security
        "security/security.xml",
        "security/ir.model.access.csv",
        # Data
        "data/sequence.xml",
        "data/tally_sync_queue_cron.xml",
        "data/tally_stock_reconciliation_cron.xml",
        # Views
        "views/tally_product_import_wizard_views.xml",
        "views/tally_partner_import_wizard_views.xml",
        "views/tally_invoice_import_wizard_views.xml",
        "views/tally_bill_import_wizard_views.xml",
        "views/tally_note_import_wizard_views.xml",
        "views/tally_payment_import_wizard_views.xml",
        "views/tally_stock_reconciliation_wizard_views.xml",
        "views/tally_opening_balance_wizard_views.xml",
        "views/tally_stock_adjustment_log_views.xml",
        "views/tally_agent_token_reveal_wizard_views.xml",
        "views/tally_connection_views.xml",
        "views/product_views.xml",
        "views/product_category_views.xml",
        "views/res_partner_views.xml",
        "views/account_account_views.xml",
        "views/account_tax_views.xml",
        "views/account_journal_views.xml",
        "views/account_move_views.xml",
        "views/account_payment_views.xml",
        "views/tally_mapping_views.xml",
        "views/tally_dashboard_views.xml",
        "views/tally_agent_job_views.xml",
        # Load actions before menus that reference them.
        "views/tally_menus.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
    "summary": "Tally Prime / Tally ERP 9 Integration via HTTP/XML",
    "description": """
        Production-grade Tally Connector for Odoo 19
        ===========================================

        This module provides HTTP/XML-based integration between Odoo and Tally Prime / Tally ERP 9.

        Supported for local/LAN deployments (private network access to Tally).

        Phase 1: Connection management and testing
        Phase 2+: Master data and transaction synchronization

        Features:
        - Modular architecture with clean separation of concerns
        - Multi-company aware
        - Idempotent synchronization
        - Comprehensive error handling and observability
        - Extensible transport abstraction
        - Production-grade retry and queue strategy
    """,
}
