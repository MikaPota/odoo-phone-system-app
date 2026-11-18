import base64
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
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
# The scopes of GCNA's apps; a credential for anything else is not stored.
KNOWN_SCOPES = frozenset({"phone-system:manage", "bizzkickr:use"})
ISO_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")


class GCNADenied(UserError):
    """GCNA refused the stored installation authorization (revoked, expired or no longer permitted)."""


class GCNATermsPending(UserError):
    """GCNA paused the installation until a company administrator accepts updated terms; it is kept."""


# GCNA answers the customer can act on; anything else stays a generic, detail-free error.
GCNA_CONFLICTS = {
    "COMPANY_EXISTS": _lt("This company already has an account. Its administrator must connect this Odoo database."),
    "ALREADY_A_USER": _lt("This email address already belongs to an account. Use a different address or contact your service provider."),
    "ALREADY_LINKED": _lt("This Odoo database already belongs to an account."),
    "LEGAL_CHANGED": _lt("The terms or privacy notice changed. Review them again."),
    "LEGAL_NOT_CONFIGURED": _lt("Purchases open once the provider has published its terms."),
    "INVALID_CODE": _lt("The verification code is not correct."),
    "ALREADY_VERIFIED": _lt("This email address is already verified. Continue to payment."),
    "RATE_LIMITED": _lt("Too many attempts. Retry later."),
    "TOO_MANY_ATTEMPTS": _lt("Too many wrong codes. Start again."),
    "SIGNUP_EXPIRED": _lt("The sign-up expired. Start again."),
    "CODE_EXPIRED": _lt("The verification code expired. Request a new one."),
    "INVALID_SIGNUP": _lt("Complete your name, company, company domain and a valid email address."),
    "CONSENT_REQUIRED": _lt("Accept the terms and privacy notice to continue."),
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
    signup_id = fields.Char(copy=False)
    signup_expires_at = fields.Datetime(copy=False)
    authorization_phase = fields.Selection([
        ("verification_pending", "Email verification"),
        ("authorization_required", "Administrator sign-in"),
        ("mfa_required", "Administrator second factor"),
        ("approval_required", "Installation approval"),
    ], default="verification_pending", required=True, copy=False)

    def _locked(self):
        record = self.env.ref("gcna_link.gcna_installation")
        self.env.cr.execute("SELECT id FROM vodia_gcna_account WHERE id = %s FOR UPDATE", [record.id])
        record.invalidate_recordset()
        return record

    @api.model
    def _status(self):
        record = self.env.ref("gcna_link.gcna_installation")
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
            record._renew_if_due()
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
                    or payload.get("accountId") != record.account_id or not self._scopes(payload.get("scopes"))):
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
                    or not self._scopes(payload["scopes"])
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
        raise UserError(_("GCNA returned an invalid account-link response. Contact your service provider."))

    @api.model
    def _scopes(self, value):
        """What GCNA lets this installation do: only the scopes of GCNA's own apps, at least one of them."""
        return isinstance(value, list) and bool(value) and set(value) <= KNOWN_SCOPES

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
            raise UserError(_("GCNA could not be reached. Retry later or contact your service provider.")) from None
        if token and response.status_code == 403 and self._error_code(response) == "TERMS_ACCEPTANCE_REQUIRED":
            raise GCNATermsPending(_("GCNA updated its terms. A company administrator must sign in at app.gcn-alliance.com and accept the updated terms; GCNA's services then work again."))
        if token and response.status_code in (401, 403):
            raise GCNADenied(_("GCNA no longer accepts this installation's authorization."))
        if response.status_code in (400, 409, 422, 429):
            code = self._error_code(response)
            if code in GCNA_CONFLICTS:
                raise UserError(str(GCNA_CONFLICTS[code]))
        if response.status_code != expected_status:
            _logger.warning("GCNA account-link request rejected (HTTP %s).", response.status_code)
            raise UserError(_("GCNA could not complete account linking. Check the code, expiry and account permissions, or contact your service provider."))
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, TypeError):
            self._invalid_response()

    # Proof that this database is the installation: derived from its own key, so a retry presents the same.
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

    # GCNA's link lasts 30 days. With fewer than 7 left, the installation renews it with its token and
    # its proof and stores the fresh token; a revoked or expired link is not renewed (link again).
    RENEW_BEFORE = timedelta(days=7)

    @api.model
    def _renew_if_due(self):
        record = self._locked()
        if (not record.token_ciphertext or not record.token_expires_at
                or record.token_expires_at <= fields.Datetime.now()
                or record.token_expires_at - fields.Datetime.now() > self.RENEW_BEFORE):
            return False
        try:
            payload = record._post("/v1/installation/renew", {
                "installationId": record.installation_id, "installationProof": record._installation_proof(),
            }, 200, record._authorization())
            if payload.get("installationId") != record.installation_id or payload.get("accountId") != record.account_id:
                record._invalid_response()
            record._store_authorization(payload)
        except GCNATermsPending:
            _logger.warning("GCNA link not renewed: a company administrator must accept GCNA's updated terms first.")
            return False
        except UserError as error:
            # Denied, unreachable, refused or an invalid answer: the stored link stays as it is; the next run tries again.
            _logger.warning("GCNA link not renewed: %s", error)
            return False
        _logger.info("GCNA link renewed until %s.", record.token_expires_at)
        return True

    @api.model
    def _cron_renew(self):
        self._renew_if_due()

    @api.model
    def _linked(self):
        record = self.env.ref("gcna_link.gcna_installation")
        return bool(record.account_id and record.token_ciphertext and record.token_expires_at
                    and record.token_expires_at > fields.Datetime.now())

    def _stored_token(self):
        try:
            return self._authorization()
        except UserError:
            return None

    # A free GCNA account for this database (GCNA REQ-006): an emailed code, then a new organisation whose
    # named admin is the verified address, and this installation's credential. No order, no payment.
    @api.model
    def _signup_start(self, values, app="odoo"):
        email = values.get("email")
        domain = (values.get("domain") or "").lower().removeprefix("www.") if isinstance(values.get("domain"), str) else ""
        if (not isinstance(email, str) or not re.fullmatch(r"[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+", email) or len(email) > 254
                or not all(isinstance(values.get(k), str) and values[k].strip() for k in ("admin_name", "company_name", "terms_version", "privacy_version"))
                or not re.fullmatch(r"(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain)):
            raise UserError(str(GCNA_CONFLICTS["INVALID_SIGNUP"]))
        if values.get("accept_terms") is not True:
            raise UserError(str(GCNA_CONFLICTS["CONSENT_REQUIRED"]))
        record = self._locked()
        if record._linked():
            raise UserError(str(GCNA_CONFLICTS["ALREADY_LINKED"]))
        payload = record._post("/v1/signups", {
            "installationId": record.installation_id, "installationProof": record._ensure_proof(),
            "email": email, "adminName": values["admin_name"].strip(), "companyName": values["company_name"].strip(),
            "domain": domain, "acceptTerms": True, "termsVersion": values["terms_version"], "privacyVersion": values["privacy_version"],
            "app": app,
        }, 202)
        try:
            signup_id = payload["signupId"]
            if not isinstance(signup_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", signup_id):
                raise ValueError()
            expires_at = self._expiry(payload["expiresAt"], 900)
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        record.write({"signup_id": signup_id, "signup_expires_at": expires_at, "email": email.lower()})
        return {"state": "verification_pending", "expires_at": expires_at.isoformat() + "Z"}

    @api.model
    def _signup_confirm(self, code):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise UserError(_("Enter the six-digit verification code."))
        record = self._locked()
        if not record.signup_id:
            raise UserError(str(GCNA_CONFLICTS["SIGNUP_EXPIRED"]))
        payload = record._post(f"/v1/signups/{record.signup_id}/confirm", {
            "installationId": record.installation_id, "installationProof": record._installation_proof(), "code": code,
        }, 200)
        if payload.get("state") != "linked" or not isinstance(payload.get("installation"), dict):
            self._invalid_response()
        record._store_authorization(payload["installation"])
        record.write({"signup_id": False, "signup_expires_at": False})
        return self._status()

    @api.model
    def _signup_resend(self):
        record = self._locked()
        if not record.signup_id:
            raise UserError(str(GCNA_CONFLICTS["SIGNUP_EXPIRED"]))
        payload = record._post(f"/v1/signups/{record.signup_id}/resend", {
            "installationId": record.installation_id, "installationProof": record._installation_proof(),
        }, 202)
        try:
            expires_at = self._expiry(payload["expiresAt"], 900)
        except (KeyError, TypeError, ValueError):
            self._invalid_response()
        record.write({"signup_expires_at": expires_at})
        return {"state": "verification_pending", "expires_at": expires_at.isoformat() + "Z"}

    @api.model
    def _legal(self):
        """The operator's current legal notices, to show before a sign-up or purchase."""
        payload = self._post("/v1/catalog", {}, 200)
        legal = payload.get("legal")
        if legal is None:
            return None
        legal = self._invitation_details({**legal, "company": "", "can_activate": True})
        return {key: legal[key] for key in ("imprint", "terms", "privacy")}
