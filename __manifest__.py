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
    'depends': ['base'],
    'data': [
        'security/ks_report_builder_security.xml',
        'security/ir.model.access.csv',
        'views/ks_report_builder_views.xml',
        'views/ks_report_builder_menus.xml',
        'views/ks_report_snapshot_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
