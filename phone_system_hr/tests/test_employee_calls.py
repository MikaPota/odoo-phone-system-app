from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "phone_system")
class TestEmployeeCalls(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = cls.env.ref("phone_system.group_pilot")
        company = cls.env.company
        company.country_id = cls.env.ref("base.de")
        connection = cls.env["vodia.pilot.connection"].create({"name": "Test PBX", "company_id": company.id})
        cls.alice, cls.bob = (cls.env["res.users"].create({
            "name": name, "login": f"vodia-hr-{ext}", "company_id": company.id,
            "company_ids": [(6, 0, [company.id])], "group_ids": [(6, 0, [group.id])]}) for name, ext in (("Alice", "401"), ("Bob", "402")))
        for user, ext in ((cls.alice, "401"), (cls.bob, "402")):
            cls.env["vodia.pilot.mapping"].create({"user_id": user.id, "connection_id": connection.id, "extension": ext,
                                                   "external_calling": True})
        cls.bob_employee = cls.env["hr.employee"].create({"name": "Bob", "user_id": cls.bob.id})
        cls.outside = cls.env["hr.employee"].create({"name": "Field engineer", "work_phone": "+49 30 3333333"})

    def _record(self, **entry):
        return self.env["vodia.pilot.call"].with_user(self.alice)._record_call(dict(
            {"call_id": "h1", "direction": "outgoing", "outcome": "answered", "duration": 3, "started_at": 1760000000000}, **entry))

    def test_a_colleague_call_appears_on_their_employee_card_for_the_caller(self):
        self._record(peer="402")
        self.assertEqual(self.bob_employee.with_user(self.alice).vodia_call_count, 1)
        action = self.bob_employee.with_user(self.alice).action_vodia_calls()
        self.assertEqual(len(self.env["vodia.pilot.call"].with_user(self.alice).search(action["domain"])), 1)
        self.assertEqual(self.bob_employee.with_user(self.bob).vodia_call_count, 0, "another user's calls stay private")

    def test_a_call_to_an_employees_work_number_appears_on_their_card(self):
        self._record(call_id="h2", peer="+49303333333")
        self.assertEqual(self.outside.with_user(self.alice).vodia_call_count, 1)
