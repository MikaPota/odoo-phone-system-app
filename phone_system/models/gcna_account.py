import csv
import io
import re
from datetime import datetime
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.tools.translate import LazyTranslate
from odoo.exceptions import UserError

from odoo.addons.gcna_link.models.gcna_account import GCNA_CONFLICTS

_lt = LazyTranslate(__name__)
ISO_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")

# What GCNA answers about purchases and phone systems, shown in the user's language.
GCNA_CONFLICTS.update({
    "CATALOG_CHANGED": _lt("Prices changed. Review the current plans."),
    "CHECKOUT_EXPIRED": _lt("The purchase expired. Start again."),
    "CAPACITY_EXCEEDED": _lt("Your plan does not include that many users."),
    "SYSTEM_NOT_READY": _lt("The phone system is not ready yet."),
    "SYSTEM_CANCELLED": _lt("This phone system is cancelled."),
    "BILLING_UNAVAILABLE": _lt("Subscription management is not available for this phone system."),
    "INVALID_MONTH": _lt("Call records are kept for the current and the previous month only."),
    "INVALID_RETURN_URL": _lt("This Odoo site must use HTTPS to manage subscriptions."),
    "PBX_REJECTED": _lt("The phone system rejected the request."),
    "PBX_REQUEST_INVALID": _lt("The phone system could not accept these settings."),
    "INVALID_CONTACT_CARD": _lt("The contact card could not be sent. Check the address and your name and email in your preferences."),
})
# GCNA writes setup progress in English; the ones known here are shown in the user's language.
GCNA_STAGE_MESSAGES = {
    "Your phone system is created and its secure address is being certified. This usually takes about a minute; you can leave this page and come back.":
        _lt("Your phone system is created and its secure address is being certified. This usually takes about a minute; you can leave this page and come back."),
    "Your phone system is created and is waiting for its license. You can leave this page and come back.":
        _lt("Your phone system is created and is waiting for its license. You can leave this page and come back."),
    "The phone system could not be created yet. It is retried automatically; contact your service provider if this persists.":
        _lt("The phone system could not be created yet. It is retried automatically; contact your service provider if this persists."),
    # GCNA platform releases before 2026-11-09 still send the old wording.
    "The phone system could not be created yet. It is retried automatically; contact support if this persists.":
        _lt("The phone system could not be created yet. It is retried automatically; contact your service provider if this persists."),
}


class GCNAAccount(models.Model):
    """Purchases and phone systems on top of GCNA account linking (gcna_link)."""
    _inherit = "vodia.gcna.account"

    checkout_id = fields.Char(copy=False)
    checkout_expires_at = fields.Datetime(copy=False)

    @api.model
    def _checkout_catalog(self):
        payload = self._post("/v1/catalog", self._country(), 200)
        try:
            plans = []
            for plan in payload["plans"]:
                prices = {term: {"amount": price["amount"], "currency": price["currency"]}
                          for term, price in plan["prices"].items()
                          if term in ("monthly", "annual") and type(price["amount"]) is int and isinstance(price["currency"], str)}
                if (not isinstance(plan["planId"], str) or not isinstance(plan["name"], str)
                        or type(plan["capacity"]) is not int or plan["capacity"] < 1 or not prices):
                    raise ValueError()
                plans.append({"planId": plan["planId"], "name": plan["name"], "capacity": plan["capacity"], "prices": prices})
            if not isinstance(payload["catalogVersion"], str):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            self._invalid_response()
        legal = payload.get("legal")
        if legal is not None:
            legal = self._invitation_details({**legal, "company": "", "can_activate": True})
            legal = {key: legal[key] for key in ("imprint", "terms", "privacy")}
        user, company = self.env.user, self.env.company
        website = urlsplit(company.website if "://" in (company.website or "") else "https://" + (company.website or "")).hostname or ""
        email = user.email or ""
        return {
            "catalog_version": payload["catalogVersion"], "plans": plans, "legal": legal, "linked": self._linked(),
            "buyer": {
                "email": email, "admin_name": user.name, "company_name": company.name,
                "domain": website.removeprefix("www.") or email.rpartition("@")[2],
            },
        }

    @api.model
    def _country(self):
        """The company's country; GCNA shows and charges euro prices, with SEPA Direct Debit, in DE, AT and CH."""
        code = self.env.company.country_id.code
        return {"country": code} if code else {}

    @api.model
    def _checkout_order(self, values):
        """A linked account buys another phone system, named after the current company, straight to payment."""
        if (values.get("term") not in ("monthly", "annual")
                or not all(isinstance(values.get(k), str) and values[k].strip() for k in ("plan_id", "catalog_version"))):
            raise UserError(_("Choose a plan."))
        record = self._locked()
        if not record._linked():
            raise UserError(_("Link this Odoo database to a GCNA account first."))
        payload = record._post("/v1/phone-systems/orders", {
            "planId": values["plan_id"], "term": values["term"], "catalogVersion": values["catalog_version"],
            "name": self.env.company.name, **self._country(),
        }, 200, record._authorization())
        order_id = payload.get("orderId")
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", order_id):
            self._invalid_response()
        return {**record._payment_step(payload), "order_id": order_id}

    @api.model
    def _checkout_start(self, values):
        email = values.get("email")
        domain = (values.get("domain") or "").lower().removeprefix("www.") if isinstance(values.get("domain"), str) else ""
        if (not isinstance(email, str) or not re.fullmatch(r"[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+", email) or len(email) > 254
                or values.get("term") not in ("monthly", "annual")
                or not all(isinstance(values.get(k), str) and values[k].strip() for k in ("plan_id", "catalog_version", "admin_name", "company_name", "terms_version", "privacy_version"))
                or not re.fullmatch(r"(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain)):
            raise UserError(_("Complete the plan, your name, company, company domain and a valid email address."))
        if values.get("accept_terms") is not True:
            raise UserError(_("Accept the terms and privacy notice to continue."))
        record = self._locked()
        if record._linked():
            raise UserError(str(GCNA_CONFLICTS["ALREADY_LINKED"]))
        payload = record._post("/v1/checkouts", {
            "installationId": record.installation_id, "installationProof": record._ensure_proof(),
            "email": email, "adminName": values["admin_name"].strip(), "companyName": values["company_name"].strip(),
            "domain": domain, "planId": values["plan_id"], "term": values["term"], "catalogVersion": values["catalog_version"],
            "acceptTerms": True, "termsVersion": values["terms_version"], "privacyVersion": values["privacy_version"],
            **self._country(),
        }, 202)
        try:
            checkout_id = payload["checkoutId"]
            if not isinstance(checkout_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", checkout_id):
                raise ValueError()
            expires_at = self._expiry(payload["expiresAt"], 900)
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        record.write({"checkout_id": checkout_id, "checkout_expires_at": expires_at, "email": email.lower()})
        return {"state": "verification_pending", "expires_at": expires_at.isoformat() + "Z"}

    @api.model
    def _checkout_confirm(self, code):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise UserError(_("Enter the six-digit verification code."))
        record = self._locked()
        if not record.checkout_id or not record.checkout_expires_at or record.checkout_expires_at <= fields.Datetime.now():
            raise UserError(str(GCNA_CONFLICTS["CHECKOUT_EXPIRED"]))
        payload = record._post(f"/v1/checkouts/{record.checkout_id}/confirm", {
            "installationId": record.installation_id, "installationProof": record._installation_proof(), "code": code,
        }, 200)
        return record._payment_step(payload)

    @api.model
    def _checkout_resend(self):
        record = self._locked()
        if not record.checkout_id:
            raise UserError(str(GCNA_CONFLICTS["CHECKOUT_EXPIRED"]))
        payload = record._post(f"/v1/checkouts/{record.checkout_id}/resend", {
            "installationId": record.installation_id, "installationProof": record._installation_proof(),
        }, 202)
        try:
            expires_at = self._expiry(payload["expiresAt"], 900)
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        record.write({"checkout_expires_at": expires_at})
        return {"state": "verification_pending", "expires_at": expires_at.isoformat() + "Z"}

    @api.model
    def _checkout_resume(self):
        record = self._locked()
        if not record.checkout_id:
            return {"state": "none"}
        try:
            payload = record._post(f"/v1/checkouts/{record.checkout_id}/status", {
                "installationId": record.installation_id, "installationProof": record._installation_proof(),
            }, 200)
        except UserError as error:
            # A purchase GCNA no longer knows (expired, or opened before Stripe went live) starts over quietly.
            if str(error) != str(GCNA_CONFLICTS["CHECKOUT_EXPIRED"]):
                raise
            record.write({"checkout_id": False, "checkout_expires_at": False})
            return {"state": "none"}
        if payload.get("state") == "verification_pending":
            return {"state": "verification_pending"}
        return record._payment_step(payload)

    def _payment_step(self, payload):
        self.ensure_one()
        try:
            if (payload["state"] not in ("payment_required", "paid")
                    or not isinstance(payload["clientSecret"], str) or not payload["clientSecret"].startswith("cs_")
                    or not isinstance(payload["publishableKey"], str) or not payload["publishableKey"].startswith("pk_")):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        installation = payload.get("installation") or {}
        if not self.token_ciphertext or installation.get("token") and self._stored_token() != installation["token"]:
            self._store_authorization(installation)
        # The client secret is meant for Stripe's checkout in the browser; the installation token never is.
        return {"state": payload["state"], "client_secret": payload["clientSecret"], "publishable_key": payload["publishableKey"]}

    @api.model
    def _phone_systems(self):
        record = self.env.ref("gcna_link.gcna_installation")
        if not record.token_ciphertext:
            return {"systems": []}
        payload = record._post("/v1/phone-systems", {}, 200, record._authorization())
        try:
            systems = []
            for system in payload["systems"]:
                if (not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", system["id"]) or not re.fullmatch(r"[A-Z0-9]{6}", system["code"])
                        or not re.fullmatch(r"[a-z0-9.-]{4,253}", system["hostname"]) or type(system["capacity"]) is not int):
                    raise ValueError()
                stages = {name: {"state": stage["state"], **({"message": str(GCNA_STAGE_MESSAGES.get(stage["message"], stage["message"]))}
                                                         if isinstance(stage.get("message"), str) else {})}
                          for name, stage in system["stages"].items() if name in ("paid", "provisioned", "users")}
                entry = {"id": system["id"], "code": system["code"], "hostname": system["hostname"],
                         "name": system.get("name") or system["code"], "capacity": system["capacity"], "stages": stages}
                if isinstance(system.get("orderId"), str) and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", system["orderId"]):
                    entry["order_id"] = system["orderId"]
                # The tenant's key and whether the License Server confirms it; None means it could not be checked.
                if "license" in system:
                    license = system["license"]
                    if (not isinstance(license, dict) or license["valid"] not in (True, False, None)
                            or not (license["key"] is None or re.fullmatch(r"[A-Za-z0-9-]{8,200}", license["key"]))):
                        raise ValueError()
                    entry["license"] = {"key": license["key"], "valid": license["valid"]}
                # A cancelled system keeps its tenant until deleteAt; then GCNA deletes it with its data.
                if "cancelled" in system:
                    cancelled = system["cancelled"]
                    if (not isinstance(cancelled, dict) or type(cancelled["deleted"]) is not bool
                            or not all(isinstance(cancelled[k], str) and ISO_TIME.fullmatch(cancelled[k]) for k in ("at", "deleteAt"))):
                        raise ValueError()
                    entry["cancelled"] = {"at": cancelled["at"], "deleteAt": cancelled["deleteAt"], "deleted": cancelled["deleted"]}
                systems.append(entry)
        except (KeyError, TypeError, ValueError, AttributeError):
            self._invalid_response()
        return {"systems": systems}

    @api.model
    def _call_records(self, system_id, month):
        """A phone system's call records of the current or the previous month, as GCNA keeps them."""
        if not isinstance(system_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", system_id):
            raise UserError(_("Choose a phone system."))
        if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
            raise UserError(_("Choose the current or the previous month."))
        record = self.env.ref("gcna_link.gcna_installation")
        payload = record._post(f"/v1/phone-systems/{system_id}/call-records", {"month": month}, 200, record._authorization())
        try:
            records = []
            for item in payload["records"]:
                row = {key: item[key] for key in ("id", "callId", "start", "answered", "end", "from", "to", "abandoned")}
                if (not all(isinstance(row[k], str) and len(row[k]) <= 300 for k in ("id", "callId", "from", "to"))
                        or not ISO_TIME.fullmatch(row["start"]) or type(row["abandoned"]) is not bool
                        or not all(row[k] is None or ISO_TIME.fullmatch(row[k]) for k in ("answered", "end"))):
                    raise ValueError()
                records.append(row)
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        return records

    @api.model
    def _call_records_csv(self, system_id, month):
        def seconds(a, b):
            return str(round((datetime.fromisoformat(b[:-1]) - datetime.fromisoformat(a[:-1])).total_seconds())) if a and b else ""
        def text(value):
            # A leading = + - @ would run as a formula in a spreadsheet, so such a cell stays text.
            return "'" + value if value[:1] in ("=", "+", "-", "@") else value
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["start_utc", "answered_utc", "end_utc", "duration_s", "talk_s", "from", "to", "abandoned", "call_id", "record_id"])
        for row in self._call_records(system_id, month):
            writer.writerow([row["start"], row["answered"] or "", row["end"] or "", seconds(row["start"], row["end"]),
                             seconds(row["answered"], row["end"]), text(row["from"]), text(row["to"]),
                             "yes" if row["abandoned"] else "no", row["callId"], row["id"]])
        return out.getvalue()

    @api.model
    def _billing_portal(self, system_id):
        """A short-lived Stripe page where the customer manages the subscription."""
        if not isinstance(system_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", system_id):
            raise UserError(_("Choose a phone system."))
        record = self.env.ref("gcna_link.gcna_installation")
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        payload = record._post(f"/v1/phone-systems/{system_id}/billing-portal", {"returnUrl": f"{base}/odoo"}, 200,
                               record._authorization())
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith("https://billing.stripe.com/"):
            self._invalid_response()
        return {"url": url}

    @api.model
    def _sync_users(self, system_id, user_ids):
        if not isinstance(system_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", system_id):
            raise UserError(_("Choose a phone system."))
        if not isinstance(user_ids, list) or not user_ids or any(type(uid) is not int for uid in user_ids):
            raise UserError(_("Choose at least one user."))
        company = self.env.company
        users = self.env["res.users"].browse(user_ids).exists()
        if len(users) != len(set(user_ids)) or any(not u.active or u.share or company not in u.company_ids for u in users):
            raise UserError(_("Choose active internal users of the current company."))
        system = next((s for s in self._phone_systems()["systems"] if s["id"] == system_id), None)
        if not system:
            raise UserError(_("Choose a phone system."))
        record = self.env.ref("gcna_link.gcna_installation")
        payload = record._post(f"/v1/phone-systems/{system_id}/users", {
            "users": [{"reference": f"user-{u.id}", "name": u.name} for u in users],
        }, 200, record._authorization())
        try:
            tenant = payload["tenant"]
            if tenant != system["hostname"] or payload["origin"] != "https://" + tenant:
                raise ValueError()
            results = {item["reference"]: item for item in payload["users"]}
            if set(results) != {f"user-{u.id}" for u in users}:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        Connection = self.env["vodia.pilot.connection"].sudo()
        connection = Connection.search([("gcna_system_id", "=", system_id)], limit=1) or Connection.create({
            "name": system["name"], "company_id": company.id, "pbx_host": tenant, "gcna_system_id": system_id,
        })
        Mapping = self.env["vodia.pilot.mapping"].sudo()
        group = self.env.ref("phone_system.group_pilot")
        extensions = {}
        for user in users:
            item = results[f"user-{user.id}"]
            extension = item.get("extension")
            if not isinstance(extension, str) or not re.fullmatch(r"[0-9]{2,6}", extension):
                self._invalid_response()
            # One phone per user per company: reuse the user's mapping, moving it onto this system if needed.
            mapping = Mapping.search([("company_id", "=", company.id), ("user_id", "=", user.id)], limit=1)
            password = item.get("webPassword")
            if password:
                if mapping:
                    # Set the system and extension before the credential so its scope matches.
                    mapping.write({"connection_id": connection.id, "extension": extension})
                else:
                    mapping = Mapping.create({"user_id": user.id, "connection_id": connection.id, "extension": extension})
                mapping.write({"web_password": password})
            elif not mapping or mapping.connection_id.id != connection.id or mapping.extension != extension or not mapping.credential_ciphertext:
                raise UserError(_("The phone credential for %s is missing. Contact your service provider.", user.name))
            if group not in user.groups_id:
                user.sudo().write({"groups_id": [(4, group.id)]})
            extensions[user.id] = extension
        return {"extensions": extensions}
