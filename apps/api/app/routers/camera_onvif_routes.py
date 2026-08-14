from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.security import decrypt_text
from app.db.session import get_db
from app.models.camera import Camera
from app.models.user import User
from app.routers.camera_connection_helpers import (
    apply_profile_assignments,
    get_camera_credentials,
    onvif_error_code,
    parse_bounded_int,
    parse_port,
    register_onvif_probe_proof,
    safe_onvif_error,
)
from app.routers.deps import require_permission
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.onvif_service import (
    OnvifConfigurationError,
    build_onvif_health_contract,
    check_onvif_events_feasibility,
    discover_onvif_devices,
    execute_onvif_ptz_command,
    fetch_onvif_profiles,
    get_onvif_profile_config,
    get_onvif_ptz_capabilities,
    probe_onvif_device,
    select_profile_config_health_target,
    update_onvif_profile,
    validate_ptz_command_payload,
    ptz_validation_response,
    ptz_command_limits,
)


router = APIRouter()


def onvif_config_http_detail(exc: Exception) -> dict:
    return {
        "code": getattr(exc, "code", None) or onvif_error_code(exc),
        "message": safe_onvif_error(exc),
        "raw_secret_exposed": False,
    }


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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)
    camera = creds.get("camera")
    host = str(creds.get("host") or "").strip()
    port = parse_port(creds.get("port"), field="port", default=80)
    rtsp_host = str(creds.get("rtsp_host") or host).strip()
    rtsp_port = parse_port(creds.get("rtsp_port"), field="rtsp_port", default=554)
    username = creds.get("username") or ""
    password = creds.get("password") or ""
    timeout = parse_bounded_int(payload.get("timeout_seconds"), field="timeout_seconds", default=5, minimum=1, maximum=10)
    if not host:
        raise HTTPException(status_code=400, detail={"code": "host_required", "message": "ONVIF host is required."})

    request_payload = {
        "protocol": "onvif",
        "host": host,
        "port": port,
        "rtsp_host": rtsp_host,
        "rtsp_port": rtsp_port,
        "username": username,
        "password": password,
        "onvif_path": payload.get("onvif_path") or getattr(camera, "onvif_path", None),
    }
    try:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            probe_onvif_device,
            host=host,
            port=port,
            username=username,
            password=password,
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
        raise HTTPException(status_code=400, detail=onvif_config_http_detail(e))


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
            config=payload.get("config") or {},
        )
    except Exception as e:
        status_code = 409 if isinstance(e, OnvifConfigurationError) and e.code == "video_encoder_configuration_mismatch" else 400
        raise HTTPException(status_code=status_code, detail=onvif_config_http_detail(e))


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
    profile_config_result = None
    profile_config_checked = False
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
                selection = select_profile_config_health_target(
                    camera,
                    profiles_result.get("profiles") or [],
                )
                profile_config_checked = True
                selected_profile_token = selection.get("profile_token")
                if selected_profile_token:
                    try:
                        profile_config_result = run_bounded_read_only_check(
                            get_onvif_profile_config,
                            host=str(camera.host),
                            port=int(camera.port or 80),
                            username=str(camera.username),
                            password=str(password),
                            profile_token=str(selected_profile_token),
                        )
                        profile_config_result["profile_source"] = selection.get("source")
                        profile_config_result["reason_codes"] = sorted(
                            set(profile_config_result.get("reason_codes") or [])
                            | set(selection.get("reason_codes") or [])
                        )
                    except Exception as exc:
                        profile_config_result = {
                            "status": "error",
                            "profile_token": selected_profile_token,
                            "profile_source": selection.get("source"),
                            "current_read": False,
                            "options_read": False,
                            "supported": {},
                            "reason_codes": [
                                getattr(exc, "code", None)
                                or onvif_error_code(exc)
                            ],
                        }
                else:
                    profile_config_result = {
                        "status": "unavailable",
                        "profile_token": None,
                        "profile_source": selection.get("source"),
                        "current_read": False,
                        "options_read": False,
                        "supported": {},
                        "reason_codes": selection.get("reason_codes") or [],
                    }
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
                profile_config_result=profile_config_result,
                profile_config_checked=profile_config_checked,
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
