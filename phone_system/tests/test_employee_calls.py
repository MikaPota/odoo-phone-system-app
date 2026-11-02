from unittest import SkipTest

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "phone_system")
class TestEmployeeCalls(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "hr.employee" not in cls.env:
            raise SkipTest("The Employees app is not installed.")
        group = cls.env.ref("phone_system.group_pilot")
        company = cls.env.company
        company.country_id = cls.env.ref("base.de")
        connection = cls.env["vodia.pilot.connection"].create({"name": "Test PBX", "company_id": company.id})
        cls.alice, cls.bob = (cls.env["res.users"].create({
            "name": name, "login": f"phone-hr-{ext}", "company_id": company.id,
            "company_ids": [(6, 0, [company.id])], "group_ids": [(6, 0, [group.id])]}) for name, ext in (("Alice", "401"), ("Bob", "402")))
        for user, ext in ((cls.alice, "401"), (cls.bob, "402")):
            cls.env["vodia.pilot.mapping"].create({"user_id": user.id, "connection_id": connection.id, "extension": ext,
                                                   "external_calling": True})
        cls.bob_employee = cls.env["hr.employee"].create({"name": "Bob", "user_id": cls.bob.id})
        cls.outside = cls.env["hr.employee"].create({"name": "Field engineer", "work_phone": "+49 30 3333333"})

    def _record(self, **entry):
        return self.env["vodia.pilot.call"].with_user(self.alice)._record_call(dict(
            {"call_id": "h1", "direction": "outgoing", "outcome": "answered", "duration": 3, "started_at": 1760000000000}, **entry))

    def _calls(self, user, employee):
        Call = self.env["vodia.pilot.call"].with_user(user)
        return Call.search(Call._employee_calls_action(employee.id)["domain"])

    def test_employee_cards_get_the_calls_button_without_a_separate_app(self):
        view = self.env.ref("phone_system.view_employee_form_calls")
        action = self.env.ref("phone_system.employee_calls_action")
        self.assertEqual(view.inherit_id, self.env.ref("hr.view_employee_form"))
        self.assertIn(f'name="{action.id}"', view.arch_db)
        self.assertEqual(action.model_id.model, "hr.employee", "an action on the read-only call model is refused")
        # The button sits on the HR form, so it runs for HR users with their own access to calls.
        officer = self.env["res.users"].create({
            "name": "Officer", "login": "phone-hr-officer", "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
            "group_ids": [(6, 0, [self.env.ref("phone_system.group_pilot").id, self.env.ref("hr.group_hr_user").id])]})
        result = action.with_user(officer).with_context(active_model="hr.employee", active_id=self.bob_employee.id,
                                                           active_ids=self.bob_employee.ids).sudo(False).run()
        self.assertEqual(result["res_model"], "vodia.pilot.call")
        self.assertIn("Calls", self.env["hr.employee"].get_views([(False, "form")])["views"]["form"]["arch"])
        if "de_DE" in dict(self.env["res.lang"].get_installed()):
            german = self.env["hr.employee"].with_context(lang="de_DE").get_views([(False, "form")])["views"]["form"]["arch"]
            self.assertIn("Anrufe", german)
        # Removed with the Employees app's form, the button comes back on the next registry load.
        view.unlink()
        self.env["vodia.pilot.call"]._ensure_employee_calls_button()
        self.assertTrue(self.env.ref("phone_system.view_employee_form_calls").exists())

    def test_a_colleague_call_appears_on_their_employee_card_for_the_caller(self):
        self._record(peer="402")
        self.assertEqual(len(self._calls(self.alice, self.bob_employee)), 1)
        self.assertFalse(self._calls(self.bob, self.bob_employee), "another user's calls stay private")

    def test_a_call_to_an_employees_work_number_appears_on_their_card(self):
        self._record(call_id="h2", peer="+49303333333")
        self.assertEqual(len(self._calls(self.alice, self.outside)), 1)
