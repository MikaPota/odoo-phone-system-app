import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from cryptography.fernet import InvalidToken

from odoo import _, api, fields, models
from odoo.tools.translate import LazyTranslate
from odoo.exceptions import UserError

from .credentials import credential_cipher, installation_key

_logger = logging.getLogger(__name__)
_lt = LazyTranslate(__name__)
GCNA_ORIGIN = "https://gcn-alliance.com"
ISO_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")


class GCNADenied(UserError):
    """GCNA refused the stored installation authorization (revoked, expired or no longer permitted)."""


class GCNATermsPending(UserError):
    """GCNA paused the installation until a company administrator accepts updated terms; it is kept."""


# GCNA answers the customer can act on; anything else stays a generic, detail-free error.
GCNA_CONFLICTS = {
    "COMPANY_EXISTS": _lt("This company already has an account. Its administrator must connect this Odoo database."),
    "ALREADY_A_USER": _lt("This email address already belongs to an account. Use a different address or contact support."),
    "ALREADY_LINKED": _lt("This Odoo database already belongs to an account."),
    "CATALOG_CHANGED": _lt("Prices changed. Review the current plans."),
    "LEGAL_CHANGED": _lt("The terms or privacy notice changed. Review them again."),
    "LEGAL_NOT_CONFIGURED": _lt("Purchases open once the provider has published its terms."),
    "INVALID_CODE": _lt("The verification code is not correct."),
    "ALREADY_VERIFIED": _lt("This email address is already verified. Continue to payment."),
    "TOO_MANY_ATTEMPTS": _lt("Too many wrong codes. Start the purchase again."),
    "CHECKOUT_EXPIRED": _lt("The purchase expired. Start again."),
    "CAPACITY_EXCEEDED": _lt("Your plan does not include that many users."),
    "SYSTEM_NOT_READY": _lt("The phone system is not ready yet."),
    "SYSTEM_CANCELLED": _lt("This phone system is cancelled."),
    "BILLING_UNAVAILABLE": _lt("Subscription management is not available for this phone system."),
    "INVALID_MONTH": _lt("Call records are kept for the current and the previous month only."),
    "INVALID_RETURN_URL": _lt("This Odoo site must use HTTPS to manage subscriptions."),
    "PBX_REJECTED": _lt("The phone system rejected the request."),
    "PBX_REQUEST_INVALID": _lt("The phone system could not accept these settings."),
    "RATE_LIMITED": _lt("Too many attempts. Retry later."),
    "INVALID_CONTACT_CARD": _lt("The contact card could not be sent. Check the address and your name and email in your preferences."),
}


class GCNAAccount(models.Model):
    _name = "vodia.gcna.account"
    _description = "GCNA Installation Authorization"
    _rec_name = "installation_id"

    installation_id = fields.Char(required=True, default=lambda self: str(uuid.uuid4()), copy=False)
    email = fields.Char(copy=False)
    verification_id = fields.Char(copy=False)
    verification_expires_at = fields.Datetime(copy=False)
    proof_ciphertext = fields.Char(copy=False)
    account_id = fields.Char(copy=False)
    token_ciphertext = fields.Char(copy=False)
    token_expires_at = fields.Datetime(copy=False)
    checkout_id = fields.Char(copy=False)
    checkout_expires_at = fields.Datetime(copy=False)
    authorization_phase = fields.Selection([
        ("verification_pending", "Email verification"),
        ("authorization_required", "Administrator sign-in"),
        ("mfa_required", "Administrator second factor"),
        ("approval_required", "Installation approval"),
    ], default="verification_pending", required=True, copy=False)

    def _locked(self):
        record = self.env.ref("phone_system.gcna_installation")
        self.env.cr.execute("SELECT id FROM vodia_gcna_account WHERE id = %s FOR UPDATE", [record.id])
        record.invalidate_recordset()
        return record

    @api.model
    def _status(self):
        record = self.env.ref("phone_system.gcna_installation")
        if record.token_ciphertext:
            if record.token_expires_at <= fields.Datetime.now():
                return {
                    "available": True,
                    "state": "expired",
                    "email": record.email,
                    "message": _("GCNA authorization expired. Verify your account again."),
                }
            record._authorization()
            return {
                "available": True,
                "state": "linked",
                "email": record.email,
                "expires_at": record.token_expires_at.isoformat() + "Z",
                "message": _("GCNA authorization is stored. Service permissions are checked by GCNA on each request."),
            }
        if record.verification_id and record.verification_expires_at > fields.Datetime.now():
            return {
                "available": True,
                "state": record.authorization_phase,
                "email": record.email,
                "expires_at": record.verification_expires_at.isoformat() + "Z",
                "message": {
                    "verification_pending": _("Enter the verification code sent to your email address."),
                    "authorization_required": _("Email verified. Sign in as the CORE company administrator to authorize this installation."),
                    "mfa_required": _("Enter your CORE sign-in code from email or your authenticator."),
                    "approval_required": _("Approve this Odoo installation to manage Phone Systems for your CORE company."),
                }[record.authorization_phase],
            }
        return {
            "available": True,
            "state": "unlinked",
            "message": _("Link your GCNA account to this Odoo installation."),
        }

    @api.model
    def _resume_status(self):
        record = self._locked()
        invitation = None
        if record.token_ciphertext and record.token_expires_at > fields.Datetime.now():
            try:
                payload = record._post("/v1/installation/status", {}, 200, record._authorization())
            except GCNATermsPending as pending:
                # Updated terms pause the installation; the authorization stays for when they are accepted.
                return {**self._status(), "state": "terms_required", "message": str(pending)}
            except GCNADenied:
                # Keep proof and account binding so re-linking stays bound to the same installation and account.
                _logger.warning("GCNA no longer accepts the stored installation authorization; it was discarded.")
                record.write({"token_ciphertext": False, "token_expires_at": False})
                message = _("GCNA no longer accepts this installation's authorization: it was revoked or the approving administrator lost access. Verify your account again.")
                return {**self._status(), "state": "revoked", "message": message}
            if (payload.get("state") != "linked" or payload.get("installationId") != record.installation_id
                    or payload.get("accountId") != record.account_id or payload.get("scopes") != ["phone-system:manage"]):
                self._invalid_response()
        elif record.verification_id and record.verification_expires_at > fields.Datetime.now():
            payload = record._post(f"/v1/account-verifications/{record.verification_id}/status", {
                "installationId": record.installation_id, "installationProof": record._installation_proof(),
            }, 200)
            if "token" in payload:
                record._store_authorization(payload)
            elif payload.get("state") in ("verification_pending", "authorization_required", "mfa_required", "approval_required"):
                record.write({"authorization_phase": payload["state"]})
                if payload.get("state") == "authorization_required" and "invitation" in payload:
                    invitation = self._invitation_details(payload["invitation"])
            else:
                self._invalid_response()
        return {**self._status(), **({"invitation": invitation} if invitation else {})}

    @api.model
    def _request_verification(self, email):
        if not isinstance(email, str) or not re.fullmatch(r"[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+", email) or len(email) > 254:
            raise UserError(_("Enter a valid email address."))
        record = self._locked()
        if record.token_ciphertext and record.token_expires_at > fields.Datetime.now():
            raise UserError(_("This installation already has GCNA authorization."))
        if record.verification_id and record.verification_expires_at > fields.Datetime.now():
            raise UserError(_("A verification is already pending. Use that code or wait until it expires."))
        cipher = credential_cipher(self.env)
        if record.proof_ciphertext:
            proof = record._installation_proof()
        else:
            # Retry must present the same proof even if the first RPC transaction rolled back.
            proof = base64.urlsafe_b64encode(hmac.digest(
                installation_key(self.env).encode("ascii"),
                ("gcna-installation-proof/v1:" + record.installation_id).encode("ascii"),
                hashlib.sha256,
            )).decode("ascii").rstrip("=")
        payload = record._post("/v1/account-verifications", {
            "email": email, "installationId": record.installation_id,
            "installationProof": proof,
        }, 202)
        try:
            verification_id = payload["verificationId"]
            if not isinstance(verification_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", verification_id):
                raise ValueError()
            expires_at = self._expiry(payload["expiresAt"], 900)
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        record.write({
            "email": email, "verification_id": verification_id,
            "verification_expires_at": expires_at,
            "proof_ciphertext": cipher.encrypt(proof.encode()).decode(),
            "token_ciphertext": False, "token_expires_at": False,
            "authorization_phase": "verification_pending",
        })
        return self._status()

    @api.model
    def _confirm_verification(self, code):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise UserError(_("Enter the six-digit verification code."))
        record = self._locked()
        if not record.verification_id or record.verification_expires_at <= fields.Datetime.now():
            raise UserError(_("The verification expired. Request a new code."))
        proof = record._installation_proof()
        payload = record._post(f"/v1/account-verifications/{record.verification_id}/confirm", {
            "code": code, "installationId": record.installation_id,
            "installationProof": proof,
        }, 200)
        if payload.get("state") not in ("authorization_required", "mfa_required", "approval_required"):
            self._invalid_response()
        record.write({"authorization_phase": payload["state"]})
        return self._status()

    @api.model
    def _account_step(self, action, value):
        if action == "login":
            if not isinstance(value, str) or not value or len(value) > 256:
                raise UserError(_("Enter your CORE account password. It is used only for this request."))
            data = {"password": value}
        elif action == "mfa":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
                raise UserError(_("Enter the six-digit verification code."))
            data = {"code": value}
        elif action == "approve":
            if value is not True:
                raise UserError(_("Explicit installation approval is required."))
            data = {"accept": True}
        elif action == "invitation":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
                raise UserError(_("Enter the invitation token from your CORE invitation email."))
            data = {"token": value}
        elif action == "activate":
            if not isinstance(value, dict) or value.get("acceptTerms") is not True:
                raise UserError(_("Accept the published terms and privacy notice to activate your invitation."))
            if (not isinstance(value.get("name"), str) or not 2 <= len(value["name"].strip()) <= 100
                    or not isinstance(value.get("password"), str) or not 12 <= len(value["password"]) <= 256
                    or any(not isinstance(value.get(field), str) or not 1 <= len(value[field]) <= 100
                           for field in ("termsVersion", "privacyVersion"))):
                raise UserError(_("Enter your name, a password of at least 12 characters, and review the current legal notices."))
            data = {field: value[field] for field in ("name", "password", "acceptTerms", "termsVersion", "privacyVersion")}
        else:
            raise UserError(_("This account operation is not supported."))
        record = self._locked()
        if not record.verification_id or record.verification_expires_at <= fields.Datetime.now():
            raise UserError(_("The verification expired. Request a new code."))
        expected = {
            "login": "authorization_required", "mfa": "mfa_required", "approve": "approval_required",
            "invitation": "authorization_required", "activate": "authorization_required",
        }
        if record.authorization_phase != expected[action]:
            raise UserError(_("Refresh account status and complete the current authorization step."))
        payload = record._post(f"/v1/account-verifications/{record.verification_id}/{action}", {
            **data, "installationId": record.installation_id,
            "installationProof": record._installation_proof(),
        }, 200)
        if action == "approve":
            record._store_authorization(payload)
        elif action == "invitation":
            if payload.get("state") != "authorization_required":
                self._invalid_response()
            return {**self._status(), "invitation": self._invitation_details(payload.get("invitation"))}
        else:
            allowed = ("mfa_required", "approval_required") if action == "login" else ("approval_required",)
            if payload.get("state") not in allowed:
                self._invalid_response()
            record.write({"authorization_phase": payload["state"]})
        return self._status()

    @api.model
    def _invitation_details(self, value):
        if (not isinstance(value, dict) or not isinstance(value.get("company"), str)
                or type(value.get("can_activate")) is not bool):
            self._invalid_response()
        result = {"company": value["company"], "can_activate": value["can_activate"]}
        if not value["can_activate"]:
            return result
        try:
            imprint, terms, privacy = value["imprint"], value["terms"], value["privacy"]
            url = urlsplit(terms["url"])
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or any(not isinstance(imprint[field], str) or not imprint[field].strip() for field in ("entity", "email"))
                    or not isinstance(imprint["address"], list) or not imprint["address"]
                    or any(not isinstance(line, str) for line in imprint["address"])
                    or not isinstance(terms["version"], str) or not terms["version"]
                    or not isinstance(privacy["version"], str) or not privacy["version"]
                    or not isinstance(privacy["sections"], list) or not privacy["sections"]):
                raise ValueError()
            sections = []
            for section in privacy["sections"]:
                if (any(not isinstance(section[field], str) for field in ("id", "title"))
                        or not isinstance(section["text"], list)
                        or any(not isinstance(line, str) for line in section["text"])):
                    raise ValueError()
                sections.append({field: section[field] for field in ("id", "title", "text")})
            result.update({
                "imprint": {field: imprint[field] for field in ("entity", "address", "email")},
                "terms": {"version": terms["version"], "url": terms["url"]},
                "privacy": {"version": privacy["version"], "sections": sections},
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            self._invalid_response()
        return result

    def _store_authorization(self, payload):
        self.ensure_one()
        record = self
        cipher = credential_cipher(self.env)
        try:
            token = payload["token"]
            account_id = payload["accountId"]
            if (payload["installationId"] != record.installation_id
                    or (record.account_id and account_id != record.account_id)
                    or payload["scopes"] != ["phone-system:manage"]
                    or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.~-]{32,512}", token)
                    or not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", account_id)):
                raise ValueError()
            expires_at = self._expiry(payload["expiresAt"], 31 * 86400)
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        envelope = json.dumps({
            "installation": record.installation_id, "account": account_id, "token": token,
        }).encode()
        record.write({
            "account_id": account_id, "token_ciphertext": cipher.encrypt(envelope).decode(),
            "token_expires_at": expires_at,
            "verification_id": False, "verification_expires_at": False,
        })

    def _installation_proof(self):
        self.ensure_one()
        try:
            return credential_cipher(self.env).decrypt(self.proof_ciphertext.encode()).decode()
        except (InvalidToken, ValueError, UnicodeError):
            self._invalid_credential()

    def _authorization(self):
        self.ensure_one()
        try:
            payload = json.loads(credential_cipher(self.env).decrypt(self.token_ciphertext.encode()))
            if (not isinstance(payload, dict)
                    or payload.get("installation") != self.installation_id
                    or payload.get("account") != self.account_id
                    or not isinstance(payload.get("token"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.~-]{32,512}", payload["token"])
                    or self.token_expires_at <= fields.Datetime.now()):
                raise ValueError()
            return payload["token"]
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            self._invalid_credential()

    @api.model
    def _invalid_credential(self):
        _logger.warning("GCNA stored installation authorization could not be validated.")
        raise UserError(_("GCNA installation authorization cannot be read. Ask the operator to check the encryption key."))

    @api.model
    def _expiry(self, value, maximum_seconds):
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError()
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        remaining = (parsed - datetime.now(timezone.utc)).total_seconds()
        if not 0 < remaining <= maximum_seconds:
            raise ValueError()
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)

    @api.model
    def _invalid_response(self):
        _logger.warning("GCNA account linking returned an invalid response.")
        raise UserError(_("GCNA returned an invalid account-link response. Contact support."))

    @api.model
    def _error_code(self, response):
        try:
            code = response.json().get("error", {}).get("code")
        except (ValueError, TypeError, AttributeError):
            return None
        return code if isinstance(code, str) else None

    def _post(self, path, data, expected_status, token=None):
        try:
            # Server-side override for local test services only; never configurable from the browser.
            response = requests.post(
                (os.environ.get("GCNA_SERVICE_ORIGIN") or GCNA_ORIGIN) + path, json=data, timeout=(3, 10), allow_redirects=False,
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
        except requests.RequestException:
            _logger.warning("GCNA account-link request could not be delivered.")
            raise UserError(_("GCNA could not be reached. Retry later or contact support.")) from None
        if token and response.status_code == 403 and self._error_code(response) == "TERMS_ACCEPTANCE_REQUIRED":
            raise GCNATermsPending(_("GCNA updated its terms. A company administrator must sign in at app.gcn-alliance.com and accept the updated terms; the phone system then works again."))
        if token and response.status_code in (401, 403):
            raise GCNADenied(_("GCNA no longer accepts this installation's authorization."))
        if response.status_code in (400, 409, 422, 429):
            code = self._error_code(response)
            if code in GCNA_CONFLICTS:
                raise UserError(str(GCNA_CONFLICTS[code]))
        if response.status_code != expected_status:
            _logger.warning("GCNA account-link request rejected (HTTP %s).", response.status_code)
            raise UserError(_("GCNA could not complete account linking. Check the code, expiry and account permissions, or contact support."))
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, TypeError):
            self._invalid_response()

    # Purchase of a new phone system: a tenant on the shared PBX for a paid subscription.
    def _ensure_proof(self):
        self.ensure_one()
        if self.proof_ciphertext:
            return self._installation_proof()
        proof = base64.urlsafe_b64encode(hmac.digest(
            installation_key(self.env).encode("ascii"),
            ("gcna-installation-proof/v1:" + self.installation_id).encode("ascii"),
            hashlib.sha256,
        )).decode("ascii").rstrip("=")
        self.proof_ciphertext = credential_cipher(self.env).encrypt(proof.encode()).decode()
        return proof

    @api.model
    def _checkout_catalog(self):
        payload = self._post("/v1/catalog", {}, 200)
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
    def _linked(self):
        record = self.env.ref("phone_system.gcna_installation")
        return bool(record.account_id and record.token_ciphertext and record.token_expires_at
                    and record.token_expires_at > fields.Datetime.now())

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
            "name": self.env.company.name,
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

    def _stored_token(self):
        try:
            return self._authorization()
        except UserError:
            return None

    @api.model
    def _phone_systems(self):
        record = self.env.ref("phone_system.gcna_installation")
        if not record.token_ciphertext:
            return {"systems": []}
        payload = record._post("/v1/phone-systems", {}, 200, record._authorization())
        try:
            systems = []
            for system in payload["systems"]:
                if (not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", system["id"]) or not re.fullmatch(r"[A-Z0-9]{6}", system["code"])
                        or not re.fullmatch(r"[a-z0-9.-]{4,253}", system["hostname"]) or type(system["capacity"]) is not int):
                    raise ValueError()
                stages = {name: {"state": stage["state"], **({"message": stage["message"]} if isinstance(stage.get("message"), str) else {})}
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
        record = self.env.ref("phone_system.gcna_installation")
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
        record = self.env.ref("phone_system.gcna_installation")
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
        record = self.env.ref("phone_system.gcna_installation")
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
                raise UserError(_("The phone credential for %s is missing. Contact support.", user.name))
            if group not in user.group_ids:
                user.sudo().write({"group_ids": [(4, group.id)]})
            extensions[user.id] = extension
        return {"extensions": extensions}
