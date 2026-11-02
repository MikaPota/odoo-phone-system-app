from unittest.mock import Mock, patch

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import AccessError
from odoo.addons.phone_system.controllers.pilot_session import PilotSessionController


@tagged("post_install", "-at_install", "phone_system")
class TestPilotDashboard(TransactionCase):
    def setUp(self):
        super().setUp()
        self.manager = self.env["res.users"].create({
            "name": "Phone System manager",
            "login": "vodia-dashboard-manager",
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
            "groups_id": [(6, 0, [self.env.ref("base.group_system").id])],
        })
        self.caller = self.env["res.users"].create({
            "name": "Phone System caller",
            "login": "vodia-dashboard-caller",
            "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
            "groups_id": [(6, 0, [self.env.ref("phone_system.group_pilot").id])],
        })

    def test_dashboard_shows_only_explicitly_unavailable_setup_options_to_admins(self):
        controller = PilotSessionController()
        fake_request = Mock()
        fake_request.env = self.env(user=self.manager.id)
        fake_request.future_response.headers = {}
        with patch("odoo.addons.phone_system.controllers.pilot_session.request", fake_request):
            payload = controller.dashboard()

        self.assertEqual(payload, {
            "can_manage": True,
            "managed_setup": {"available": True, "message": ""},
            "existing_setup": {"available": True, "message": ""},
        })
        self.assertEqual(fake_request.future_response.headers["Cache-Control"], "no-store")

    def test_callers_do_not_receive_management_setup_details(self):
        controller = PilotSessionController()
        fake_request = Mock()
        fake_request.env = self.env(user=self.caller.id)
        fake_request.future_response.headers = {}
        with patch("odoo.addons.phone_system.controllers.pilot_session.request", fake_request):
            with self.assertRaises(AccessError):
                controller.dashboard()

    def test_phone_system_menu_is_available_to_managers_without_a_phone_mapping(self):
        menu = self.env.ref("phone_system.menu_pilot")
        visible_ids = self.env["ir.ui.menu"].with_user(self.manager)._visible_menu_ids()
        self.assertIn(menu.id, visible_ids)
