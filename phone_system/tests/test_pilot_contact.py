import os
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

CLIENT = "odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient"
ACCOUNT = "odoo.addons.phone_system.models.gcna_account.GCNAAccount"


class FakeTrunks:
    reads = 0

    def __init__(self, *args, **kwargs):
        pass

    def list_trunks(self):
        return [{"id": "9", "status1": "408 Timeout", "disabled": ""}, {"id": "13", "status1": "200 OK", "disabled": ""}]

    def read_trunk(self, trunk_id):
        FakeTrunks.reads += 1
        assert trunk_id == "13"
        return {"ani_regular": "", "reg_account": "00493043972971"}


@tagged("post_install", "-at_install", "phone_system")
class TestPilotContact(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        company.country_id = cls.env.ref("base.de")
        cls.connection = cls.env["vodia.pilot.connection"].create({"name": "Pilot", "company_id": company.id})
        cls.caller = cls.env["res.users"].create({
            "name": "Martin Högl", "login": "vodia-contact-caller", "email": "mh@vodia.com",
            "company_id": company.id, "company_ids": [(6, 0, [company.id])],
            "group_ids": [(6, 0, [cls.env.ref("phone_system.group_pilot").id])]})
        cls.mapping = cls.env["vodia.pilot.mapping"].create({
            "user_id": cls.caller.id, "connection_id": cls.connection.id, "extension": "203", "external_calling": True})

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                                          "VODIA_PROVISIONING_USER": "provision", "VODIA_PROVISIONING_PASSWORD": "synthetic"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mapping.web_password = "shared-individual-password"
        FakeTrunks.reads = 0

    def _config(self):
        return self.env["vodia.pilot.mapping"].with_user(self.caller)._browser_configuration()

    def test_the_phone_shows_the_trunk_number_read_from_the_pbx_once_a_day(self):
        with patch(CLIENT, FakeTrunks):
            self.assertEqual(self._config()["number"], "+49 30 43972971")
            self.assertEqual(self._config()["number"], "+49 30 43972971")
        self.assertEqual(FakeTrunks.reads, 1, "the PBX is asked again only after a day")

    def test_without_provisioning_or_a_registered_trunk_no_number_is_shown(self):
        with patch.dict(os.environ, {"VODIA_PROVISIONING_PASSWORD": ""}):
            self.assertEqual(self._config()["number"], "")

    def _send(self, email, response=None):
        post = MagicMock(return_value=response or MagicMock(status_code=202, json=lambda: {"sent": True}))
        with patch(CLIENT, FakeTrunks), patch(f"{ACCOUNT}._authorization", return_value="t" * 43), \
                patch("requests.post", post):
            result = self.env["vodia.pilot.mapping"].with_user(self.caller)._send_contact_card(email)
        return result, post

    def test_send_contact_info_asks_gcna_to_mail_the_users_own_card(self):
        result, post = self._send(" ada@lovelace.test ")
        self.assertEqual(result, {"sent": True})
        self.assertTrue(post.call_args.args[0].endswith("/v1/contact-cards"))
        self.assertEqual(post.call_args.kwargs["json"], {"to": "ada@lovelace.test", "card": {
            "name": "Martin Högl", "company": self.env.company.name, "email": "mh@vodia.com", "phone": "+493043972971"},
            "lang": self.caller.lang})
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer " + "t" * 43})

    def test_send_contact_info_passes_the_senders_language(self):
        for lang in ("de_DE", "en_US"):
            self.caller.lang = lang
            _result, post = self._send("ada@lovelace.test")
            self.assertEqual(post.call_args.kwargs["json"]["lang"], lang)

    def test_send_contact_info_refuses_a_malformed_address_before_asking_gcna(self):
        for bad in ("", "nope", "a@b", "a@b.test\r\nBcc: x@y.test"):
            with self.assertRaises(UserError, msg=bad):
                self._send(bad)

    def test_the_recipient_is_suggested_from_the_last_call_with_a_known_contact(self):
        customer = self.env["res.partner"].create({"name": "Ada", "phone": "+49 30 555 78749 47", "email": "ada@lovelace.test"})
        self.env["vodia.pilot.call"].with_user(self.caller)._record_call({
            "call_id": "r1", "direction": "outgoing", "peer": "+49305557874947", "outcome": "answered",
            "duration": 5, "started_at": 1760000000000})
        suggestion = self.env["vodia.pilot.mapping"].with_user(self.caller)._contact_card_recipient()
        self.assertEqual(suggestion, {"email": "ada@lovelace.test", "name": customer.name})
