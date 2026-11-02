import os
from unittest.mock import MagicMock, patch

import requests
from cryptography.fernet import Fernet
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

CLIENT = "odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient"
SPELLINGS = ["+493043972971", "493043972971", "00493043972971", "03043972971"]


class FakePbx:
    """The tenant's accounts and their names, as the Vodia REST API keeps them."""

    def __init__(self, **aliases):
        self.aliases = {extension: list(names) for extension, names in aliases.items()}
        self.writes = []
        self.trunks = [{"id": "13", "name": "easybell", "dial_extension": ""}]

    def list_trunks(self):
        return [{"id": trunk["id"]} for trunk in self.trunks]

    def read_trunk(self, trunk_id):
        return next(trunk for trunk in self.trunks if trunk["id"] == trunk_id)

    def list_accounts(self):
        return [{"account": extension, "alias": list(names)} for extension, names in self.aliases.items()]

    def read_aliases(self, extension):
        return list(self.aliases[extension])

    def write_aliases(self, extension, aliases):
        self.writes.append((extension, list(aliases)))
        self.aliases[extension] = list(aliases)
        return list(aliases)


@tagged("post_install", "-at_install", "phone_system")
class TestPilotNumber(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The test database also has German; these tests compare English messages.
        cls.env = cls.env(context=dict(cls.env.context, lang="en_US"))
        cls.env.company.country_id = cls.env.ref("base.de")
        cls.connection = cls.env["vodia.pilot.connection"].create({"name": "Berlin"})
        users = [cls.env["res.users"].create({"name": name, "login": f"vodia-number-{name.lower()}"})
                 for name in ("Ada", "Bob")]
        cls.ada, cls.bob = (cls.env["vodia.pilot.mapping"].create(
            {"user_id": user.id, "connection_id": cls.connection.id, "extension": extension})
            for user, extension in zip(users, ("201", "202")))

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                                          "VODIA_PROVISIONING_USER": "provision",
                                          "VODIA_PROVISIONING_PASSWORD": "synthetic-admin-secret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pbx = FakePbx(**{"201": ["201", "ada"], "202": ["202"], "203": ["203"]})
        patcher = patch(CLIENT, return_value=self.pbx)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _unlinked(self):
        user = self.env["res.users"].create({"name": "Cy", "login": "vodia-number-cy"})
        return self.env["vodia.pilot.mapping"].create({"user_id": user.id, "connection_id": self.connection.id})

    def _number(self, mapping=None, number="+49 30 43972971"):
        return self.env["vodia.pilot.number"].create(
            {"connection_id": self.connection.id, "number": number, "mapping_id": (mapping or self.ada).id})

    def test_a_number_rings_its_extension_in_every_spelling_a_provider_may_send(self):
        number = self._number()
        self.assertEqual(number.number, "+493043972971")
        self.assertEqual(self.pbx.aliases["201"], ["201", "ada"] + SPELLINGS)
        self.assertEqual(number.state, "active")
        self.assertIn("ring extension 201", number.message)

    def test_removing_a_number_removes_only_the_names_odoo_added(self):
        self.pbx.aliases["201"].append("03043972971")
        number = self._number()
        number.unlink()
        self.assertEqual(self.pbx.aliases["201"], ["201", "ada", "03043972971"])

    def test_another_user_takes_the_number_over(self):
        number = self._number()
        number.mapping_id = self.bob
        self.assertEqual(self.pbx.aliases["201"], ["201", "ada"])
        self.assertEqual(self.pbx.aliases["202"], ["202"] + SPELLINGS)
        self.assertEqual((number.state, number.pbx_extension), ("active", "202"))

    def test_a_changed_number_replaces_the_old_one(self):
        number = self._number()
        number.number = "+49 30 1234567"
        self.assertEqual(self.pbx.aliases["201"], ["201", "ada", "+49301234567", "49301234567", "0049301234567",
                                                   "0301234567"])

    def test_a_number_another_pbx_account_answers_is_left_alone(self):
        self.pbx.aliases["203"].append("493043972971")
        number = self._number()
        self.assertEqual((number.state, self.pbx.writes), ("failed", []))
        self.assertIn("Extension 203", number.message)

    def test_an_unreachable_pbx_is_reported_on_the_record(self):
        with patch(CLIENT, side_effect=requests.ConnectionError()):
            number = self._number()
        self.assertEqual((number.state, number.message), ("failed", "The phone system could not be reached."))
        number.action_sync()
        self.assertEqual(number.state, "active")

    def test_a_number_that_was_never_on_the_pbx_is_removed_without_asking_it(self):
        with patch(CLIENT, side_effect=requests.ConnectionError()):
            number = self._number()
            number.unlink()

    def test_an_unconfirmed_write_is_reported(self):
        self.pbx.write_aliases = lambda extension, aliases: aliases[:1]
        self.assertEqual(self._number().state, "failed")

    def test_a_refused_change_leaves_the_pbx_as_it_was(self):
        number = self._number()
        self.pbx.writes.clear()
        for bad in ({"number": "abc"}, {"mapping_id": self._unlinked().id}):
            with self.assertRaises(ValidationError, msg=str(bad)), self.cr.savepoint():
                number.write(bad)
        self.assertEqual(self.pbx.writes, [])

    def test_names_written_but_not_confirmed_are_still_removed_later(self):
        write = self.pbx.write_aliases

        def timeout(extension, aliases):
            write(extension, aliases)
            raise requests.Timeout()
        self.pbx.write_aliases = timeout
        number = self._number()
        self.assertEqual(number.state, "failed")
        self.pbx.write_aliases = write
        number.action_sync()
        number.unlink()
        self.assertEqual(self.pbx.aliases["201"], ["201", "ada"])

    def test_a_second_account_answering_the_number_blocks_a_move(self):
        number = self._number()
        self.pbx.aliases["203"].append("03043972971")
        number.mapping_id = self.bob
        self.assertEqual(number.state, "failed")
        self.assertEqual(self.pbx.aliases["202"], ["202"])

    def test_the_number_follows_the_users_extension(self):
        number = self._number()
        self.ada.extension = "203"
        self.assertEqual((self.pbx.aliases["201"], self.pbx.aliases["203"]), (["201", "ada"], ["203"] + SPELLINGS))
        self.ada.extension = False
        self.assertEqual((number.state, self.pbx.aliases["203"]), ("pending", ["203"]))

    def test_removing_a_users_phone_removes_their_numbers(self):
        self._number()
        self.ada.unlink()
        self.assertEqual(self.pbx.aliases["201"], ["201", "ada"])
        self.assertFalse(self.env["vodia.pilot.number"].search([]))

    def test_an_unreachable_pbx_blocks_removal_with_a_message(self):
        number = self._number()
        with patch(CLIENT, side_effect=requests.ConnectionError()), self.assertRaises(UserError):
            number.unlink()

    def test_a_trunk_with_a_fixed_destination_is_reported(self):
        self.pbx.trunks[0]["dial_extension"] = "!"
        number = self._number()
        self.assertEqual(number.state, "failed")
        self.assertIn("trunk easybell", number.message)
        number.unlink()
        self.assertEqual(self.pbx.aliases["201"], ["201", "ada"])

    def test_a_spelling_the_old_extension_had_before_blocks_a_move(self):
        self.pbx.aliases["201"].append("03043972971")
        number = self._number()
        number.mapping_id = self.bob
        self.assertEqual((number.state, self.pbx.aliases["202"]), ("failed", ["202"]))

    def test_a_number_without_connection_is_stored_in_e164(self):
        number = self.env["vodia.pilot.number"].create({"number": "+49 30 43972971", "mapping_id": self.ada.id})
        self.assertEqual((number.number, number.connection_id), ("+493043972971", self.connection))

    def test_a_foreign_number_has_no_national_spelling(self):
        self._number(number="+43 1 234567")
        self.assertEqual(self.pbx.aliases["201"][2:], ["+431234567", "431234567", "00431234567"])

    def test_an_archived_user_or_phone_rings_no_one(self):
        number = self._number()
        self.ada.user_id.active = False
        self.assertEqual((number.state, self.pbx.aliases["201"]), ("pending", ["201", "ada"]))
        self.ada.user_id.active = True
        self.assertEqual(number.state, "active")
        self.ada.active = False
        self.assertEqual((number.state, self.pbx.aliases["201"]), ("pending", ["201", "ada"]))

    def test_deleting_a_user_removes_their_numbers(self):
        self._number()
        self.ada.user_id.unlink()
        self.assertEqual(self.pbx.aliases["201"], ["201", "ada"])

    def test_an_unreadable_pbx_answer_is_reported(self):
        self.pbx.list_accounts = lambda: [{"account": "203", "alias": 5}]
        self.assertEqual(self._number().message, "The PBX returned an unreadable answer.")

    def test_invalid_numbers_and_unlinked_users_are_refused(self):
        for bad in ("12", "abc", ""):
            with self.assertRaises(ValidationError, msg=bad):
                self._number(number=bad)
        self.ada.extension = False
        with self.assertRaises(ValidationError):
            self._number()

    def test_a_number_rings_only_one_extension(self):
        self._number()
        with self.assertRaises(Exception), self.cr.savepoint():
            self._number(self.bob, "+493043972971")

    def test_only_administrators_assign_numbers(self):
        user = self.env["res.users"].create({
            "name": "Plain", "login": "plain-number", "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        with self.assertRaises(AccessError):
            self.env["vodia.pilot.number"].with_user(user).create(
                {"connection_id": self.connection.id, "number": "+493043972971", "mapping_id": self.ada.id})

    def test_the_trunk_wizard_lets_the_pbx_route_by_number_and_assigns_the_trunk_number(self):
        client = MagicMock()
        client.list_trunks.return_value = [{"id": "12", "status1": "200 OK"}]
        client.create_trunk.return_value = "12"
        client.read_trunk.return_value = {"reg_registrar": "voip.easybell.de", "reg_user": "00493043972971"}
        wizard = self.env["vodia.pilot.trunk.wizard"].create({
            "connection_id": self.connection.id, "name": "easybell", "registrar": "voip.easybell.de",
            "sip_user": "00493043972971", "sip_password": "s3cret", "incoming_mapping_id": self.bob.id})
        with patch(CLIENT, return_value=client), \
                patch("odoo.addons.phone_system.models.pilot_trunk_wizard.time.sleep"), \
                patch("odoo.addons.phone_system.models.pilot_number.PilotNumber._client", return_value=self.pbx):
            wizard.action_create()
        self.assertEqual(client.create_trunk.call_args.args[0]["dial_extension"], "")
        number = self.env["vodia.pilot.number"].search([("connection_id", "=", self.connection.id)])
        self.assertEqual((number.number, number.mapping_id, number.state), ("+493043972971", self.bob, "active"))
        self.assertIn("ring extension 202", wizard.result)

    def test_the_trunk_wizard_needs_a_number_for_incoming_calls(self):
        wizard = self.env["vodia.pilot.trunk.wizard"].create({
            "connection_id": self.connection.id, "name": "other", "registrar": "sip.example.com",
            "sip_user": "office", "sip_password": "s3cret", "incoming_mapping_id": self.bob.id})
        with patch(CLIENT) as client, self.assertRaises(ValidationError):
            wizard.action_create()
        client.assert_not_called()
