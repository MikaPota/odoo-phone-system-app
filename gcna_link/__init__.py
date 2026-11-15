from . import models


def pre_init_hook(env):
    """Databases that linked through the Phone System app keep their link: its record moves to this module.

    Up to Phone System 19.0.0.42.0 the installation's link record was phone_system.gcna_installation. Renaming
    the external id before this module's data loads keeps the same record, so its installation id, proof and
    credential stay, and no second installation is registered with GCNA.
    """
    env.cr.execute("""
        UPDATE ir_model_data SET module = 'gcna_link'
         WHERE module = 'phone_system' AND name = 'gcna_installation' AND model = 'vodia.gcna.account'
           AND NOT EXISTS (SELECT 1 FROM ir_model_data WHERE module = 'gcna_link' AND name = 'gcna_installation')
    """)
