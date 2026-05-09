from pathlib import Path
import shutil
import subprocess
import json
import time
import hashlib
from datetime import datetime
from uuid import uuid4
from urllib.parse import quote, urlparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import user_has_permission
from app.core.sanitization import redact_text
from app.core.security import encrypt_text, decrypt_text
from app.db.session import get_db
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.user import User
from app.routers.deps import FORBIDDEN_DETAIL, get_current_user, require_permission
from app.schemas.camera import CameraCreate, CameraResponse, CameraUpdate
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.storage import build_unique_folder_name, ensure_camera_folder
from app.services.onvif_service import (
    build_onvif_health_contract,
    check_onvif_events_feasibility,
    discover_onvif_devices,
    execute_onvif_ptz_command,
    fetch_onvif_profiles,
    get_onvif_profile_config,
    get_onvif_ptz_capabilities,
    probe_onvif_device,
    rtsp_path_from_uri,
    update_onvif_profile,
    validate_ptz_command_payload,
    ptz_validation_response,
    ptz_command_limits,
)
from app.services.recording_retention import execute_segments, preview_segments

router = APIRouter(prefix="/cameras", tags=["cameras"])
viewer_router = APIRouter(prefix="/viewer/cameras", tags=["viewer-cameras"])

CAMERA_DELETE_FILES_REASON = "camera_delete_with_files"
CAMERA_DELETE_NO_FILES_BLOCK_REASON = "recordings_exist_delete_files_false_requires_safe_policy"
CAMERA_DELETE_UNSAFE_WITH_FILES_REASON = "camera_delete_with_files_requires_all_segments_safe"
CAMERA_DELETE_NO_PERMISSION_REASON = "delete_recordings_permission_missing"
PROOF_TTL_SECONDS = 15 * 60
ONVIF_PROBE_PROOFS: dict[str, dict] = {}
RTSP_TEST_PROOFS: dict[str, dict] = {}
CONNECTION_SENSITIVE_FIELDS = {
    "protocol",
    "host",
    "port",
    "username",
    "password",
    "rtsp_main_url",
    "rtsp_sub_url",
    "rtsp_host",
    "rtsp_port",
    "rtsp_transport",
    "onvif_path",
    "onvif_profile_token",
    "onvif_channel_id",
}
SECRET_METADATA_FIELDS = {
    "password",
    "password_encrypted",
    "rtsp_main_url",
    "rtsp_sub_url",
    "preview_token",
    "validation_token",
    "onvif_probe_token",
}


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


def safe_changed_metadata(changed: dict) -> dict:
    safe = {}
    for key, value in changed.items():
        if key in SECRET_METADATA_FIELDS:
            safe[key] = {
                "changed": True,
                "value_redacted": True,
            }
        else:
            safe[key] = value
    return safe


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


def get_active_camera_or_404(db: Session, camera_id: int) -> Camera:
    camera = db.query(Camera).filter(Camera.id == int(camera_id), Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail={"code": "camera_not_active", "message": "Camera was not found or is no longer active."})
    return camera


def safe_camera_onvif_credentials(camera: Camera) -> dict:
    if str(camera.protocol or "").lower() != "onvif":
        return {
            "ok": True,
            "supported": False,
            "source": "not_onvif",
            "can_pan_tilt": False,
            "can_zoom": False,
            "can_stop": False,
            "can_presets": False,
            "limits": ptz_command_limits(),
            "warnings": ["camera_protocol_is_not_onvif"],
            "unsupported_reasons": ["not_onvif"],
            "raw_secret_exposed": False,
        }
    password = decrypt_text(camera.password_encrypted)
    if not camera.host or not camera.port or not camera.username or not password:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "camera_onvif_credentials_required",
                "message": "ONVIF camera host, port, username and password are required for camera controls.",
            },
        )
    return {
        "host": camera.host,
        "port": int(camera.port or 80),
        "username": camera.username,
        "password": password,
    }


def run_bounded_read_only_check(callable_obj, *, timeout_seconds: int = 8, **kwargs):
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(callable_obj, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeout:
        future.cancel()
        raise TimeoutError("onvif_health_check_timeout")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


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


@router.post("/onvif/profiles")
def onvif_profiles(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)

    host = creds["host"]
    port = creds["port"] or 80
    username = creds["username"]
    password = creds["password"]

    if not host or not username or not password:
        raise HTTPException(status_code=400, detail="Для ONVIF нужны host, username, password")

    try:
        data = fetch_onvif_profiles(
            host=str(host),
            port=int(port),
            username=str(username),
            password=str(password),
            rtsp_host=str(creds["rtsp_host"] or host),
            rtsp_port=int(creds["rtsp_port"] or 554),
        )
        data = apply_profile_assignments(data, creds.get("camera"))
        return {
            "ok": True,
            **data,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=safe_onvif_error(e))


@router.post("/onvif/discover")
def onvif_discover(
    payload: dict | None = None,
    current_user: User = Depends(require_permission("manage_cameras")),
):
    timeout = parse_bounded_int((payload or {}).get("timeout_seconds"), field="timeout_seconds", default=5, minimum=1, maximum=10)
    return discover_onvif_devices(timeout_seconds=timeout)


@router.post("/onvif/probe")
def onvif_probe(
    payload: dict,
    current_user: User = Depends(require_permission("manage_cameras")),
):
    host = str(payload.get("host") or "").strip()
    port = parse_port(payload.get("port"), field="port", default=80)
    rtsp_host = str(payload.get("rtsp_host") or host).strip()
    rtsp_port = parse_port(payload.get("rtsp_port"), field="rtsp_port", default=554)
    timeout = parse_bounded_int(payload.get("timeout_seconds"), field="timeout_seconds", default=5, minimum=1, maximum=10)
    if not host:
        raise HTTPException(status_code=400, detail={"code": "host_required", "message": "ONVIF host is required."})

    request_payload = {
        "protocol": "onvif",
        "host": host,
        "port": port,
        "rtsp_host": rtsp_host,
        "rtsp_port": rtsp_port,
        "username": payload.get("username"),
        "password": payload.get("password") or "",
    }
    try:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            probe_onvif_device,
            host=host,
            port=port,
            username=payload.get("username") or "",
            password=payload.get("password") or "",
            rtsp_host=rtsp_host,
            rtsp_port=rtsp_port,
            timeout_seconds=timeout,
        )
        try:
            result = future.result(timeout=timeout)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        result["onvif_probe_token"] = register_onvif_probe_proof(request_payload)
        return result
    except FutureTimeout:
        raise HTTPException(status_code=400, detail={"code": "timeout", "message": "ONVIF service did not respond in time.", "raw_secret_exposed": False})
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": onvif_error_code(exc),
                "message": safe_onvif_error(exc),
                "raw_secret_exposed": False,
            },
        )


@router.post("/onvif/profile_config")
def onvif_profile_config(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)
    if not creds["host"] or not creds["username"] or not creds["password"] or not payload.get("profile_token"):
        raise HTTPException(status_code=400, detail="ONVIF profile settings require host, credentials, and profile token.")

    try:
        return get_onvif_profile_config(
            host=str(creds["host"]),
            port=int(creds["port"] or 80),
            username=str(creds["username"]),
            password=str(creds["password"]),
            profile_token=str(payload["profile_token"]),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=safe_onvif_error(e))


@router.post("/onvif/update_profile")
def update_onvif_profile_route(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)
    if not creds["host"] or not creds["username"] or not creds["password"] or not payload.get("profile_token"):
        raise HTTPException(status_code=400, detail="ONVIF profile update requires host, credentials, and profile token.")

    try:
        return update_onvif_profile(
            host=str(creds["host"]),
            port=int(creds["port"] or 80),
            username=str(creds["username"]),
            password=str(creds["password"]),
            profile_token=str(payload["profile_token"]),
            config=payload["config"],
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=safe_onvif_error(e))


@router.get("/{camera_id}/onvif/ptz/capabilities")
def onvif_ptz_capabilities(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = get_active_camera_or_404(db, camera_id)
    creds = safe_camera_onvif_credentials(camera)
    if creds.get("source") == "not_onvif":
        return {
            "camera_id": camera.id,
            "camera_name": camera.name,
            **creds,
        }
    try:
        result = get_onvif_ptz_capabilities(**creds)
        return {
            "camera_id": camera.id,
            "camera_name": camera.name,
            **result,
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "camera_id": camera.id,
            "camera_name": camera.name,
            "ok": True,
            "supported": False,
            "source": "unreachable",
            "can_pan_tilt": False,
            "can_zoom": False,
            "can_stop": False,
            "can_presets": False,
            "limits": ptz_command_limits(),
            "warnings": [onvif_error_code(e)],
            "unsupported_reasons": ["onvif_ptz_capability_check_failed"],
            "message": safe_onvif_error(e),
            "raw_secret_exposed": False,
        }


@router.post("/{camera_id}/onvif/ptz/command")
def onvif_ptz_command(
    camera_id: int,
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = get_active_camera_or_404(db, camera_id)
    try:
        command = validate_ptz_command_payload(payload)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": str(e), "message": "PTZ command payload is invalid."},
        )

    if command["validation_only"]:
        result = ptz_validation_response(command)
    else:
        creds = safe_camera_onvif_credentials(camera)
        if creds.get("source") == "not_onvif":
            raise HTTPException(
                status_code=400,
                detail={"code": "camera_not_onvif", "message": "PTZ commands require an ONVIF camera."},
            )
        try:
            result = execute_onvif_ptz_command(**creds, payload=payload)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail={"code": str(e), "message": "PTZ command payload is invalid."},
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"code": onvif_error_code(e), "message": safe_onvif_error(e)},
            )

    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.ptz_command",
        message_ru=f"{current_user.username} проверил PTZ команду для камеры {camera.name}",
        message_en=f"{current_user.username} validated PTZ command for camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
        metadata={
            "action": result.get("action"),
            "execution_mode": result.get("execution_mode"),
            "executed": bool(result.get("executed")),
            "physical_camera_mutated": bool(result.get("physical_camera_mutated")),
            "duration_seconds": result.get("duration_seconds"),
        },
    )
    return {
        "camera_id": camera.id,
        "camera_name": camera.name,
        **result,
    }


@router.get("/{camera_id}/onvif/health")
def onvif_health(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = get_active_camera_or_404(db, camera_id)
    return build_onvif_health_contract(camera, check_performed=False)


@router.post("/{camera_id}/onvif/health/check")
def onvif_health_check(
    camera_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = get_active_camera_or_404(db, camera_id)
    checked_at = datetime.utcnow().isoformat() + "Z"
    profiles_result = None
    ptz_result = None
    events_result = None
    check_status = "not_checked"
    reason_codes: list[str] = []

    if str(camera.protocol or "").lower() != "onvif":
        result = build_onvif_health_contract(camera, check_performed=True, checked_at=checked_at)
        check_status = "unsupported"
        reason_codes = ["not_onvif"]
    else:
        password = decrypt_text(camera.password_encrypted)
        if not camera.host or not camera.port or not camera.username or not password:
            result = build_onvif_health_contract(camera, password=None, check_performed=True, checked_at=checked_at)
            check_status = "misconfigured"
            reason_codes = ["onvif_credentials_required"]
        else:
            try:
                profiles_result = run_bounded_read_only_check(
                    fetch_onvif_profiles,
                    host=str(camera.host),
                    port=int(camera.port or 80),
                    username=str(camera.username),
                    password=str(password),
                    rtsp_host=str(camera.rtsp_reachable_host or camera.host),
                    rtsp_port=int(camera.rtsp_reachable_port or 554),
                )
                check_status = "reachable"
            except Exception as exc:
                reason_codes.append(onvif_error_code(exc))
                profiles_result = None
                check_status = "unreachable"

            try:
                ptz_result = run_bounded_read_only_check(
                    get_onvif_ptz_capabilities,
                    host=str(camera.host),
                    port=int(camera.port or 80),
                    username=str(camera.username),
                    password=str(password),
                )
            except Exception as exc:
                reason_codes.append(onvif_error_code(exc))
                ptz_result = {
                    "supported": False,
                    "source": "unknown",
                    "unsupported_reasons": ["ptz_check_failed"],
                    "raw_secret_exposed": False,
                }

            try:
                events_result = run_bounded_read_only_check(
                    check_onvif_events_feasibility,
                    host=str(camera.host),
                    port=int(camera.port or 80),
                    username=str(camera.username),
                    password=str(password),
                )
            except Exception as exc:
                reason_codes.append(onvif_error_code(exc))
                events_result = {
                    "events_supported": False,
                    "events_status": "unknown",
                    "reason_codes": ["events_feasibility_check_failed"],
                    "limitations": ["feasibility_only_no_subscription_started"],
                    "raw_secret_exposed": False,
                }

            result = build_onvif_health_contract(
                camera,
                password=password,
                profiles_result=profiles_result,
                ptz_result=ptz_result,
                events_result=events_result,
                check_performed=True,
                checked_at=checked_at,
            )
            if reason_codes:
                result["warnings"] = sorted(set(result.get("warnings", [])) | set(reason_codes))

    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.onvif_health_check",
        message_ru=f"{current_user.username} выполнил ONVIF health check для камеры {camera.name}",
        message_en=f"{current_user.username} ran ONVIF health check for camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
        metadata={
            "check_type": "onvif_health",
            "status": check_status,
            "reason_codes": sorted(set(reason_codes)),
            "domains_checked": sorted(result.get("compatibility_matrix", {}).keys()),
            "redaction_status": "sanitized",
            "raw_secret_exposed": False,
        },
    )
    return result


@router.get("", response_model=list[CameraResponse])
def list_cameras(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    return db.query(Camera).filter(Camera.deleted_at.is_(None)).order_by(Camera.name.asc()).all()


@viewer_router.get("")
def list_viewer_cameras(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    role = getattr(current_user, "role", "")
    if not (user_has_permission(role, "view_live") or user_has_permission(role, "view_timeline")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)

    cameras = db.query(Camera).filter(Camera.deleted_at.is_(None)).order_by(Camera.name.asc()).all()
    return [
        {
            "id": camera.id,
            "name": camera.name,
            "enabled": bool(camera.enabled),
            "host": camera.host,
            "port": camera.port,
            "status": camera.status,
            "default_live_stream": camera.default_live_stream,
            "rtsp_main_url": bool(camera.rtsp_main_url),
            "rtsp_sub_url": bool(camera.rtsp_sub_url),
        }
        for camera in cameras
    ]


@router.get("/{camera_id}", response_model=CameraResponse)
def get_camera(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    return camera


@router.post("/test")
def test_camera(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    input_url = build_test_url(payload, db=db)
    if not input_url:
        raise HTTPException(status_code=400, detail="Укажите RTSP path или URL для проверки камеры.")

    transport = payload.get("rtsp_transport") or "tcp"

    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", transport,
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        input_url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=400, detail="Камера не ответила вовремя. Проверьте адрес, порт и доступность камеры.")

    if result.returncode != 0:
        raise HTTPException(status_code=400, detail="Не удалось подключиться к камере. Проверьте RTSP path, логин, пароль и сетевой доступ.")

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Камера ответила нестандартно. Проверьте параметры потока.")

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    preview_path, preview_url, preview_token = test_preview_destination(payload)
    preview_ok = capture_camera_preview(input_url, transport, preview_path)
    validation_token = register_rtsp_test_proof(payload)

    return {
        "ok": True,
        "display_path": safe_rtsp_display_path(input_url),
        "transport": transport,
        "preview_url": preview_url if preview_ok else None,
        "preview_token": preview_token if preview_ok and preview_token else None,
        "validation_token": validation_token,
        "preview_ok": preview_ok,
        "preview_message": None if preview_ok else "Соединение установлено, но кадр превью получить не удалось.",
        "video": {
            "codec": video.get("codec_name") if video else None,
            "profile": video.get("profile") if video else None,
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "fps": video.get("r_frame_rate") if video else None,
            "pix_fmt": video.get("pix_fmt") if video else None,
        } if video else None,
        "audio": {
            "codec": audio.get("codec_name") if audio else None,
            "sample_rate": audio.get("sample_rate") if audio else None,
            "channels": audio.get("channels") if audio else None,
        } if audio else None,
        "format": {
            "format_name": data.get("format", {}).get("format_name"),
            "bit_rate": data.get("format", {}).get("bit_rate"),
        },
    }


@router.post("", response_model=CameraResponse, status_code=status.HTTP_201_CREATED)
def create_camera(
    payload: CameraCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    existing = db.query(Camera).filter(Camera.name == payload.name, Camera.deleted_at.is_(None)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Камера с таким именем уже существует")

    payload_dict = payload.model_dump()
    manual_unverified = require_save_gate(payload_dict, connection_sensitive_change=True)
    folder_name = build_unique_folder_name(db, payload.name)
    ensure_camera_folder(folder_name)

    rtsp_host = payload.rtsp_host or payload.host
    rtsp_port = payload.rtsp_port or 554
    if str(payload.protocol or "rtsp").lower() == "rtsp":
        rtsp_host = payload.host
        rtsp_port = payload.port

    rtsp_main_url = assemble_rtsp_url(
        rtsp_host,
        rtsp_port,
        payload.username,
        payload.password,
        payload.rtsp_main_url,
    )
    rtsp_sub_url = assemble_rtsp_url(
        rtsp_host,
        rtsp_port,
        payload.username,
        payload.password,
        payload.rtsp_sub_url,
    )

    camera = Camera(
        name=payload.name,
        storage_folder_name=folder_name,
        enabled=payload.enabled,
        protocol=payload.protocol,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password_encrypted=encrypt_text(payload.password),
        rtsp_main_url=rtsp_main_url,
        rtsp_sub_url=rtsp_sub_url,
        rtsp_host=rtsp_host if str(payload.protocol or "rtsp").lower() == "onvif" else None,
        rtsp_port=rtsp_port if str(payload.protocol or "rtsp").lower() == "onvif" else None,
        rtsp_transport=payload.rtsp_transport,
        onvif_path=payload.onvif_path,
        onvif_profile_token=payload.onvif_profile_token,
        onvif_channel_id=payload.onvif_channel_id,
        recording_mode=payload.recording_mode,
        default_live_stream=payload.default_live_stream,
        default_record_stream=payload.default_record_stream,
        segment_minutes=payload.segment_minutes,
        retention_days=payload.retention_days,
        storage_quota_gb=payload.storage_quota_gb,
        status="manual_unverified" if manual_unverified else "created",
        last_error="created_unverified" if manual_unverified else None,
    )

    db.add(camera)
    db.commit()
    db.refresh(camera)
    attach_test_preview_to_camera(payload.preview_token, camera.id)
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.created",
        message_ru=f"{current_user.username} добавил камеру {camera.name}",
        message_en=f"{current_user.username} added camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        metadata={
            "protocol": camera.protocol,
            "enabled": bool(camera.enabled),
            "default_live_stream": camera.default_live_stream,
            "default_record_stream": camera.default_record_stream,
            "validation_state": "manual_unverified" if manual_unverified else "verified",
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return camera


def safe_rtsp_display_path(input_url: str | None) -> str:
    if not input_url:
        return "RTSP path указан"
    try:
        parsed = urlparse(input_url)
        path = f"{parsed.path or ''}{('?' + parsed.query) if parsed.query else ''}"
        return path or "RTSP path указан"
    except Exception:
        return "RTSP path указан"


def safe_preview_token(value: str | None) -> str | None:
    if not value:
        return None
    token = str(value).strip()
    if not token:
        return None
    if all(ch.isalnum() or ch in {"-", "_"} for ch in token):
        return token
    return None


def capture_camera_preview(input_url: str, transport: str, output_path: Path) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-v", "error",
        "-rtsp_transport", transport,
        "-i", input_url,
        "-vf", "scale=640:-2:flags=lanczos,boxblur=0.25",
        "-frames:v", "1",
        "-q:v", "5",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        return False
    return output_path.exists() and output_path.stat().st_size > 0


def test_preview_destination(payload: dict) -> tuple[Path, str, str]:
    camera_id = payload.get("camera_id")
    if camera_id:
        path = settings.camera_preview_path(int(camera_id))
        return path, settings.camera_preview_url(int(camera_id)), ""
    token = uuid4().hex
    path = settings.camera_test_preview_path(token)
    return path, settings.camera_test_preview_url(token), token


def attach_test_preview_to_camera(token: str | None, camera_id: int) -> None:
    safe_token = safe_preview_token(token)
    if not safe_token:
        return
    source = settings.camera_test_preview_path(safe_token)
    if not source.exists():
        return
    destination = settings.camera_preview_path(camera_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    source.unlink(missing_ok=True)


def camera_recording_segments(db: Session, camera_id: int) -> list[RecordingSegment]:
    return (
        db.query(RecordingSegment)
        .filter(RecordingSegment.camera_id == int(camera_id))
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
        .all()
    )


def deletion_reason_counts(result: dict) -> dict:
    counts = {}
    for item in result.get("items") or []:
        reason = item.get("reason") or "unknown"
        counts[reason] = int(counts.get(reason) or 0) + 1
    for reason, value in (result.get("reason_counts") or {}).items():
        counts.setdefault(reason, int(value or 0))
    return counts


def compact_deletion_result(result: dict) -> dict:
    return {
        "ok": bool(result.get("ok")),
        "operation": result.get("operation"),
        "dry_run": bool(result.get("dry_run")),
        "requested_count": int(result.get("requested_count") or 0),
        "planned_count": int(result.get("planned_count") or 0),
        "deleted_count": int(result.get("deleted_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "not_found_count": int(result.get("not_found_count") or 0),
        "bytes_freed": int(result.get("bytes_freed") or 0),
        "estimated_freed_bytes": int(result.get("estimated_freed_bytes") or result.get("bytes_freed") or 0),
        "reason_counts": deletion_reason_counts(result),
        "active_blockers": int(deletion_reason_counts(result).get("active_job") or 0),
        "limit_exceeded": bool(result.get("limit_exceeded")),
        "warnings": list(result.get("warnings") or []),
        "items": [
            {
                "segment_id": item.get("segment_id"),
                "camera_id": item.get("camera_id"),
                "action": item.get("action"),
                "reason": item.get("reason"),
                "size_bytes": int(item.get("size_bytes") or 0),
                "error": item.get("error"),
            }
            for item in (result.get("items") or [])[:100]
        ],
    }


def mark_camera_delete_preview_unsafe(recordings: dict) -> dict:
    if (
        recordings["requested_count"] != recordings["planned_count"]
        or recordings["failed_count"]
        or recordings["skipped_count"]
    ):
        recordings["ok"] = False
        if CAMERA_DELETE_UNSAFE_WITH_FILES_REASON not in recordings["warnings"]:
            recordings["warnings"].append(CAMERA_DELETE_UNSAFE_WITH_FILES_REASON)
    return recordings


def camera_delete_response(
    *,
    camera: Camera,
    delete_files: bool,
    status_value: str,
    recordings: dict,
    preview_deleted: bool = False,
) -> dict:
    warnings = list(recordings.get("warnings") or [])
    return {
        "ok": status_value in {"deleted", "deleted_archive_cleanup_partial"},
        "status": status_value,
        "camera_id": camera.id,
        "camera_name": camera.name,
        "camera_removed": status_value in {"deleted", "deleted_archive_cleanup_partial"},
        "delete_files": bool(delete_files),
        "recordings": recordings,
        "archive_cleanup": recordings,
        "warnings": warnings,
        "preview_cleanup": {
            "deleted": bool(preview_deleted),
            "scope": "camera_preview_only",
        },
    }


def require_recording_delete_permission_for_camera_delete(current_user: User) -> None:
    if not user_has_permission(getattr(current_user, "role", ""), "delete_recordings"):
        raise HTTPException(status_code=403, detail="delete_files=true requires delete_recordings permission")


def audit_camera_delete(
    db: Session,
    *,
    actor: User,
    request: Request,
    event_type: str,
    severity: str,
    camera: Camera,
    delete_files: bool,
    status_value: str,
    recordings: dict,
    camera_name: str | None = None,
) -> None:
    target_camera_name = camera_name or camera.name
    create_event(
        db=db,
        actor=actor,
        category="cameras",
        event_type=event_type,
        severity=severity,
        message_ru=f"{actor.username} запросил удаление камеры {camera.name}: {status_value}",
        message_en=f"{actor.username} requested camera deletion for {target_camera_name}: {status_value}",
        target_type="camera",
        target_id=camera.id,
        target_name=target_camera_name,
        metadata={
            "delete_files": bool(delete_files),
            "status": status_value,
            "recordings": {
                "requested_count": recordings.get("requested_count"),
                "planned_count": recordings.get("planned_count"),
                "deleted_count": recordings.get("deleted_count"),
                "skipped_count": recordings.get("skipped_count"),
                "failed_count": recordings.get("failed_count"),
                "bytes_freed": recordings.get("bytes_freed"),
                "reason_counts": recordings.get("reason_counts"),
                "active_blockers": recordings.get("active_blockers"),
            },
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )


def delete_camera_preview(camera_id: int) -> bool:
    preview_path = settings.camera_preview_path(camera_id)
    existed = preview_path.exists()
    preview_path.unlink(missing_ok=True)
    return existed


def deleted_unique_value(value: str, camera_id: int) -> str:
    suffix = f"__deleted_{int(camera_id)}_{int(time.time())}"
    return f"{str(value or 'camera')[: max(1, 255 - len(suffix))]}{suffix}"


@router.put("/{camera_id}", response_model=CameraResponse)
def update_camera(
    camera_id: int,
    payload: CameraUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    data = payload.model_dump(exclude_unset=True)
    preview_token = data.pop("preview_token", None)
    validation_token = data.pop("validation_token", None)
    onvif_probe_token = data.pop("onvif_probe_token", None)
    manual_confirm_unverified = bool(data.pop("manual_confirm_unverified", False))

    if "name" in data and data["name"] != camera.name:
        existing = db.query(Camera).filter(Camera.name == data["name"], Camera.deleted_at.is_(None)).first()
        if existing and existing.id != camera.id:
            raise HTTPException(status_code=400, detail="Камера с таким именем уже существует")

    existing_password = decrypt_text(camera.password_encrypted)
    password_for_rtsp = data.get("password") if data.get("password") else existing_password
    username_for_rtsp = data.get("username", camera.username)
    next_protocol = str(data.get("protocol", camera.protocol or "rtsp")).lower()
    if next_protocol == "rtsp":
        data.pop("rtsp_host", None)
        data.pop("rtsp_port", None)
        host_for_rtsp = data.get("host", camera.host)
        port_for_rtsp = data.get("port", camera.port)
        next_rtsp_host = None
        next_rtsp_port = None
    else:
        next_rtsp_host = data.pop("rtsp_host", None) or camera.rtsp_host or data.get("host", camera.host)
        next_rtsp_port = data.pop("rtsp_port", None) or camera.rtsp_port or 554
        host_for_rtsp = next_rtsp_host
        port_for_rtsp = next_rtsp_port

    if "rtsp_main_url" in data and data["rtsp_main_url"] is not None:
        data["rtsp_main_url"] = assemble_rtsp_url(
            host_for_rtsp,
            port_for_rtsp,
            username_for_rtsp,
            password_for_rtsp,
            data["rtsp_main_url"],
        )

    if "rtsp_sub_url" in data and data["rtsp_sub_url"] is not None:
        data["rtsp_sub_url"] = assemble_rtsp_url(
            host_for_rtsp,
            port_for_rtsp,
            username_for_rtsp,
            password_for_rtsp,
            data["rtsp_sub_url"],
        )

    if "password" in data and data["password"]:
        camera.password_encrypted = encrypt_text(data.pop("password"))
    elif "password" in data:
        data.pop("password")

    old_values = {
        "name": camera.name,
        "enabled": bool(camera.enabled),
        "protocol": camera.protocol,
        "host": camera.host,
        "port": camera.port,
        "username": camera.username,
        "rtsp_host": camera.rtsp_host,
        "rtsp_port": camera.rtsp_port,
        "rtsp_main_url": camera.rtsp_main_url,
        "rtsp_sub_url": camera.rtsp_sub_url,
        "rtsp_transport": camera.rtsp_transport,
        "onvif_path": camera.onvif_path,
        "onvif_profile_token": camera.onvif_profile_token,
        "onvif_channel_id": camera.onvif_channel_id,
        "recording_mode": camera.recording_mode,
        "default_live_stream": camera.default_live_stream,
        "default_record_stream": camera.default_record_stream,
        "segment_minutes": camera.segment_minutes,
        "retention_days": camera.retention_days,
        "storage_quota_gb": camera.storage_quota_gb,
    }
    proposed_values = {
        **old_values,
        **data,
        "rtsp_host": next_rtsp_host,
        "rtsp_port": next_rtsp_port,
    }
    connection_sensitive_change = any(old_values.get(key) != proposed_values.get(key) for key in CONNECTION_SENSITIVE_FIELDS if key != "password")
    connection_sensitive_change = connection_sensitive_change or bool(payload.password)
    gate_payload = {
        **proposed_values,
        "password": payload.password or existing_password or "",
        "validation_token": validation_token,
        "onvif_probe_token": onvif_probe_token,
        "manual_confirm_unverified": manual_confirm_unverified,
    }
    manual_unverified = require_save_gate(gate_payload, connection_sensitive_change=connection_sensitive_change)

    for key, value in data.items():
        setattr(camera, key, value)
    camera.rtsp_host = next_rtsp_host
    camera.rtsp_port = next_rtsp_port
    if manual_unverified:
        camera.status = "manual_unverified"
        camera.last_error = "updated_unverified"

    db.add(camera)
    db.commit()
    db.refresh(camera)
    attach_test_preview_to_camera(preview_token, camera.id)
    changed = {}
    for key, old_value in old_values.items():
        new_value = getattr(camera, key, None)
        if old_value != new_value:
            changed[key] = {"old": old_value, "new": new_value}
    credential_changed = bool(payload.password)
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.updated",
        message_ru=f"{current_user.username} изменил камеру {camera.name}",
        message_en=f"{current_user.username} updated camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        metadata={
            "changed": safe_changed_metadata(changed),
            "credential_changed": credential_changed,
            "validation_state": "manual_unverified" if manual_unverified else ("unchanged" if not connection_sensitive_change else "verified"),
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return camera


@router.post("/{camera_id}/enable", response_model=CameraResponse)
def enable_camera(
    camera_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    camera.enabled = True
    camera.status = "enabled"
    camera.last_error = None
    db.add(camera)
    db.commit()
    db.refresh(camera)
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.enabled",
        message_ru=f"{current_user.username} включил камеру {camera.name}",
        message_en=f"{current_user.username} enabled camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return camera


@router.post("/{camera_id}/disable", response_model=CameraResponse)
def disable_camera(
    camera_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    camera.enabled = False
    camera.status = "disabled"
    camera.last_error = None
    db.add(camera)
    db.commit()
    db.refresh(camera)
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.disabled",
        message_ru=f"{current_user.username} отключил камеру {camera.name}",
        message_en=f"{current_user.username} disabled camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return camera


@router.get("/{camera_id}/delete-preview")
def preview_delete_camera(
    camera_id: int,
    request: Request,
    delete_files: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    segments = camera_recording_segments(db, camera.id)
    if delete_files:
        require_recording_delete_permission_for_camera_delete(current_user)
        recordings = mark_camera_delete_preview_unsafe(
            compact_deletion_result(
                preview_segments(
                    db,
                    segments,
                    operation="camera_delete_preview",
                    reason=CAMERA_DELETE_FILES_REASON,
                )
            )
        )
    else:
        recordings = {
            "ok": len(segments) == 0,
            "operation": "camera_delete_preview",
            "dry_run": True,
            "requested_count": len(segments),
            "planned_count": 0,
            "deleted_count": 0,
            "skipped_count": len(segments),
            "failed_count": 0,
            "not_found_count": 0,
            "bytes_freed": 0,
            "estimated_freed_bytes": 0,
            "reason_counts": {CAMERA_DELETE_NO_FILES_BLOCK_REASON: len(segments)} if segments else {},
            "active_blockers": 0,
            "limit_exceeded": False,
            "warnings": [],
            "items": [
                {
                    "segment_id": segment.id,
                    "camera_id": segment.camera_id,
                    "action": "skipped",
                    "reason": CAMERA_DELETE_NO_FILES_BLOCK_REASON,
                    "size_bytes": int(segment.size_bytes or 0),
                    "error": None,
                }
                for segment in segments[:100]
            ],
        }

    status_value = "preview_safe" if recordings["ok"] else "preview_blocked"
    audit_camera_delete(
        db,
        actor=current_user,
        request=request,
        event_type="cameras.delete_preview",
        severity="info" if recordings["ok"] else "warning",
        camera=camera,
        delete_files=delete_files,
        status_value=status_value,
        recordings=recordings,
    )
    return camera_delete_response(
        camera=camera,
        delete_files=delete_files,
        status_value=status_value,
        recordings=recordings,
    )


@router.delete("/{camera_id}")
def delete_camera(
    camera_id: int,
    request: Request,
    delete_files: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    segments = camera_recording_segments(db, camera.id)

    if not delete_files:
        recordings = {
            "ok": True,
            "operation": "camera_delete_without_files",
            "dry_run": False,
            "requested_count": len(segments),
            "planned_count": 0,
            "deleted_count": 0,
            "skipped_count": len(segments),
            "failed_count": 0,
            "not_found_count": 0,
            "bytes_freed": 0,
            "estimated_freed_bytes": 0,
            "reason_counts": {CAMERA_DELETE_NO_FILES_BLOCK_REASON: len(segments)} if segments else {},
            "active_blockers": 0,
            "limit_exceeded": False,
            "warnings": ["archive_retained"] if segments else [],
            "items": [
                {
                    "segment_id": segment.id,
                    "camera_id": segment.camera_id,
                    "action": "skipped",
                    "reason": CAMERA_DELETE_NO_FILES_BLOCK_REASON,
                    "size_bytes": int(segment.size_bytes or 0),
                    "error": None,
                }
                for segment in segments[:100]
            ],
        }
    else:
        if not user_has_permission(getattr(current_user, "role", ""), "delete_recordings"):
            recordings = {
                "ok": len(segments) == 0,
                "operation": "camera_delete_with_files",
                "dry_run": False,
                "requested_count": len(segments),
                "planned_count": 0,
                "deleted_count": 0,
                "skipped_count": len(segments),
                "failed_count": 0,
                "not_found_count": 0,
                "bytes_freed": 0,
                "estimated_freed_bytes": 0,
                "reason_counts": {CAMERA_DELETE_NO_PERMISSION_REASON: len(segments)} if segments else {},
                "active_blockers": 0,
                "limit_exceeded": False,
                "warnings": [CAMERA_DELETE_NO_PERMISSION_REASON] if segments else [],
                "items": [
                    {
                        "segment_id": segment.id,
                        "camera_id": segment.camera_id,
                        "action": "skipped",
                        "reason": CAMERA_DELETE_NO_PERMISSION_REASON,
                        "size_bytes": int(segment.size_bytes or 0),
                        "error": None,
                    }
                    for segment in segments[:100]
                ],
            }
        else:
            preview = mark_camera_delete_preview_unsafe(
                compact_deletion_result(
                    preview_segments(
                        db,
                        segments,
                        operation="camera_delete_preview",
                        reason=CAMERA_DELETE_FILES_REASON,
                    )
                )
            )
            if not preview["ok"]:
                recordings = preview
            else:
                recordings = compact_deletion_result(
                    execute_segments(
                        db,
                        segments,
                        actor=current_user,
                        operation="camera_delete_with_files",
                        reason=CAMERA_DELETE_FILES_REASON,
                        max_candidates=max(len(segments), 1),
                    )
                )
                if recordings["failed_count"] or recordings["skipped_count"] or recordings["limit_exceeded"]:
                    recordings["ok"] = False
                    if "archive_cleanup_partial" not in recordings["warnings"]:
                        recordings["warnings"].append("archive_cleanup_partial")

    preview_deleted = delete_camera_preview(camera.id)
    status_value = "deleted" if recordings.get("ok") else "deleted_archive_cleanup_partial"
    response = camera_delete_response(
        camera=camera,
        delete_files=delete_files,
        status_value=status_value,
        recordings=recordings,
        preview_deleted=preview_deleted,
    )
    original_camera_name = camera.name
    original_folder_name = camera.storage_folder_name
    audit_camera_delete(
        db,
        actor=current_user,
        request=request,
        event_type="cameras.deleted" if recordings.get("ok") else "cameras.delete_partial_cleanup",
        severity="info" if recordings.get("ok") else "warning",
        camera=camera,
        delete_files=delete_files,
        status_value=status_value,
        recordings=recordings,
        camera_name=original_camera_name,
    )
    camera.enabled = False
    camera.status = "deleted"
    camera.deleted_at = datetime.utcnow()
    camera.name = deleted_unique_value(original_camera_name, camera.id)
    camera.storage_folder_name = deleted_unique_value(original_folder_name, camera.id)
    db.add(camera)
    db.commit()
    return response
