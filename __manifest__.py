{
    'name': 'KS Report Builder',
    'version': '19.0.1.0.0',
    'category': 'Productivity/Reporting',
    'summary': 'Build list/pivot/graph reports from the UI without writing SQL or Python',
    'description': """
KS Report Builder
=================

Define a report by picking a base model and drilling into related fields with
dropdowns. The module generates a read-only Odoo model backed by a SQL query
(``_table_query``), plus list/pivot views, an action and a menu.

No SQL is typed by anyone. Field paths are chosen from dropdowns; computed
columns accept arithmetic only, validated through an AST whitelist.
    """,
    'author': 'Ksolves India Ltd.',
    'website': 'https://www.ksolves.com',
    'depends': ['base', 'web'],
    'data': [
        'security/ks_report_builder_security.xml',
        'security/ir.model.access.csv',
        'views/ks_report_builder_views.xml',
        'views/ks_report_builder_menus.xml',
        'views/ks_report_snapshot_views.xml',
        'views/ks_report_designer_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ks_report_builder/static/src/js/ks_expression_field/ks_expression_field.js',
            'ks_report_builder/static/src/js/ks_expression_field/ks_expression_field.xml',
            'ks_report_builder/static/src/js/ks_field_chain_field/ks_field_chain_field.js',
            'ks_report_builder/static/src/js/ks_report_designer/ks_report_designer.js',
            'ks_report_builder/static/src/js/ks_report_designer/ks_report_designer.xml',
            'ks_report_builder/static/src/scss/ks_report_designer.scss',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
