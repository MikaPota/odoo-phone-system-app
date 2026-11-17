import json
import logging
import os
import re
from datetime import datetime
from urllib.parse import quote

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import LazyTranslate

from odoo.addons.gcna_link.models.gcna_account import GCNA_ORIGIN, GCNADenied, GCNATermsPending

_logger = logging.getLogger(__name__)
_lt = LazyTranslate(__name__)
EMAIL = re.compile(r"[^\s@<>()\",;:\\]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
TEXT = re.compile(r"[^\x00-\x1f<>]{1,100}")
CARD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
HASH = re.compile(r"[0-9a-f]{64}")
ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
CARD_FIELDS = ("name", "organization", "role", "phone", "email", "web")

# What BIZZKICKR answers through GCNA, in the user's words.
BIZZKICKR_ERRORS = {
    "PERSON_UNKNOWN": _lt("Register for BIZZKICKR first."),
    "PERSON_UNCONFIRMED": _lt("Open the link BIZZKICKR emailed you to confirm, then refresh."),
    "CARDS_EXIST": _lt("You already have a card. Refresh to see it."),
    "INVALID_CARD": _lt("Check the card: each field takes at most 100 characters, without < or >."),
    "INVALID_INPUT": _lt("Check the card: a phone such as +49 30 1234567 and a web address such as example.com."),
    "INVALID_EMAIL": _lt("Enter a valid email address."),
    "INVALID_RECIPIENT": _lt("This contact has no valid email address."),
    "OWN_CARD": _lt("This is your own address."),
    "MAIL_OFF": _lt("BIZZKICKR cannot send email right now. Retry later."),
    "NOT_FOUND": _lt("This no longer exists. Refresh and try again."),
    "TOO_MANY_REQUESTS": _lt("Too many attempts. Retry later."),
    "RATE_LIMITED": _lt("Too many attempts. Retry later."),
    "BIZZKICKR_UNAVAILABLE": _lt("BIZZKICKR cannot be reached right now. Retry later."),
}


def _text(value):
    """A short text from BIZZKICKR, or False; anything else is a malformed answer."""
    if value in (None, ""):
        return False
    if not isinstance(value, str) or not TEXT.fullmatch(value):
        raise ValueError()
    return value


def _when(value):
    if not isinstance(value, str) or not ISO.fullmatch(value):
        raise ValueError()
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")


class BizzKickRPerson(models.Model):
    """A user of this database in BIZZKICKR, through GCNA as reseller (GCNA REQ-006).

    The person's cards, requests and connections live at BIZZKICKR; this record keeps only the user's
    state there and a copy of their default card to show it, and when their connections were last fetched.
    """
    _name = "bizzkickr.person"
    _description = "BIZZKICKR person"
    _rec_name = "user_id"

    user_id = fields.Many2one("res.users", required=True, readonly=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, ondelete="cascade")
    email = fields.Char(readonly=True, help="The address BIZZKICKR knows the user by.")
    state = fields.Selection([
        ("none", "Not registered"),
        ("unconfirmed", "Waiting for the email confirmation"),
        ("no_card", "Confirmed, no card yet"),
        ("ready", "Ready"),
    ], default="none", required=True, readonly=True)
    card_id = fields.Char(readonly=True)
    card_name = fields.Char("Name")
    card_organization = fields.Char("Organization")
    card_role = fields.Char("Role")
    card_phone = fields.Char("Phone")
    card_email = fields.Char("Email on the card")
    card_web = fields.Char("Web")
    card_link = fields.Char("Card link", readonly=True)
    card_qr_url = fields.Char("QR code", compute="_compute_card_qr_url")
    synced_at = fields.Datetime(readonly=True, help="Until when connections were fetched.")
    connection_ids = fields.One2many("bizzkickr.connection", "person_id")

    _user_unique = models.Constraint("unique(user_id)", "A user is one BIZZKICKR person.")

    @api.depends("card_link")
    def _compute_card_qr_url(self):
        # Odoo draws the QR code of the card's link itself; no image from outside is stored or shown.
        for person in self:
            person.card_qr_url = (f"/report/barcode/?barcode_type=QR&value={quote(person.card_link, safe='')}&width=480&height=480"
                                  if person.card_link else False)

    # ── talking to BIZZKICKR through GCNA ─────────────────────────────────────────────────────────────
    def _call(self, path, data=None, expected=(200,)):
        """One request to GCNA's /v1/bizzkickr/*, as this person; GCNA forwards it to BIZZKICKR."""
        self.ensure_one()
        account = self.env.ref("gcna_link.gcna_installation").sudo()
        if not account._linked():
            raise UserError(_("An administrator must connect this database to GCN-ALLIANCE first: BIZZKICKR > Setup."))
        try:
            response = requests.post(
                (os.environ.get("GCNA_SERVICE_ORIGIN") or GCNA_ORIGIN) + "/v1/bizzkickr/" + path,
                json={"person": self.email, **(data or {})}, timeout=(3, 15), allow_redirects=False,
                headers={"Authorization": f"Bearer {account._authorization()}"},
            )
        except requests.RequestException:
            _logger.warning("BIZZKICKR request could not be delivered to GCNA.")
            raise UserError(str(BIZZKICKR_ERRORS["BIZZKICKR_UNAVAILABLE"])) from None
        code = account._error_code(response)
        if response.status_code == 403 and code == "TERMS_ACCEPTANCE_REQUIRED":
            raise GCNATermsPending(_("GCNA updated its terms. A company administrator must sign in at app.gcn-alliance.com and accept them; BIZZKICKR then works again."))
        if response.status_code in (401, 403):
            raise GCNADenied(_("GCNA no longer accepts this database's connection. An administrator must connect it again: BIZZKICKR > Setup."))
        if response.status_code not in expected:
            if code in BIZZKICKR_ERRORS:
                raise UserError(str(BIZZKICKR_ERRORS[code]))
            _logger.warning("BIZZKICKR request rejected (HTTP %s).", response.status_code)
            raise UserError(str(BIZZKICKR_ERRORS["BIZZKICKR_UNAVAILABLE"]))
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, TypeError):
            self._malformed()

    @api.model
    def _malformed(self):
        _logger.warning("BIZZKICKR returned a malformed answer.")
        raise UserError(_("BIZZKICKR answered unexpectedly. Retry later or contact your service provider."))

    # ── the user's own person ─────────────────────────────────────────────────────────────────────────
    @api.model
    def _mine(self):
        user = self.env.user
        if user.share:
            raise UserError(_("BIZZKICKR is for the users of this company."))
        person = self.sudo().search([("user_id", "=", user.id)], limit=1)
        if not person:
            person = self.sudo().create({"user_id": user.id, "company_id": self.env.company.id})
        if user.email and person.email != user.email.strip().lower() and person.state == "none":
            person.email = user.email.strip().lower()
        return person

    @api.model
    def action_open_mine(self):
        person = self._mine()
        if person.state != "none":
            try:
                person._refresh()
            except UserError as error:
                # The page still opens; it says what is wrong.
                _logger.info("BIZZKICKR could not be refreshed: %s", error)
        return person._form()

    def _form(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "bizzkickr.person", "res_id": self.id, "view_mode": "form",
                "target": "current", "name": _("My BIZZKICKR card")}

    def _check_owner(self):
        for person in self:
            if person.user_id != self.env.user:
                raise UserError(_("This is another user's BIZZKICKR card."))

    def action_register(self):
        """BIZZKICKR emails the user a link; opening it confirms that GCNA may act for them for this company."""
        self._check_owner()
        person = self.sudo()
        email = (self.env.user.email or "").strip().lower()
        if not EMAIL.fullmatch(email):
            raise UserError(_("Add your email address in your preferences first."))
        person.email = email
        payload = person._call("people", {"lang": self.env.user.lang or "en_US", "companyName": self.env.company.name, "app": "odoo"}, (200, 202))
        if payload.get("state") not in ("confirmed", "unconfirmed"):
            self._malformed()
        person.state = "unconfirmed" if payload["state"] == "unconfirmed" else "no_card"
        if person.state != "unconfirmed":
            person._refresh()
        return person._form()

    def action_refresh(self):
        self._check_owner()
        self.sudo()._refresh()
        return self._form()

    def _refresh(self):
        """Ask BIZZKICKR for the person's state and default card."""
        self.ensure_one()
        state = self._call("person").get("state")
        if state == "none":
            self.write({"state": "none", "card_id": False})
            return
        if state == "unconfirmed":
            self.state = "unconfirmed"
            return
        if state != "confirmed":
            self._malformed()
        cards = self._call("cards").get("cards")
        if not isinstance(cards, list):
            self._malformed()
        card = next((c for c in cards if isinstance(c, dict) and c.get("isDefault")), cards[0] if cards else None)
        if not card:
            self.write({"state": "no_card", "card_id": False, "card_link": False})
            return
        self._show(card)

    def _show(self, card):
        try:
            if not isinstance(card.get("id"), str) or not CARD_ID.fullmatch(card["id"]):
                raise ValueError()
            values = {f"card_{key}": _text(card.get(key)) for key in CARD_FIELDS}
            link = card.get("link")
            if not isinstance(link, str) or not re.fullmatch(r"https://[^\s<>\"]{1,300}", link):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            self._malformed()
        self.write({**values, "state": "ready", "card_id": card["id"], "card_link": link})

    def _from_odoo(self):
        """A first card from what this database knows about the user and their company."""
        user, company = self.user_id, self.company_id
        partner = user.partner_id
        website = company.website or ""
        return {
            "name": user.name, "organization": company.name, "role": partner.function or "",
            "phone": partner.phone or getattr(partner, "mobile", "") or company.phone or "", "email": user.email or "",
            "web": website.removeprefix("https://").removeprefix("http://"),
        }

    def action_create_card(self):
        self._check_owner()
        person = self.sudo()
        card = {key: (value or "")[:100] for key, value in person._from_odoo().items()}
        payload = person._call("cards/create", {"card": card}, (201,))
        person._show(payload.get("card") or {})
        return person._form()

    def action_save_card(self):
        self._check_owner()
        person = self.sudo()
        if not person.card_id:
            raise UserError(_("Create your card first."))
        card = {key: person[f"card_{key}"] or "" for key in CARD_FIELDS}
        if not card["name"].strip():
            raise UserError(_("Your card needs a name."))
        payload = person._call(f"cards/{person.card_id}/update", {"card": card})
        person._show(payload.get("card") or {})
        return person._form()

    def _send_to(self, address):
        """BIZZKICKR mails the person's default card, with its vCard and the link to connect."""
        self.ensure_one()
        if not self.card_id:
            raise UserError(_("Create your BIZZKICKR card first: BIZZKICKR > My card."))
        if not isinstance(address, str) or not EMAIL.fullmatch(address.strip()):
            raise UserError(str(BIZZKICKR_ERRORS["INVALID_RECIPIENT"]))
        self._call(f"cards/{self.card_id}/send", {"to": address.strip().lower(), "lang": self.env.user.lang or "en_US"}, (202,))

    # ── requests and connections ──────────────────────────────────────────────────────────────────────
    def action_requests(self):
        self._check_owner()
        person = self.sudo()
        rows = person._call("requests").get("requests")
        if not isinstance(rows, list):
            self._malformed()
        Request = self.env["bizzkickr.request"]
        Request.search([("create_uid", "=", self.env.uid)]).unlink()
        records = Request.browse()
        for row in rows:
            try:
                if not isinstance(row.get("id"), str) or not HASH.fullmatch(row["id"]):
                    raise ValueError()
                records |= Request.create({
                    "request_id": row["id"], "name": _text(row.get("name")) or "?", "organization": _text(row.get("organization")),
                    "email": row["email"] if isinstance(row.get("email"), str) and EMAIL.fullmatch(row["email"]) else False,
                    "event": _text(row.get("event")), "requested_at": _when(row["requestedAt"]) if row.get("requestedAt") else False,
                })
            except (ValueError, TypeError, AttributeError, KeyError):
                _logger.warning("BIZZKICKR returned a malformed request; it is not shown.")
        return {"type": "ir.actions.act_window", "res_model": "bizzkickr.request", "view_mode": "list",
                "domain": [("id", "in", records.ids)], "name": _("BIZZKICKR requests"), "target": "current",
                "context": {"create": False, "edit": False, "delete": False}}

    @api.model
    def action_open_requests(self):
        return self._mine().action_requests()

    @api.model
    def _sync_all(self):
        """The cron: every person's connections changed since the last run become contacts here."""
        for person in self.sudo().search([("state", "in", ("no_card", "ready"))]):
            try:
                with self.env.cr.savepoint():
                    person._sync()
            except UserError as error:
                _logger.info("BIZZKICKR connections of one user were not fetched now: %s", error)

    def action_sync(self):
        self._check_owner()
        self.sudo()._sync()
        return {"type": "ir.actions.act_window", "res_model": "bizzkickr.connection", "view_mode": "list",
                "domain": [("person_id", "=", self.id)], "name": _("BIZZKICKR connections"), "target": "current",
                "context": {"create": False}}

    @api.model
    def action_open_connections(self):
        return self._mine().action_sync()

    def _sync(self):
        self.ensure_one()
        started = fields.Datetime.now()
        data = {"since": self.synced_at.isoformat() + "Z"} if self.synced_at else {}
        rows = self._call("connections", data).get("connections")
        if not isinstance(rows, list):
            self._malformed()
        for row in rows:
            try:
                self.env["bizzkickr.connection"]._apply(self, row)
            except (ValueError, TypeError, AttributeError, KeyError):
                _logger.warning("BIZZKICKR returned a malformed connection; it was skipped.")
        self.synced_at = started


class BizzKickRRequest(models.TransientModel):
    """A connection request waiting for the user, as BIZZKICKR listed it a moment ago."""
    _name = "bizzkickr.request"
    _description = "BIZZKICKR request"
    _order = "requested_at desc"

    request_id = fields.Char(required=True, readonly=True)
    name = fields.Char(readonly=True)
    organization = fields.Char(readonly=True)
    email = fields.Char(readonly=True)
    event = fields.Char(readonly=True)
    requested_at = fields.Datetime(readonly=True)

    def _answer(self, verb):
        person = self.env["bizzkickr.person"]._mine()
        for request in self:
            if request.create_uid != self.env.user:
                raise UserError(_("This is another user's request."))
            person._call(f"requests/{request.request_id}/{verb}")
        self.unlink()
        if verb == "accept":
            person._sync()
        return person.action_requests()

    def action_accept(self):
        return self._answer("accept")

    def action_decline(self):
        return self._answer("decline")


class BizzKickRConnection(models.Model):
    """A confirmed BIZZKICKR connection of a user, and the contact it became in this database."""
    _name = "bizzkickr.connection"
    _description = "BIZZKICKR connection"
    _order = "connected_at desc"

    person_id = fields.Many2one("bizzkickr.person", required=True, readonly=True, ondelete="cascade", index=True)
    user_id = fields.Many2one(related="person_id.user_id", store=True)
    connection_id = fields.Char(required=True, readonly=True)
    partner_id = fields.Many2one("res.partner", readonly=True, ondelete="set null", index=True)
    name = fields.Char(readonly=True)
    organization = fields.Char(readonly=True)
    email = fields.Char(readonly=True)
    connected_at = fields.Datetime(readonly=True)
    synced_values = fields.Text(readonly=True, help="What BIZZKICKR last said, so a change made here is not overwritten.")

    _connection_unique = models.Constraint("unique(person_id, connection_id)", "A connection is recorded once per user.")

    @api.model
    def _apply(self, person, row):
        if not isinstance(row.get("id"), str) or not HASH.fullmatch(row["id"]):
            raise ValueError()
        email = row["email"]
        if not isinstance(email, str) or not EMAIL.fullmatch(email):
            raise ValueError()
        name = _text(row.get("name"))
        if not name:
            raise ValueError()
        website = row.get("web")
        if website and (not isinstance(website, str) or not re.fullmatch(r"https?://[^\s<>\"]{1,200}", website)):
            raise ValueError()
        theirs = {"name": name, "company_name": _text(row.get("organization")), "function": _text(row.get("role")),
                  "phone": _text(row.get("phone")), "website": website or False}
        connected = _when(row["connectedAt"]) if row.get("connectedAt") else fields.Datetime.now()
        record = self.sudo().search([("person_id", "=", person.id), ("connection_id", "=", row["id"])], limit=1)
        if not record:
            record = self.sudo().create({"person_id": person.id, "connection_id": row["id"], "email": email.lower()})
        partner = record.partner_id
        if partner:
            # A field follows BIZZKICKR only while nobody here changed it since the last sync (GCNA-BK-CONN-3).
            before = json.loads(record.synced_values or "{}")
            changes = {key: value for key, value in theirs.items()
                       if (partner[key] or False) != value and (partner[key] or False) == before.get(key, False)}
        else:
            # The user's own view of contacts decides the match, so no other company's contact is touched.
            found = self.env["res.partner"].with_user(person.user_id).with_company(person.company_id).search(
                [("email", "=ilike", email)], limit=2)
            if len(found) == 1:
                # A contact kept here already: BIZZKICKR only fills what is empty.
                partner = found.sudo()
                changes = {key: value for key, value in theirs.items() if value and not partner[key]}
            else:
                partner = self.env["res.partner"].sudo().create({"email": email.lower(), "company_id": person.company_id.id, **theirs})
                changes = {}
        if changes:
            partner.sudo().write(changes)
        record.write({"partner_id": partner.id, "name": name, "organization": theirs["company_name"], "connected_at": connected,
                      "synced_values": json.dumps(theirs)})
        return record
