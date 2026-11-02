from markupsafe import Markup

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools.translate import code_translations

EMPLOYEE_VIEW = "view_employee_form_calls"
# Not "action_employee_calls": 19.0.0.33.0 shipped that name as a data record on the call model, which an
# upgrade now removes.
EMPLOYEE_ACTION = "employee_calls_action"


class PilotCallEmployees(models.Model):
    """A "Calls" button on employee cards, without making Phone System depend on the Employees app.

    When the Employees app is installed (now or later), the button and its action are added on the next
    registry load and owned by Phone System, so uninstalling either app removes them again. The action runs
    on the employee, because Odoo runs a server action only for users who may edit its model, and call
    records are read-only."""

    _inherit = "vodia.pilot.call"

    def _register_hook(self):
        super()._register_hook()
        self._ensure_employee_calls_button()

    def _owned(self, name, model, values):
        """The record Phone System owns under `name`, created again if it is gone."""
        Data = self.env["ir.model.data"].sudo()
        xmlid = Data.search([("module", "=", "phone_system"), ("name", "=", name)], limit=1)
        record = self.env[model].sudo().browse(xmlid.res_id).exists() if xmlid else None
        if record:
            return record
        record = self.env[model].sudo().create(values)
        data = {"module": "phone_system", "name": name, "model": model, "res_id": record.id, "noupdate": True}
        xmlid.write(data) if xmlid else Data.create(data)
        return record

    @api.model
    def _ensure_employee_calls_button(self):
        parent = self.env.ref("hr.view_employee_form", raise_if_not_found=False)
        if not parent:
            return
        action = self._owned(EMPLOYEE_ACTION, "ir.actions.server", {
            "name": "Calls", "model_id": self.env["ir.model"]._get_id("hr.employee"), "state": "code",
            "code": "action = env['vodia.pilot.call']._employee_calls_action(record.id)",
        })
        arch = Markup(
            '<xpath expr="//div[@name=\'button_box\']" position="inside">'
            '<button name="%s" type="action" class="oe_stat_button" icon="fa-phone" '
            'groups="phone_system.group_pilot,base.group_system">'
            '<div class="o_stat_info"><span class="o_stat_text">Calls</span></div>'
            '</button></xpath>'
        ) % action.id
        view = self._owned(EMPLOYEE_VIEW, "ir.ui.view", {
            "name": "hr.employee.form.phone.system.calls", "model": "hr.employee", "inherit_id": parent.id, "arch": arch,
        })
        if f'name="{action.id}"' not in view.arch_db:
            view.arch = arch  # the button pointed at an action that was replaced
        # Data files translate their own records; these take the "Calls" of each language installed so far,
        # including languages installed after the button was added.
        # A term that was not available yet when the button was added (during installation) is filled in later.
        self.env.cr.execute("SELECT arch_db FROM ir_ui_view WHERE id = %s", [view.id])
        translated = self.env.cr.fetchone()[0] or {}
        terms = {}
        for code, _name in self.env["res.lang"].get_installed():
            term = code_translations.get_python_translations("phone_system", code).get("Calls", "Calls")
            if term != "Calls" and f">{term}<" not in (translated.get(code) or ""):
                terms[code] = term
        if terms:
            # Odoo keys a view's terms by their whole inline markup, e.g. <span class="o_stat_text">Calls</span>.
            sources = {entry["source"] for entry in view.get_field_translations("arch_db")[0] if ">Calls<" in entry["source"]}
            view.update_field_translations("arch_db", {
                code: {source: source.replace(">Calls<", f">{term}<") for source in sources} for code, term in terms.items()
            })
            action.update_field_translations("name", terms)

    @api.model
    def _employee_calls_action(self, employee_id):
        if "hr.employee" not in self.env:
            raise UserError(_("The Employees app is not installed."))
        # Only the employee's user and work contact are read; the calls themselves keep the user's own
        # record rules, so nobody sees a call they could not see in Call History.
        employee = self.env["hr.employee"].sudo().browse(employee_id).exists() if isinstance(employee_id, int) else None
        if not employee:
            raise UserError(_("Choose an employee."))
        # A colleague is the peer by their user; an outside number by the employee's work contact or their own
        # work and mobile numbers (Odoo 18 does not copy the work phone to the work contact).
        terms = [("peer_partner_id", "=", employee.work_contact_id.id)] if employee.work_contact_id else []
        if employee.user_id:
            terms.append(("peer_user_id", "=", employee.user_id.id))
        company = employee.company_id or self.env.company
        numbers = {self._external_number(raw, company) for raw in (employee.work_phone, employee.mobile_phone)} - {None}
        if numbers:
            terms.append(("peer_number", "in", sorted(numbers)))
        domain = ["|"] * (len(terms) - 1) + terms if terms else [("id", "=", 0)]
        return {
            "type": "ir.actions.act_window", "name": _("Calls"), "res_model": "vodia.pilot.call",
            "view_mode": "list", "domain": domain, "context": {"create": False},
        }
