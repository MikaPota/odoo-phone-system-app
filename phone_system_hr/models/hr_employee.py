from odoo import _, api, fields, models


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    vodia_call_count = fields.Integer(compute="_compute_vodia_call_count", string="Calls")

    def _vodia_call_domain(self):
        # A colleague is the peer by their user; an outside number by the employee's work contact.
        self.ensure_one()
        terms = [("peer_partner_id", "=", self.work_contact_id.id)] if self.work_contact_id else []
        if self.user_id:
            terms.append(("peer_user_id", "=", self.user_id.id))
        return ["|"] * (len(terms) - 1) + terms if terms else [("id", "=", 0)]

    @api.depends_context("uid")
    def _compute_vodia_call_count(self):
        Call = self.env["vodia.pilot.call"]
        allowed = Call.has_access("read")
        for employee in self:
            employee.vodia_call_count = Call.search_count(employee._vodia_call_domain()) if allowed else 0

    def action_vodia_calls(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Calls"), "res_model": "vodia.pilot.call",
            "view_mode": "list", "domain": self._vodia_call_domain(), "context": {"create": False},
        }
