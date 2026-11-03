import json
import os
from datetime import timedelta
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.http import Response
from odoo.tests import TransactionCase, tagged

from odoo.addons.phone_system.controllers.pilot_session import PilotSessionController

LEGAL = {
    "imprint": {"entity": "Example operator", "address": ["Example street 1"], "email": "legal@example.test"},
    "terms": {"version": "2", "url": "https://example.test/terms"},
    "privacy": {"version": "3", "sections": [{"id": "account", "title": "Your account", "text": ["Notice."]}]},
}
CATALOG = {
    "catalogVersion": "0123456789abcdef",
    "plans": [{"planId": "prod_5", "name": "Up to 5 users", "capacity": 5,
               "prices": {"monthly": {"amount": 2900, "currency": "usd"}}}],
    "legal": LEGAL,
}
TOKEN = "t" * 48


def gcna(responses, sent):
    def post(url, **kwargs):
        sent.append((url, kwargs))
        for suffix, (status, payload) in responses.items():
            if url.endswith(suffix):
                response = Mock(status_code=status)
                response.json.return_value = payload(kwargs) if callable(payload) else payload
                return response
        raise AssertionError("Unexpected GCNA call " + url)
    return post


@tagged("post_install", "-at_install", "phone_system")
class TestCheckout(TransactionCase):
    def setUp(self):
        super().setUp()
        # The test database also has German; these tests compare English messages.
        self.env = self.env(context=dict(self.env.context, lang="en_US"))
        self.manager = self.env["res.users"].create({
            "name": "Ada Lovelace", "login": "checkout-manager", "email": "ada@lovelace.test",
            "group_ids": [(6, 0, [self.env.ref("base.group_system").id])],
        })
        self.staff = self.env["res.users"].create([{
            "name": f"Staff {n}", "login": f"checkout-staff-{n}", "company_id": self.env.company.id,
            "company_ids": [(6, 0, [self.env.company.id])],
        } for n in range(3)])
        self.caller = self.env["res.users"].create({
            "name": "Plain caller", "login": "checkout-caller",
            "group_ids": [(6, 0, [self.env.ref("phone_system.group_pilot").id])],
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
        self.sent = []
        installation_id = self.env.ref("phone_system.gcna_installation").installation_id
        expires = (fields.Datetime.now() + timedelta(days=30)).isoformat() + "Z"
        self.paid = {
            "state": "payment_required", "orderId": "order_test_1234", "clientSecret": "cs_test_1_secret_abc",
            "publishableKey": "pk_test_1",
            "installation": {"token": TOKEN, "accountId": "account_test_01", "installationId": installation_id,
                             "scopes": ["phone-system:manage"], "expiresAt": expires},
        }
        self.system = {"id": "system_test_12345678", "code": "SD5TZ7", "hostname": "sd5tz7.pbx.gcn-alliance.com",
                       "name": "Lovelace Analytics", "capacity": 5,
                       "stages": {"paid": {"state": "complete"}, "provisioned": {"state": "complete"}}}
        self.responses = {
            "/v1/catalog": (200, CATALOG),
            "/v1/checkouts": (202, {"checkoutId": "c" * 43, "expiresAt": (fields.Datetime.now() + timedelta(minutes=9)).isoformat() + "Z"}),
            "/confirm": (200, self.paid),
            "/resend": (202, {"checkoutId": "c" * 43, "expiresAt": (fields.Datetime.now() + timedelta(minutes=9)).isoformat() + "Z"}),
            "/v1/phone-systems": (200, {"systems": [self.system]}),
            "/users": (200, lambda kwargs: {
                "origin": "https://sd5tz7.pbx.gcn-alliance.com", "tenant": "sd5tz7.pbx.gcn-alliance.com",
                "users": [{"reference": u["reference"], "extension": str(201 + i), "webPassword": "w" * 32}
                          for i, u in enumerate(kwargs["json"]["users"])],
            }),
        }
        self.post = patch("requests.post", side_effect=gcna(self.responses, self.sent))
        self.post.start()
        self.addCleanup(self.post.stop)

    def controller_country(self):
        code = self.env.company.country_id.code
        return {"country": code} if code else {}

    def _start(self, **values):
        return self.controller.checkout_start(**{
            "plan_id": "prod_5", "term": "monthly", "catalog_version": "0123456789abcdef",
            "email": "ada@lovelace.test", "admin_name": "Ada Lovelace", "company_name": "Lovelace Analytics",
            "domain": "lovelace.test", "accept_terms": True, "terms_version": "2", "privacy_version": "3", **values,
        })

    def test_the_company_country_reaches_gcna_so_dach_buyers_see_euro_prices(self):
        self.env.company.country_id = self.env.ref("base.de")
        self.controller.checkout_catalog()
        self.assertEqual(self.sent[-1][1]["json"], {"country": "DE"})
        self._start()
        self.assertEqual(self.sent[-1][1]["json"]["country"], "DE")
        self.env.company.country_id = False
        self.controller.checkout_catalog()
        self.assertEqual(self.sent[-1][1]["json"], {})

    def test_catalog_offers_plans_and_prefills_the_buyer_from_odoo(self):
        result = self.controller.checkout_catalog()
        self.assertEqual(result["plans"][0]["capacity"], 5)
        self.assertEqual(result["legal"]["terms"]["version"], "2")
        self.assertEqual(result["buyer"]["email"], "ada@lovelace.test")
        self.assertEqual(result["buyer"]["admin_name"], "Ada Lovelace")
        self.assertEqual(result["buyer"]["company_name"], self.env.company.name)

    def test_checkout_verifies_email_then_hands_payment_to_stripe_and_stores_the_installation_credential(self):
        started = self._start()
        self.assertEqual(started["state"], "verification_pending")
        body = self.sent[-1][1]["json"]
        self.assertEqual(body["planId"], "prod_5")
        self.assertGreaterEqual(len(body["installationProof"]), 32)
        self.assertNotIn(body["installationProof"], str(started))
        confirmed = self.controller.checkout_confirm(code="123456")
        self.assertEqual(confirmed, {"state": "payment_required", "client_secret": "cs_test_1_secret_abc", "publishable_key": "pk_test_1"})
        self.assertNotIn(TOKEN, str(confirmed))
        record = self.env.ref("phone_system.gcna_installation")
        self.assertEqual(record.account_id, "account_test_01")
        self.assertEqual(record._authorization(), TOKEN)

    def test_paid_system_users_get_encrypted_mappings_on_the_new_tenant(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        systems = self.controller.phone_systems()
        self.assertEqual(systems["systems"][0]["code"], "SD5TZ7")
        self.assertEqual(self.sent[-1][1]["headers"]["Authorization"], "Bearer " + TOKEN)
        result = self.controller.phone_system_users(system_id="system_test_12345678", user_ids=self.staff.ids)
        self.assertEqual(sorted(result["extensions"].values()), ["201", "202", "203"])
        connection = self.env["vodia.pilot.connection"].search([("gcna_system_id", "=", "system_test_12345678")])
        self.assertEqual(connection.pbx_host, "sd5tz7.pbx.gcn-alliance.com")
        mappings = self.env["vodia.pilot.mapping"].search([("connection_id", "=", connection.id)])
        self.assertEqual(mappings.user_id, self.staff)
        self.assertTrue(all(mappings.mapped("credential_configured")))
        self.assertNotIn("w" * 32, str(result))
        self.assertTrue(all(user.has_group("phone_system.group_pilot") for user in self.staff))
        references = [u["reference"] for u in self.sent[-1][1]["json"]["users"]]
        self.assertEqual(references, [f"user-{uid}" for uid in self.staff.ids])

    def test_a_system_reports_its_license_key_and_whether_it_is_valid(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        self.assertNotIn("license", self.controller.phone_systems()["systems"][0])
        for license in ({"key": "5V8C277J14FW", "valid": True}, {"key": "5XEPZ5TS7ZYG", "valid": False}, {"key": None, "valid": None}):
            self.system["license"] = license
            self.assertEqual(self.controller.phone_systems()["systems"][0]["license"], license)
        for broken in ({"key": "<b>x</b>", "valid": True}, {"key": "5V8C277J14FW", "valid": "yes"}, "5V8C277J14FW"):
            self.system["license"] = broken
            with self.assertRaises(UserError):
                self.controller.phone_systems()

    def test_a_cancelled_system_reports_when_its_tenant_is_deleted(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        self.assertNotIn("cancelled", self.controller.phone_systems()["systems"][0])
        cancelled = {"at": "2026-10-30T20:00:00.000Z", "deleteAt": "2026-11-29T20:00:00.000Z", "deleted": False}
        self.system["cancelled"] = cancelled
        self.assertEqual(self.controller.phone_systems()["systems"][0]["cancelled"], cancelled)
        for broken in ({**cancelled, "deleted": "no"}, {**cancelled, "deleteAt": "<b>soon</b>"}, "cancelled"):
            self.system["cancelled"] = broken
            with self.assertRaises(UserError):
                self.controller.phone_systems()

    def test_assigning_a_user_moves_their_single_company_phone_to_the_new_system(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        self.controller.phone_systems()
        # The first staff member already holds a phone on another system in this company.
        other = self.env["vodia.pilot.connection"].create({
            "name": "Existing pilot", "company_id": self.env.company.id,
            "pbx_host": "odoo-pilot.pbx.gcn-alliance.com", "gcna_system_id": "existing_system_01",
        })
        prior = self.env["vodia.pilot.mapping"].create({
            "user_id": self.staff[0].id, "connection_id": other.id, "extension": "299",
        })
        prior.web_password = "x" * 32
        result = self.controller.phone_system_users(system_id="system_test_12345678", user_ids=self.staff.ids)
        self.assertEqual(result["extensions"][self.staff[0].id], "201")
        moved = self.env["vodia.pilot.mapping"].search([
            ("user_id", "=", self.staff[0].id), ("company_id", "=", self.env.company.id)])
        self.assertEqual(len(moved), 1, "a user keeps exactly one phone per company")
        self.assertEqual(moved.connection_id.gcna_system_id, "system_test_12345678")
        self.assertEqual(moved.extension, "201")
        self.assertTrue(moved.credential_configured)

    def test_the_subscription_page_comes_from_gcna_for_administrators_only(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        self.responses["/billing-portal"] = (200, {"url": "https://billing.stripe.com/session/x"})
        result = self.controller.phone_system_billing("system-12345")
        self.assertEqual(result, {"url": "https://billing.stripe.com/session/x"})
        self.assertTrue(self.sent[-1][1]["json"]["returnUrl"].endswith("/odoo"))
        self.request.env = self.env(user=self.caller.id)
        with self.assertRaises(AccessError):
            self.controller.phone_system_billing("system-12345")

    def test_a_link_to_anywhere_else_is_refused(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        self.responses["/billing-portal"] = (200, {"url": "https://evil.example/x"})
        with self.assertRaises(UserError):
            self.controller.phone_system_billing("system-12345")

    def test_checkout_is_admin_only_and_open_without_server_variables(self):
        self.request.env = self.env(user=self.caller.id)
        with self.assertRaises(AccessError):
            self.controller.checkout_catalog()
        self.request.env = self.env(user=self.manager.id)
        self.assertTrue(self.controller.dashboard()["managed_setup"]["available"])

    def test_invalid_checkout_input_never_reaches_gcna(self):
        before = len(self.sent)
        for values in ({"accept_terms": False}, {"email": "invalid"}, {"term": "weekly"}, {"domain": "not a domain"}):
            with self.assertRaises(UserError):
                self._start(**values)
        with self.assertRaises(UserError):
            self.controller.checkout_confirm(code="12345")
        self.assertEqual(len(self.sent), before)

    def test_resend_reopens_the_window_and_then_the_code_confirms(self):
        self._start()
        record = self.env.ref("phone_system.gcna_installation")
        record.checkout_expires_at = fields.Datetime.now() - timedelta(minutes=1)
        result = self.controller.checkout_resend()
        self.assertEqual(result["state"], "verification_pending")
        self.assertTrue(self.sent[-1][0].endswith("/" + ("c" * 43) + "/resend"))
        self.assertGreater(record.checkout_expires_at, fields.Datetime.now())
        confirmed = self.controller.checkout_confirm(code="123456")
        self.assertEqual(confirmed["state"], "payment_required")

    def test_resend_without_a_checkout_is_refused_before_reaching_gcna(self):
        before = len(self.sent)
        with self.assertRaises(UserError):
            self.controller.checkout_resend()
        self.assertEqual(len(self.sent), before)

    def test_gcna_conflicts_are_explained_without_upstream_detail(self):
        self.responses["/v1/checkouts"] = (409, {"error": {"code": "COMPANY_EXISTS", "message": "secret upstream detail"}})
        with self.assertRaises(UserError) as error:
            self._start()
        self.assertIn("already has an account", str(error.exception))
        self.assertNotIn("secret upstream detail", str(error.exception))

    def test_a_linked_account_buys_another_phone_system_straight_to_payment(self):
        self.assertFalse(self.controller.checkout_catalog()["linked"])
        before = len(self.sent)
        with self.assertRaises(UserError):
            self.controller.checkout_order(plan_id="prod_5", term="monthly", catalog_version="0123456789abcdef")
        self.assertEqual(len(self.sent), before, "an unlinked installation buys through the email checkout")
        self._start()
        self.controller.checkout_confirm(code="123456")
        self.assertTrue(self.controller.checkout_catalog()["linked"])
        self.responses["/v1/phone-systems/orders"] = (200, {
            "state": "payment_required", "orderId": "order_test_5678", "clientSecret": "cs_test_2_secret_abc", "publishableKey": "pk_test_1"})
        result = self.controller.checkout_order(plan_id="prod_5", term="monthly", catalog_version="0123456789abcdef")
        self.assertEqual(result, {"state": "payment_required", "client_secret": "cs_test_2_secret_abc",
                                  "publishable_key": "pk_test_1", "order_id": "order_test_5678"})
        url, request = self.sent[-1]
        self.assertTrue(url.endswith("/v1/phone-systems/orders"))
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(request["json"], {"planId": "prod_5", "term": "monthly", "catalogVersion": "0123456789abcdef",
                                           "name": self.env.company.name, **self.controller_country()})
        for values in ({"term": "weekly"}, {"plan_id": ""}):
            with self.assertRaises(UserError):
                self.controller.checkout_order(**{"plan_id": "prod_5", "term": "monthly", "catalog_version": "0123456789abcdef", **values})

    def test_each_system_names_the_order_that_bought_it(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        self.system["orderId"] = "order_test_5678"
        self.assertEqual(self.controller.phone_systems()["systems"][0]["order_id"], "order_test_5678")

    def test_setup_progress_from_gcna_is_shown_in_the_users_language(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        message = "Your phone system is created and is waiting for its license. You can leave this page and come back."
        self.system["stages"]["provisioned"] = {"state": "in_progress", "message": message}
        self.assertEqual(self.controller.phone_systems()["systems"][0]["stages"]["provisioned"]["message"], message)
        self.system["stages"]["provisioned"]["message"] = "Something GCNA added later."
        self.assertEqual(self.controller.phone_systems()["systems"][0]["stages"]["provisioned"]["message"], "Something GCNA added later.")
        if "de_DE" in dict(self.env["res.lang"].get_installed()):
            self.system["stages"]["provisioned"]["message"] = message
            self.request.env = self.request.env(context=dict(self.request.env.context, lang="de_DE"))
            self.assertIn("wartet auf ihre Lizenz", self.controller.phone_systems()["systems"][0]["stages"]["provisioned"]["message"])

    def test_a_purchase_gcna_no_longer_knows_is_dropped_when_resumed(self):
        self._start()
        self.responses["/status"] = (400, {"error": {"code": "CHECKOUT_EXPIRED"}})
        self.assertEqual(self.controller.checkout_resume(), {"state": "none"})
        self.assertFalse(self.env.ref("phone_system.gcna_installation").checkout_id)

    def test_administrators_download_call_records_as_csv_and_through_the_api(self):
        self._start()
        self.controller.checkout_confirm(code="123456")
        self.responses["/call-records"] = (200, {"month": "2026-10", "records": [
            {"id": "c1", "callId": "x1", "start": "2026-10-18T09:00:00.000Z", "answered": "2026-10-18T09:00:05.000Z",
             "end": "2026-10-18T09:03:00.000Z", "from": "=HYPERLINK(\"x\")", "to": "<sip:+4930123@t>", "abandoned": False},
            {"id": "c2", "callId": "x2", "start": "2026-10-19T10:00:00Z", "answered": None,
             "end": "2026-10-19T10:00:20Z", "from": "<sip:202@t>", "to": "<sip:201@t>", "abandoned": True},
        ]})
        self.request.make_response = lambda data, headers: Response(data, headers=headers)
        response = self.controller.phone_system_call_records(system_id="system_test_12345678", month="2026-10")
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="call-records-2026-10.csv"')
        lines = response.get_data(as_text=True).splitlines()
        self.assertEqual(lines[0], "start_utc,answered_utc,end_utc,duration_s,talk_s,from,to,abandoned,call_id,record_id")
        self.assertIn(",180,175,", lines[1])
        self.assertIn("'=HYPERLINK", lines[1], "a formula from the PBX stays text in a spreadsheet")
        self.assertTrue(lines[2].endswith(",20,,<sip:202@t>,<sip:201@t>,yes,x2,c2"))
        self.assertEqual(self.sent[-1][1]["json"], {"month": "2026-10"})
        self.assertEqual(self.sent[-1][1]["headers"]["Authorization"], "Bearer " + TOKEN)
        api = self.env["vodia.pilot.call"].with_user(self.manager).phone_system_call_records("system_test_12345678", "2026-10")
        self.assertEqual([row["id"] for row in api], ["c1", "c2"])
        before = len(self.sent)
        with self.assertRaises(UserError):
            self.controller.phone_system_call_records(system_id="system_test_12345678", month="October")
        self.assertEqual(len(self.sent), before, "an invalid month never reaches GCNA")
        self.responses["/call-records"] = (422, {"error": {"code": "INVALID_MONTH"}})
        with self.assertRaises(UserError) as older:
            self.env["vodia.pilot.call"].with_user(self.manager).phone_system_call_records("system_test_12345678", "2026-08")
        self.assertIn("current and the previous month", str(older.exception))
        with self.assertRaises(AccessError):
            self.env["vodia.pilot.call"].with_user(self.caller).phone_system_call_records("system_test_12345678", "2026-10")
        self.request.env = self.env(user=self.caller.id)
        with self.assertRaises(AccessError):
            self.controller.phone_system_call_records(system_id="system_test_12345678", month="2026-10")
