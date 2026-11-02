import logging

import requests

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .pilot_configuration import HOSTNAME

_logger = logging.getLogger(__name__)


class PilotConnectWizard(models.TransientModel):
    """Connects an existing Vodia PBX tenant without changing it. The administrator only names the tenant and
    the users who get a phone; each user then links their own extension by signing in with its web password."""

    _name = "vodia.pilot.connect.wizard"
    _description = "Connect an existing Vodia PBX"

    name = fields.Char("Name", required=True, default=lambda self: self.env.company.name)
    pbx_host = fields.Char("PBX tenant address", required=True)
    user_ids = fields.Many2many("res.users", string="Users with a phone",
                                domain="[('share', '=', False), ('company_ids', 'in', allowed_company_ids)]")
    state = fields.Selection([("draft", "Draft"), ("done", "Done")], default="draft")
    result = fields.Text(readonly=True)

    def action_connect(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an administrator may connect a phone system."))
        host = (self.pbx_host or "").strip().lower()
        if not HOSTNAME.fullmatch(host):
            raise ValidationError(_("Enter the PBX tenant address only, for example pbx.example.com."))
        if not self.user_ids:
            raise ValidationError(_("Choose at least one user who gets a phone."))
        company = self.env.company
        if any(user.share or not user.active or company not in user.company_ids for user in self.user_ids):
            raise ValidationError(_("Choose active internal users of the current company."))
        try:
            # Only proves that a phone system answers there over valid TLS; nothing is sent to it.
            requests.get(f"https://{host}/", timeout=(3, 10), allow_redirects=False)
        except requests.RequestException:
            raise UserError(_("No phone system answers at %s. Check the address.", host)) from None
        Connection = self.env["vodia.pilot.connection"].sudo()
        connection = Connection.search([("pbx_host", "=", host), ("company_id", "=", company.id),
                                        ("gcna_system_id", "=", False)], limit=1) \
            or Connection.create({"name": self.name, "company_id": company.id, "pbx_host": host})
        Mapping = self.env["vodia.pilot.mapping"].sudo()
        group = self.env.ref("phone_system.group_pilot")
        for user in self.user_ids:
            mapping = Mapping.search([("company_id", "=", company.id), ("user_id", "=", user.id)], limit=1)
            if not mapping:
                Mapping.create({"user_id": user.id, "connection_id": connection.id})
            elif mapping.connection_id != connection:
                # The old extension and login belong to the old system; the user links a new one by signing in.
                mapping.write({"connection_id": connection.id, "extension": False, "credential_ciphertext": False})
            if group not in user.group_ids:
                user.sudo().write({"group_ids": [(4, group.id)]})
        _logger.info("Vodia existing PBX connected for %s users.", len(self.user_ids))
        self.write({"pbx_host": host, "state": "done", "result": _(
            "Phone system connected. %s users will be asked for their extension and Vodia PBX web password "
            "the next time they open the phone.", len(self.user_ids))})
        return {"type": "ir.actions.act_window", "name": _("Connect existing phone system"), "res_model": self._name,
                "res_id": self.id, "view_mode": "form", "views": [[False, "form"]], "target": "new"}
