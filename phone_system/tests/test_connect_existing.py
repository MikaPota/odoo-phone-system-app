import hashlib
import os
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

WIZARD = "odoo.addons.phone_system.models.pilot_connect_wizard"
CONFIG = "odoo.addons.phone_system.models.pilot_configuration"


def reply(payload, status=200):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    return response


@tagged("post_install", "-at_install", "phone_system")
class TestConnectExisting(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The test database also has German; these tests compare English messages.
        cls.env = cls.env(context=dict(cls.env.context, lang="en_US"))
        company = cls.env.company
        cls.ada = cls.env["res.users"].create({
            "name": "Ada Lovelace", "login": "vodia-connect-ada",
            "company_id": company.id, "company_ids": [(6, 0, [company.id])],
        })
        cls.bob = cls.env["res.users"].create({
            "name": "Bob Builder", "login": "vodia-connect-bob",
            "company_id": company.id, "company_ids": [(6, 0, [company.id])],
        })

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wizard(self, **values):
        return self.env["vodia.pilot.connect.wizard"].create(dict({
            "name": "Head office", "pbx_host": "pbx.customer.example", "user_ids": [(6, 0, (self.ada | self.bob).ids)],
        }, **values))

    def _connect(self, **values):
        with patch(f"{WIZARD}.requests.get", return_value=reply({})) as get:
            wizard = self._wizard(**values)
            wizard.action_connect()
        return wizard, get

    def test_connect_checks_the_address_and_gives_the_chosen_users_a_phone_without_extension(self):
        wizard, get = self._connect()
        self.assertEqual(get.call_args.args[0], "https://pbx.customer.example/")
        connection = self.env["vodia.pilot.connection"].search([("pbx_host", "=", "pbx.customer.example")])
        self.assertEqual((connection.name, connection.company_id), ("Head office", self.env.company))
        mappings = self.env["vodia.pilot.mapping"].search([("connection_id", "=", connection.id)])
        self.assertEqual(set(mappings.mapped("user_id").ids), {self.ada.id, self.bob.id})
        self.assertEqual(set(mappings.mapped("extension")), {False})
        self.assertIn(self.env.ref("phone_system.group_pilot"), self.ada.group_ids)
        self.assertEqual(wizard.state, "done")

    def test_connection_counts_its_users_and_those_signed_in(self):
        self._connect()
        connection = self.env["vodia.pilot.connection"].search([("pbx_host", "=", "pbx.customer.example")])
        mapping = connection.env["vodia.pilot.mapping"].search([("user_id", "=", self.ada.id)])
        mapping.extension = "201"
        mapping.web_password = "shared-individual-password"
        self.assertEqual((connection.user_count, connection.linked_count), (2, 1))

    def test_connection_tells_whether_the_current_user_has_a_phone_on_it(self):
        self._connect(user_ids=[(6, 0, self.ada.ids)])
        connection = self.env["vodia.pilot.connection"].search([("pbx_host", "=", "pbx.customer.example")])
        self.assertTrue(connection.with_user(self.ada).has_own_phone)
        self.assertFalse(connection.with_user(self.bob).has_own_phone)

    def test_own_phone_counts_only_in_the_active_company_because_the_phone_follows_it(self):
        self._connect(user_ids=[(6, 0, self.ada.ids)])
        connection = self.env["vodia.pilot.connection"].search([("pbx_host", "=", "pbx.customer.example")])
        other = self.env["res.company"].create({"name": "Vodia other company"})
        self.ada.company_ids = [(4, other.id)]
        self.assertTrue(connection.with_user(self.ada).with_company(self.env.company).has_own_phone)
        self.assertFalse(connection.with_user(self.ada).with_company(other).has_own_phone)

    def test_connect_moves_a_phone_from_another_system_and_drops_its_extension_and_login(self):
        old = self.env["vodia.pilot.connection"].create({"name": "Old"})
        mapping = self.env["vodia.pilot.mapping"].create({"user_id": self.ada.id, "connection_id": old.id, "extension": "299"})
        mapping.web_password = "old-individual-password"
        self._connect()
        self.assertEqual((mapping.connection_id.pbx_host, mapping.extension, mapping.credential_configured),
                         ("pbx.customer.example", False, False))

    def test_reconnecting_keeps_existing_phones_on_the_same_system(self):
        self._connect()
        mapping = self.env["vodia.pilot.mapping"].search([("user_id", "=", self.ada.id)])
        mapping.extension = "201"
        mapping.web_password = "shared-individual-password"
        self._connect()
        self.assertEqual((mapping.extension, mapping.credential_configured), ("201", True))
        self.assertEqual(self.env["vodia.pilot.connection"].search_count([("pbx_host", "=", "pbx.customer.example")]), 1)

    def test_unreachable_or_bad_address_is_reported_plainly(self):
        import requests
        with patch(f"{WIZARD}.requests.get", side_effect=requests.ConnectionError("no such host")):
            with self.assertRaisesRegex(UserError, "pbx.customer.example"):
                self._wizard().action_connect()
        with patch(f"{WIZARD}.requests.get") as get:
            for host in ("https://pbx.customer.example", "pbx", "a b.example"):
                with self.assertRaises(ValidationError):
                    self._wizard(pbx_host=host).action_connect()
        get.assert_not_called()

    def test_connecting_without_users_is_refused(self):
        with patch(f"{WIZARD}.requests.get") as get:
            with self.assertRaises(ValidationError):
                self._wizard(user_ids=[(5, 0, 0)]).action_connect()
        get.assert_not_called()

    def test_only_administrators_may_connect(self):
        with self.assertRaises(AccessError):
            self._wizard().with_user(self.ada).action_connect()

    def _mapped_ada(self):
        connection = self.env["vodia.pilot.connection"].create({"name": "Existing", "pbx_host": "pbx.customer.example"})
        self.ada.group_ids = [(4, self.env.ref("phone_system.group_pilot").id)]
        return self.env["vodia.pilot.mapping"].create({"user_id": self.ada.id, "connection_id": connection.id})

    def test_configuration_asks_for_consent_until_the_user_shared_their_login(self):
        self._mapped_ada()
        payload = self.env["vodia.pilot.mapping"].with_user(self.ada)._browser_configuration()
        self.assertEqual(payload, {"origin": "https://pbx.customer.example", "tenant": "pbx.customer.example",
                                   "extension": "", "credential_required": True, "directory": []})

    def test_consent_verifies_the_password_with_the_pbx_and_stores_it(self):
        mapping = self._mapped_ada()
        password = "short1"
        with patch(f"{CONFIG}.requests.post", side_effect=[reply({"id": "c1", "challenge": "xyz"}), reply({"success": True})]) as post:
            self.env["vodia.pilot.mapping"].with_user(self.ada)._consent_credential("201", password, True)
        digest = hashlib.md5(password.encode()).hexdigest()
        proof = hashlib.sha256(f"201:{digest}:xyz".encode()).hexdigest()
        self.assertEqual(post.call_args_list[1].kwargs["json"],
                         {"method": "login-password", "username": "201", "id": "c1", "hash": proof})
        self.assertEqual(mapping.extension, "201")
        self.assertEqual(mapping.sudo()._password_digest(), digest)

    def test_consent_refuses_a_wrong_password_or_missing_agreement(self):
        mapping = self._mapped_ada()
        with patch(f"{CONFIG}.requests.post", side_effect=[reply({"id": "c1", "challenge": "xyz"}), reply({"success": False})]):
            with self.assertRaises(UserError):
                self.env["vodia.pilot.mapping"].with_user(self.ada)._consent_credential("201", "wrong", True)
        with patch(f"{CONFIG}.requests.post") as post:
            with self.assertRaises(UserError):
                self.env["vodia.pilot.mapping"].with_user(self.ada)._consent_credential("201", "right", False)
            with self.assertRaises(UserError):
                self.env["vodia.pilot.mapping"].with_user(self.ada)._consent_credential("2x1", "right", True)
        post.assert_not_called()
        self.assertFalse(mapping.credential_configured)
        self.assertFalse(mapping.extension)

    def test_consent_refuses_an_extension_linked_to_another_user(self):
        self._mapped_ada()
        connection = self.env["vodia.pilot.connection"].search([("pbx_host", "=", "pbx.customer.example")])
        self.env["vodia.pilot.mapping"].create({"user_id": self.bob.id, "connection_id": connection.id, "extension": "201"})
        with patch(f"{CONFIG}.requests.post") as post:
            with self.assertRaisesRegex(UserError, "another Odoo user"):
                self.env["vodia.pilot.mapping"].with_user(self.ada)._consent_credential("201", "right", True)
        post.assert_not_called()
