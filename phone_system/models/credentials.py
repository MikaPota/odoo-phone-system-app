import logging
import os

from cryptography.fernet import Fernet
from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)
KEY_PARAMETER = "phone_system.credential_key"


def installation_key(env):
    """An operator's server key wins; otherwise the database creates its own, so buyers set up nothing."""
    key = os.environ.get("VODIA_CREDENTIAL_KEY")
    if key:
        return key
    env.cr.execute("""
        INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
        VALUES (%s, %s, %s, now() AT TIME ZONE 'UTC', %s, now() AT TIME ZONE 'UTC')
        ON CONFLICT (key) DO NOTHING
    """, [KEY_PARAMETER, Fernet.generate_key().decode("ascii"), env.uid, env.uid])
    if env.cr.rowcount:
        env.registry.clear_cache()
    env.cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [KEY_PARAMETER])
    return env.cr.fetchone()[0]


def credential_cipher(env):
    try:
        return Fernet(installation_key(env).encode("ascii"))
    except (ValueError, UnicodeError):
        _logger.warning("Vodia credential encryption key is unavailable.")
        raise UserError(_("The PBX credential encryption key is missing or invalid.")) from None
