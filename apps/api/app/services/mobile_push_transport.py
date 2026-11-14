"""RFC Web Push delivery, with encrypted payloads and a public-HTTPS-only target.

The service address is a capability secret. Never log it or remote response bodies.
DNS is checked and pinned for the request, while TLS verifies the original name.
"""
from __future__ import annotations

import base64
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from urllib3 import HTTPSConnectionPool
from urllib3.util import Timeout

from app.core.config import settings

# The maintained project contact site; no private owner mailbox is embedded.
# py-vapid's strict URI validator accepts an HTTPS origin, not a URL path.
VAPID_SUBJECT = "https://" + "кмпрайм.рф".encode("idna").decode("ascii")
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class PushDeliveryError(RuntimeError):
    def __init__(self, code: str, *, permanent: bool = False):
        self.code, self.permanent = code, permanent
        super().__init__(code)


def decode_key(value: str, size: int) -> bytes:
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("mobile_push_key_invalid")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("mobile_push_key_invalid") from exc
    if len(raw) != size:
        raise ValueError("mobile_push_key_invalid")
    return raw


def vapid_key():
    material = HKDF(algorithm=hashes.SHA256(), length=48, salt=b"KMVMS mobile webpush v1",
                    info=b"installation VAPID identity").derive(settings.encryption_key.encode())
    return ec.derive_private_key(int.from_bytes(material, "big") % (P256_ORDER - 1) + 1, ec.SECP256R1())


def vapid_public_key() -> str:
    raw = vapid_key().public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass(frozen=True)
class PushTarget:
    host: str
    address: str
    path: str


def public_target(endpoint: str) -> PushTarget:
    try:
        if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048 or any(ord(c) <= 32 for c in endpoint):
            raise ValueError()
        url = urlsplit(endpoint)
        if url.scheme != "https" or not url.hostname or url.port not in (None, 443) or url.username or url.password or url.fragment:
            raise ValueError()
        host = url.hostname.encode("idna").decode("ascii").lower()
        if len(host) > 253 or host.endswith((".local", ".localhost")) or host == "localhost":
            raise ValueError()
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or len(addresses) > 32 or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError()
        return PushTarget(host, addresses[0], (url.path or "/") + ("?" + url.query if url.query else ""))
    except (ValueError, OSError, UnicodeError) as exc:
        raise ValueError("mobile_push_endpoint_invalid") from exc


def validate_subscription(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"endpoint", "keys"} or not isinstance(value.get("keys"), dict) or set(value["keys"]) != {"p256dh", "auth"}:
        raise ValueError("mobile_push_subscription_invalid")
    public_target(value["endpoint"])
    raw = decode_key(value["keys"]["p256dh"], 65)
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    decode_key(value["keys"]["auth"], 16)
    return value


class _PinnedSession:
    """The small requests-session interface pywebpush actually uses.

    No proxy environment, redirect following, second DNS lookup or unbounded body.
    Response bodies are neither needed nor returned to the library's exceptions.
    """
    def __init__(self, endpoint: str):
        self.endpoint, self.target = endpoint, public_target(endpoint)

    def post(self, url, *, data, headers, timeout=None):
        if url != self.endpoint:
            raise PushDeliveryError("push_target_changed", permanent=True)
        target = self.target
        pool = HTTPSConnectionPool(target.address, 443, cert_reqs=ssl.CERT_REQUIRED,
            ca_certs=requests.certs.where(), assert_hostname=target.host, server_hostname=target.host,
            timeout=Timeout(connect=4, read=8), retries=False, maxsize=1)
        try:
            response = pool.urlopen("POST", target.path, body=data, headers={**headers, "Host": target.host},
                redirect=False, retries=False, preload_content=False)
            result = requests.Response()
            result.status_code = response.status
            result.reason, result._content = "", b""
            response.close()
            return result
        finally:
            pool.close()


def send_push(subscription: dict, payload: str) -> None:
    if len(payload.encode("utf-8")) > 3000:
        raise PushDeliveryError("push_payload_too_large", permanent=True)
    from pywebpush import WebPushException, webpush
    key = vapid_key().private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    try:
        session = _PinnedSession(subscription["endpoint"])
        webpush(subscription, payload, vapid_private_key=base64.urlsafe_b64encode(key).decode(),
                vapid_claims={"sub": VAPID_SUBJECT}, requests_session=session, ttl=86400,
                timeout=12, headers={"Urgency": "high"}, verbose=False)
    except WebPushException as exc:
        code = exc.response.status_code if exc.response is not None else 0
        raise PushDeliveryError("push_registration_expired" if code in (404, 410) else "push_service_rejected",
                                permanent=code in (400, 401, 403, 404, 410, 413)) from None
    except ValueError:
        raise PushDeliveryError("push_endpoint_invalid", permanent=True) from None
    except PushDeliveryError:
        raise
    except Exception:
        raise PushDeliveryError("push_delivery_unavailable") from None
