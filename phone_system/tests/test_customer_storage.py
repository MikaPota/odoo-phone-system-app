import json
import os
from datetime import date, datetime
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCustomerStorage(TransactionCase):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"VODIA_CREDENTIAL_KEY": Fernet.generate_key().decode()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = self.env["res.users"].create({"name": "Storage caller", "login": "storage-caller"})
        self.connection = self.env["vodia.pilot.connection"].create({
            "name": "Storage test", "pbx_host": "abc123.pbx.example", "storage_endpoint": "https://s3.example.com",
            "storage_bucket": "customer-bucket", "storage_region": "eu-central-1", "storage_access_key": "AKIDEXAMPLE",
            "storage_secret": "very-secret-key",
        })
        self.env["vodia.pilot.mapping"].create(
            {"user_id": self.user.id, "connection_id": self.connection.id, "extension": "201"})

    def _call(self, uid, started, name=False):
        partner = self.env["res.partner"].create({"name": name}).id if name else False
        return self.env["vodia.pilot.call"].create({
            "call_uid": uid, "user_id": self.user.id, "company_id": self.connection.company_id.id,
            "direction": "outgoing", "peer_number": "+49305550100", "outcome": "answered",
            "started_at": started, "duration": 60, "peer_partner_id": partner,
        })

    def _put(self, status=200):
        return patch("requests.put", return_value=Mock(status_code=status))

    def _keys(self, put):
        return [call.args[0].split("customer-bucket/", 1)[1] for call in put.call_args_list]

    def test_the_secret_key_is_encrypted_and_never_readable(self):
        self.assertFalse(self.connection.storage_secret)
        self.assertNotIn("very-secret-key", self.connection.sudo().storage_secret_ciphertext)

    def test_leaving_the_secret_empty_keeps_the_stored_key(self):
        stored = self.connection.sudo().storage_secret_ciphertext
        self.connection.storage_secret = False
        self.assertEqual(self.connection.sudo().storage_secret_ciphertext, stored)

    def test_the_url_must_be_https_without_a_path(self):
        for url in ("http://s3.example.com", "https://s3.example.com/bucket", "s3.example.com"):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self.connection.storage_endpoint = url

    def test_uploads_are_signed_and_do_not_contain_the_secret(self):
        with self._put() as put:
            self.connection.action_storage_test()
        headers = put.call_args.kwargs["headers"]
        self.assertRegex(headers["Authorization"],
                         r"^AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/\d{8}/eu-central-1/s3/aws4_request, "
                         r"SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}$")
        self.assertNotIn("very-secret-key", json.dumps(headers))
        self.assertFalse(put.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.connection.storage_state, "ok")

    def test_a_refused_upload_is_reported_without_credentials(self):
        with self._put(403):
            self.connection.action_storage_test()
        self.assertEqual(self.connection.storage_state, "failed")
        self.assertIn("403", self.connection.storage_message)
        self.assertNotIn("AKIDEXAMPLE", self.connection.storage_message)

    def test_an_unreachable_storage_is_reported(self):
        with patch("requests.put", side_effect=requests.ConnectionError("boom")):
            self.connection.action_storage_test()
        self.assertEqual(self.connection.storage_state, "failed")

    def test_only_administrators_may_test_storage(self):
        with self.assertRaises(AccessError):
            self.connection.with_user(self.user).action_storage_test()

    def test_each_past_day_goes_to_its_own_file_and_today_waits(self):
        self._call("a", datetime(2026, 10, 29, 9))
        self._call("b", datetime(2026, 10, 30, 9))
        self._call("c", datetime(2026, 10, 31, 9))
        with self._put() as put:
            self.env["vodia.pilot.connection"]._storage_export_all(date(2026, 10, 31))
        self.assertEqual(self._keys(put), [
            "phone-system/abc123.pbx.example/calls/2026-10-29.csv",
            "phone-system/abc123.pbx.example/calls/2026-10-30.csv",
            "phone-system/abc123.pbx.example/backup/2026-10-31.json",
        ])
        self.assertEqual(self.connection.storage_exported_on, date(2026, 10, 30))

    def test_the_next_run_does_not_repeat_saved_days(self):
        self._call("a", datetime(2026, 10, 29, 9))
        with self._put():
            self.connection._storage_export(date(2026, 10, 30))
        with self._put() as put:
            self.connection._storage_export(date(2026, 10, 31))
        self.assertFalse([key for key in self._keys(put) if "/calls/" in key])

    def test_a_failed_day_is_retried_on_the_next_run(self):
        self._call("a", datetime(2026, 10, 29, 9))
        with self._put(500):
            self.connection._storage_export(date(2026, 10, 30))
        self.assertFalse(self.connection.storage_exported_on)
        self.assertEqual(self.connection.storage_state, "failed")
        with self._put() as put:
            self.connection._storage_export(date(2026, 10, 30))
        self.assertIn("phone-system/abc123.pbx.example/calls/2026-10-29.csv", self._keys(put))
        self.assertEqual(self.connection.storage_state, "ok")

    def test_the_backup_never_contains_credentials(self):
        mapping = self.env["vodia.pilot.mapping"].search([("connection_id", "=", self.connection.id)])
        mapping.web_password = "individual-password-16"
        backup = json.loads(self.connection._storage_backup())
        self.assertEqual(backup["users"][0]["extension"], "201")
        text = json.dumps(backup)
        self.assertNotIn("individual-password-16", text)
        self.assertNotIn(mapping.sudo().credential_ciphertext, text)
        self.assertNotIn("very-secret-key", text)

    def test_a_contact_name_cannot_run_as_a_formula(self):
        call = self._call("a", datetime(2026, 10, 29, 9), name="=cmd()")
        self.assertIn(b"'=cmd()", self.connection._storage_calls_csv(call))

    def test_another_companys_calls_stay_out(self):
        other = self.env["res.company"].create({"name": "Other company"})
        self.user.company_ids += other
        self.env["vodia.pilot.call"].create({
            "call_uid": "other", "user_id": self.user.id, "company_id": other.id, "direction": "outgoing",
            "peer_number": "+49305550199", "outcome": "answered", "started_at": datetime(2026, 10, 29, 9), "duration": 1,
        })
        with self._put() as put:
            self.connection._storage_export(date(2026, 10, 30))
        self.assertFalse([key for key in self._keys(put) if "/calls/" in key])
