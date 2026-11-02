import hashlib
import json
import logging
import os
import re
from datetime import timedelta

import requests
from cryptography.fernet import InvalidToken

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.phone_validation.tools import phone_validation
from .credentials import credential_cipher

_logger = logging.getLogger(__name__)
PBX_TENANT = "odoo-pilot.pbx.gcn-alliance.com"
PBX_ORIGIN = "https://" + PBX_TENANT
HOSTNAME = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
EMAIL = re.compile(r"[^\s@<>()\",;:\\]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")


def server_admin_digest():
    """(login, digest) of the tenant-admin login the operator gave this server, or None."""
    login = os.environ.get("VODIA_PROVISIONING_USER", "")
    password = os.environ.get("VODIA_PROVISIONING_PASSWORD", "")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", login) or not password:
        return None
    return login, hashlib.md5(password.encode("utf-8")).hexdigest()


class PilotConnection(models.Model):
    _name = "vodia.pilot.connection"
    _description = "Phone System Connection"

    name = fields.Char(required=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    # The PBX tenant this connection's users register with; its web origin is https://<pbx_host>.
    pbx_host = fields.Char("PBX tenant host", required=True, default=PBX_TENANT)
    # Set for phone systems bought through checkout; the GCNA system this connection belongs to.
    gcna_system_id = fields.Char(copy=False, index=True, readonly=True)
    # The number outside parties see, read from the tenant's registered trunk at most once a day.
    outbound_number = fields.Char(readonly=True, copy=False, help="Caller number of the PBX trunk in E.164 format.")
    outbound_number_checked = fields.Datetime(readonly=True, copy=False)
    # Only for a PBX the customer already runs; GCNA makes the changes on a purchased system.
    pbx_admin_user = fields.Char("PBX administrator login", copy=False)
    pbx_admin_password = fields.Char("PBX administrator password", compute="_compute_pbx_admin_password",
                                     inverse="_set_pbx_admin_password",
                                     help="Write-only. Leave empty to keep the stored password.")
    pbx_admin_ciphertext = fields.Char(groups="base.group_system", copy=False)

    user_count = fields.Integer(compute="_compute_user_counts", string="Users with a phone")
    linked_count = fields.Integer(compute="_compute_user_counts", string="Users signed in")
    has_own_phone = fields.Boolean(compute="_compute_user_counts", string="Your phone")

    _sql_constraints = [
        ('gcna_system_unique', 'UNIQUE(gcna_system_id)', 'A purchased phone system has one connection.'),
    ]

    def _compute_pbx_admin_password(self):
        for record in self:
            record.pbx_admin_password = False

    def _set_pbx_admin_password(self):
        for record in self:
            password = record.pbx_admin_password
            if password:
                if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", record.pbx_admin_user or ""):
                    raise ValidationError(_("Enter the PBX administrator login before its password."))
                record.pbx_admin_ciphertext = credential_cipher(self.env).encrypt(json.dumps({
                    "user": record.pbx_admin_user, "host": record.pbx_host,
                    "digest": hashlib.md5(password.encode("utf-8")).hexdigest(),
                }).encode("utf-8")).decode("ascii")
            record.invalidate_recordset(["pbx_admin_password"])

    @api.depends_context("uid", "allowed_company_ids")
    def _compute_user_counts(self):
        mappings = self.env["vodia.pilot.mapping"].sudo().search([("connection_id", "in", self.ids), ("active", "=", True)])
        for record in self:
            own = mappings.filtered(lambda mapping: mapping.connection_id == record)
            record.user_count = len(own)
            record.linked_count = len(own.filtered("credential_ciphertext"))
            # The browser phone uses the active company's mapping, so only that one is the user's phone here.
            record.has_own_phone = bool(own.filtered(
                lambda mapping: mapping.user_id == self.env.user and mapping.company_id == self.env.company))

    @api.constrains("pbx_host")
    def _check_pbx_host(self):
        for record in self:
            if not HOSTNAME.fullmatch(record.pbx_host or ""):
                raise ValidationError(_("Enter the PBX tenant host name only, for example abc123.pbx.gcn-alliance.com."))

    def _pbx_origin(self):
        self.ensure_one()
        return "https://" + self.pbx_host

    def _outbound_number(self):
        self.ensure_one()
        if self.outbound_number_checked and self.outbound_number_checked > fields.Datetime.now() - timedelta(days=1):
            return self.outbound_number or ""
        if not self.gcna_system_id and not self._pbx_login():
            return ""
        number = ""
        try:
            client = self._pbx_client()
            for trunk in client.list_trunks():
                if not isinstance(trunk, dict) or trunk.get("disabled") or not str(trunk.get("status1", "")).startswith("200"):
                    continue
                details = client.read_trunk(str(trunk.get("id", "")))
                number = self._e164(details.get("ani_regular") or details.get("reg_account") or "")
                if number:
                    break
        except (UserError, requests.RequestException, ValueError, TypeError, AttributeError):
            _logger.warning("The PBX trunk number of %s could not be read.", self.pbx_host)
            return self.outbound_number or ""
        self.sudo().write({"outbound_number": number or False, "outbound_number_checked": fields.Datetime.now()})
        return number

    def _e164(self, raw):
        digits = re.sub(r"[^0-9+]", "", raw if isinstance(raw, str) else "")
        if digits.startswith("00"):
            digits = "+" + digits[2:]
        if not digits.startswith("+"):
            country = self.company_id.country_id
            digits = phone_validation.phone_format(digits, country.code or None, country.phone_code or None,
                                                   force_format="E164", raise_exception=False) or ""
        return digits if re.fullmatch(r"\+[0-9]{7,15}", digits) else ""

    def _number_label(self, number):
        """The E.164 number as people read it, e.g. +49 30 43972971."""
        if not number:
            return ""
        country = self.company_id.country_id
        return phone_validation.phone_format(number, country.code or None, country.phone_code or None,
                                             force_format="INTERNATIONAL", raise_exception=False) or number

    def _pbx_login(self):
        """(login, digest) from the settings page, else from an operator's server environment."""
        self.ensure_one()
        stored = self.sudo().pbx_admin_ciphertext
        if stored:
            try:
                value = json.loads(credential_cipher(self.env).decrypt(stored.encode("ascii")))
            except (InvalidToken, ValueError):
                raise UserError(_("The stored administrator login cannot be read. Enter it again.")) from None
            # A login saved for one PBX is never sent to another host.
            return (value["user"], value["digest"]) if value["host"] == self.pbx_host else None
        return server_admin_digest()

    def _pbx_client(self):
        self.ensure_one()
        from .pilot_trunk_wizard import GcnaPbxClient, PilotTrunkClient
        if self.gcna_system_id:
            return GcnaPbxClient(self.env, self.gcna_system_id)
        login = self._pbx_login()
        if not login:
            raise UserError(_("Enter the PBX administrator login in this phone system's settings."))
        return PilotTrunkClient(*login, origin=self._pbx_origin(), tenant=self.pbx_host)


class PilotMapping(models.Model):
    _name = "vodia.pilot.mapping"
    _description = "Phone System User Mapping"
    _rec_name = "user_id"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade")
    connection_id = fields.Many2one("vodia.pilot.connection", required=True, ondelete="restrict")
    company_id = fields.Many2one(related="connection_id.company_id", store=True)
    # Empty until the user links their extension by signing in with its web password.
    extension = fields.Char()
    credential_ciphertext = fields.Char(groups="base.group_system", copy=False)
    web_password = fields.Char(
        compute="_compute_web_password", inverse="_set_web_password",
        groups="base.group_system", copy=False,
        help="Write-only individual extension web password. An empty value clears the stored credential.",
    )
    credential_configured = fields.Boolean(compute="_compute_credential_configured")
    active = fields.Boolean(default=True)
    # Calling numbers outside the PBX is off until an administrator allows it; the PBX dial plan stays the real control.
    external_calling = fields.Boolean(help="Allow this user to call external numbers through the PBX trunk.")


    _sql_constraints = [
        ('user_company_unique', 'UNIQUE(user_id, company_id)', 'Only one mapping per user and company is permitted.'),
        ('extension_connection_unique', 'UNIQUE(connection_id, extension)', 'An extension can be mapped to only one user.'),
    ]

    @api.constrains("user_id", "connection_id")
    def _check_user(self):
        for record in self:
            if (not record.user_id.active or record.user_id.share
                    or record.company_id not in record.user_id.company_ids):
                raise ValidationError(_("Map only an active internal user belonging to the connection company."))

    @api.constrains("extension", "user_id")
    def _check_extension(self):
        for record in self.filtered("extension"):
            if not re.fullmatch(r"[0-9]{2,6}", record.extension):
                raise ValidationError(_("Use an internal numeric extension containing two to six digits."))
            if self.sudo().search_count([
                ("connection_id.pbx_host", "=", record.connection_id.pbx_host),
                ("extension", "=", record.extension), ("user_id", "!=", record.user_id.id),
                ("id", "!=", record.id),
            ]):
                raise ValidationError(_("An extension cannot belong to different Odoo users."))

    def _compute_web_password(self):
        for record in self:
            record.web_password = False

    @api.depends("credential_ciphertext")
    def _compute_credential_configured(self):
        for record in self:
            record.credential_configured = bool(record.credential_ciphertext)

    def action_clear_credential(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an administrator may clear a PBX credential."))
        self.write({"credential_ciphertext": False})
        return True

    def _credential_cipher(self):
        return credential_cipher(self.env)

    def _credential_scope(self):
        self.ensure_one()
        return {"user": self.user_id.id, "connection": self.connection_id.id,
                "extension": self.extension}

    def _set_web_password(self):
        for record in self:
            password = record.web_password
            if not password:
                record.credential_ciphertext = False
            else:
                if not record.extension:
                    raise ValidationError(_("Set the extension before its web password."))
                if len(password) < 16:
                    raise ValidationError(_("Use an individual web password containing at least 16 characters."))
                record._store_digest(hashlib.md5(password.encode("utf-8")).hexdigest())
            record.invalidate_recordset(["web_password"])

    def _store_digest(self, digest):
        self.ensure_one()
        value = dict(self._credential_scope(), digest=digest)
        self.credential_ciphertext = self._credential_cipher().encrypt(json.dumps(value).encode("utf-8")).decode("ascii")

    def _password_digest(self):
        self.ensure_one()
        if not self.credential_ciphertext:
            raise UserError(_("Ask an administrator to configure your individual PBX web credential."))
        try:
            value = json.loads(self._credential_cipher().decrypt(
                self.credential_ciphertext.encode("ascii")
            ))
            if not isinstance(value, dict):
                raise ValueError("Invalid credential envelope.")
            digest = value.pop("digest")
            if value != self._credential_scope() or not re.fullmatch(r"[0-9a-f]{32}", digest):
                raise ValueError("Credential scope mismatch.")
            return digest
        except (InvalidToken, ValueError, KeyError, TypeError, UnicodeError):
            _logger.warning("Vodia stored individual credential validation failed.")
            raise UserError(_("The individual PBX credential is invalid; ask an administrator to replace it.")) from None

    def _caller_mapping(self):
        user = self.env.user
        if (not user.active or user.share
                or not user.has_group("phone_system.group_pilot")):
            raise AccessError(_("You are not authorized for the Phone System."))
        company = self.env.company
        if company not in user.company_ids:
            raise AccessError(_("The current company is not authorized."))
        mapping = self.sudo().search([
            ("user_id", "=", user.id), ("company_id", "=", company.id),
            ("active", "=", True), ("connection_id.active", "=", True),
        ], limit=2)
        if len(mapping) != 1:
            raise UserError(_("Ask an administrator to configure exactly one active mapping for your current company."))
        return mapping

    def _browser_configuration(self):
        mapping = self._caller_mapping()
        directory = self.sudo().search([
            ("connection_id", "=", mapping.connection_id.id),
            ("active", "=", True), ("connection_id.active", "=", True),
            ("user_id.active", "=", True), ("user_id.share", "=", False),
            ("user_id.groups_id", "in", self.env.ref("phone_system.group_pilot").id),
            ("credential_ciphertext", "!=", False),
        ], order="extension, id")
        contacts = {record.extension: {"extension": record.extension, "name": record.user_id.name}
                    for record in directory if record.company_id in record.user_id.company_ids}
        connection = mapping.connection_id
        if not mapping.sudo().credential_ciphertext:
            # The user has not shared their PBX login yet; the phone asks for consent before anything else.
            return {"origin": connection._pbx_origin(), "tenant": connection.pbx_host, "extension": mapping.extension or "",
                    "credential_required": True, "directory": []}
        return {"origin": connection._pbx_origin(), "tenant": connection.pbx_host, "extension": mapping.extension,
                "directory": list(contacts.values()), "external_calling": mapping.external_calling,
                "number": connection._number_label(connection._outbound_number())}

    def _send_contact_card(self, email):
        """A courtesy of GCNA: it mails the caller's own contact card, so no mail credential is needed here."""
        mapping = self._caller_mapping()
        email = email.strip() if isinstance(email, str) else ""
        if not EMAIL.fullmatch(email):
            raise UserError(_("Enter a valid email address."))
        user = self.env.user
        card = {"name": user.name, "company": mapping.company_id.name}
        if user.email and EMAIL.fullmatch(user.email):
            card["email"] = user.email
        number = mapping.connection_id._outbound_number()
        if number:
            card["phone"] = number
        account = self.env.ref("phone_system.gcna_installation").sudo()
        # GCNA writes the email in the sender's language (German or English).
        payload = {"to": email, "card": card, "lang": user.lang or "en_US"}
        result = account._post("/v1/contact-cards", payload, 202, account._authorization())
        # With invitations switched on, GCNA names the invitation, so its acceptance can be followed.
        if isinstance(result, dict):
            self.env["vodia.network.invitation"]._remember(result.get("invitationId"), email, mapping.company_id)
        return {"sent": True}

    def _contact_card_follow_up(self, call_uid):
        """After an answered call with someone outside, offer the caller to send their card, unless already sent."""
        self._caller_mapping()
        call = self.env["vodia.pilot.call"].search([("user_id", "=", self.env.user.id), ("call_uid", "=", call_uid)], limit=1) \
            if isinstance(call_uid, str) else self.env["vodia.pilot.call"]
        if not call or call.outcome != "answered" or call.peer_user_id or not call.peer_number:
            return {"prompt": False}
        partner = call.peer_partner_id
        email = partner.email.strip().lower() if partner.email and EMAIL.fullmatch(partner.email.strip()) else ""
        if email and (partner.vodia_network_connected or self.env["vodia.network.invitation"].search_count(
                [("user_id", "=", self.env.user.id), ("email", "=", email)])):
            return {"prompt": False}
        return {"prompt": True, "email": email, "name": partner.name or "", "number": call.peer_number}

    def _network_pending(self):
        self._caller_mapping()
        return self.env["vodia.network.invitation"]._pending()

    def _network_create_contact(self, invitation_id):
        self._caller_mapping()
        return self.env["vodia.network.invitation"]._create_contact(invitation_id)

    def _contact_card_recipient(self):
        """The contact of the caller's most recent call that matched one, to suggest as recipient."""
        self._caller_mapping()
        call = self.env["vodia.pilot.call"].search(
            [("user_id", "=", self.env.user.id), ("peer_partner_id", "!=", False)], limit=1)
        partner = call.peer_partner_id
        return {"email": partner.email or "", "name": partner.name or ""}

    def _consent_credential(self, extension, password, consent):
        """The caller agrees to share their own PBX web login with Odoo. Signing in with the extension and its
        web password proves the extension is theirs; only then is it linked to the caller and the login stored,
        encrypted and scoped like any other individual credential."""
        mapping = self._caller_mapping()
        if consent is not True:
            raise UserError(_("Agree to use your PBX login in Odoo first."))
        if not isinstance(extension, str) or not re.fullmatch(r"[0-9]{2,6}", extension):
            raise UserError(_("Enter your extension number."))
        if not isinstance(password, str) or not 1 <= len(password) <= 128 or re.search(r"[\x00-\x1f]", password):
            raise UserError(_("Enter your PBX web password."))
        if self.sudo().search_count([("connection_id.pbx_host", "=", mapping.connection_id.pbx_host),
                                     ("extension", "=", extension), ("user_id", "!=", mapping.user_id.id)]):
            raise UserError(_("Extension %s is already linked to another Odoo user.", extension))
        digest = hashlib.md5(password.encode("utf-8")).hexdigest()
        login = mapping.connection_id._pbx_origin() + "/rest/system/login"
        try:
            # The challenge id carries the login, as in the browser's own sign-in; no cookie is needed.
            challenge = requests.post(login, json={"method": "get-password", "username": extension},
                                      timeout=(2, 4), allow_redirects=False).json()
            proof = hashlib.sha256(f"{extension}:{digest}:{challenge['challenge']}".encode("utf-8")).hexdigest()
            result = requests.post(login, json={"method": "login-password", "username": extension,
                                                "id": challenge["id"], "hash": proof},
                                   timeout=(2, 4), allow_redirects=False).json()
        except (requests.RequestException, ValueError, KeyError, TypeError):
            _logger.warning("Vodia login check failed for a consenting mapping.")
            raise UserError(_("The phone system could not be reached. Try again later.")) from None
        if not isinstance(result, dict) or result.get("success") is not True:
            raise UserError(_("The phone system did not accept this extension and password."))
        mapping = mapping.sudo()
        # Link the extension before storing the login, so the credential's scope names it.
        mapping.extension = extension
        mapping._store_digest(digest)
        _logger.info("Vodia user %s shared their PBX login by consent.", self.env.user.id)
        return True

    def _mint_browser_session(self):
        mapping = self._caller_mapping()
        digest = mapping._password_digest()
        try:
            response = requests.post(
                mapping.connection_id._pbx_origin() + "/rest/system/login",
                json={"method": "get-password", "username": mapping.extension},
                timeout=(2, 4), allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError("PBX rejected challenge request.")
            payload = response.json()
            if (not isinstance(payload, dict) or payload.get("error")
                    or not isinstance(payload.get("id"), str)
                    or not re.fullmatch(r"[0-9a-zA-Z_-]{8,128}", payload["id"])
                    or not isinstance(payload.get("challenge"), str)
                    or not re.fullmatch(r"[0-9a-zA-Z_-]{16,128}", payload["challenge"])
                    or type(payload.get("timeout")) not in (int, float)
                    or not 0 < payload["timeout"] <= 600000):
                raise ValueError("Invalid PBX password challenge.")
        except (requests.RequestException, ValueError):
            _logger.warning("Vodia password challenge delivery failed for an authorized mapping.")
            raise UserError(_("PBX login failed. Ask an administrator to check the PBX and your mapping.")) from None
        proof = hashlib.sha256(
            (mapping.extension + ":" + digest + ":" + payload["challenge"]).encode("utf-8")
        ).hexdigest()
        return {"origin": mapping.connection_id._pbx_origin(), "tenant": mapping.connection_id.pbx_host, "extension": mapping.extension,
                "login": {"method": "login-password", "username": mapping.extension,
                          "id": payload["id"], "hash": proof}}
