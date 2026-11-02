import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.phone_validation.tools import phone_validation


_logger = logging.getLogger(__name__)


class PilotNumber(models.Model):
    """A phone number of the trunk whose incoming calls ring one user's extension.

    The PBX routes a call by the extension names (aliases) that match the called number, so Odoo adds the
    number's spellings to the extension's names and removes only those again; other names stay untouched."""
    _name = "vodia.pilot.number"
    _description = "Incoming phone number"
    _rec_name = "number"
    _order = "number"

    connection_id = fields.Many2one("vodia.pilot.connection", required=True, ondelete="restrict",
                                    default=lambda self: self.env["vodia.pilot.connection"].search(
                                        [("company_id", "=", self.env.company.id)], limit=1))
    company_id = fields.Many2one(related="connection_id.company_id", store=True)
    number = fields.Char("Phone number", required=True, help="A number of your SIP trunk, e.g. +49 30 1234567.")
    mapping_id = fields.Many2one("vodia.pilot.mapping", "Rings", required=True, ondelete="restrict",
                                 domain="[('connection_id', '=', connection_id), ('extension', '!=', False)]")
    extension = fields.Char(related="mapping_id.extension")
    # The extension and names Odoo wrote on the PBX, so a change or removal undoes exactly those.
    pbx_extension = fields.Char(readonly=True, copy=False)
    pbx_aliases = fields.Char(readonly=True, copy=False)
    state = fields.Selection([("pending", "Not on the PBX yet"), ("active", "Active"), ("failed", "Failed")],
                             default="pending", readonly=True, copy=False)
    message = fields.Char(readonly=True, copy=False)

    _connection_number_unique = models.Constraint(
        "UNIQUE(connection_id, number)", "A phone number can ring only one extension.")

    @api.model
    def _checked(self, connection, mapping, raw):
        """The number in E.164 for this mapping on this connection; the trunk wizard checks with it, too."""
        if mapping.connection_id != connection or not mapping.extension:
            raise ValidationError(_("Choose a user whose extension is linked on this phone system."))
        number = connection._e164(raw or "")
        if not number:
            raise ValidationError(_("Enter the phone number in international format, e.g. +49 30 1234567."))
        return number

    def _e164(self, vals):
        """Stores the number in E.164, so the same number written differently is still one number."""
        if vals.get("number"):
            connection = self.env["vodia.pilot.connection"].browse(vals.get("connection_id")) or self.connection_id[:1] \
                or self.env["vodia.pilot.connection"].search([("company_id", "=", self.env.company.id)], limit=1)
            if connection:
                vals = dict(vals, number=connection._e164(vals["number"]) or vals["number"])
        return vals

    @api.constrains("mapping_id", "connection_id", "number")
    def _check_mapping(self):
        for record in self:
            self._checked(record.connection_id, record.mapping_id, record.number)

    def _spellings(self):
        """The ways a provider may send this number: +4930…, 4930…, 004930… and, nationally, 030…"""
        self.ensure_one()
        digits = self.number[1:]
        spellings = ["+" + digits, digits, "00" + digits]
        country = self.company_id.country_id
        if not country.phone_code or not digits.startswith(str(country.phone_code)):
            # A foreign number has no national spelling here; 0… would be another, domestic number.
            return spellings
        national = phone_validation.phone_format(self.number, country.code or None, country.phone_code or None,
                                                 force_format="NATIONAL", raise_exception=False) or ""
        national = "".join(c for c in national if c.isdigit())
        if national.startswith("0") and national not in spellings:
            spellings.append(national)
        return spellings

    def _client(self, clients=None):
        """A tenant-admin client for the number's own PBX, logged in once per connection and batch."""
        self.ensure_one()
        clients = {} if clients is None else clients
        connection = self.connection_id.sudo()
        if connection.id not in clients:
            clients[connection.id] = connection._pbx_client()
        return clients[connection.id]

    def _trunks(self, clients):
        """The tenant's trunks with their settings, read once per batch."""
        key = ("trunks", self.connection_id.id)
        if key not in clients:
            client = self._client(clients)
            clients[key] = [client.read_trunk(str(trunk.get("id"))) for trunk in client.list_trunks()
                            if isinstance(trunk, dict) and not trunk.get("disabled")]
        return [trunk for trunk in clients[key] if isinstance(trunk, dict)]

    def _remove_from_pbx(self, clients=None):
        """Takes the names Odoo added off the extension; raises if the PBX does not confirm it."""
        self.ensure_one()
        if not self.pbx_extension:
            return
        client = self._client(clients)
        managed = set((self.pbx_aliases or "").split()) - {self.pbx_extension}
        aliases = client.read_aliases(self.pbx_extension)
        kept = [alias for alias in aliases if alias not in managed]
        if kept != aliases and set(client.write_aliases(self.pbx_extension, kept)) & managed:
            raise UserError(_("The PBX did not remove %(number)s from extension %(extension)s.",
                              number=self.number, extension=self.pbx_extension))
        self.sudo().write({"pbx_extension": False, "pbx_aliases": False})

    def _sync(self):
        """Puts each number on its extension and reports the outcome on the record; never raises for PBX errors."""
        clients = {}
        for record in self:
            try:
                mapping = record.mapping_id
                if (mapping.connection_id != record.connection_id or not mapping.extension or not mapping.active
                        or not mapping.user_id.active):
                    # The user's phone moved to another system or lost its extension; the number rings no one.
                    record._remove_from_pbx(clients)
                    record.sudo().write({"state": "pending", "message": _(
                        "%s has no linked extension on this phone system; choose another user.", mapping.user_id.name)})
                    continue
                client = record._client(clients)
                extension = mapping.extension
                spellings = record._spellings()
                # Another account that already answers this number keeps it; Odoo does not take names away.
                managed = set((record.pbx_aliases or "").split())
                taken = [str(account.get("account")) for account in client.list_accounts()
                         if str(account.get("account")) != extension
                         and set(spellings) & set(map(str, account.get("alias") or []))
                         - (managed if str(account.get("account")) == record.pbx_extension else set())]
                if taken:
                    raise UserError(_("Extension %(extension)s on the PBX already answers %(number)s.",
                                      extension=taken[0], number=record.number))
                if record.pbx_extension and record.pbx_extension != extension:
                    record._remove_from_pbx(clients)
                aliases = client.read_aliases(extension)
                added = [spelling for spelling in spellings if spelling not in aliases]
                # Names that were there before stay when the number is removed later. Odoo notes its names before
                # writing them, so a write the PBX applied but did not confirm can still be undone.
                previous = set((record.pbx_aliases or "").split()) if record.pbx_extension == extension else set()
                record.sudo().write({"pbx_extension": extension, "pbx_aliases": " ".join(sorted(previous | set(added)))})
                stored = client.write_aliases(extension, aliases + added) if added else aliases
                if not set(spellings) <= set(stored):
                    raise UserError(_("The PBX did not confirm the number on extension %s.", extension))
                # A trunk that sends every call to one destination ignores the extension names.
                fixed = [str(trunk.get("name") or trunk.get("id")) for trunk in record._trunks(clients)
                         if str(trunk.get("dial_extension") or "").strip()]
                if fixed:
                    raise UserError(_("The number is on extension %(extension)s, but trunk %(trunk)s sends every call "
                                      "to one destination; clear its \"Send call to extension\" on the PBX.",
                                      extension=extension, trunk=", ".join(fixed)))
                record.sudo().write({"state": "active", "message": _(
                    "Calls to %(number)s ring extension %(extension)s.", number=record.number, extension=extension)})
                _logger.info("Vodia number mapping %s now rings extension %s.", record.id, extension)
            except (UserError, requests.RequestException, ValueError, TypeError, AttributeError) as error:
                message = error.args[0] if isinstance(error, UserError) else \
                    _("The phone system could not be reached.") if isinstance(error, requests.RequestException) else \
                    _("The PBX returned an unreadable answer.")
                record.sudo().write({"state": "failed", "message": message})
                _logger.warning("Vodia number mapping %s failed.", record.id)
        return True

    def _remove_all(self):
        clients = {}
        try:
            for record in self:
                record._remove_from_pbx(clients)
        except requests.RequestException:
            raise UserError(_("The phone system could not be reached.")) from None

    def _check_admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an administrator may assign phone numbers."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_admin()
        records = super().create([self._e164(vals) for vals in vals_list])
        records._sync()
        return records

    def write(self, vals):
        if not {"number", "mapping_id", "connection_id"} & set(vals):
            return super().write(vals)
        self._check_admin()
        if "connection_id" in vals and self.filtered(
                lambda record: record.pbx_extension and record.connection_id.id != vals["connection_id"]):
            raise UserError(_("Remove the number and add it on the other phone system."))
        result = super().write(self._e164(vals))
        # Odoo checks the new values first; only then do the old number's names come off the PBX.
        self.flush_recordset()
        if "number" in vals:
            self._remove_all()
        self._sync()
        return result

    def unlink(self):
        self._check_admin()
        self._remove_all()
        return super().unlink()

    def action_sync(self):
        self._check_admin()
        return self._sync()


class PilotMapping(models.Model):
    _inherit = "vodia.pilot.mapping"

    def write(self, vals):
        result = super().write(vals)
        if {"extension", "connection_id", "active"} & set(vals):
            # A user's phone moved or got another extension: their numbers follow it, or ring no one.
            self.env["vodia.pilot.number"].sudo().search([("mapping_id", "in", self.ids)])._sync()
        return result

    def unlink(self):
        self.env["vodia.pilot.number"].search([("mapping_id", "in", self.ids)]).unlink()
        return super().unlink()


class ResUsers(models.Model):
    _inherit = "res.users"

    def write(self, vals):
        result = super().write(vals)
        if "active" in vals:
            # An archived user's numbers ring no one; restored, they ring again.
            self.env["vodia.pilot.number"].sudo().with_context(active_test=False).search(
                [("mapping_id.user_id", "in", self.ids)])._sync()
        return result

    def unlink(self):
        # The database removes the user's phone without asking Odoo, so their numbers come off the PBX first.
        self.env["vodia.pilot.number"].sudo().with_context(active_test=False).search(
                [("mapping_id.user_id", "in", self.ids)]).unlink()
        return super().unlink()
