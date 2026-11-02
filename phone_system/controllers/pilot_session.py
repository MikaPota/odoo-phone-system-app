from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request


class PilotSessionController(http.Controller):
    def _account_model(self):
        request.future_response.headers["Cache-Control"] = "no-store"
        request.future_response.headers["Pragma"] = "no-cache"
        user = request.env.user
        if not user.active or user.share or not user.has_group("base.group_system"):
            raise AccessError(request.env._("Only an administrator may manage Phone System setup."))
        return request.env["vodia.gcna.account"].sudo()

    @http.route("/vodia/account/status", type="jsonrpc", auth="user", methods=["POST"])
    def account_status(self):
        return self._account_model()._status()

    @http.route("/vodia/account/resume", type="jsonrpc", auth="user", methods=["POST"])
    def account_resume(self):
        return self._account_model()._resume_status()

    @http.route("/vodia/account/request", type="jsonrpc", auth="user", methods=["POST"])
    def account_request(self, email=None):
        return self._account_model()._request_verification(email)

    @http.route("/vodia/account/confirm", type="jsonrpc", auth="user", methods=["POST"])
    def account_confirm(self, code=None):
        return self._account_model()._confirm_verification(code)

    @http.route("/vodia/account/login", type="jsonrpc", auth="user", methods=["POST"])
    def account_login(self, password=None):
        return self._account_model()._account_step("login", password)

    @http.route("/vodia/account/mfa", type="jsonrpc", auth="user", methods=["POST"])
    def account_mfa(self, code=None):
        return self._account_model()._account_step("mfa", code)

    @http.route("/vodia/account/approve", type="jsonrpc", auth="user", methods=["POST"])
    def account_approve(self, accept=False):
        return self._account_model()._account_step("approve", accept)

    @http.route("/vodia/account/invitation", type="jsonrpc", auth="user", methods=["POST"])
    def account_invitation(self, token=None):
        return self._account_model()._account_step("invitation", token)

    @http.route("/vodia/account/activate", type="jsonrpc", auth="user", methods=["POST"])
    def account_activate(self, name=None, password=None, accept_terms=False, terms_version=None, privacy_version=None):
        return self._account_model()._account_step("activate", {
            "name": name, "password": password, "acceptTerms": accept_terms,
            "termsVersion": terms_version, "privacyVersion": privacy_version,
        })

    @http.route("/vodia/checkout/catalog", type="jsonrpc", auth="user", methods=["POST"])
    def checkout_catalog(self):
        return self._account_model()._checkout_catalog()

    @http.route("/vodia/checkout/start", type="jsonrpc", auth="user", methods=["POST"])
    def checkout_start(self, plan_id=None, term=None, catalog_version=None, email=None, admin_name=None,
                       company_name=None, domain=None, accept_terms=False, terms_version=None, privacy_version=None):
        return self._account_model()._checkout_start({
            "plan_id": plan_id, "term": term, "catalog_version": catalog_version, "email": email,
            "admin_name": admin_name, "company_name": company_name, "domain": domain, "accept_terms": accept_terms,
            "terms_version": terms_version, "privacy_version": privacy_version,
        })

    @http.route("/vodia/checkout/order", type="jsonrpc", auth="user", methods=["POST"])
    def checkout_order(self, plan_id=None, term=None, catalog_version=None):
        return self._account_model()._checkout_order({"plan_id": plan_id, "term": term, "catalog_version": catalog_version})

    @http.route("/vodia/checkout/confirm", type="jsonrpc", auth="user", methods=["POST"])
    def checkout_confirm(self, code=None):
        return self._account_model()._checkout_confirm(code)

    @http.route("/vodia/checkout/resend", type="jsonrpc", auth="user", methods=["POST"])
    def checkout_resend(self):
        return self._account_model()._checkout_resend()

    @http.route("/vodia/checkout/resume", type="jsonrpc", auth="user", methods=["POST"])
    def checkout_resume(self):
        return self._account_model()._checkout_resume()

    @http.route("/vodia/systems", type="jsonrpc", auth="user", methods=["POST"])
    def phone_systems(self):
        return self._account_model()._phone_systems()

    @http.route("/vodia/systems/billing", type="jsonrpc", auth="user", methods=["POST"])
    def phone_system_billing(self, system_id=None):
        return self._account_model()._billing_portal(system_id)

    @http.route("/vodia/systems/call-records", type="http", auth="user", methods=["GET"])
    def phone_system_call_records(self, system_id=None, month=None):
        data = self._account_model()._call_records_csv(system_id, month)
        return request.make_response(data, headers=[
            ("Content-Type", "text/csv; charset=utf-8"),
            ("Content-Disposition", f'attachment; filename="call-records-{month}.csv"'),
            ("Cache-Control", "no-store"),
        ])

    @http.route("/vodia/systems/users", type="jsonrpc", auth="user", methods=["POST"])
    def phone_system_users(self, system_id=None, user_ids=None):
        return self._account_model()._sync_users(system_id, user_ids)

    @http.route("/vodia/pilot/dashboard", type="jsonrpc", auth="user", methods=["POST"])
    def dashboard(self):
        request.future_response.headers["Cache-Control"] = "no-store"
        if not request.env.user.has_group("base.group_system"):
            raise AccessError(request.env._("Only an administrator may manage Phone System setup."))
        return {
            "can_manage": True,
            "managed_setup": {"available": True, "message": ""},
            "existing_setup": {"available": True, "message": ""},
        }

    def _mapping_model(self, company_id):
        if type(company_id) is not int or company_id not in request.env.user.company_ids.ids:
            raise AccessError(request.env._("The selected company is not authorized for your phone."))
        return request.env["vodia.pilot.mapping"].with_context(allowed_company_ids=[company_id])

    @http.route("/vodia/pilot/configuration", type="jsonrpc", auth="user", methods=["POST"])
    def configuration(self, company_id=None):
        request.future_response.headers["Cache-Control"] = "no-store"
        request.future_response.headers["Pragma"] = "no-cache"
        return self._mapping_model(company_id)._browser_configuration()

    @http.route("/vodia/pilot/call", type="jsonrpc", auth="user", methods=["POST"])
    def call(self, company_id=None, entry=None):
        request.future_response.headers["Cache-Control"] = "no-store"
        return self._mapping_model(company_id).env["vodia.pilot.call"]._record_call(entry)

    @http.route("/vodia/pilot/contact_card", type="jsonrpc", auth="user", methods=["POST"])
    def contact_card(self, company_id=None, email=None):
        request.future_response.headers["Cache-Control"] = "no-store"
        return self._mapping_model(company_id)._send_contact_card(email)

    @http.route("/vodia/pilot/contact_card/recipient", type="jsonrpc", auth="user", methods=["POST"])
    def contact_card_recipient(self, company_id=None):
        request.future_response.headers["Cache-Control"] = "no-store"
        return self._mapping_model(company_id)._contact_card_recipient()

    @http.route("/vodia/pilot/contact_card/follow_up", type="jsonrpc", auth="user", methods=["POST"])
    def contact_card_follow_up(self, company_id=None, call_id=None):
        request.future_response.headers["Cache-Control"] = "no-store"
        return self._mapping_model(company_id)._contact_card_follow_up(call_id)

    @http.route("/vodia/pilot/network/pending", type="jsonrpc", auth="user", methods=["POST"])
    def network_pending(self, company_id=None):
        request.future_response.headers["Cache-Control"] = "no-store"
        model = self._mapping_model(company_id)
        model.env["vodia.network.invitation"]._sync_connections_soon()
        return {"connections": model._network_pending()}

    @http.route("/vodia/pilot/network/contact", type="jsonrpc", auth="user", methods=["POST"])
    def network_contact(self, company_id=None, invitation_id=None):
        request.future_response.headers["Cache-Control"] = "no-store"
        partner = self._mapping_model(company_id)._network_create_contact(invitation_id)
        return {"id": partner.id, "name": partner.name}

    @http.route("/vodia/pilot/session", type="jsonrpc", auth="user", methods=["POST"])
    def session(self, company_id=None):
        request.future_response.headers["Cache-Control"] = "no-store"
        request.future_response.headers["Pragma"] = "no-cache"
        return self._mapping_model(company_id)._mint_browser_session()

    @http.route("/vodia/pilot/consent", type="jsonrpc", auth="user", methods=["POST"])
    def consent(self, company_id=None, extension=None, password=None, consent=False):
        request.future_response.headers["Cache-Control"] = "no-store"
        return self._mapping_model(company_id)._consent_credential(extension, password, consent)
