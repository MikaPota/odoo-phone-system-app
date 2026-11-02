import csv
import hashlib
import hmac
import io
import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote

import requests
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .credentials import credential_cipher

_logger = logging.getLogger(__name__)
ENDPOINT = re.compile(r"https://[a-z0-9]([a-z0-9.-]{0,253}[a-z0-9])?(:[0-9]{1,5})?")
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
REGION = re.compile(r"[a-z0-9-]{1,32}")
CALL_COLUMNS = ("started_at", "user", "direction", "extension", "number", "contact", "outcome", "duration_seconds")


class StorageError(Exception):
    """The customer's storage refused an upload; the status never carries credentials."""

    def __init__(self, status):
        super().__init__(str(status))
        self.status = status


def put_object(endpoint, bucket, region, access_key, secret_key, key, body, content_type, now=None, http=requests):
    """One PUT signed with AWS Signature Version 4, which every S3-compatible service accepts."""
    now = now or datetime.now(timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    path = "/" + quote(bucket + "/" + key, safe="/-_.~")
    digest = hashlib.sha256(body).hexdigest()
    host = endpoint.split("://", 1)[1]
    headers = {"host": host, "x-amz-content-sha256": digest, "x-amz-date": stamp}
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["PUT", path, "", *(f"{name}:{headers[name]}" for name in sorted(headers)), "", signed, digest])
    scope = f"{day}/{region}/s3/aws4_request"
    text = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    signing = ("AWS4" + secret_key).encode()
    for part in (day, region, "s3", "aws4_request"):
        signing = hmac.digest(signing, part.encode(), hashlib.sha256)
    signature = hmac.digest(signing, text.encode(), hashlib.sha256).hex()
    response = http.put(endpoint + path, data=body, timeout=(5, 60), allow_redirects=False, headers={
        "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}",
        "Content-Type": content_type,
        "x-amz-content-sha256": digest,
        "x-amz-date": stamp,
    })
    if not 200 <= response.status_code < 300:
        raise StorageError(response.status_code)


def _cell(value):
    # A contact named "=cmd()" must not run as a formula when the customer opens the file.
    text = "" if value in (False, None) else str(value)
    return "'" + text if text[:1] in ("=", "@", "-", "\t") else text


class PilotConnectionStorage(models.Model):
    _inherit = "vodia.pilot.connection"

    storage_endpoint = fields.Char("Storage URL", copy=False, help="For example https://s3.eu-central-1.amazonaws.com")
    storage_bucket = fields.Char("Bucket", copy=False)
    storage_region = fields.Char("Region", copy=False, default="us-east-1")
    storage_access_key = fields.Char("Access key", copy=False)
    storage_secret = fields.Char("Secret key", compute="_compute_storage_secret", inverse="_set_storage_secret",
                                 help="Write-only. Leave empty to keep the stored key.")
    storage_secret_ciphertext = fields.Char(groups="base.group_system", copy=False)
    storage_state = fields.Selection([("ok", "Working"), ("failed", "Failed")], "Storage status", readonly=True, copy=False)
    storage_message = fields.Char("Storage message", readonly=True, copy=False)
    storage_checked_at = fields.Datetime("Last storage check", readonly=True, copy=False)
    storage_exported_on = fields.Date("Calls saved through", readonly=True, copy=False)

    def _compute_storage_secret(self):
        for record in self:
            record.storage_secret = False

    def _set_storage_secret(self):
        for record in self:
            if record.storage_secret:
                record.storage_secret_ciphertext = credential_cipher(self.env).encrypt(
                    record.storage_secret.encode()).decode("ascii")
            record.invalidate_recordset(["storage_secret"])

    @api.constrains("storage_endpoint", "storage_bucket", "storage_region")
    def _check_storage(self):
        for record in self:
            if record.storage_endpoint and not ENDPOINT.fullmatch(record.storage_endpoint):
                raise ValidationError(_("Enter the storage URL with https:// and no path, for example https://s3.example.com."))
            if record.storage_bucket and not BUCKET.fullmatch(record.storage_bucket):
                raise ValidationError(_("Enter the bucket name only, in lowercase letters, digits, dots or dashes."))
            if record.storage_region and not REGION.fullmatch(record.storage_region):
                raise ValidationError(_("Enter the storage region, for example eu-central-1."))

    def _storage_ready(self):
        self.ensure_one()
        return bool(self.storage_endpoint and self.storage_bucket and self.storage_region
                    and self.storage_access_key and self.sudo().storage_secret_ciphertext)

    def _storage_put(self, name, body, content_type):
        self.ensure_one()
        secret = credential_cipher(self.env).decrypt(self.sudo().storage_secret_ciphertext.encode()).decode()
        put_object(self.storage_endpoint, self.storage_bucket, self.storage_region, self.storage_access_key, secret,
                   f"phone-system/{self.pbx_host}/{name}", body, content_type)

    def _storage_report(self, error=None):
        self.ensure_one()
        if error is None:
            message = False
        elif isinstance(error, StorageError):
            message = _("The storage refused the upload (HTTP %s). Check the URL, bucket and keys.", error.status)
        else:
            message = _("The storage could not be reached. Check the URL.")
        self.write({"storage_state": "failed" if error else "ok", "storage_message": message,
                    "storage_checked_at": fields.Datetime.now()})

    def action_storage_test(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an administrator may configure storage."))
        if not self._storage_ready():
            raise UserError(_("Enter the storage URL, bucket, region, access key and secret key first."))
        try:
            self._storage_put("connection-test.txt", b"Phone System can write to this storage.\n", "text/plain")
            self._storage_report()
        except (StorageError, requests.RequestException) as error:
            self._storage_report(error)
        return True

    def _storage_calls_csv(self, calls):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(CALL_COLUMNS)
        for call in calls.sorted("started_at"):
            writer.writerow([_cell(value) for value in (
                call.started_at, call.user_id.name, call.direction, call.peer_extension, call.peer_number,
                call.peer_partner_id.name, call.outcome, call.duration)])
        return output.getvalue().encode("utf-8")

    def _storage_backup(self):
        """What it takes to rebuild this system's Odoo side. Never credentials."""
        self.ensure_one()
        mappings = self.env["vodia.pilot.mapping"].with_context(active_test=False).search([("connection_id", "=", self.id)])
        numbers = self.env["vodia.pilot.number"].search([("connection_id", "=", self.id)])
        return json.dumps({
            "name": self.name, "host": self.pbx_host, "company": self.company_id.name,
            "users": [{"login": mapping.user_id.login, "name": mapping.user_id.name, "extension": mapping.extension,
                       "external_calling": mapping.external_calling, "active": mapping.active} for mapping in mappings],
            "numbers": [{"number": number.number, "extension": number.extension} for number in numbers],
        }, indent=2).encode("utf-8")

    def _storage_export(self, today):
        """Whole past days only, so a rerun rewrites the same files instead of duplicating calls."""
        self.ensure_one()
        users = self.env["vodia.pilot.mapping"].with_context(active_test=False).search(
            [("connection_id", "=", self.id)]).user_id
        calls = self.env["vodia.pilot.call"].search(
            [("company_id", "=", self.company_id.id), ("user_id", "in", users.ids)])
        first = min(calls.mapped("started_at"), default=datetime.combine(today, time.min)).date()
        day = self.storage_exported_on + timedelta(days=1) if self.storage_exported_on else first
        try:
            while day < today:
                start, end = datetime.combine(day, time.min), datetime.combine(day + timedelta(days=1), time.min)
                daily = calls.filtered(lambda call: start <= call.started_at < end)
                if daily:
                    self._storage_put(f"calls/{day.isoformat()}.csv", self._storage_calls_csv(daily), "text/csv")
                self.storage_exported_on = day
                day += timedelta(days=1)
            self._storage_put(f"backup/{today.isoformat()}.json", self._storage_backup(), "application/json")
            self._storage_report()
        except (StorageError, requests.RequestException) as error:
            _logger.warning("Saving Phone System data for %s failed.", self.pbx_host)
            self._storage_report(error)

    @api.model
    def _storage_export_all(self, today=None):
        today = today or date.today()
        for connection in self.search([("storage_endpoint", "!=", False)]):
            if connection._storage_ready():
                connection._storage_export(today)
