from datetime import timedelta
from unittest.mock import Mock, patch

from odoo import fields
from odoo.tests import TransactionCase, tagged


def iso(delta):
    return (fields.Datetime.now() + delta).isoformat() + "Z"


@tagged("post_install", "-at_install")
class TestRenew(TransactionCase):
    """GCNA's link lasts 30 days; with fewer than 7 left the installation renews it on its own."""

    def setUp(self):
        super().setUp()
        self.account = self.env["vodia.gcna.account"].sudo()
        self.record = self.env.ref("gcna_link.gcna_installation")
        self.record._ensure_proof()
        self.record._store_authorization({
            "token": "o" * 48, "accountId": "org_shop_1234", "installationId": self.record.installation_id,
            "scopes": ["phone-system:manage"], "expiresAt": iso(timedelta(days=30)),
        })

    def _reply(self, status, payload):
        response = Mock(status_code=status)
        response.json.return_value = payload
        return response

    def _ends_in(self, delta):
        self.record.token_expires_at = fields.Datetime.now() + delta

    def test_a_link_with_more_than_seven_days_left_is_left_alone(self):
        self._ends_in(timedelta(days=8))
        with patch("requests.post") as post:
            self.assertFalse(self.account._renew_if_due())
        post.assert_not_called()

    def test_a_link_with_fewer_than_seven_days_left_is_renewed(self):
        self._ends_in(timedelta(days=6))
        sent = []

        def gcna(url, **kwargs):
            sent.append((url, kwargs))
            return self._reply(200, {
                "token": "n" * 48, "accountId": "org_shop_1234", "installationId": self.record.installation_id,
                "scopes": ["phone-system:manage"], "expiresAt": iso(timedelta(days=30)),
            })

        with patch("requests.post", side_effect=gcna):
            self.assertTrue(self.account._renew_if_due())
        url, kwargs = sent[0]
        self.assertTrue(url.endswith("/v1/installation/renew"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + "o" * 48)
        self.assertEqual(kwargs["json"]["installationId"], self.record.installation_id)
        self.assertEqual(kwargs["json"]["installationProof"], self.record._installation_proof())
        self.assertEqual(self.record._authorization(), "n" * 48)
        self.assertGreater(self.record.token_expires_at, fields.Datetime.now() + timedelta(days=29))

    def test_a_refused_or_unreachable_renewal_keeps_the_stored_link(self):
        self._ends_in(timedelta(days=2))
        for reply in (self._reply(401, {"error": {"code": "UNAUTHORIZED"}}), self._reply(502, {})):
            with patch("requests.post", return_value=reply):
                self.assertFalse(self.account._renew_if_due())
            self.assertEqual(self.record._authorization(), "o" * 48)

    def test_an_expired_link_is_not_renewed(self):
        self._ends_in(timedelta(minutes=-1))
        with patch("requests.post") as post:
            self.assertFalse(self.account._renew_if_due())
        post.assert_not_called()

    def test_the_daily_job_is_registered(self):
        cron = self.env.ref("gcna_link.cron_renew")
        self.assertEqual(cron.interval_type, "days")
        self.assertTrue(cron.active)
