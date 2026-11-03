import hashlib
import os
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.addons.phone_system.controllers.pilot_session import PilotSessionController


@tagged("post_install", "-at_install", "phone_system")
class TestPilotMapping(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.group = cls.env.ref("phone_system.group_pilot")
        cls.caller = cls.env["res.users"].create({
            "name": "Pilot caller test",
            "login": "vodia-pilot-test-caller",
            "company_id": cls.env.company.id,
            "company_ids": [(6, 0, [cls.env.company.id])],
            "groups_id": [(6, 0, [cls.group.id])],
        })
        cls.denied = cls.env["res.users"].create({
            "name": "Pilot denied test",
            "login": "vodia-pilot-test-denied",
            "company_id": cls.env.company.id,
            "company_ids": [(6, 0, [cls.env.company.id])],
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
        })
        cls.connection = cls.env["vodia.pilot.connection"].create({
            "name": "Test PBX", "company_id": cls.env.company.id,
        })
        cls.mapping = cls.env["vodia.pilot.mapping"].create({
            "user_id": cls.caller.id, "connection_id": cls.connection.id, "extension": "201",
        })
        cls.key = Fernet.generate_key().decode()
        cls.password = "synthetic-individual-password"

    def setUp(self):
        super().setUp()
        self.key_patch = patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": self.key})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.mapping.web_password = self.password

    def test_configuration_returns_only_caller_scope(self):
        payload = self.env["vodia.pilot.mapping"].with_user(self.caller)._browser_configuration()
        # Browsers reach a tenant on GCNA's PBX through GCNA's relay, which keeps the sign-in working in Safari.
        self.assertEqual(payload, {"origin": "https://gcn-alliance.com/phone/odoo-pilot.pbx.gcn-alliance.com/",
                                   "tenant": "odoo-pilot.pbx.gcn-alliance.com", "extension": "201",
                                   "directory": [{"extension": "201", "name": self.caller.name}],
                                   "external_calling": False, "number": ""})

    def test_phone_system_launcher_is_visible_only_to_pilot_users(self):
        menu = self.env.ref("phone_system.menu_pilot")
        action = self.env.ref("phone_system.action_phone_system")
        self.assertEqual(menu.name, "Phone System")
        self.assertEqual(menu.action, action)
        self.assertEqual(action.tag, "phone_system.open")
        self.assertIn(menu.id, self.env["ir.ui.menu"].with_user(self.caller)._visible_menu_ids())
        self.assertNotIn(menu.id, self.env["ir.ui.menu"].with_user(self.denied)._visible_menu_ids())

    def test_denied_user_cannot_mint_or_read_credentials(self):
        with self.assertRaises(AccessError):
            self.env["vodia.pilot.mapping"].with_user(self.denied)._mint_browser_session()
        with self.assertRaises(AccessError):
            self.mapping.with_user(self.caller).read(["credential_ciphertext"])
        with self.assertRaises(AccessError):
            self.mapping.with_user(self.caller).read(["web_password"])
        with self.assertRaises(AccessError):
            self.mapping.with_user(self.caller).read(["user_id", "extension"])

    def test_unmapped_or_inactive_connection_never_requests_challenge(self):
        self.connection.active = False
        with patch("odoo.addons.phone_system.models.pilot_configuration.requests.post") as post:
            with self.assertRaises(UserError):
                self.env["vodia.pilot.mapping"].with_user(self.caller)._mint_browser_session()
            post.assert_not_called()

    def test_challenge_proof_is_scoped_and_never_returns_password_digest(self):
        result = {"id": "synthetic-challenge-id", "challenge": "synthetic-random-challenge", "timeout": 60000}
        response = Mock(status_code=200)
        response.json.return_value = result
        with patch("odoo.addons.phone_system.models.pilot_configuration.requests.post",
                   return_value=response) as post:
            payload = self.env["vodia.pilot.mapping"].with_user(self.caller)._mint_browser_session()
            digest = hashlib.md5(self.password.encode()).hexdigest()
            expected = hashlib.sha256(("201:" + digest + ":" + result["challenge"]).encode()).hexdigest()
            self.assertEqual(payload["extension"], "201")
            self.assertEqual(payload["login"], {"method": "login-password", "username": "201",
                                               "id": result["id"], "hash": expected})
            self.assertNotIn(digest, str(payload))
            self.assertNotIn(self.password, str(payload))
            self.assertEqual(post.call_args.kwargs["json"], {"method": "get-password", "username": "201"})
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            response.json.return_value = dict(result, timeout=0)
            with self.assertRaises(UserError):
                self.env["vodia.pilot.mapping"].with_user(self.caller)._mint_browser_session()

    def test_write_only_encrypted_credential_rotation_and_clear(self):
        first = self.mapping.credential_ciphertext
        self.assertTrue(self.mapping.credential_configured)
        self.assertFalse(self.mapping.web_password)
        self.assertNotIn(self.password, first)
        self.mapping.web_password = "replacement-individual-password"
        self.assertNotEqual(self.mapping.credential_ciphertext, first)
        self.mapping.action_clear_credential()
        self.assertFalse(self.mapping.credential_configured)
        with self.assertRaises(UserError):
            self.mapping._password_digest()
        with self.assertRaises(AccessError):
            self.mapping.with_user(self.caller).action_clear_credential()

    def test_mapping_changes_and_wrong_key_fail_closed(self):
        self.mapping.extension = "202"
        with self.assertRaises(UserError):
            self.mapping._password_digest()
        self.mapping.extension = "201"
        with patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode()}):
            with self.assertRaises(UserError):
                self.mapping._password_digest()

    def test_invalid_key_and_invalid_extension_are_explicit_errors(self):
        with patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": "invalid"}):
            with self.assertRaises(UserError):
                self.mapping._password_digest()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.mapping.extension = "201@other"

    def test_inactive_and_uncredentialed_targets_are_excluded(self):
        self.mapping.web_password = False
        self.assertEqual(self.env["vodia.pilot.mapping"].with_user(self.caller)._browser_configuration()["directory"], [])

    def test_browser_routes_require_an_authorized_selected_company(self):
        controller = PilotSessionController()
        fake_request = Mock()
        fake_request.env = self.env(user=self.caller.id)
        fake_request.future_response.headers = {}
        with patch("odoo.addons.phone_system.controllers.pilot_session.request", fake_request):
            for value in [None, True, "1", -1]:
                with self.assertRaises(AccessError):
                    controller.configuration(company_id=value)
            payload = controller.configuration(company_id=self.env.company.id)
            self.assertEqual(payload["extension"], "201")
            self.assertEqual(fake_request.future_response.headers["Cache-Control"], "no-store")
            denied_company = self.env["res.company"].create({"name": "Other test company"})
            with self.assertRaises(AccessError):
                controller.session(company_id=denied_company.id)

    def _tenant(self, host, user_name, login):
        user = self.env["res.users"].create({
            "name": user_name, "login": login, "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])], "groups_id": [(6, 0, [self.group.id])],
        })
        connection = self.env["vodia.pilot.connection"].create({
            "name": host, "company_id": self.env.company.id, "pbx_host": host,
        })
        mapping = self.env["vodia.pilot.mapping"].create({
            "user_id": user.id, "connection_id": connection.id, "extension": "201",
        })
        mapping.web_password = "synthetic-other-tenant-password"
        return user, connection

    def test_each_connection_uses_its_own_tenant(self):
        self.assertEqual(self.connection.pbx_host, "odoo-pilot.pbx.gcn-alliance.com")
        user, connection = self._tenant("sd5tz7.pbx.gcn-alliance.com", "Second tenant caller", "vodia-second-tenant")
        payload = self.env["vodia.pilot.mapping"].with_user(user)._browser_configuration()
        self.assertEqual(payload["origin"], "https://gcn-alliance.com/phone/sd5tz7.pbx.gcn-alliance.com/")
        self.assertEqual(payload["tenant"], "sd5tz7.pbx.gcn-alliance.com")
        result = {"id": "synthetic-challenge-id", "challenge": "synthetic-random-challenge", "timeout": 60000}
        response = Mock(status_code=200)
        response.json.return_value = result
        with patch("odoo.addons.phone_system.models.pilot_configuration.requests.post",
                   return_value=response) as post:
            session = self.env["vodia.pilot.mapping"].with_user(user)._mint_browser_session()
        # Odoo asks the tenant for the challenge directly; only the browser goes through the relay.
        self.assertEqual(post.call_args.args[0], "https://sd5tz7.pbx.gcn-alliance.com/rest/system/login")
        self.assertEqual(session["tenant"], "sd5tz7.pbx.gcn-alliance.com")
        self.assertEqual(session["origin"], "https://gcn-alliance.com/phone/sd5tz7.pbx.gcn-alliance.com/")

    def test_tenants_share_extension_numbers_without_sharing_colleagues(self):
        user, _connection = self._tenant("sd5tz7.pbx.gcn-alliance.com", "Second tenant caller", "vodia-second-tenant")
        mine = self.env["vodia.pilot.mapping"].with_user(self.caller)._browser_configuration()
        theirs = self.env["vodia.pilot.mapping"].with_user(user)._browser_configuration()
        self.assertEqual(mine["directory"], [{"extension": "201", "name": self.caller.name}])
        self.assertEqual(theirs["directory"], [{"extension": "201", "name": user.name}])

    def test_pbx_host_must_be_a_plain_hostname(self):
        for host in ("https://evil.example", "a/b", "-bad.example", "", "localhost"):
            with self.assertRaises(ValidationError):
                self.env["vodia.pilot.connection"].create({"name": "Bad", "company_id": self.env.company.id, "pbx_host": host})
