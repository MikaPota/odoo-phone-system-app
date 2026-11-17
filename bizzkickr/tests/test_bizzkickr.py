from unittest.mock import Mock, patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

ACCOUNT = "odoo.addons.gcna_link.models.gcna_account.GCNAAccount"
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0h1v1H0z"/></svg>'
REQUEST = "a" * 64
CONNECTION = "b" * 64


class FakeGCNA:
    """GCNA's /v1/bizzkickr/* as the app sees it: answers per path, every call recorded."""

    def __init__(self):
        self.calls = []
        self.answers = {}

    def __call__(self, url, **kwargs):
        path = url.split("/v1/bizzkickr/", 1)[1]
        self.calls.append((path, kwargs["json"], kwargs["headers"]))
        status, payload = self.answers.get(path, (200, {}))
        response = Mock(status_code=status)
        response.json.return_value = payload
        return response

    def last(self, path):
        return next(body for p, body, _ in reversed(self.calls) if p == path)


def card(**over):
    return {"id": "c-eva", "name": "Eva Shop", "organization": "Shop GmbH", "role": "Owner", "phone": "+49 30 123456",
            "email": "eva@shop.test", "web": "https://shop.test", "isDefault": True,
            "link": "https://bizzkickr.comwize.app/k/AAAAAAAAAAAAAAAAAAAAAA", "qrSvg": SVG, **over}


@tagged("post_install", "-at_install")
class TestBizzKickR(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write({"name": "Shop GmbH", "website": "https://www.shop.test"})
        cls.eva = new_test_user(cls.env, login="eva", email="Eva@Shop.test", name="Eva Shop", groups="base.group_user")
        cls.eva.partner_id.function = "Owner"
        cls.max = new_test_user(cls.env, login="max", email="max@shop.test", name="Max Shop", groups="base.group_user")

    def setUp(self):
        super().setUp()
        self.gcna = FakeGCNA()
        for target, value in ((f"{ACCOUNT}._linked", True), (f"{ACCOUNT}._authorization", "t" * 48)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        post = patch("requests.post", side_effect=self.gcna)
        post.start()
        self.addCleanup(post.stop)
        self.Person = self.env["bizzkickr.person"].with_user(self.eva)

    def _ready(self):
        person = self.Person._mine()
        self.gcna.answers.update({"people": (202, {"state": "unconfirmed"}), "person": (200, {"state": "confirmed"}),
                                  "cards": (200, {"cards": [card()]})})
        person.with_user(self.eva).action_register()
        person.with_user(self.eva).action_refresh()
        return person

    def test_register_confirm_and_create_the_first_card_from_odoo(self):
        person = self.Person._mine()
        self.assertEqual(person.state, "none")
        self.gcna.answers["people"] = (202, {"state": "unconfirmed"})
        person.with_user(self.eva).action_register()
        self.assertEqual(person.state, "unconfirmed")
        body = self.gcna.last("people")
        self.assertEqual(body["person"], "eva@shop.test")
        self.assertEqual(body["companyName"], "Shop GmbH")
        self.assertEqual(body["app"], "odoo", "BIZZKICKR's confirmation mail names the app")
        self.assertEqual(self.gcna.calls[0][2]["Authorization"], "Bearer " + "t" * 48)

        self.gcna.answers.update({"person": (200, {"state": "confirmed"}), "cards": (200, {"cards": []})})
        person.with_user(self.eva).action_refresh()
        self.assertEqual(person.state, "no_card")

        self.gcna.answers["cards/create"] = (201, {"card": card()})
        person.with_user(self.eva).action_create_card()
        sent = self.gcna.last("cards/create")["card"]
        self.assertEqual(sent["name"], "Eva Shop")
        self.assertEqual(sent["organization"], "Shop GmbH")
        self.assertEqual(sent["role"], "Owner")
        self.assertEqual(sent["web"], "www.shop.test")
        self.assertEqual(person.state, "ready")
        self.assertIn("barcode_type=QR", person.card_qr_url)
        self.assertIn("value=https%3A%2F%2Fbizzkickr.comwize.app%2Fk%2F", person.card_qr_url)
        self.assertTrue(person.card_link.startswith("https://bizzkickr.comwize.app/k/"))

    def test_the_card_is_saved_and_sent_to_a_contact(self):
        person = self._ready()
        person.with_user(self.eva).write({"card_role": "Managing Director"})
        self.gcna.answers["cards/c-eva/update"] = (200, {"card": card(role="Managing Director")})
        person.with_user(self.eva).action_save_card()
        self.assertEqual(self.gcna.last("cards/c-eva/update")["card"]["role"], "Managing Director")

        contact = self.env["res.partner"].create({"name": "Bob Buyer", "email": "Bob@Buyer.test"})
        self.gcna.answers["cards/c-eva/send"] = (202, {"sent": True})
        contact.with_user(self.eva).action_bizzkickr_send_card()
        self.assertEqual(self.gcna.last("cards/c-eva/send")["to"], "bob@buyer.test")
        with self.assertRaises(UserError):
            self.env["res.partner"].create({"name": "No Mail"}).with_user(self.eva).action_bizzkickr_send_card()

    def test_a_confirmed_request_becomes_a_contact_and_follows_later_changes(self):
        person = self._ready()
        self.gcna.answers["requests"] = (200, {"requests": [{"id": REQUEST, "name": "Bob Buyer", "organization": "Buyer Ltd",
                                                              "email": "bob@buyer.test", "event": "Expo", "requestedAt": "2026-11-15T09:00:00Z"}]})
        action = person.with_user(self.eva).action_requests()
        requests = self.env["bizzkickr.request"].with_user(self.eva).browse(action["domain"][0][2])
        self.assertEqual(requests.mapped("name"), ["Bob Buyer"])

        row = {"id": CONNECTION, "name": "Bob Buyer", "organization": "Buyer Ltd", "email": "bob@buyer.test", "role": "CTO",
               "phone": None, "web": None, "connectedAt": "2026-11-15T09:05:00Z", "updatedAt": "2026-11-15T09:05:00Z"}
        self.gcna.answers.update({f"requests/{REQUEST}/accept": (200, {"connection": {}}),
                                  "connections": (200, {"connections": [row]}), "requests": (200, {"requests": []})})
        requests.action_accept()
        contact = self.env["res.partner"].search([("email", "=", "bob@buyer.test")])
        self.assertEqual(len(contact), 1)
        self.assertEqual((contact.name, contact.function, contact.company_name), ("Bob Buyer", "CTO", "Buyer Ltd"))
        self.assertTrue(contact.with_user(self.eva).bizzkickr_connected)
        self.assertFalse(contact.with_user(self.max).bizzkickr_connected, "another user's connection stays theirs")
        self.assertTrue(person.synced_at)
        self.assertNotIn("since", self.gcna.last("connections"), "the first fetch asks for everything")

        # Bob changes his card; a field changed here since stays as it is here.
        contact.function = "Chief Technology Officer"
        self.gcna.answers["connections"] = (200, {"connections": [{**row, "role": "CEO", "organization": "Buyer Group"}]})
        person._sync()
        self.assertIn("since", self.gcna.last("connections"), "later fetches ask only for changes")
        self.assertEqual(contact.company_name, "Buyer Group")
        self.assertEqual(contact.function, "Chief Technology Officer")

    def test_an_existing_contact_only_gets_its_empty_fields_filled(self):
        person = self._ready()
        contact = self.env["res.partner"].create({"name": "Robert Buyer", "email": "bob@buyer.test", "function": "Buyer"})
        self.gcna.answers["connections"] = (200, {"connections": [{"id": CONNECTION, "name": "Bob Buyer", "organization": "Buyer Ltd",
                                                                    "email": "bob@buyer.test", "role": "CTO", "connectedAt": "2026-11-15T09:05:00Z"}]})
        person._sync()
        self.assertEqual((contact.name, contact.function, contact.company_name), ("Robert Buyer", "Buyer", "Buyer Ltd"))
        self.assertEqual(self.env["res.partner"].search_count([("email", "=", "bob@buyer.test")]), 1)

    def test_errors_reach_the_user_in_words_and_a_revoked_link_is_not_mistaken(self):
        person = self.Person._mine()
        person.sudo().write({"email": "eva@shop.test", "state": "unconfirmed"})
        self.gcna.answers["cards"] = (409, {"error": {"code": "PERSON_UNCONFIRMED"}})
        with self.assertRaisesRegex(UserError, "confirm|Bestätigung"):
            person.sudo()._call("cards")
        self.gcna.answers["cards"] = (403, {"error": {"code": "UNAUTHORIZED"}})
        with self.assertRaisesRegex(UserError, "GCNA no longer accepts|GCNA akzeptiert"):
            person.sudo()._call("cards")

    def test_a_malformed_card_or_link_is_not_shown(self):
        person = self.Person._mine()
        person.sudo().write({"email": "eva@shop.test"})
        for bad in (card(link="javascript:alert(1)"), card(link="http://plain.test/k/x"), card(name="<b>Eva</b>")):
            self.gcna.answers.update({"person": (200, {"state": "confirmed"}), "cards": (200, {"cards": [bad]})})
            with self.assertRaises(UserError):
                person.sudo()._refresh()
        self.assertFalse(person.card_qr_url)

    def test_one_user_cannot_act_on_another_users_card(self):
        person = self._ready()
        with self.assertRaises(UserError):
            person.with_user(self.max).action_save_card()
        self.assertFalse(self.env["bizzkickr.person"].with_user(self.max).search([]), "nobody sees another's person")

    def test_without_a_gcna_link_nothing_is_sent(self):
        with patch(f"{ACCOUNT}._linked", return_value=False), self.assertRaisesRegex(UserError, "Setup|Einrichtung"):
            self.Person._mine().with_user(self.eva).action_register()
        self.assertFalse(self.gcna.calls)


@tagged("post_install", "-at_install")
class TestQrImage(HttpCase):
    def test_odoo_draws_the_cards_qr_code(self):
        new_test_user(self.env, login="qr", password="qr-password-1234", groups="base.group_user")
        person = self.env["bizzkickr.person"].create({"user_id": self.env.ref("base.user_admin").id, "company_id": self.env.company.id,
                                                      "card_link": "https://bizzkickr.comwize.app/k/AAAAAAAAAAAAAAAAAAAAAA"})
        self.authenticate("qr", "qr-password-1234")
        response = self.url_open(person.card_qr_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
