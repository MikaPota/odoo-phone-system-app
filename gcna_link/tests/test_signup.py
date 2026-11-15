from datetime import timedelta
from unittest.mock import Mock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

SIGNUP_ID = "s" * 43
VALUES = {
    "email": "Eva@Shop.test", "admin_name": "Eva Shop", "company_name": "Shop GmbH", "domain": "www.shop.test",
    "accept_terms": True, "terms_version": "2", "privacy_version": "3",
}


def soon(minutes=10):
    return (fields.Datetime.now() + timedelta(minutes=minutes)).isoformat() + "Z"


@tagged("post_install", "-at_install")
class TestSignup(TransactionCase):
    """A free GCNA account for this database (GCNA REQ-006): code, new organisation, stored credential."""

    def setUp(self):
        super().setUp()
        self.account = self.env["vodia.gcna.account"].sudo()
        self.record = self.env.ref("gcna_link.gcna_installation")

    def _reply(self, status, payload):
        response = Mock(status_code=status)
        response.json.return_value = payload
        return response

    def test_sign_up_stores_the_installation_credential(self):
        sent = []

        def gcna(url, **kwargs):
            sent.append((url, kwargs["json"]))
            if url.endswith("/v1/signups"):
                return self._reply(202, {"signupId": SIGNUP_ID, "expiresAt": soon()})
            return self._reply(200, {"state": "linked", "installation": {
                "token": "t" * 48, "accountId": "org_shop_1234", "installationId": kwargs["json"]["installationId"],
                "scopes": ["phone-system:manage", "bizzkickr:use"], "expiresAt": soon(60 * 24 * 30),
            }})

        with patch("requests.post", side_effect=gcna):
            self.assertEqual(self.account._signup_start(VALUES)["state"], "verification_pending")
            url, body = sent[0]
            self.assertTrue(url.endswith("/v1/signups"))
            self.assertEqual(body["domain"], "shop.test")
            self.assertEqual(body["installationId"], self.record.installation_id)
            self.assertTrue(body["installationProof"])
            self.assertEqual(body["app"], "odoo")
            self.assertEqual(self.record.signup_id, SIGNUP_ID)

            status = self.account._signup_confirm("123456")
        self.assertEqual(status["state"], "linked")
        self.assertTrue(self.account._linked())
        self.assertEqual(self.record.account_id, "org_shop_1234")
        self.assertFalse(self.record.signup_id)
        self.assertNotIn("t" * 48, self.record.token_ciphertext, "the credential is stored encrypted")
        self.assertEqual(self.record._authorization(), "t" * 48)
        self.assertEqual(sent[1][1]["installationProof"], body["installationProof"], "the same proof both times")

    def test_a_credential_for_an_unknown_scope_is_not_stored(self):
        with patch("requests.post", return_value=self._reply(202, {"signupId": SIGNUP_ID, "expiresAt": soon()})):
            self.account._signup_start(VALUES)
        reply = self._reply(200, {"state": "linked", "installation": {
            "token": "t" * 48, "accountId": "org_shop_1234", "installationId": self.record.installation_id,
            "scopes": ["operator:admin"], "expiresAt": soon(60)}})
        with patch("requests.post", return_value=reply), self.assertRaises(UserError):
            self.account._signup_confirm("123456")
        self.assertFalse(self.account._linked())

    def test_consent_a_valid_form_and_an_unlinked_database_are_required(self):
        with patch("requests.post") as post:
            with self.assertRaises(UserError):
                self.account._signup_start({**VALUES, "accept_terms": False})
            with self.assertRaises(UserError):
                self.account._signup_start({**VALUES, "domain": "nope"})
            self.record.write({"account_id": "org_x_12345678", "token_ciphertext": "x", "token_expires_at": fields.Datetime.now() + timedelta(days=1)})
            with self.assertRaises(UserError):
                self.account._signup_start(VALUES)
            post.assert_not_called()

    def test_gcna_refusals_reach_the_user_in_words(self):
        reply = self._reply(400, {"error": {"code": "SIGNUP_EXPIRED"}})
        self.record.write({"signup_id": SIGNUP_ID, "proof_ciphertext": False})
        self.record._ensure_proof()
        with patch("requests.post", return_value=reply), self.assertRaisesRegex(UserError, "sign-up expired|Registrierung ist abgelaufen"):
            self.account._signup_confirm("123456")

    def test_a_new_code_can_be_sent(self):
        self.record.write({"signup_id": SIGNUP_ID})
        self.record._ensure_proof()
        with patch("requests.post", return_value=self._reply(202, {"signupId": SIGNUP_ID, "expiresAt": soon()})) as post:
            self.assertEqual(self.account._signup_resend()["state"], "verification_pending")
        self.assertTrue(post.call_args.args[0].endswith(f"/v1/signups/{SIGNUP_ID}/resend"))
