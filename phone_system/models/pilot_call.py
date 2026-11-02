import re
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.addons.phone_validation.tools import phone_validation
from odoo.exceptions import ValidationError

OUTCOMES = [("answered", "Answered"), ("missed", "Missed"), ("failed", "Failed")]


class PilotCall(models.Model):
    _name = "vodia.pilot.call"
    _description = "Phone System Internal Call"
    _order = "started_at desc, id desc"
    _rec_name = "call_uid"

    call_uid = fields.Char(required=True, readonly=True)
    user_id = fields.Many2one("res.users", required=True, readonly=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    direction = fields.Selection([("outgoing", "Outgoing"), ("incoming", "Incoming")],
                                 required=True, readonly=True)
    peer_extension = fields.Char(readonly=True)
    peer_number = fields.Char(readonly=True, help="External number in E.164 format.")
    peer_user_id = fields.Many2one("res.users", readonly=True, ondelete="set null")
    # A colleague's contact, or the one contact the caller may read with the external number.
    peer_partner_id = fields.Many2one("res.partner", compute="_compute_peer_partner_id", store=True,
                                      readonly=False, ondelete="set null", index=True)
    outcome = fields.Selection(OUTCOMES, required=True, readonly=True)
    started_at = fields.Datetime(required=True, readonly=True)
    duration = fields.Integer(readonly=True, help="Talk time in seconds.")

    _owner_uid_unique = models.Constraint(
        "UNIQUE(user_id, call_uid)", "A call is recorded only once per user."
    )

    @api.depends("peer_user_id")
    def _compute_peer_partner_id(self):
        for record in self:
            if record.peer_user_id:
                record.peer_partner_id = record.peer_user_id.partner_id

    @api.model
    def _external_number(self, raw, company):
        """The external number in E.164, or None when it is not a plausible phone number."""
        if not isinstance(raw, str) or not re.fullmatch(r"\+?[0-9 ()./-]{7,30}", raw):
            return None
        digits = re.sub(r"[^0-9+]", "", raw)
        if not re.fullmatch(r"\+?[0-9]{7,15}", digits):
            return None
        country = company.country_id
        number = phone_validation.phone_format(digits, country.code or None, country.phone_code or None,
                                               force_format="E164", raise_exception=False)
        if not re.fullmatch(r"\+[0-9]{7,15}", number or ""):
            number = digits if digits.startswith("+") else None
        return number

    @api.constrains("duration")
    def _check_duration(self):
        if any(record.duration < 0 or record.duration > 86400 for record in self):
            raise ValidationError(_("Invalid call duration."))

    @api.model
    def _record_call(self, entry):
        """Record one finished call for the current user; peer data is validated server-side."""
        mapping = self.env["vodia.pilot.mapping"]._caller_mapping()
        if not isinstance(entry, dict):
            raise ValidationError(_("Invalid call entry."))
        call_uid, direction, outcome = entry.get("call_id"), entry.get("direction"), entry.get("outcome")
        peer, duration = entry.get("peer"), entry.get("duration")
        started = entry.get("started_at")
        if (not isinstance(call_uid, str) or not 1 <= len(call_uid) <= 200
                or direction not in ("outgoing", "incoming")
                or outcome not in dict(OUTCOMES)
                or type(duration) is not int or not 0 <= duration <= 86400
                or type(started) not in (int, float) or started <= 0):
            raise ValidationError(_("Invalid call entry."))
        peer_user = self.env["res.users"]
        peer_partner = self.env["res.partner"]
        extension = number = None
        if isinstance(peer, str) and re.fullmatch(r"[0-9]{2,6}", peer):
            extension = peer
            other = self.env["vodia.pilot.mapping"].sudo().search([
                ("connection_id", "=", mapping.connection_id.id),
                ("extension", "=", peer), ("active", "=", True),
            ], limit=1)
            peer_user = other.user_id
        elif peer is not None:
            number = self._external_number(peer, mapping.company_id)
            if not number or (direction == "outgoing" and not mapping.external_calling):
                raise ValidationError(_("Invalid call entry."))
            # Matched with the caller's own access, so no contact is revealed they could not already read;
            # two contacts with the number stay unmatched.
            matches = self.env["res.partner"].search([("phone_sanitized", "=", number)], limit=2)
            peer_partner = matches if len(matches) == 1 else peer_partner
        existing = self.sudo().search([("user_id", "=", self.env.user.id), ("call_uid", "=", call_uid)], limit=1)
        if existing:
            return existing.id
        return self.sudo().create({
            "call_uid": call_uid, "user_id": self.env.user.id, "company_id": mapping.company_id.id,
            "direction": direction, "peer_extension": extension, "peer_number": number, "peer_user_id": peer_user.id,
            **({"peer_partner_id": peer_partner.id} if peer_partner else {}),
            "outcome": outcome, "duration": duration,
            "started_at": datetime.fromtimestamp(started / 1000, timezone.utc).replace(tzinfo=None),
        }).id
