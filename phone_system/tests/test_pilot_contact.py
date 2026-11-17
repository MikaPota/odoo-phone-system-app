import os
from unittest.mock import patch

from cryptography.fernet import Fernet
from odoo.tests import TransactionCase, tagged

CLIENT = "odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient"


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
            "groups_id": [(6, 0, [cls.env.ref("phone_system.group_pilot").id])]})
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
