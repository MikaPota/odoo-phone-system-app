import hashlib
import logging
import re
import time

import requests

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .pilot_configuration import PBX_ORIGIN, PBX_TENANT

_logger = logging.getLogger(__name__)
HOST = r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
TIMEOUT = (3, 10)


class PilotTrunkClient:
    """Minimal tenant-admin client for the PBX REST API; the digest never leaves the server."""

    def __init__(self, username, digest, origin=PBX_ORIGIN, tenant=PBX_TENANT, http=None):
        self.origin, self.tenant = origin, tenant
        self.http = http or requests.Session()
        self._login(username, digest)

    def _json(self, response):
        if response.status_code != 200:
            raise UserError(_("The PBX rejected the request (HTTP %s).", response.status_code))
        try:
            return response.json()
        except ValueError:
            raise UserError(_("The PBX returned an unreadable answer.")) from None

    def _login(self, username, digest):
        challenge = self._json(self.http.post(
            self.origin + "/rest/system/login", json={"method": "get-password", "username": username},
            timeout=TIMEOUT, allow_redirects=False))
        if (not isinstance(challenge, dict) or challenge.get("error")
                or not isinstance(challenge.get("id"), str)
                or not isinstance(challenge.get("challenge"), str)):
            raise UserError(_("The PBX provisioning login failed."))
        proof = hashlib.sha256(f"{username}:{digest}:{challenge['challenge']}".encode()).hexdigest()
        result = self._json(self.http.post(
            self.origin + "/rest/system/login",
            json={"method": "login-password", "username": username, "id": challenge["id"], "hash": proof},
            timeout=TIMEOUT, allow_redirects=False))
        if not isinstance(result, dict) or not result.get("success"):
            raise UserError(_("The PBX provisioning login failed."))

    def _url(self, resource):
        return f"{self.origin}/rest/domain/{self.tenant}/{resource}"

    def _read(self, resource):
        return self._json(self.http.get(self._url(resource), timeout=TIMEOUT, allow_redirects=False))

    def _write(self, resource, payload):
        return self._json(self.http.post(self._url(resource), json=payload, timeout=TIMEOUT, allow_redirects=False))

    def _delete(self, resource):
        return self.http.delete(self._url(resource), timeout=TIMEOUT, allow_redirects=False).status_code == 200

    def list_trunks(self):
        data = self._read("domain_trunks")
        if isinstance(data, dict):
            data = data.get("trunks", list(data.values()))
        return data if isinstance(data, list) else []

    def create_trunk(self, payload):
        """Same call the tenant UI makes for its generic 'Other' SIP trunk; returns the new trunk id."""
        trunk_id = self._write("domain_trunks", payload)
        if not re.fullmatch(r"[0-9]{1,9}", str(trunk_id)):
            raise UserError(_("The PBX did not return a trunk id."))
        return str(trunk_id)

    def update_trunk(self, trunk_id, values):
        return self._write("domain_trunks?trunk=" + trunk_id, values)

    def read_trunk(self, trunk_id):
        return self._read("edit_trunk/" + trunk_id)

    def list_accounts(self):
        data = self._read("userlist")
        accounts = data.get("accounts") if isinstance(data, dict) else None
        return [a for a in accounts if isinstance(a, dict)] if isinstance(accounts, list) else []

    def read_aliases(self, extension):
        data = self._read("user_settings/" + extension)
        aliases = data.get("alias") if isinstance(data, dict) else None
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise UserError(_("The PBX did not return the names of extension %s.", extension))
        return aliases

    def write_aliases(self, extension, aliases):
        """Replaces the extension's names (its number first) and returns what the PBX stored."""
        self._write("user_settings/" + extension, {"alias": aliases})
        return self.read_aliases(extension)

    def delete_trunk(self, trunk_id):
        return self._delete("domain_trunks?trunk=" + trunk_id)


class GcnaPbxClient(PilotTrunkClient):
    """The same operations for a purchased phone system: GCNA holds the PBX login and makes the change."""

    def __init__(self, env, system_id):
        self.account = env.ref("phone_system.gcna_installation").sudo()
        self.path = f"/v1/phone-systems/{system_id}/pbx"

    def _call(self, method, resource, payload=None):
        return self.account._post(self.path, {"method": method, "resource": resource, "body": payload}, 200,
                                  self.account._authorization()).get("result")

    def _read(self, resource):
        return self._call("GET", resource)

    def _write(self, resource, payload):
        return self._call("POST", resource, payload)

    def _delete(self, resource):
        self._call("DELETE", resource)
        return True


class PilotTrunkWizard(models.TransientModel):
    _name = "vodia.pilot.trunk.wizard"
    _description = "Bring-your-own SIP trunk setup"

    connection_id = fields.Many2one("vodia.pilot.connection", required=True,
                                    default=lambda self: self.env["vodia.pilot.connection"].search(
                                        [("company_id", "=", self.env.company.id)], limit=1))
    name = fields.Char("Trunk name", required=True)
    registrar = fields.Char("SIP server (registrar)", required=True)
    outbound_proxy = fields.Char("Outbound proxy")
    sip_user = fields.Char("SIP user name", required=True)
    sip_password = fields.Char("SIP password", required=True)
    incoming_mapping_id = fields.Many2one(
        "vodia.pilot.mapping", "Incoming calls ring",
        domain="[('connection_id', '=', connection_id), ('extension', '!=', False)]",
        help="The user whose extension rings when the trunk's number is called.")
    incoming_number = fields.Char("Trunk phone number", help="Leave empty when the SIP user name is the number.")
    state = fields.Selection([("draft", "Draft"), ("done", "Done")], default="draft")
    result = fields.Text(readonly=True)

    def _clean(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an administrator may configure trunks."))
        if self.connection_id.company_id not in self.env.user.company_ids:
            raise AccessError(_("The connection belongs to a company you cannot access."))
        data = {"name": (self.name or "").strip(), "registrar": (self.registrar or "").strip().lower(),
                "outbound_proxy": (self.outbound_proxy or "").strip().lower(),
                "reg_user": (self.sip_user or "").strip(), "reg_pass": self.sip_password or ""}
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}", data["name"]):
            raise ValidationError(_("Use up to 40 letters, digits, spaces, dots, dashes or underscores for the trunk name."))
        for key in ("registrar", "outbound_proxy"):
            value = data[key]
            if value and not re.fullmatch(HOST + r"(:[0-9]{1,5})?", value):
                raise ValidationError(_("Enter a valid host name, optionally with a port."))
        if not data["registrar"]:
            raise ValidationError(_("The SIP server is required."))
        if not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,128}", data["reg_user"]):
            raise ValidationError(_("The SIP user name contains unsupported characters."))
        if not 1 <= len(data["reg_pass"]) <= 128 or re.search(r"[\x00-\x1f]", data["reg_pass"]):
            raise ValidationError(_("The SIP password must have 1 to 128 printable characters."))
        if self.incoming_mapping_id:
            data["incoming_number"] = self.env["vodia.pilot.number"]._checked(
                self.connection_id, self.incoming_mapping_id, self.incoming_number or data["reg_user"])
        return data

    def action_create(self):
        self.ensure_one()
        data = self._clean()
        connection = self.connection_id.sudo()
        client = connection._pbx_client()
        if any(isinstance(t, dict) and t.get("name") == data["name"] for t in client.list_trunks()):
            raise UserError(_("A trunk with this name already exists on the PBX."))
        proxy = "sip:" + data["outbound_proxy"] if data["outbound_proxy"] else ""
        trunk_id = client.create_trunk({
            # An empty destination lets the PBX route each called number by the extension names it matches.
            "dial_extension": "", "name": data["name"], "type": "register", "outbound_proxy": proxy,
            "reg_user": data["reg_user"], "reg_pass": data["reg_pass"], "hru": "{request-uri}", "hf": "{trunk}",
            "ht": "{request-uri}", "hrpi": "{from}", "hpr": "{if clip true}id{fi clip true}", "dids": []})
        client.update_trunk(trunk_id, {"reg_registrar": data["registrar"], "reg_account": data["reg_user"],
                                       "reg_display": data["name"]})
        stored = client.read_trunk(trunk_id)
        stored = stored if isinstance(stored, dict) else {}
        problems = [label for key, label in (("reg_registrar", _("SIP server")), ("reg_user", _("SIP user name")))
                    if str(stored.get(key, "")).lower() != data["registrar" if key == "reg_registrar" else key].lower()]
        if problems:
            client.delete_trunk(trunk_id)
            raise UserError(_("The PBX did not confirm the trunk (%s); it was removed again.", ", ".join(problems)))
        time.sleep(3)
        row = next((t for t in client.list_trunks() if isinstance(t, dict) and str(t.get("id")) == trunk_id), {})
        status = " ".join(str(row.get(k) or "") for k in ("status1", "status2", "status3")).strip()
        _logger.info("Vodia trunk wizard created trunk '%s'.", data["name"])
        result = _("Trunk created and read back from the PBX.\nRegistration status: %s", status or _("not reported yet"))
        if data.get("incoming_number"):
            number = self.env["vodia.pilot.number"].search(
                [("connection_id", "=", self.connection_id.id), ("number", "=", data["incoming_number"])], limit=1)
            if number:
                number.mapping_id = self.incoming_mapping_id
            else:
                number = self.env["vodia.pilot.number"].create({"connection_id": self.connection_id.id,
                                                                 "number": data["incoming_number"],
                                                                 "mapping_id": self.incoming_mapping_id.id})
            result += "\n" + number.message
        else:
            result += "\n" + _("Incoming calls ring no one until you assign the trunk's number under Phone Numbers.")
        # The password must not stay in the transient row.
        self.write({"sip_password": "*", "state": "done", "result": result})
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new"}
