import os
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

CLIENT = "odoo.addons.phone_system.models.pilot_trunk_wizard.PilotTrunkClient"
ACCOUNT = "odoo.addons.phone_system.models.gcna_account.GCNAAccount"
INVITATION = "i" * 43


class NoTrunks:
    def __init__(self, *args, **kwargs):
        pass

    def list_trunks(self):
        return []


def reply(status, payload):
    return MagicMock(status_code=status, json=lambda: payload)


@tagged("post_install", "-at_install", "phone_system")
class TestNetwork(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.connection = cls.env["vodia.pilot.connection"].create({"name": "Pilot", "company_id": company.id})
        group = cls.env.ref("phone_system.group_pilot").id
        cls.caller, cls.colleague = cls.env["res.users"].create([{
            "name": name, "login": login, "email": email, "company_id": company.id,
            "company_ids": [(6, 0, [company.id])], "group_ids": [(6, 0, [group])],
        } for name, login, email in (("Martin Högl", "vodia-network-caller", "mh@vodia.com"),
                                     ("Grace Hopper", "vodia-network-colleague", "gh@vodia.com"))])
        cls.mapping, cls.colleague_mapping = cls.env["vodia.pilot.mapping"].create([
            {"user_id": cls.caller.id, "connection_id": cls.connection.id, "extension": "203"},
            {"user_id": cls.colleague.id, "connection_id": cls.connection.id, "extension": "204"}])

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mapping.web_password = self.colleague_mapping.web_password = "shared-individual-password"
        # A linked installation; its credential is read through the patched _authorization.
        self.env.ref("phone_system.gcna_installation").sudo().token_ciphertext = "linked"
        for target in (patch(CLIENT, NoTrunks), patch(f"{ACCOUNT}._authorization", return_value="t" * 43)):
            target.start()
            self.addCleanup(target.stop)
        self.Invitation = self.env["vodia.network.invitation"]

    def _send(self, email="ada@lovelace.test", payload=None):
        post = MagicMock(return_value=reply(202, payload or {"sent": True, "invitationId": INVITATION}))
        with patch("requests.post", post):
            self.env["vodia.pilot.mapping"].with_user(self.caller)._send_contact_card(email)
        return post

    def _sync(self, connections):
        with patch("requests.post", MagicMock(return_value=reply(200, {"connections": connections}))) as post:
            self.Invitation._sync_connections()
        return post

    def _accepted(self, **values):
        return {"invitationId": INVITATION, "email": "ada@lovelace.test", "name": "Ada Lovelace",
                "company": "Lovelace Analytics", "clearance": "cleared", "acceptedAt": "2026-10-28T10:00:00.000Z", **values}

    def test_a_sent_invitation_is_remembered_for_its_sender(self):
        self._send()
        invitation = self.Invitation.search([("invitation_id", "=", INVITATION)])
        self.assertEqual((invitation.user_id, invitation.email, invitation.state), (self.caller, "ada@lovelace.test", "sent"))

    def test_a_contact_card_without_invitations_switched_on_leaves_nothing_to_follow(self):
        self._send(payload={"sent": True})
        self.assertFalse(self.Invitation.search([]))

    def test_an_accepted_invitation_marks_the_contact_with_that_email_as_connected(self):
        ada = self.env["res.partner"].create({"name": "Ada", "email": "Ada@Lovelace.test"})
        self._send()
        post = self._sync([self._accepted()])
        self.assertTrue(post.call_args.args[0].endswith("/v1/connections"))
        invitation = self.Invitation.search([("invitation_id", "=", INVITATION)])
        self.assertEqual((invitation.state, invitation.partner_id, invitation.name), ("connected", ada, "Ada Lovelace"))
        self.assertTrue(ada.with_user(self.caller).vodia_network_connected)
        self.assertEqual(ada.name, "Ada", "the contact is not changed without the caller")

    def test_without_a_unique_contact_the_caller_creates_one_on_confirmation(self):
        self.env["res.partner"].create([{"name": "Ada 1", "email": "ada@lovelace.test"}, {"name": "Ada 2", "email": "ada@lovelace.test"}])
        self._send()
        self._sync([self._accepted()])
        Model = self.env["vodia.pilot.mapping"].with_user(self.caller)
        with self.assertRaises(AccessError, msg="a contact is created only with the right to create contacts"):
            Model._network_create_contact(INVITATION)
        self.caller.group_ids = [(4, self.env.ref("base.group_partner_manager").id)]
        pending = Model._network_pending()
        self.assertEqual(pending, [{"invitation_id": INVITATION, "name": "Ada Lovelace", "company": "Lovelace Analytics",
                                    "email": "ada@lovelace.test"}])
        partner = Model._network_create_contact(INVITATION)
        self.assertEqual((partner.name, partner.email, partner.company_name), ("Ada Lovelace", "ada@lovelace.test", "Lovelace Analytics"))
        self.assertTrue(partner.vodia_network_connected)
        self.assertEqual(Model._network_pending(), [])

    def test_only_the_sender_sees_and_confirms_their_connections(self):
        self._send()
        self._sync([self._accepted()])
        colleague = self.env["vodia.pilot.mapping"].with_user(self.colleague)
        self.assertEqual(colleague._network_pending(), [])
        with self.assertRaises(UserError):
            colleague._network_create_contact(INVITATION)
        self.assertFalse(self.Invitation.with_user(self.colleague).search([]))
        with self.assertRaises(AccessError):
            self.Invitation.search([]).with_user(self.colleague).read(["email"])

    def test_unknown_or_malformed_connections_change_nothing(self):
        self._send()
        self._sync([self._accepted(invitationId="u" * 43), self._accepted(email="not an address"), {"invitationId": INVITATION}])
        self.assertEqual(self.Invitation.search([]).state, "sent")

    def test_a_connection_awaiting_review_is_shown_as_such(self):
        self.env["res.partner"].create({"name": "Ada", "email": "ada@lovelace.test"})
        self._send()
        self._sync([self._accepted(clearance="review")])
        self.assertEqual(self.Invitation.search([]).clearance, "review")

    def test_sync_is_skipped_without_a_linked_installation(self):
        with patch(f"{ACCOUNT}._authorization", side_effect=UserError("no credential")), \
                patch("requests.post") as post:
            self.env.ref("phone_system.gcna_installation").sudo().token_ciphertext = False
            self.Invitation._sync_connections()
        post.assert_not_called()

    def test_a_connections_changed_card_updates_the_linked_contact(self):
        ada = self.env["res.partner"].create({"name": "Ada", "email": "ada@lovelace.test", "phone": "+44 20 0000000"})
        self._send()
        self._sync([self._accepted()])
        card = {"name": "Ada Lovelace", "role": "Analyst", "company": "Lovelace Analytics", "email": "ada@lovelace.test",
                "phones": [{"label": "mobile", "number": "+447700900123"}, {"label": "work", "number": "+442071234567"}],
                "website": "https://lovelace.test", "address": {"street": "1 Engine Street", "postcode": "W1 1AA", "city": "London", "country": "GB"}}
        self._sync([self._accepted(card=card, cardUpdatedAt="2026-10-29T09:00:00.000Z")])
        self.assertEqual((ada.name, ada.function, ada.phone, ada.website, ada.street, ada.zip, ada.city, ada.country_id.code),
                         ("Ada Lovelace", "Analyst", "+442071234567", "https://lovelace.test", "1 Engine Street", "W1 1AA", "London", "GB"))
        self.assertIn("GCNA", ada.message_ids[:1].body or "", "the change is noted on the contact")
        ada.phone = "+44 20 1111111"
        self._sync([self._accepted(card=card, cardUpdatedAt="2026-10-29T09:00:00.000Z")])
        self.assertEqual(ada.phone, "+44 20 1111111", "the same card version is applied once")

    def test_a_malformed_card_changes_nothing(self):
        ada = self.env["res.partner"].create({"name": "Ada", "email": "ada@lovelace.test"})
        self._send()
        self._sync([self._accepted()])
        self._sync([self._accepted(card={"name": "<script>"}, cardUpdatedAt="2026-10-29T09:00:00.000Z")])
        self.assertEqual(ada.name, "Ada")

    def _call(self, uid, peer, outcome="answered"):
        self.mapping.external_calling = True
        self.env.company.country_id = self.env.ref("base.de")
        self.env["vodia.pilot.call"].with_user(self.caller)._record_call({
            "call_id": uid, "direction": "outgoing", "peer": peer, "outcome": outcome,
            "duration": 30 if outcome == "answered" else 0, "started_at": 1760000000000})
        return self.env["vodia.pilot.mapping"].with_user(self.caller)._contact_card_follow_up(uid)

    def test_after_an_answered_outside_call_the_caller_is_offered_to_send_their_card(self):
        self.env["res.partner"].create({"name": "Ada Lovelace", "phone": "+49 30 5557874947", "email": "ada@lovelace.test"})
        self.assertEqual(self._call("f1", "+49305557874947"),
                         {"prompt": True, "email": "ada@lovelace.test", "name": "Ada Lovelace", "number": "+49305557874947"})
        self.assertEqual(self._call("f2", "+49301234567"), {"prompt": True, "email": "", "name": "", "number": "+49301234567"},
                         "an unknown caller is offered too, with the address left to fill in")

    def test_no_offer_after_a_colleague_call_an_unanswered_call_or_an_existing_invitation(self):
        self.assertEqual(self._call("f3", "204"), {"prompt": False})
        self.assertEqual(self._call("f4", "+49301234567", outcome="failed"), {"prompt": False})
        self.env["res.partner"].create({"name": "Ada Lovelace", "phone": "+49 30 5557874947", "email": "ada@lovelace.test"})
        self._send()
        self.assertEqual(self._call("f5", "+49305557874947"), {"prompt": False}, "Ada was already invited")
        self.assertEqual(self.env["vodia.pilot.mapping"].with_user(self.colleague)._contact_card_follow_up("f5"), {"prompt": False},
                         "only the caller's own calls")
