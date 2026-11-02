import logging
import re
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

INVITATION_ID = re.compile(r"[A-Za-z0-9_-]{32,128}")
EMAIL = re.compile(r"[^\s@<>()\",;:\\]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
TEXT = re.compile(r"[^\x00-\x1f<>]{1,100}")


class NetworkInvitation(models.Model):
    """A contact card a user sent through GCNA, and the connection once its recipient accepted it.
    Only its sender sees it; GCNA tells this installation which invitations were accepted."""
    _name = "vodia.network.invitation"
    _description = "GCNA network invitation"
    _order = "create_date desc"

    invitation_id = fields.Char(required=True, readonly=True, index=True)
    user_id = fields.Many2one("res.users", required=True, readonly=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, ondelete="cascade")
    email = fields.Char(required=True, readonly=True)
    state = fields.Selection([("sent", "Sent"), ("connected", "Connected")], default="sent", required=True, readonly=True)
    name = fields.Char(readonly=True)
    company_name = fields.Char(readonly=True)
    clearance = fields.Selection([("cleared", "Cleared"), ("review", "Awaiting review")], readonly=True)
    accepted_at = fields.Datetime(readonly=True)
    partner_id = fields.Many2one("res.partner", readonly=True, ondelete="set null", index=True)
    card_updated_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        ('invitation_unique', 'unique(invitation_id)', 'An invitation is recorded once.'),
    ]

    @api.model
    def _remember(self, invitation_id, email, company):
        if isinstance(invitation_id, str) and INVITATION_ID.fullmatch(invitation_id):
            self.sudo().create({"invitation_id": invitation_id, "user_id": self.env.user.id,
                                "company_id": company.id, "email": email.lower()})

    @api.model
    def _sync_connections(self):
        """Fetch the accepted invitations from GCNA and link each to its sender's contact with that email."""
        account = self.env.ref("phone_system.gcna_installation").sudo()
        if not account.token_ciphertext or not self.sudo().search_count([]):
            return
        payload = account._post("/v1/connections", {}, 200, account._authorization())
        connections = payload.get("connections") if isinstance(payload, dict) else None
        for connection in connections if isinstance(connections, list) else []:
            try:
                self._connect(connection)
            except (KeyError, TypeError, ValueError):
                _logger.warning("GCNA reported a malformed network connection; it was skipped.")

    @api.model
    def _sync_connections_soon(self):
        """Sync when the phone panel asks, at most every five minutes; the cron covers the rest."""
        parameters = self.env["ir.config_parameter"].sudo()
        last = parameters.get_param("phone_system.network_synced_at")
        now = fields.Datetime.now()
        if last and fields.Datetime.from_string(last) > now - timedelta(minutes=5):
            return
        parameters.set_param("phone_system.network_synced_at", fields.Datetime.to_string(now))
        try:
            self._sync_connections()
        except UserError:
            _logger.warning("GCNA network connections could not be fetched now; the cron retries.")

    def _connect(self, connection):
        invitation = self.sudo().search([("invitation_id", "=", connection["invitationId"])])
        if not invitation:
            return
        if invitation.state == "connected":
            if "card" in connection:
                invitation._apply_card(connection["card"], connection["cardUpdatedAt"])
            return
        email, name, company = connection["email"], connection["name"], connection.get("company") or ""
        if (not EMAIL.fullmatch(email) or email.lower() != invitation.email or not TEXT.fullmatch(name)
                or (company and not TEXT.fullmatch(company)) or connection["clearance"] not in ("cleared", "review")):
            raise ValueError()
        accepted = datetime.strptime(connection["acceptedAt"][:19], "%Y-%m-%dT%H:%M:%S")
        # The sender's own view of contacts decides the match, so no other company's contact is linked.
        partners = self.env["res.partner"].with_user(invitation.user_id).with_company(invitation.company_id).search(
            [("email", "=ilike", email)], limit=2)
        invitation.write({"state": "connected", "name": name, "company_name": company, "clearance": connection["clearance"],
                          "accepted_at": accepted, "partner_id": partners.id if len(partners) == 1 else False})

    def _apply_card(self, card, updated):
        """The connection changed their GCNA card: the linked contact follows it (a customer decision)."""
        updated = datetime.strptime(updated[:19], "%Y-%m-%dT%H:%M:%S")
        if self.card_updated_at and updated <= self.card_updated_at:
            return
        values = self._card_values(card)
        if self.partner_id:
            self.partner_id.sudo().write(values)
            self.partner_id.sudo().message_post(body=_("Contact details updated from %s's GCNA business card.", values["name"]))
        self.write({"card_updated_at": updated, "name": values["name"], "company_name": values.get("company_name") or False})

    def _card_values(self, card):
        name = card["name"]
        if not TEXT.fullmatch(name):
            raise ValueError()
        def text(value):
            if value in (None, ""):
                return False
            if not isinstance(value, str) or not TEXT.fullmatch(value):
                raise ValueError()
            return value
        phones = {phone["label"]: phone["number"] for phone in card.get("phones") or []
                  if phone.get("label") in ("work", "mobile") and re.fullmatch(r"\+?[0-9]{2,15}", phone.get("number") or "")}
        address = card.get("address") or {}
        website = card.get("website")
        if website and not re.fullmatch(r"https?://[^\s<>\"]{1,200}", website):
            raise ValueError()
        country = self.env["res.country"].search([("code", "=", address["country"].upper())], limit=1) if text(address.get("country")) else False
        return {"name": name, "function": text(card.get("role")), "company_name": text(card.get("company")),
                "phone": phones.get("work") or phones.get("mobile") or False, "website": website or False,
                "street": text(address.get("street")), "zip": text(address.get("postcode")), "city": text(address.get("city")),
                "country_id": country.id if country else False}

    @api.model
    def _pending(self):
        """The caller's accepted invitations without a contact yet."""
        invitations = self.search([("user_id", "=", self.env.user.id), ("state", "=", "connected"), ("partner_id", "=", False)])
        return [{"invitation_id": i.invitation_id, "name": i.name, "company": i.company_name or "", "email": i.email}
                for i in invitations]

    @api.model
    def _create_contact(self, invitation_id):
        invitation = self.search([("invitation_id", "=", invitation_id), ("user_id", "=", self.env.user.id),
                                  ("state", "=", "connected"), ("partner_id", "=", False)]) if isinstance(invitation_id, str) else self
        if not invitation:
            raise UserError(_("This connection has no contact to create any more."))
        partner = self.env["res.partner"].create({"name": invitation.name, "email": invitation.email,
                                                  "company_name": invitation.company_name or False})
        invitation.sudo().partner_id = partner
        return partner
