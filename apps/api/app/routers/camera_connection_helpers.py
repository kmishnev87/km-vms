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


def validation_fingerprint(payload: dict) -> dict:
    protocol = str(payload.get("protocol") or "rtsp").lower()
    rtsp_host = payload.get("rtsp_host") or (payload.get("host") if protocol == "onvif" else None)
    rtsp_port = payload.get("rtsp_port") or (554 if protocol == "onvif" else None)
    password = str(payload.get("password") or "")
    return {
        "protocol": protocol,
        "host": str(payload.get("host") or ""),
        "port": safe_int(payload.get("port"), 80 if protocol == "onvif" else 554),
        "rtsp_host": str(rtsp_host or ""),
        "rtsp_port": safe_int(rtsp_port, 0),
        "username": str(payload.get("username") or ""),
        "password_sha256": hashlib.sha256(password.encode("utf-8")).hexdigest() if password else "",
        "rtsp_main_url": str(payload.get("rtsp_main_url") or ""),
        "rtsp_sub_url": str(payload.get("rtsp_sub_url") or ""),
        "rtsp_transport": str(payload.get("rtsp_transport") or ""),
        "onvif_path": str(payload.get("onvif_path") or ""),
        "onvif_profile_token": str(payload.get("onvif_profile_token") or ""),
        "onvif_channel_id": str(payload.get("onvif_channel_id") or ""),
        "default_live_stream": str(payload.get("default_live_stream") or ""),
        "default_record_stream": str(payload.get("default_record_stream") or ""),
    }


def register_validation_proof(store: dict[str, dict], payload: dict) -> str:
    token = uuid4().hex
    store[token] = {"created_at": time.time(), "fingerprint": validation_fingerprint(payload)}
    return token


def register_onvif_probe_proof(payload: dict) -> str:
    return register_validation_proof(ONVIF_PROBE_PROOFS, payload)


def register_rtsp_test_proof(payload: dict) -> str:
    return register_validation_proof(RTSP_TEST_PROOFS, payload)


def store_has_valid_proof(store: dict[str, dict], token: str | None, payload: dict) -> bool:
    proof = store.get(token or "")
    if not proof:
        return False
    if time.time() - float(proof.get("created_at") or 0) > PROOF_TTL_SECONDS:
        store.pop(token or "", None)
        return False
    return proof.get("fingerprint") == validation_fingerprint(payload)


def has_valid_onboarding_proof(payload: dict) -> bool:
    validation_token = safe_preview_token(payload.get("validation_token"))
    if store_has_valid_proof(RTSP_TEST_PROOFS, validation_token, payload):
        return True
    token = safe_preview_token(payload.get("onvif_probe_token"))
    return store_has_valid_proof(ONVIF_PROBE_PROOFS, token, payload)


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
    main_path = camera.rtsp_main_url
    sub_path = camera.rtsp_sub_url
    assignments = {
        "main": {"profile_token": main_token or None, "stream_path": saved_stream_path(main_path)},
        "sub": {"profile_token": None, "stream_path": saved_stream_path(sub_path)},
        "default_live_stream": camera.default_live_stream,
        "default_record_stream": camera.default_record_stream,
    }

    for profile in data.get("profiles") or []:
        roles = []
        token = str(profile.get("token") or "")
        if (main_token and token == main_token) or profile_matches_stream(profile, main_path):
            roles.append("main")
            assignments["main"]["profile_token"] = token or assignments["main"]["profile_token"]
        if profile_matches_stream(profile, sub_path):
            roles.append("sub")
            assignments["sub"]["profile_token"] = token or assignments["sub"]["profile_token"]
        profile["assigned_roles"] = roles
        profile["assigned_role"] = "_".join(roles) if roles else "unknown"

    data["assignments"] = assignments
    return data


def build_test_url(payload: dict, db: Session | None = None) -> str | None:
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

    if db is not None:
        creds = get_camera_credentials(db, payload)
        if protocol == "rtsp":
            host = creds["host"] or host
            port = creds["port"] or port
        else:
            host = payload.get("rtsp_host") or creds["rtsp_host"] or host
            port = payload.get("rtsp_port") or creds["rtsp_port"] or port
        username = creds["username"] or username
        password = creds["password"] or password
        camera = creds.get("camera")
        if camera is not None:
            rtsp_main_url = rtsp_main_url if rtsp_main_url not in (None, "") else camera.rtsp_main_url
            rtsp_sub_url = rtsp_sub_url if rtsp_sub_url not in (None, "") else camera.rtsp_sub_url

    return (
        assemble_rtsp_url(
            host,
            port,
            username,
            password,
            rtsp_main_url,
        )
        or
        assemble_rtsp_url(
            host,
            port,
            username,
            password,
            rtsp_sub_url,
        )
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
