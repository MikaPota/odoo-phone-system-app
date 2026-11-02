import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet
from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.phone_system.models.credentials import KEY_PARAMETER, credential_cipher, installation_key
from odoo.addons.phone_system.controllers.pilot_session import PilotSessionController


@tagged("post_install", "-at_install", "phone_system")
class TestGCNAAccountLink(TransactionCase):
    def setUp(self):
        super().setUp()
        # The test database also has German; these tests compare English messages.
        self.env = self.env(context=dict(self.env.context, lang="en_US"))
        self.manager = self.env["res.users"].create({
            "name": "Account manager", "login": "gcna-account-manager",
            "groups_id": [(6, 0, [self.env.ref("base.group_system").id])],
        })
        self.caller = self.env["res.users"].create({
            "name": "Account caller", "login": "gcna-account-caller",
            "groups_id": [(6, 0, [self.env.ref("phone_system.group_pilot").id])],
        })
        self.controller = PilotSessionController()
        self.request = Mock()
        self.request.env = self.env(user=self.manager.id)
        self.request.future_response.headers = {}
        for patcher in (
            patch("odoo.addons.phone_system.controllers.pilot_session.request", self.request),
            patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode()}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_requesting_a_code_survives_reload_without_exposing_installation_proof(self):
        response = Mock(status_code=202)
        response.json.return_value = {
            "verificationId": "verify_test_1234",
            "expiresAt": (fields.Datetime.now() + timedelta(minutes=10)).isoformat() + "Z",
        }
        with patch("requests.post", return_value=response) as post:
            pending = self.controller.account_request(email="owner@example.com")
            resumed = self.controller.account_status()
        self.assertEqual(pending, resumed)
        self.assertEqual(pending["state"], "verification_pending")
        self.assertEqual(pending["email"], "owner@example.com")
        sent = post.call_args.kwargs["json"]
        self.assertGreaterEqual(len(sent["installationProof"]), 32)
        self.assertNotIn(sent["installationProof"], str(pending))
        self.assertNotIn("verificationId", pending)
        self.assertNotIn("installationId", pending)

    def test_confirming_a_code_stores_private_authorization_and_resumes_linked_status(self):
        token = "synthetic_installation_token_" + "x" * 40
        proof = None

        def gcna_post(url, **kwargs):
            nonlocal proof
            response = Mock()
            sent = kwargs["json"]
            if url.endswith("/v1/account-verifications"):
                proof = sent["installationProof"]
                response.status_code = 202
                response.json.return_value = {
                    "verificationId": "verify_test_1234",
                    "expiresAt": (fields.Datetime.now() + timedelta(minutes=10)).isoformat() + "Z",
                }
            else:
                self.assertEqual(sent["installationProof"], proof)
                response.status_code = 200
                if not url.endswith("/approve"):
                    response.json.return_value = {"state": "approval_required" if url.endswith("/login") else "authorization_required"}
                    return response
                response.json.return_value = {
                    "installationId": sent["installationId"],
                    "accountId": "account_test_1234",
                    "token": token,
                    "scopes": ["phone-system:manage"],
                    "expiresAt": (fields.Datetime.now() + timedelta(days=1)).isoformat() + "Z",
                }
            return response

        with patch("requests.post", side_effect=gcna_post):
            self.controller.account_request(email="owner@example.com")
            self.assertEqual(self.controller.account_confirm(code="123456")["state"], "authorization_required")
            self.controller.account_login(password="request-local-password")
            linked = self.controller.account_approve(accept=True)
            resumed = self.controller.account_status()
        self.assertEqual(linked["state"], "linked")
        self.assertEqual(linked, resumed)
        self.assertNotIn(token, str(linked))
        self.assertNotIn(proof, str(linked))
        for user in (self.manager, self.caller):
            with self.assertRaises(AccessError):
                self.env["vodia.gcna.account"].with_user(user).search_read([], ["token_ciphertext"])

    def _pending(self):
        response = Mock(status_code=202)
        response.json.return_value = {
            "verificationId": "verify_test_1234",
            "expiresAt": (fields.Datetime.now() + timedelta(minutes=10)).isoformat() + "Z",
        }
        with patch("requests.post", return_value=response):
            return self.controller.account_request(email="owner@example.com")

    def test_email_code_requires_separate_sign_in_and_explicit_approval(self):
        def gcna_post(url, **kwargs):
            response = Mock(status_code=200)
            if url.endswith("/confirm"):
                response.json.return_value = {"state": "authorization_required"}
            elif url.endswith("/login"):
                self.assertEqual(kwargs["json"]["password"], "request-local-password")
                response.json.return_value = {"state": "mfa_required", "methods": ["email"]}
            elif url.endswith("/mfa"):
                response.json.return_value = {"state": "approval_required"}
            elif url.endswith("/approve"):
                self.assertIs(kwargs["json"]["accept"], True)
                response.json.return_value = {
                    "installationId": kwargs["json"]["installationId"],
                    "accountId": "account_test_1234", "token": "x" * 48,
                    "scopes": ["phone-system:manage"],
                    "expiresAt": (fields.Datetime.now() + timedelta(hours=1)).isoformat() + "Z",
                }
            return response
        self._pending()
        with patch("requests.post", side_effect=gcna_post):
            self.assertEqual(self.controller.account_confirm(code="123456")["state"], "authorization_required")
            self.assertEqual(self.controller.account_login(password="request-local-password")["state"], "mfa_required")
            self.assertEqual(self.controller.account_mfa(code="123456")["state"], "approval_required")
            with self.assertRaises(UserError):
                self.controller.account_approve(accept=False)
            linked = self.controller.account_approve(accept=True)
        self.assertEqual(linked["state"], "linked")
        self.assertNotIn("request-local-password", str(linked))
        self.assertNotIn("x" * 48, str(linked))

    def test_resume_recovers_a_lost_authorization_step_without_exposing_secrets(self):
        self._pending()
        response = Mock(status_code=200)
        response.json.return_value = {"state": "mfa_required"}
        with patch("requests.post", return_value=response):
            resumed = self.controller.account_resume()
        self.assertEqual(resumed["state"], "mfa_required")
        self.assertNotIn("installationProof", resumed)

    def test_invitation_activation_keeps_secrets_request_local_and_requires_separate_approval(self):
        def gcna_post(url, **kwargs):
            response = Mock(status_code=200)
            if url.endswith("/confirm"):
                response.json.return_value = {"state": "authorization_required"}
            elif url.endswith("/invitation"):
                self.assertEqual(kwargs["json"]["token"], "i" * 43)
                response.json.return_value = {
                    "state": "authorization_required",
                    "invitation": {"company": "Invited company", "can_activate": False},
                }
            elif url.endswith("/activate"):
                self.assertEqual(kwargs["json"]["password"], "request-local-password")
                self.assertIs(kwargs["json"]["acceptTerms"], True)
                response.json.return_value = {"state": "approval_required", "accessToken": "must-not-reach-browser"}
            return response
        self._pending()
        with patch("requests.post", side_effect=gcna_post):
            self.controller.account_confirm(code="123456")
            notice = self.controller.account_invitation(token="i" * 43)
            self.assertFalse(notice["invitation"]["can_activate"])
            self.assertNotIn("i" * 43, str(notice))
            with self.assertRaises(UserError):
                self.controller.account_activate(name="Invited admin", password="request-local-password", accept_terms=False)
            result = self.controller.account_activate(
                name="Invited admin", password="request-local-password", accept_terms=True,
                terms_version="2", privacy_version="3",
            )
        self.assertEqual(result["state"], "approval_required")
        self.assertNotIn("must-not-reach-browser", str(result))
        self.assertNotIn("request-local-password", str(result))

    def _linked(self):
        self._pending()
        def confirm(url, **kwargs):
            response = Mock(status_code=200)
            if not url.endswith("/approve"):
                response.json.return_value = {"state": "approval_required" if url.endswith("/login") else "authorization_required"}
                return response
            response.json.return_value = {
                "installationId": kwargs["json"]["installationId"],
                "accountId": "account_test_1234", "token": "x" * 48,
                "scopes": ["phone-system:manage"],
                "expiresAt": (fields.Datetime.now() + timedelta(hours=1)).isoformat() + "Z",
            }
            return response
        with patch("requests.post", side_effect=confirm):
            self.controller.account_confirm(code="123456")
            self.controller.account_login(password="request-local-password")
            return self.controller.account_approve(accept=True)

    def test_expired_authorization_can_start_a_fresh_verification(self):
        self._linked()
        later = fields.Datetime.now() + timedelta(hours=2)
        response = Mock(status_code=202)
        response.json.return_value = {
            "verificationId": "verify_renew_1234",
            "expiresAt": (later + timedelta(minutes=10)).isoformat() + "Z",
        }
        with patch.object(fields.Datetime, "now", return_value=later), \
                patch("requests.post", return_value=response), \
                patch("odoo.addons.phone_system.models.gcna_account.datetime") as clock:
            clock.now.return_value = later.replace(tzinfo=timezone.utc)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(self.controller.account_status()["state"], "expired")
            self.assertEqual(self.controller.account_request(email="owner@example.com")["state"],
                             "verification_pending")

    def test_authorization_revoked_by_gcna_can_be_linked_again(self):
        for status_code in (401, 403):
            proof = None

            def capture(url, **kwargs):
                nonlocal proof
                proof = kwargs["json"]["installationProof"]
                response = Mock(status_code=202)
                response.json.return_value = {
                    "verificationId": "verify_test_1234",
                    "expiresAt": (fields.Datetime.now() + timedelta(minutes=10)).isoformat() + "Z",
                }
                return response
            with patch("requests.post", side_effect=capture):
                self.controller.account_request(email="owner@example.com")
            first_proof = proof
            with patch("requests.post", side_effect=lambda url, **kwargs: Mock(
                    status_code=200, json=Mock(return_value={"state": "authorization_required"}))):
                self.controller.account_confirm(code="123456")
            approve = Mock(status_code=200)
            approve.json.return_value = {
                "installationId": self.env.ref("phone_system.gcna_installation").installation_id,
                "accountId": "account_test_1234", "token": "x" * 48,
                "scopes": ["phone-system:manage"],
                "expiresAt": (fields.Datetime.now() + timedelta(hours=1)).isoformat() + "Z",
            }
            with patch("requests.post", side_effect=[Mock(status_code=200, json=Mock(
                    return_value={"state": "approval_required"})), approve]):
                self.controller.account_login(password="request-local-password")
                self.assertEqual(self.controller.account_approve(accept=True)["state"], "linked")
            with patch("requests.post", return_value=Mock(status_code=status_code)):
                resumed = self.controller.account_resume()
            self.assertEqual(resumed["state"], "revoked")
            self.assertEqual(self.controller.account_status()["state"], "unlinked")
            self.assertFalse(self.env.ref("phone_system.gcna_installation").token_ciphertext)
            with patch("requests.post", side_effect=capture):
                self.assertEqual(self.controller.account_request(email="owner@example.com")["state"],
                                 "verification_pending")
            self.assertEqual(proof, first_proof)
            self.env.ref("phone_system.gcna_installation").write({
                "verification_id": False, "verification_expires_at": False,
            })

    def test_rebinding_after_revocation_keeps_the_original_account(self):
        self._linked()
        with patch("requests.post", return_value=Mock(status_code=401)):
            self.controller.account_resume()
        record = self.env.ref("phone_system.gcna_installation")
        self.assertEqual(record.account_id, "account_test_1234")

    def test_unreachable_gcna_does_not_discard_stored_authorization(self):
        self._linked()
        for failure in (requests.Timeout(), Mock(status_code=500)):
            with patch("requests.post", side_effect=[failure]), self.assertRaises(UserError):
                self.controller.account_resume()
            self.assertEqual(self.controller.account_status()["state"], "linked")

    def test_updated_terms_pause_the_installation_but_keep_its_authorization(self):
        self._linked()
        paused = Mock(status_code=403)
        paused.json.return_value = {"error": {"code": "TERMS_ACCEPTANCE_REQUIRED"}}
        with patch("requests.post", return_value=paused):
            resumed = self.controller.account_resume()
        self.assertEqual(resumed["state"], "terms_required")
        self.assertIn("accept the updated terms", resumed["message"])
        self.assertTrue(self.env.ref("phone_system.gcna_installation").token_ciphertext)
        record = self.env.ref("phone_system.gcna_installation")
        linked = Mock(status_code=200)
        linked.json.return_value = {"state": "linked", "installationId": record.installation_id,
                                    "accountId": "account_test_1234", "scopes": ["phone-system:manage"]}
        with patch("requests.post", return_value=linked):
            self.assertEqual(self.controller.account_resume()["state"], "linked")

    def test_callers_cannot_read_or_change_installation_linking(self):
        self.request.env = self.env(user=self.caller.id)
        for method, kwargs in (
            (self.controller.account_status, {}),
            (self.controller.account_request, {"email": "owner@example.com"}),
            (self.controller.account_confirm, {"code": "123456"}),
            (self.controller.account_login, {"password": "request-local-password"}),
            (self.controller.account_mfa, {"code": "123456"}),
            (self.controller.account_approve, {"accept": True}),
        ):
            with self.assertRaises(AccessError), patch("requests.post") as post:
                method(**kwargs)
            post.assert_not_called()

    def test_invalid_inputs_do_not_contact_gcna(self):
        with patch("requests.post") as post:
            for email in (None, True, "", "invalid", "a@b.example\n", "owner@host"):
                with self.assertRaises(UserError):
                    self.controller.account_request(email=email)
            for code in (None, True, "123", "1234567", "123456\n", "abcdef"):
                with self.assertRaises(UserError):
                    self.controller.account_confirm(code=code)
            post.assert_not_called()

    def test_wrong_scope_or_installation_does_not_link_an_account(self):
        self._pending()
        response = Mock(status_code=200)
        response.json.return_value = {"state": "authorization_required"}
        with patch("requests.post", return_value=response):
            self.controller.account_confirm(code="123456")
        response.json.return_value = {"state": "approval_required"}
        with patch("requests.post", return_value=response):
            self.controller.account_login(password="request-local-password")
        for payload in (
            {"installationId": "other_installation"},
            {"scopes": ["operator:admin"]},
            {"expiresAt": "2000-01-01T00:00:00Z"},
            {"token": ""},
        ):
            response = Mock(status_code=200)
            def confirm(url, **kwargs):
                response.json.return_value = {
                    "installationId": kwargs["json"]["installationId"],
                    "accountId": "account_test_1234", "token": "x" * 48,
                    "scopes": ["phone-system:manage"],
                    "expiresAt": (fields.Datetime.now() + timedelta(days=1)).isoformat() + "Z",
                    **payload,
                }
                return response
            with patch("requests.post", side_effect=confirm), self.assertRaises(UserError):
                self.controller.account_approve(accept=True)
            self.assertEqual(self.controller.account_status()["state"], "approval_required")

    def test_delivery_failure_never_creates_a_pending_or_linked_account(self):
        with patch("requests.post", side_effect=requests.Timeout("synthetic upstream failure")):
            with self.assertRaises(UserError):
                self.controller.account_request(email="owner@example.com")
            self.assertEqual(self.controller.account_status()["state"], "unlinked")

    def test_request_retry_preserves_installation_proof_after_a_lost_response(self):
        response = Mock(status_code=202)
        response.json.return_value = {
            "verificationId": "verify_retry_1234",
            "expiresAt": (fields.Datetime.now() + timedelta(minutes=10)).isoformat() + "Z",
        }
        with patch("requests.post", side_effect=[requests.Timeout(), response]) as post:
            with self.assertRaises(UserError):
                self.controller.account_request(email="owner@example.com")
            self.controller.account_request(email="owner@example.com")
            self.assertEqual(post.call_args_list[0].kwargs["json"]["installationProof"],
                             post.call_args_list[1].kwargs["json"]["installationProof"])

    def test_rejected_confirmation_remains_pending_and_does_not_echo_upstream_secrets(self):
        self._pending()
        response = Mock(status_code=403, text="sensitive upstream response")
        with patch("requests.post", return_value=response), self.assertRaises(UserError) as error:
            self.controller.account_confirm(code="123456")
        self.assertNotIn("sensitive", str(error.exception))
        self.assertEqual(self.controller.account_status()["state"], "verification_pending")

    def test_pending_verification_expiry_requires_a_new_code(self):
        self._pending()
        later = fields.Datetime.now() + timedelta(minutes=11)
        with patch.object(fields.Datetime, "now", return_value=later), patch("requests.post") as post:
            self.assertEqual(self.controller.account_status()["state"], "unlinked")
            with self.assertRaises(UserError):
                self.controller.account_confirm(code="123456")
            post.assert_not_called()

    def test_email_and_code_patterns_reject_trailing_newlines(self):
        with patch("requests.post") as post:
            with self.assertRaises(UserError):
                self.controller.account_request(email="owner@example.com\n")
            post.assert_not_called()

    def test_a_fresh_database_creates_one_key_and_needs_no_server_setup(self):
        parameters = self.env["ir.config_parameter"].sudo()
        with patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": ""}):
            first = installation_key(self.env)
            self.assertEqual(installation_key(self.env), first)
            self.assertEqual(parameters.get_param(KEY_PARAMETER), first)
            cipher = credential_cipher(self.env)
            self.assertEqual(cipher.decrypt(cipher.encrypt(b"secret")), b"secret")

    def test_an_operator_key_wins_over_the_database_key(self):
        operator = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": operator}):
            self.assertEqual(installation_key(self.env), operator)

    def test_a_fresh_installation_can_link_without_server_variables(self):
        self.assertEqual(self.controller.account_status()["state"], "unlinked")

    def test_an_invalid_operator_key_blocks_verification_before_delivery(self):
        with patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": "invalid"}), patch("requests.post") as post:
            with self.assertRaises(UserError):
                self.controller.account_request(email="owner@example.com")
            post.assert_not_called()

    def test_wrong_encryption_key_never_returns_linked_status(self):
        self._linked()
        with patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode()}):
            with self.assertRaises(UserError):
                self.controller.account_status()

    def test_pending_requests_are_not_silently_replaced_or_resent(self):
        pending = self._pending()
        with patch("requests.post") as post, self.assertRaises(UserError):
            self.controller.account_request(email="different@example.com")
        post.assert_not_called()
        self.assertEqual(self.controller.account_status(), pending)

    def test_upstream_redirects_and_invalid_responses_are_not_followed_or_accepted(self):
        for response in (Mock(status_code=302), Mock(status_code=202)):
            response.json.return_value = {"verificationId": "../other", "expiresAt": "never"}
            with patch("requests.post", return_value=response) as post, self.assertRaises(UserError):
                self.controller.account_request(email="owner@example.com")
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            self.assertEqual(post.call_args.args[0], "https://gcn-alliance.com/v1/account-verifications")
            self.assertEqual(self.controller.account_status()["state"], "unlinked")


@tagged("post_install", "-at_install", "phone_system")
class TestGCNAAccountHTTP(HttpCase):
    def test_authenticated_routes_reject_callers_and_allow_admin_safe_status(self):
        caller = self.env["res.users"].create({
            "name": "HTTP phone caller", "login": "gcna-http-caller",
            "password": "synthetic-http-password",
            "groups_id": [(6, 0, [self.env.ref("phone_system.group_pilot").id])],
        })
        manager = self.env["res.users"].create({
            "name": "HTTP phone administrator", "login": "gcna-http-admin",
            "password": "synthetic-http-password",
            "groups_id": [(6, 0, [self.env.ref("base.group_system").id])],
        })
        self.authenticate(caller.login, "synthetic-http-password")
        for route, params in (
            ("/vodia/account/status", {}),
            ("/vodia/account/resume", {}),
            ("/vodia/account/request", {"email": "owner@example.com"}),
            ("/vodia/account/confirm", {"code": "123456"}),
            ("/vodia/account/login", {"password": "request-local-password"}),
            ("/vodia/account/mfa", {"code": "123456"}),
            ("/vodia/account/approve", {"accept": True}),
            ("/vodia/account/invitation", {"token": "i" * 43}),
            ("/vodia/account/activate", {"name": "Invited admin", "password": "request-local-password"}),
        ):
            response = self.url_open(route, data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "params": params, "id": 1,
            }), headers={"Content-Type": "application/json"})
            self.assertEqual(response.json()["error"]["data"]["name"], "odoo.exceptions.AccessError")
        self.authenticate(manager.login, "synthetic-http-password")
        response = self.url_open("/vodia/account/status", data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "params": {}, "id": 2,
        }), headers={"Content-Type": "application/json"})
        self.assertEqual(response.json()["result"]["state"], "unlinked")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("token", response.json()["result"])
