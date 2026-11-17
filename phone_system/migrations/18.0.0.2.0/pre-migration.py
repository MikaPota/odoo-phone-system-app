def migrate(cr, version):
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'vodia_pilot_connection'
          AND column_name IN ('broker_url', 'installation_credential')
    """)
    for (column,) in cr.fetchall():
        if column == "broker_url":
            cr.execute("ALTER TABLE vodia_pilot_connection ALTER COLUMN broker_url DROP NOT NULL")
        else:
            cr.execute("ALTER TABLE vodia_pilot_connection ALTER COLUMN installation_credential DROP NOT NULL")
