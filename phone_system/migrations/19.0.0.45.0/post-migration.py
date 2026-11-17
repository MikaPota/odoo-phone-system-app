def migrate(cr, version):
    """Business cards moved to the BIZZKICKR app: Odoo removes the network invitation model's records, but
    cannot drop the table of a model no longer in the registry, so the sent invitations are dropped here."""
    cr.execute("DROP TABLE IF EXISTS vodia_network_invitation CASCADE")
    cr.execute("DELETE FROM ir_config_parameter WHERE key = 'phone_system.network_synced_at'")
