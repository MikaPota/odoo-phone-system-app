from markupsafe import Markup

from odoo import _, api, models
from odoo.exceptions import UserError

EMPLOYEE_VIEW = "view_employee_form_calls"


class PilotCallEmployees(models.Model):
    """A "Calls" button on employee cards, without making Phone System depend on the Employees app.

    When the Employees app is installed (now or later), the button's view is added on the next registry
    load and owned by Phone System, so uninstalling either app removes it again."""

    _inherit = "vodia.pilot.call"

    def _register_hook(self):
        super()._register_hook()
        self._ensure_employee_calls_button()

    @api.model
    def _ensure_employee_calls_button(self):
        parent = self.env.ref("hr.view_employee_form", raise_if_not_found=False)
        action = self.env.ref("phone_system.action_employee_calls", raise_if_not_found=False)
        if not parent or not action:
            return
        Data = self.env["ir.model.data"].sudo()
        xmlid = Data.search([("module", "=", "phone_system"), ("name", "=", EMPLOYEE_VIEW)], limit=1)
        if xmlid and self.env["ir.ui.view"].browse(xmlid.res_id).exists():
            return
        arch = Markup(
            '<xpath expr="//div[@name=\'button_box\']" position="inside">'
            '<button name="%s" type="action" class="oe_stat_button" icon="fa-phone" '
            'groups="phone_system.group_pilot,base.group_system">'
            '<div class="o_stat_info"><span class="o_stat_text">Calls</span></div>'
            '</button></xpath>'
        ) % action.id
        view = self.env["ir.ui.view"].sudo().create({
            "name": "hr.employee.form.phone.system.calls", "model": "hr.employee", "inherit_id": parent.id, "arch": arch,
        })
        values = {"module": "phone_system", "name": EMPLOYEE_VIEW, "model": "ir.ui.view", "res_id": view.id, "noupdate": True}
        xmlid.write(values) if xmlid else Data.create(values)
        # Data files translate their own views; this one takes the "Calls" of each installed language.
        langs = [code for code, _name in self.env["res.lang"].get_installed() if code != "en_US"]
        view.update_field_translations("arch_db", {
            code: {"Calls": self.with_context(lang=code).env._("Calls")} for code in langs
        })

    @api.model
    def _employee_calls_action(self, employee_id):
        if "hr.employee" not in self.env:
            raise UserError(_("The Employees app is not installed."))
        # Only the employee's user and work contact are read; the calls themselves keep the user's own
        # record rules, so nobody sees a call they could not see in Call History.
        employee = self.env["hr.employee"].sudo().browse(employee_id).exists() if isinstance(employee_id, int) else None
        if not employee:
            raise UserError(_("Choose an employee."))
        # A colleague is the peer by their user; an outside number by the employee's work contact.
        terms = [("peer_partner_id", "=", employee.work_contact_id.id)] if employee.work_contact_id else []
        if employee.user_id:
            terms.append(("peer_user_id", "=", employee.user_id.id))
        domain = ["|"] * (len(terms) - 1) + terms if terms else [("id", "=", 0)]
        return {
            "type": "ir.actions.act_window", "name": _("Calls"), "res_model": "vodia.pilot.call",
            "view_mode": "list", "domain": domain, "context": {"create": False},
        }
