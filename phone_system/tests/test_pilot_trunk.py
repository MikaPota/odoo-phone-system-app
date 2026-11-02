import hashlib
import os
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.phone_system.models.pilot_trunk_wizard import PilotTrunkClient


def reply(payload, status=200):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    return response


@tagged("post_install", "-at_install", "phone_system")
class TestPilotTrunk(TransactionCase):
    def setUp(self):
        super().setUp()
        # The test database also has German; these tests compare English messages.
        self.env = self.env(context=dict(self.env.context, lang="en_US"))
        patcher = patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                                          "VODIA_PROVISIONING_USER": "provision",
                                          "VODIA_PROVISIONING_PASSWORD": "synthetic-admin-secret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = self.env["vodia.pilot.connection"].create({"name": "T"})
        self.values = {"connection_id": self.connection.id, "name": "Provider Main",
                       "registrar": "sip.example.com", "sip_user": "4930123", "sip_password": "s3cret"}

    def _wizard(self, **kw):
        return self.env["vodia.pilot.trunk.wizard"].create(dict(self.values, **kw))

    def _client(self, stored=None, existing=()):
        client = MagicMock()
        client.list_trunks.return_value = list(existing) or [{"id": "12", "status1": "Registered"}]
        client.create_trunk.return_value = "12"
        client.read_trunk.return_value = stored if stored is not None else {
            "reg_registrar": "sip.example.com", "reg_user": "4930123"}
        return client

    def test_an_operators_server_login_still_works(self):
        user, digest = self.connection._pbx_login()
        self.assertEqual((user, len(digest)), ("provision", 32))

    def test_without_any_login_the_customer_is_sent_to_settings(self):
        with patch.dict(os.environ, {"VODIA_PROVISIONING_PASSWORD": ""}):
            with self.assertRaisesRegex(UserError, "settings"):
                self.connection._pbx_client()

    def test_the_settings_login_is_encrypted_and_wins_over_the_server(self):
        self.connection.write({"pbx_admin_user": "owner", "pbx_admin_password": "tenant-password"})
        self.assertFalse(self.connection.pbx_admin_password)
        self.assertNotIn("tenant-password", self.connection.sudo().pbx_admin_ciphertext)
        self.assertEqual(self.connection._pbx_login(), ("owner", hashlib.md5(b"tenant-password").hexdigest()))
        with patch("odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient") as client:
            self.connection._pbx_client()
        self.assertEqual(client.call_args.args[0], "owner")

    def test_a_saved_login_is_never_sent_to_another_host(self):
        self.connection.write({"pbx_admin_user": "owner", "pbx_admin_password": "tenant-password"})
        self.connection.pbx_host = "other.pbx.example"
        self.assertIsNone(self.connection._pbx_login())

    def test_a_password_needs_its_login(self):
        with self.assertRaises(ValidationError):
            self.connection.write({"pbx_admin_user": False, "pbx_admin_password": "tenant-password"})

    def test_a_purchased_system_goes_through_gcna_without_any_login(self):
        self.connection.sudo().gcna_system_id = "system-12345"
        response = MagicMock(status_code=200, json=lambda: {"result": {"trunks": [{"id": 3}]}})
        with patch.dict(os.environ, {"VODIA_PROVISIONING_USER": "", "VODIA_PROVISIONING_PASSWORD": ""}), \
                patch("requests.post", return_value=response) as post, \
                patch("odoo.addons.phone_system.models.gcna_account.GCNAAccount._authorization", return_value="t" * 43):
            trunks = self.connection._pbx_client().list_trunks()
        self.assertEqual(trunks, [{"id": 3}])
        self.assertTrue(post.call_args.args[0].endswith("/v1/phone-systems/system-12345/pbx"))
        self.assertEqual(post.call_args.kwargs["json"], {"method": "GET", "resource": "domain_trunks", "body": None})

    def test_create_verifies_and_wipes_password(self):
        client = self._client()
        with patch("odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient", return_value=client), \
                patch("odoo.addons.phone_system.models.pilot_trunk_wizard.time.sleep"):
            wizard = self._wizard()
            wizard.action_create()
        sent = client.create_trunk.call_args.args[0]
        self.assertEqual((sent["reg_user"], sent["reg_pass"], sent["type"]), ("4930123", "s3cret", "register"))
        client.update_trunk.assert_called_once()
        self.assertEqual(client.update_trunk.call_args.args[1]["reg_registrar"], "sip.example.com")
        self.assertEqual(wizard.sip_password, "*")
        self.assertIn("read back", wizard.result)
        self.assertIn("Registered", wizard.result)

    def test_mismatch_removes_the_trunk_again(self):
        client = self._client(stored={"reg_registrar": "other.example.com", "reg_user": "4930123"})
        with patch("odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient", return_value=client):
            wizard = self._wizard()
            with self.assertRaises(UserError):
                wizard.action_create()
        client.delete_trunk.assert_called_once_with("12")

    def test_duplicate_name_is_refused(self):
        client = self._client(existing=[{"id": "9", "name": "Provider Main"}])
        with patch("odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient", return_value=client):
            with self.assertRaises(UserError):
                self._wizard().action_create()
        client.create_trunk.assert_not_called()

    def test_invalid_input_is_refused_before_any_pbx_call(self):
        for bad in ({"registrar": "bad host!"}, {"registrar": "a.com/../x"}, {"name": "../x"},
                    {"sip_user": "a b"}, {"outbound_proxy": "x y"}, {"sip_password": "a\nb"}):
            with patch("odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient") as client:
                with self.assertRaises(ValidationError, msg=str(bad)):
                    self._wizard(**bad).action_create()
                client.assert_not_called()

    def test_non_administrators_are_refused(self):
        user = self.env["res.users"].create({
            "name": "Plain", "login": "plain-trunk", "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        with self.assertRaises(AccessError):
            self.env["vodia.pilot.trunk.wizard"].with_user(user).create(self.values).action_create()

    def test_client_login_uses_challenge_digest_without_sending_the_digest(self):
        http = MagicMock()
        http.post.side_effect = [reply({"id": "abc12345", "challenge": "c" * 20}), reply({"success": True})]
        PilotTrunkClient("provision", "d" * 32, http=http)
        second = http.post.call_args_list[1].kwargs["json"]
        self.assertNotIn("d" * 32, str(second))
        self.assertEqual(len(second["hash"]), 64)
        http.post.side_effect = [reply({"id": "abc12345", "challenge": "c" * 20}), reply({"success": False})]
        with self.assertRaises(UserError):
            PilotTrunkClient("provision", "d" * 32, http=http)
