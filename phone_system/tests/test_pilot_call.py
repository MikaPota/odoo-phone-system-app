import os
from unittest.mock import patch

from cryptography.fernet import Fernet
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "phone_system")
class TestPilotCall(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = cls.env.ref("phone_system.group_pilot")
        cls.connection = cls.env["vodia.pilot.connection"].create({
            "name": "Test PBX", "company_id": cls.env.company.id})
        cls.alice = cls._staff("Alice", "301", group)
        cls.bob = cls._staff("Bob", "302", group)

    @classmethod
    def _staff(cls, name, extension, group):
        user = cls.env["res.users"].create({
            "name": name, "login": "vodia-call-" + extension, "company_id": cls.env.company.id,
            "company_ids": [(6, 0, [cls.env.company.id])], "groups_id": [(6, 0, [group.id])]})
        cls.env["vodia.pilot.mapping"].create({
            "user_id": user.id, "connection_id": cls.connection.id,
            "extension": extension})
        return user

    def _entry(self, **kw):
        entry = {"call_id": "c1", "direction": "outgoing", "peer": "302", "outcome": "answered",
                 "duration": 12, "started_at": 1760000000000}
        return dict(entry, **kw)

    def _record(self, user, **kw):
        return self.env["vodia.pilot.call"].with_user(user)._record_call(self._entry(**kw))

    def test_peer_is_resolved_server_side_and_recorded_once(self):
        first = self._record(self.alice)
        self.assertEqual(first, self._record(self.alice))
        call = self.env["vodia.pilot.call"].browse(first)
        self.assertEqual((call.user_id, call.peer_user_id, call.duration), (self.alice, self.bob, 12))

    def test_colleague_contact_is_matched_for_the_owner_only(self):
        self._record(self.alice)
        self._record(self.bob, call_id="b1", direction="incoming", peer="301")
        calls = self.env["vodia.pilot.call"].with_user(self.alice)
        self.assertEqual(calls.search([("peer_partner_id", "=", self.bob.partner_id.id)]).mapped("peer_partner_id"),
                         self.bob.partner_id)
        self.assertFalse(calls.search([("peer_partner_id", "=", self.alice.partner_id.id)]))

    def test_unknown_peer_is_kept_without_a_user(self):
        call = self.env["vodia.pilot.call"].browse(self._record(self.alice, call_id="c2", peer="399"))
        self.assertFalse(call.peer_user_id)

    def test_history_is_private_to_the_owner(self):
        self._record(self.alice)
        calls = self.env["vodia.pilot.call"]
        self.assertEqual(len(calls.with_user(self.alice).search([])), 1)
        self.assertFalse(calls.with_user(self.bob).search([]))

    def test_users_cannot_write_history_directly(self):
        call_id = self._record(self.alice)
        calls = self.env["vodia.pilot.call"].with_user(self.alice)
        with self.assertRaises(AccessError):
            calls.browse(call_id).write({"duration": 1})
        with self.assertRaises(AccessError):
            calls.create({"call_uid": "x", "user_id": self.alice.id, "company_id": self.env.company.id,
                          "direction": "outgoing", "outcome": "failed", "started_at": "2026-01-01"})

    def test_invalid_entries_are_rejected(self):
        for bad in ({"outcome": "x"}, {"direction": "z"}, {"duration": -1}, {"duration": 90000},
                    {"peer": "12ab"}, {"call_id": ""}, {"started_at": 0}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                self._record(self.alice, **bad)

    def _external(self, user, enabled=True):
        mapping = self.env["vodia.pilot.mapping"].search([("user_id", "=", user.id)])
        mapping.external_calling = enabled
        self.env.company.country_id = self.env.ref("base.de")
        return mapping

    def test_external_numbers_need_the_users_permission_and_are_stored_normalized(self):
        self._external(self.alice, enabled=False)
        with self.assertRaises(ValidationError):
            self._record(self.alice, call_id="e1", peer="+49 30 555 78749 47")
        self._external(self.alice)
        call = self.env["vodia.pilot.call"].browse(self._record(self.alice, call_id="e2", peer="030 55578749-47"))
        self.assertEqual((call.peer_number, call.peer_extension, call.peer_user_id), ("+49305557874947", False, self.env["res.users"]))

    def test_incoming_external_callers_are_recorded_without_the_permission(self):
        self._external(self.alice, enabled=False)
        call = self.env["vodia.pilot.call"].browse(
            self._record(self.alice, call_id="e3", direction="incoming", peer="+4930555787494"))
        self.assertEqual(call.peer_number, "+4930555787494")

    def test_an_external_number_matches_a_contact_only_the_caller_may_read(self):
        self._external(self.alice)
        customer = self.env["res.partner"].create({"name": "Lovelace Analytics", "phone": "+49 30 555 78749 47"})
        call = self.env["vodia.pilot.call"].browse(self._record(self.alice, call_id="e4", peer="+49305557874947"))
        self.assertEqual(call.peer_partner_id, customer)
        self.assertEqual(customer.with_user(self.alice).vodia_call_count, 1)
        self.assertEqual(customer.with_user(self.bob).vodia_call_count, 0, "another user's calls stay private")
        action = customer.with_user(self.alice).action_vodia_calls()
        self.assertEqual(self.env["vodia.pilot.call"].with_user(self.alice).search(action["domain"]), call)
        hidden = self.env["res.partner"].create({"name": "Private", "phone": "+49 30 1111111", "company_id": self.env["res.company"].create({"name": "Elsewhere"}).id})
        unmatched = self.env["vodia.pilot.call"].browse(self._record(self.alice, call_id="e5", peer="+49301111111"))
        self.assertFalse(unmatched.peer_partner_id, "a contact the caller cannot read is never revealed")
        self.assertTrue(hidden.exists())

    def test_two_contacts_with_the_same_number_stay_unmatched(self):
        self._external(self.alice)
        for name in ("Twin A", "Twin B"):
            self.env["res.partner"].create({"name": name, "phone": "+49 30 2222222"})
        call = self.env["vodia.pilot.call"].browse(self._record(self.alice, call_id="e6", peer="+49302222222"))
        self.assertFalse(call.peer_partner_id)

    def test_colleague_calls_appear_on_the_colleagues_contact(self):
        self._record(self.alice)
        self.assertEqual(self.bob.partner_id.with_user(self.alice).vodia_call_count, 1)

    def test_the_browser_is_told_whether_the_user_may_call_externally(self):
        self._external(self.alice)
        with patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode()}):
            self.env["vodia.pilot.mapping"].search([("user_id", "=", self.alice.id)]).web_password = "shared-individual-password"
        config = self.env["vodia.pilot.mapping"].with_user(self.alice)._browser_configuration()
        self.assertIs(config["external_calling"], True)

    def test_malformed_numbers_are_rejected(self):
        self._external(self.alice)
        for bad in ("+49 abc", "123456789012345678", "+", "0049-"):
            with self.assertRaises(ValidationError, msg=bad):
                self._record(self.alice, call_id="bad", peer=bad)
