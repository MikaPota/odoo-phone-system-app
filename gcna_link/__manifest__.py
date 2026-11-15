{
    "name": "GCNA Account",
    "summary": "Links this Odoo database to a GCN-ALLIANCE account, for GCNA's apps",
    "description": "The link between this Odoo database and a GCN-ALLIANCE account that GCNA's apps (Phone System, BizzKickR) share: a free sign-up or an existing account's approval, and the installation's encrypted credential.",
    "author": "GCN-ALLIANCE",
    "website": "https://gcn-alliance.com/support/",
    "version": "19.0.1.0.0",
    "category": "Hidden",
    "license": "LGPL-3",
    "depends": ["base"],
    "external_dependencies": {"python": ["cryptography"]},
    "data": [
        "data/gcna_account.xml",
    ],
    "pre_init_hook": "pre_init_hook",
    "application": False,
    "installable": True,
    "auto_install": False,
}
