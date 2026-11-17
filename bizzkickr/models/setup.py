from odoo import _, api, fields, models
from odoo.exceptions import UserError


class BizzKickRSetup(models.TransientModel):
    """An administrator connects this database to GCN-ALLIANCE, which resells BIZZKICKR: a free account."""
    _name = "bizzkickr.setup"
    _description = "BIZZKICKR setup"

    state = fields.Selection([("form", "Sign up"), ("code", "Enter the code"), ("linked", "Connected")], default="form", required=True)
    email = fields.Char("Your email address")
    admin_name = fields.Char("Your name")
    company_name = fields.Char("Company")
    domain = fields.Char("Company domain", help="e.g. example.com")
    accept_terms = fields.Boolean("I accept the terms and the privacy notice")
    terms_url = fields.Char(readonly=True)
    terms_version = fields.Char(readonly=True)
    privacy_version = fields.Char(readonly=True)
    operator = fields.Char(readonly=True)
    code = fields.Char("Verification code")

    def _account(self):
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only an administrator may connect this database to GCN-ALLIANCE."))
        return self.env["vodia.gcna.account"].sudo()

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        account = self._account()
        if account._linked():
            return {**values, "state": "linked"}
        user, company = self.env.user, self.env.company
        website = (company.website or "").removeprefix("https://").removeprefix("http://").split("/")[0]
        values.update({"email": user.email, "admin_name": user.name, "company_name": company.name,
                       "domain": website.removeprefix("www.") or (user.email or "").rpartition("@")[2]})
        try:
            legal = account._legal()
        except UserError:
            legal = None
        if legal:
            values.update({"terms_url": legal["terms"]["url"], "terms_version": legal["terms"]["version"],
                           "privacy_version": legal["privacy"]["version"], "operator": legal["imprint"]["entity"]})
        return values

    def _again(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form", "target": "new"}

    def action_send_code(self):
        self.ensure_one()
        if not self.terms_version:
            raise UserError(_("GCN-ALLIANCE has not published its terms yet. Retry later."))
        self._account()._signup_start({
            "email": self.email, "admin_name": self.admin_name, "company_name": self.company_name, "domain": self.domain,
            "accept_terms": self.accept_terms, "terms_version": self.terms_version, "privacy_version": self.privacy_version,
        })
        self.state = "code"
        return self._again()

    def action_resend(self):
        self.ensure_one()
        self._account()._signup_resend()
        return self._again()

    def action_confirm(self):
        self.ensure_one()
        self._account()._signup_confirm((self.code or "").strip())
        self.state = "linked"
        return self._again()
