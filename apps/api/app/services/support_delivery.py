"""Explicit, bounded delivery of an already-redacted diagnostic archive."""
from __future__ import annotations

import hashlib
import io
import json
import threading
from urllib.parse import urlsplit
import zipfile

import requests
from fastapi import HTTPException

from app.core.config import settings
from app.core.version import APP_VERSION

MAX_SUPPORT_ARCHIVE_BYTES = 32 * 1024 * 1024
SUPPORT_DELIVERY_GATE = threading.BoundedSemaphore(1)


def delivery_error(code: str, status: int = 503):
    raise HTTPException(status_code=status, detail={"code": code})


def support_destination() -> str:
    value = str(settings.support_report_url or "").strip()
    try:
        parsed = urlsplit(value)
        valid = bool(value and parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.fragment and not parsed.query and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        delivery_error("support_not_configured")
    return value


def report_fingerprint(archive: io.BytesIO, mode: str) -> str:
    with zipfile.ZipFile(archive) as bundle:
        description = bundle.read("bug-report.txt").decode("utf-8").strip()
    archive.seek(0)
    return hashlib.sha256(json.dumps([description, mode, APP_VERSION], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def deliver_report(archive: io.BytesIO, *, mode: str, submission_id: str) -> dict:
    destination = support_destination()
    size = archive.getbuffer().nbytes
    if size > MAX_SUPPORT_ARCHIVE_BYTES:
        delivery_error("support_archive_too_large", 413)
    headers = {
        "Content-Type": "application/zip", "Content-Length": str(size),
        "X-KM-Report-Id": submission_id, "X-KM-Report-Mode": mode,
        "X-KM-Report-Version": APP_VERSION,
        "X-KM-Report-Fingerprint": report_fingerprint(archive, mode),
        "User-Agent": f"KM-VMS/{APP_VERSION} SupportDelivery",
        "Accept": "application/json",
    }
    try:
        with requests.Session() as client:
            # Do not forward browser credentials or pick up ambient proxy/.netrc secrets.
            client.trust_env = False
            with client.post(destination, data=archive, headers=headers, timeout=(10, 90), allow_redirects=False, stream=True) as response:
                if response.status_code in (429, 507):
                    delivery_error("support_busy" if response.status_code == 429 else "support_storage_full")
                if response.status_code not in (200, 201):
                    delivery_error("support_delivery_failed")
                body = bytearray()
                for chunk in response.iter_content(2048):
                    if len(body)+len(chunk) > 8192:
                        delivery_error("support_delivery_failed")
                    body.extend(chunk)
                receipt = json.loads(body)
                if not isinstance(receipt, dict) or receipt.get("accepted") is not True or receipt.get("report_id") != submission_id:
                    delivery_error("support_delivery_failed")
                return {"accepted": True, "report_id": submission_id}
    except (requests.RequestException, ValueError, UnicodeError):
        # A retry retains the same id. An accepted upload is not duplicated when
        # only its response was lost; no automatic retries or background worker.
        delivery_error("support_delivery_failed")
