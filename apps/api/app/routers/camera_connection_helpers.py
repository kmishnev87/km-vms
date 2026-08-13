import hashlib
import time
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.sanitization import redact_text
from app.core.security import decrypt_text
from app.models.camera import Camera
from app.services.onvif_service import rtsp_path_from_uri


PROOF_TTL_SECONDS = 15 * 60
ONVIF_PROBE_PROOFS: dict[str, dict] = {}
RTSP_TEST_PROOFS: dict[str, dict] = {}


def safe_onvif_error(exc: Exception) -> str:
    text = redact_text(str(exc) if exc else "")
    lower = text.lower()
    if "auth" in lower or "401" in lower or "forbidden" in lower:
        return "ONVIF service is reachable, but authentication failed. Check camera ONVIF credentials."
    if "connection refused" in lower or "failed to establish" in lower or "newconnectionerror" in lower:
        return "ONVIF service is not reachable on the selected host and port."
    if "timeout" in lower or "timed out" in lower:
        return "ONVIF service did not respond in time. Check ONVIF host, port, and network access."
    if len(text) > 180 or "traceback" in lower or "envelope" in lower or "soap" in lower:
        return "ONVIF operation failed. Check camera ONVIF service, permissions, and profile support."
    return text or "ONVIF operation failed."


def onvif_error_code(exc: Exception) -> str:
    text = redact_text(str(exc) if exc else "").lower()
    if "auth" in text or "401" in text or "forbidden" in text:
        return "wrong_credentials"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connection refused" in text:
        return "wrong_port_or_service_unavailable"
    if "failed to establish" in text or "newconnectionerror" in text or "unreachable" in text:
        return "wrong_ip_or_unreachable"
    if "media service" in text:
        return "media_service_unavailable"
    if "profile" in text:
        return "profiles_unavailable"
    if "stream" in text:
        return "stream_uri_unavailable"
    if "wsdl" in text or "onvif" in text:
        return "unsupported_onvif"
    return "unknown_safe_error"


def _password_fingerprint(payload: dict) -> str:
    password = str(payload.get("password") or "")
    return hashlib.sha256(password.encode("utf-8")).hexdigest() if password else ""


def management_fingerprint(payload: dict) -> dict:
    protocol = str(payload.get("protocol") or "rtsp").lower()
    rtsp_host = payload.get("rtsp_host") or (payload.get("host") if protocol == "onvif" else None)
    rtsp_port = payload.get("rtsp_port") or (554 if protocol == "onvif" else None)
    return {
        "protocol": protocol,
        "host": str(payload.get("host") or ""),
        "port": safe_int(payload.get("port"), 80 if protocol == "onvif" else 554),
        "rtsp_host": str(rtsp_host or ""),
        "rtsp_port": safe_int(rtsp_port, 0),
        "username": str(payload.get("username") or ""),
        "password_sha256": _password_fingerprint(payload),
        "onvif_path": saved_stream_path(payload.get("onvif_path")),
    }


def normalize_stream_role(value: str | None) -> str | None:
    role = str(value or "").strip().lower()
    return role if role in {"main", "sub"} else None


def stream_role_fingerprint(payload: dict, role: str) -> dict:
    normalized_role = normalize_stream_role(role)
    if normalized_role is None:
        raise ValueError("stream role must be main or sub")
    protocol = str(payload.get("protocol") or "rtsp").lower()
    host = payload.get("rtsp_host") if protocol == "onvif" else payload.get("host")
    if not host and protocol == "onvif":
        host = payload.get("host")
    port = payload.get("rtsp_port") if protocol == "onvif" else payload.get("port")
    if not port:
        port = 554
    path_key = f"rtsp_{normalized_role}_url"
    token_key = "onvif_profile_token" if normalized_role == "main" else "onvif_sub_profile_token"
    return {
        "role": normalized_role,
        "protocol": protocol,
        "rtsp_host": str(host or ""),
        "rtsp_port": safe_int(port, 554),
        "rtsp_transport": str(payload.get("rtsp_transport") or ""),
        "username": str(payload.get("username") or ""),
        "password_sha256": _password_fingerprint(payload),
        "stream_path": saved_stream_path(payload.get(path_key)),
        "profile_token": str(payload.get(token_key) or ""),
        "onvif_channel_id": str(payload.get("onvif_channel_id") or ""),
    }


def validation_fingerprint(payload: dict) -> dict:
    """Legacy aggregate fingerprint retained for compatibility-focused callers."""
    return {
        "management": management_fingerprint(payload),
        "main": stream_role_fingerprint(payload, "main"),
        "sub": stream_role_fingerprint(payload, "sub"),
    }


def register_validation_proof(
    store: dict[str, dict],
    payload: dict,
    *,
    fingerprint: dict | None = None,
    proof_type: str = "legacy",
    role: str | None = None,
) -> str:
    token = uuid4().hex
    store[token] = {
        "created_at": time.time(),
        "fingerprint": fingerprint if fingerprint is not None else validation_fingerprint(payload),
        "proof_type": proof_type,
        "role": role,
    }
    return token


def register_onvif_probe_proof(payload: dict) -> str:
    return register_validation_proof(
        ONVIF_PROBE_PROOFS,
        payload,
        fingerprint=management_fingerprint(payload),
        proof_type="onvif_management",
    )


def register_rtsp_test_proof(payload: dict, role: str | None = None) -> str:
    normalized_role = normalize_stream_role(role or payload.get("stream_role")) or "main"
    return register_validation_proof(
        RTSP_TEST_PROOFS,
        payload,
        fingerprint=stream_role_fingerprint(payload, normalized_role),
        proof_type="rtsp_exact_role",
        role=normalized_role,
    )


def store_has_valid_proof(store: dict[str, dict], token: str | None, payload: dict) -> bool:
    proof = store.get(token or "")
    if not proof:
        return False
    if time.time() - float(proof.get("created_at") or 0) > PROOF_TTL_SECONDS:
        store.pop(token or "", None)
        return False
    return proof.get("fingerprint") == validation_fingerprint(payload)


def store_has_valid_management_proof(token: str | None, payload: dict) -> bool:
    proof = ONVIF_PROBE_PROOFS.get(token or "")
    if not _proof_is_fresh(ONVIF_PROBE_PROOFS, token, proof):
        return False
    return (
        proof.get("proof_type") == "onvif_management"
        and proof.get("fingerprint") == management_fingerprint(payload)
    )


def _proof_is_fresh(store: dict[str, dict], token: str | None, proof: dict | None) -> bool:
    if not proof:
        return False
    if time.time() - float(proof.get("created_at") or 0) > PROOF_TTL_SECONDS:
        store.pop(token or "", None)
        return False
    return True


def store_has_valid_stream_proof(token: str | None, payload: dict, role: str) -> bool:
    normalized_role = normalize_stream_role(role)
    proof = RTSP_TEST_PROOFS.get(token or "")
    if normalized_role is None or not _proof_is_fresh(RTSP_TEST_PROOFS, token, proof):
        return False
    return (
        proof.get("proof_type") == "rtsp_exact_role"
        and proof.get("role") == normalized_role
        and proof.get("fingerprint") == stream_role_fingerprint(payload, normalized_role)
    )


def required_stream_roles(payload: dict) -> tuple[str, ...]:
    roles = []
    if saved_stream_path(payload.get("rtsp_main_url")):
        roles.append("main")
    if saved_stream_path(payload.get("rtsp_sub_url")):
        roles.append("sub")
    return tuple(roles)


def has_valid_onboarding_proof(payload: dict) -> bool:
    roles = required_stream_roles(payload)
    if not roles:
        return False
    main_token = safe_preview_token(
        payload.get("main_validation_token") or payload.get("validation_token")
    )
    sub_token = safe_preview_token(payload.get("sub_validation_token"))
    role_tokens = {"main": main_token, "sub": sub_token}
    if not all(store_has_valid_stream_proof(role_tokens[role], payload, role) for role in roles):
        return False
    if str(payload.get("protocol") or "rtsp").lower() != "onvif":
        return True
    probe_token = safe_preview_token(payload.get("onvif_probe_token"))
    return store_has_valid_management_proof(probe_token, payload)


def require_save_gate(payload: dict, *, connection_sensitive_change: bool) -> bool:
    if not connection_sensitive_change:
        return False
    if has_valid_onboarding_proof(payload):
        return False
    if bool(payload.get("manual_confirm_unverified")):
        return True
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "camera_validation_required",
            "message": "Camera connection must be tested/probed before saving, or explicitly saved as unverified.",
            "manual_confirm_available": True,
        },
    )


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def parse_bounded_int(value, *, field: str, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"code": f"invalid_{field}", "message": f"{field} must be an integer."})
    if parsed < minimum or parsed > maximum:
        raise HTTPException(
            status_code=400,
            detail={"code": f"{field}_out_of_range", "message": f"{field} must be between {minimum} and {maximum}."},
        )
    return parsed


def parse_port(value, *, field: str, default: int) -> int:
    return parse_bounded_int(value, field=field, default=default, minimum=1, maximum=65535)


def assemble_rtsp_url(
    host: str | None,
    port: int | None,
    username: str | None,
    password: str | None,
    value: str | None,
) -> str | None:
    if not value:
        return None

    value = str(value).strip()
    if not value:
        return None

    if value.lower().startswith("rtsp://"):
        return value

    if not host:
        return value

    path = value if value.startswith("/") else f"/{value}"
    auth = ""
    if username and password:
        auth = f"{quote(str(username), safe='')}:{quote(str(password), safe='')}@"
    elif username:
        auth = f"{quote(str(username), safe='')}@"

    port_part = f":{int(port)}" if port else ""
    return f"rtsp://{auth}{host}{port_part}{path}"


def get_camera_credentials(
    db: Session,
    payload: dict,
):
    camera = None
    camera_id = payload.get("camera_id")
    username = payload.get("username")
    password = payload.get("password")
    host = payload.get("host")
    port = payload.get("port")
    rtsp_host = payload.get("rtsp_host")
    rtsp_port = payload.get("rtsp_port")

    if camera_id is not None and str(camera_id).strip() != "":
        try:
            active_camera_id = int(camera_id)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_camera_id", "message": "Camera id is invalid."},
            )

        camera = (
            db.query(Camera)
            .filter(Camera.id == active_camera_id, Camera.deleted_at.is_(None))
            .first()
        )
        if not camera:
            raise HTTPException(
                status_code=404,
                detail={"code": "camera_not_active", "message": "Camera was not found or is no longer active."},
            )
        if not host:
            host = camera.host
        if not port:
            port = camera.port
        if not username:
            username = camera.username
        if not password:
            password = decrypt_text(camera.password_encrypted)
        if not rtsp_host:
            rtsp_host = camera.rtsp_reachable_host
        if not rtsp_port:
            rtsp_port = camera.rtsp_reachable_port

    return {
        "camera_id": camera_id,
        "camera": camera,
        "host": host,
        "port": port,
        "rtsp_host": rtsp_host,
        "rtsp_port": rtsp_port,
        "username": username,
        "password": password,
    }


def resolve_test_connection_payload(db: Session, payload: dict) -> dict:
    """Resolve a test request to the same final connection truth used by save."""
    resolved = dict(payload)
    creds = get_camera_credentials(db, payload)
    camera = creds.get("camera")
    if camera is not None:
        for field in (
            "protocol",
            "host",
            "port",
            "rtsp_main_url",
            "rtsp_sub_url",
            "rtsp_host",
            "rtsp_port",
            "rtsp_transport",
            "onvif_path",
            "onvif_profile_token",
            "onvif_sub_profile_token",
            "onvif_channel_id",
            "default_live_stream",
            "default_record_stream",
        ):
            if resolved.get(field) in (None, ""):
                resolved[field] = getattr(camera, field, None)
    resolved["host"] = creds.get("host") or resolved.get("host")
    resolved["port"] = creds.get("port") or resolved.get("port")
    resolved["rtsp_host"] = creds.get("rtsp_host") or resolved.get("rtsp_host")
    resolved["rtsp_port"] = creds.get("rtsp_port") or resolved.get("rtsp_port")
    resolved["username"] = creds.get("username") or resolved.get("username")
    resolved["password"] = creds.get("password") or resolved.get("password") or ""
    protocol = str(resolved.get("protocol") or "rtsp").lower()
    if protocol == "onvif":
        stream_host = resolved.get("rtsp_host") or resolved.get("host")
        stream_port = resolved.get("rtsp_port") or 554
    else:
        stream_host = resolved.get("host")
        stream_port = resolved.get("port") or 554
    for role in ("main", "sub"):
        key = f"rtsp_{role}_url"
        resolved[key] = assemble_rtsp_url(
            stream_host,
            stream_port,
            resolved.get("username"),
            resolved.get("password"),
            saved_stream_path(resolved.get(key)),
        )
    return resolved


def saved_stream_path(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower().startswith("rtsp://"):
        return rtsp_path_from_uri(text)
    return text if text.startswith("/") else f"/{text}"


def profile_matches_stream(profile: dict, saved_value: str | None) -> bool:
    saved_path = saved_stream_path(saved_value)
    if not saved_path:
        return False
    profile_path = saved_stream_path(profile.get("stream_path") or profile.get("stream_uri"))
    return bool(profile_path and profile_path == saved_path)


def apply_profile_assignments(data: dict, camera: Camera | None) -> dict:
    if not camera:
        return data

    main_token = str(camera.onvif_profile_token or "")
    sub_token = str(camera.onvif_sub_profile_token or "")
    main_path = camera.rtsp_main_url
    sub_path = camera.rtsp_sub_url
    assignments = {
        "main": {"profile_token": main_token or None, "stream_path": saved_stream_path(main_path)},
        "sub": {"profile_token": sub_token or None, "stream_path": saved_stream_path(sub_path)},
        "default_live_stream": camera.default_live_stream,
        "default_record_stream": camera.default_record_stream,
    }

    for profile in data.get("profiles") or []:
        roles = []
        token = str(profile.get("token") or "")
        if (main_token and token == main_token) or (not main_token and profile_matches_stream(profile, main_path)):
            roles.append("main")
            assignments["main"]["profile_token"] = token or assignments["main"]["profile_token"]
        if (sub_token and token == sub_token) or (not sub_token and profile_matches_stream(profile, sub_path)):
            roles.append("sub")
            assignments["sub"]["profile_token"] = token or assignments["sub"]["profile_token"]
        profile["assigned_roles"] = roles
        profile["assigned_role"] = "_".join(roles) if roles else "unknown"

    data["assignments"] = assignments
    return data


def build_test_url(
    payload: dict,
    db: Session | None = None,
    *,
    role: str | None = None,
) -> str | None:
    if db is not None:
        payload = resolve_test_connection_payload(db, payload)
    protocol = str(payload.get("protocol") or "rtsp").lower()
    rtsp_main_url = payload.get("rtsp_main_url")
    rtsp_sub_url = payload.get("rtsp_sub_url")
    if protocol == "rtsp":
        host = payload.get("host")
        port = payload.get("port") or 554
    else:
        host = payload.get("rtsp_host") or payload.get("host")
        port = payload.get("rtsp_port") or 554
    username = payload.get("username")
    password = payload.get("password")

    selected_role = normalize_stream_role(role or payload.get("stream_role"))
    if selected_role:
        selected_value = rtsp_main_url if selected_role == "main" else rtsp_sub_url
        return assemble_rtsp_url(host, port, username, password, selected_value)

    # Compatibility for older callers that did not name a role. New Cameras UI
    # always supplies an exact role and never crosses over to the other stream.
    return assemble_rtsp_url(host, port, username, password, rtsp_main_url) or assemble_rtsp_url(
        host, port, username, password, rtsp_sub_url
    )


def safe_preview_token(value: str | None) -> str | None:
    if not value:
        return None
    token = str(value).strip()
    if not token:
        return None
    if all(ch.isalnum() or ch in {"-", "_"} for ch in token):
        return token
    return None
