from pathlib import Path
import os
import shutil
import subprocess
import json
import time
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import user_has_permission
from app.core.security import encrypt_text, decrypt_text
from app.db.session import get_db
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.user import User
from app.routers.camera_connection_helpers import (
    ONVIF_PROBE_PROOFS,
    PROOF_TTL_SECONDS,
    RTSP_TEST_PROOFS,
    apply_profile_assignments,
    assemble_rtsp_url,
    build_test_url,
    get_camera_credentials,
    has_valid_onboarding_proof,
    onvif_error_code,
    parse_bounded_int,
    parse_port,
    profile_matches_stream,
    normalize_stream_role,
    register_onvif_probe_proof,
    register_rtsp_test_proof,
    register_validation_proof,
    require_save_gate,
    resolve_test_connection_payload,
    safe_int,
    safe_onvif_error,
    safe_preview_token,
    saved_stream_path,
    store_has_valid_proof,
    validation_fingerprint,
)
from app.routers.camera_onvif_routes import (
    get_active_camera_or_404,
    onvif_discover,
    onvif_health,
    onvif_health_check,
    onvif_probe,
    onvif_profile_config,
    onvif_profiles,
    onvif_ptz_capabilities,
    onvif_ptz_command,
    router as onvif_router,
    run_bounded_read_only_check,
    safe_camera_onvif_credentials,
    update_onvif_profile_route,
)
from app.routers.deps import FORBIDDEN_DETAIL, get_current_user, require_permission
from app.schemas.camera import (
    CameraCreate,
    CameraResponse,
    CameraUpdate,
    restore_rtsp_management_value,
    safe_rtsp_management_value,
)
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.storage import build_unique_folder_name, ensure_camera_folder
from app.services.recording_retention import EXECUTION_POLICY_MANUAL_COMPLETE, execute_segments, preview_segments
from app.services.retention_automation import advance_retention_signal
from app.services.storage_operation_conflicts import (
    StorageOperationLifecycle,
    StorageOperationConflict as StorageOuterConflict,
    claim_state_detail,
    claim_operation_with_conflicts,
    operation_instance_id,
    scope_with_physical_volumes,
    terminal_replay_result,
    terminal_result_summary,
)
from app.services.storage_operations_foundation import StorageOperationContractError, claim_operation, safe_reason_code

router = APIRouter(prefix="/cameras", tags=["cameras"])
viewer_router = APIRouter(prefix="/viewer/cameras", tags=["viewer-cameras"])
router.include_router(onvif_router)

CAMERA_DELETE_FILES_REASON = "camera_delete_with_files"
CAMERA_DELETE_NO_FILES_BLOCK_REASON = "recordings_exist_delete_files_false_requires_safe_policy"
CAMERA_DELETE_UNSAFE_WITH_FILES_REASON = "camera_delete_with_files_requires_all_segments_safe"
CAMERA_DELETE_NO_PERMISSION_REASON = "delete_recordings_permission_missing"
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
    "onvif_sub_profile_token",
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
    requested_role_value = payload.get("stream_role")
    requested_role = normalize_stream_role(requested_role_value)
    if requested_role_value not in (None, "") and requested_role is None:
        raise HTTPException(status_code=422, detail="stream_role must be main or sub.")
    resolved_payload = resolve_test_connection_payload(db, payload)
    input_url = build_test_url(resolved_payload, role=requested_role)
    if not input_url:
        role_label = requested_role or "selected"
        raise HTTPException(status_code=400, detail=f"RTSP path for {role_label} stream is required.")

    tested_role = requested_role or (
        "main" if saved_stream_path(resolved_payload.get("rtsp_main_url")) else "sub"
    )

    transport = resolved_payload.get("rtsp_transport") or "tcp"

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
    validation_token = register_rtsp_test_proof(resolved_payload, tested_role)

    return {
        "ok": True,
        "display_path": safe_rtsp_display_path(input_url),
        "transport": transport,
        "preview_url": preview_url if preview_ok else None,
        "preview_token": preview_token if preview_ok and preview_token else None,
        "validation_token": validation_token,
        "tested_role": tested_role,
        "stream_identity": {
            "role": tested_role,
            "path": safe_rtsp_management_value(
                resolved_payload.get(f"rtsp_{tested_role}_url")
            ),
        },
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
        saved_stream_path(payload.rtsp_main_url),
    )
    rtsp_sub_url = assemble_rtsp_url(
        rtsp_host,
        rtsp_port,
        payload.username,
        payload.password,
        saved_stream_path(payload.rtsp_sub_url),
    )

    payload_dict = payload.model_dump()
    final_connection_payload = {
        **payload_dict,
        "rtsp_host": rtsp_host if str(payload.protocol or "rtsp").lower() == "onvif" else None,
        "rtsp_port": rtsp_port if str(payload.protocol or "rtsp").lower() == "onvif" else None,
        "rtsp_main_url": rtsp_main_url,
        "rtsp_sub_url": rtsp_sub_url,
    }
    manual_unverified = require_save_gate(final_connection_payload, connection_sensitive_change=True)
    folder_name = build_unique_folder_name(db, payload.name)
    ensure_camera_folder(folder_name)

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
        onvif_sub_profile_token=payload.onvif_sub_profile_token,
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
    return safe_rtsp_management_value(input_url) or "RTSP path указан"


def ensure_static_preview_permissions(path: Path, *, include_file: bool = False) -> None:
    preview_root = Path(settings.storage_previews)
    directories = [preview_root]
    try:
        relative_parent = path.parent.relative_to(preview_root)
        current = preview_root
        for part in relative_parent.parts:
            current = current / part
            directories.append(current)
    except ValueError:
        directories.append(path.parent)

    for directory in directories:
        try:
            if directory.exists():
                os.chmod(directory, 0o755)
        except OSError:
            pass

    if include_file:
        try:
            if path.exists():
                os.chmod(path, 0o644)
        except OSError:
            pass


def capture_camera_preview(input_url: str, transport: str, output_path: Path) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_static_preview_permissions(output_path)
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
    preview_ready = output_path.exists() and output_path.stat().st_size > 0
    if preview_ready:
        ensure_static_preview_permissions(output_path, include_file=True)
    return preview_ready


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
    ensure_static_preview_permissions(destination)
    shutil.copyfile(source, destination)
    ensure_static_preview_permissions(destination, include_file=True)
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
    camera_name: str | None = None,
    replayed: bool = False,
) -> dict:
    warnings = list(recordings.get("warnings") or [])
    response = {
        "ok": status_value in {"deleted", "deleted_archive_cleanup_partial"},
        "status": status_value,
        "camera_id": camera.id,
        "camera_name": camera_name if camera_name is not None else camera.name,
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
    if replayed:
        response["replayed"] = True
    return response


def camera_delete_replay_response(
    *,
    camera: Camera,
    delete_files: bool,
    terminal: dict,
    operation_status: str | None = None,
) -> dict:
    replay_result = dict(terminal)
    product_status = str(replay_result.pop("camera_delete_status", "") or "")
    camera_removed = replay_result.pop("camera_removed", None) is True
    camera_name = replay_result.pop("camera_name", None)
    preview_deleted = bool(replay_result.pop("camera_preview_deleted", False))
    replay_result.pop("camera_id", None)
    replay_result.pop("delete_files", None)
    if camera_removed and product_status in {"deleted", "deleted_archive_cleanup_partial"}:
        return camera_delete_response(
            camera=camera,
            delete_files=delete_files,
            status_value=product_status,
            recordings=replay_result,
            preview_deleted=preview_deleted,
            camera_name=str(camera_name) if camera_name is not None else None,
            replayed=True,
        )

    terminal_status = str(operation_status or replay_result.get("status") or "failed")
    replay_result["status"] = terminal_status
    replay_result["ok"] = False
    replay_result["camera_removed"] = False
    replay_result["replayed"] = True
    response = {
        "ok": False,
        "status": terminal_status,
        "camera_id": camera.id,
        "camera_name": str(camera_name) if camera_name is not None else camera.name,
        "camera_removed": False,
        "delete_files": bool(delete_files),
        "recordings": replay_result,
        "archive_cleanup": replay_result,
        "warnings": list(replay_result.get("warnings") or []),
        "preview_cleanup": {
            "deleted": preview_deleted,
            "scope": "camera_preview_only",
        },
        "replayed": True,
    }
    for field in ("operation_id", "reason_code", "next_action", "retry_mode", "retry_allowed", "cancel_allowed"):
        if field in replay_result:
            response[field] = replay_result[field]
    return response


def camera_delete_operation_scope(camera: Camera, segments: list[RecordingSegment]) -> dict:
    return {
        "global": False,
        "root_ids": sorted({str(segment.archive_root_id) for segment in segments if segment.archive_root_id}),
        "camera_ids": [camera.id],
        "segment_ids": [segment.id for segment in segments],
        "physical_volume_ids": [],
    }


def claim_nonmutating_camera_delete_outcome(
    db: Session,
    *,
    camera: Camera,
    segments: list[RecordingSegment],
    actor: User,
    operation_id: str,
) -> dict:
    canonical_scope = scope_with_physical_volumes(db, camera_delete_operation_scope(camera, segments))
    try:
        return claim_operation(
            db,
            operation_type="camera_delete_with_files",
            scope=canonical_scope,
            request_identity={"camera_id": camera.id, "delete_files": True},
            actor=actor,
            operation_id=operation_id,
            idempotency_key=operation_id,
            owner_instance_id=operation_instance_id("camera-delete-outcome"),
            scope_is_canonical=True,
        )
    except StorageOperationContractError as exc:
        if str(exc) in {"operation_identity_mismatch", "operation_idempotency_identity_mismatch"}:
            raise HTTPException(
                status_code=409,
                detail={"reason_code": "storage_operation_identity_mismatch", "retryable": False},
            ) from exc
        raise


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

    raw_payload = payload.model_dump(exclude_unset=True)
    data = dict(raw_payload)
    preview_token = data.pop("preview_token", None)
    validation_token = data.pop("validation_token", None)
    main_validation_token = data.pop("main_validation_token", None)
    sub_validation_token = data.pop("sub_validation_token", None)
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

    submitted_main = data.get("rtsp_main_url", camera.rtsp_main_url)
    submitted_sub = data.get("rtsp_sub_url", camera.rtsp_sub_url)
    data["rtsp_main_url"] = assemble_rtsp_url(
        host_for_rtsp,
        port_for_rtsp,
        username_for_rtsp,
        password_for_rtsp,
        saved_stream_path(
            restore_rtsp_management_value(submitted_main, camera.rtsp_main_url)
        ),
    )
    data["rtsp_sub_url"] = assemble_rtsp_url(
        host_for_rtsp,
        port_for_rtsp,
        username_for_rtsp,
        password_for_rtsp,
        saved_stream_path(
            restore_rtsp_management_value(submitted_sub, camera.rtsp_sub_url)
        ),
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
        "onvif_sub_profile_token": camera.onvif_sub_profile_token,
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
        "password": password_for_rtsp or "",
        "validation_token": validation_token,
        "main_validation_token": main_validation_token,
        "sub_validation_token": sub_validation_token,
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

    retention_rules_changed = any(
        old_values.get(key) != getattr(camera, key, None)
        for key in ("retention_days", "storage_quota_gb")
    )
    if retention_rules_changed:
        camera.retention_policy_version = int(getattr(camera, "retention_policy_version", 1) or 1) + 1
    db.add(camera)
    if retention_rules_changed:
        advance_retention_signal(db, commit=False)
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
    operation_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    operation_id = operation_id if isinstance(operation_id, str) and operation_id.strip() else None
    camera_query = db.query(Camera).filter(Camera.id == camera_id)
    if not delete_files:
        camera_query = camera_query.filter(Camera.deleted_at.is_(None))
    camera = camera_query.first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    segments = camera_recording_segments(db, camera.id)
    storage_scope = camera_delete_operation_scope(camera, segments)

    outer_handle = None
    outer_lifecycle = None
    if delete_files and camera.deleted_at is not None:
        if not operation_id:
            raise HTTPException(status_code=404, detail="Камера не найдена")
        try:
            replay_claim = claim_operation_with_conflicts(
                db,
                operation_type="camera_delete_with_files",
                scope=storage_scope,
                request_identity={"camera_id": camera.id, "delete_files": True},
                actor=current_user,
                operation_id=operation_id,
                idempotency_key=operation_id,
                owner_instance_id=operation_instance_id("camera-delete-replay"),
            )
        except StorageOuterConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        if replay_claim.get("state") == "terminal":
            recordings = terminal_replay_result(replay_claim)
            return camera_delete_replay_response(
                camera=camera,
                delete_files=delete_files,
                terminal=recordings,
                operation_status=(replay_claim.get("operation") or {}).get("status"),
            )
        if replay_claim.get("state") == "claimed":
            replay_lifecycle = StorageOperationLifecycle(
                db,
                replay_claim["handle"],
                failure_reason="camera_delete_with_files_failed",
            )
            replay_lifecycle.block("camera_not_found", retry_allowed=False)
            raise HTTPException(status_code=404, detail="Камера не найдена")
        raise HTTPException(status_code=409, detail=claim_state_detail(replay_claim))

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
                "status": "completed" if len(segments) == 0 else "blocked",
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
                try:
                    outer_claim = claim_operation_with_conflicts(
                        db,
                        operation_type="camera_delete_with_files",
                        scope=storage_scope,
                        request_identity={"camera_id": camera.id, "delete_files": True},
                        actor=current_user,
                        operation_id=operation_id,
                        idempotency_key=operation_id,
                        owner_instance_id=operation_instance_id("camera-delete"),
                    )
                except StorageOuterConflict as exc:
                    raise HTTPException(status_code=409, detail=exc.detail) from exc
                if outer_claim.get("state") == "terminal":
                    recordings = terminal_replay_result(outer_claim)
                    return camera_delete_replay_response(
                        camera=camera,
                        delete_files=delete_files,
                        terminal=recordings,
                        operation_status=(outer_claim.get("operation") or {}).get("status"),
                    )
                if outer_claim.get("state") != "claimed":
                    raise HTTPException(status_code=409, detail=claim_state_detail(outer_claim))
                outer_handle = outer_claim["handle"]
                outer_lifecycle = StorageOperationLifecycle(
                    db,
                    outer_handle,
                    failure_reason="camera_delete_with_files_failed",
                )
                if camera.deleted_at is not None:
                    outer_lifecycle.block("camera_not_found", retry_allowed=False)
                    raise HTTPException(status_code=404, detail="Камера не найдена")
                try:
                    recordings = compact_deletion_result(
                        execute_segments(
                            db,
                            segments,
                            actor=current_user,
                            operation="camera_delete_with_files",
                            reason=CAMERA_DELETE_FILES_REASON,
                            policy=EXECUTION_POLICY_MANUAL_COMPLETE,
                            scope={"type": "camera", "segment_ids": [], "camera_ids": [camera.id], "root_ids": []},
                            outer_operation_handle=outer_handle,
                            manage_outer_operation=False,
                        )
                    )
                    if recordings["failed_count"] or recordings["skipped_count"] or recordings["limit_exceeded"]:
                        recordings["ok"] = False
                        if "archive_cleanup_partial" not in recordings["warnings"]:
                            recordings["warnings"].append("archive_cleanup_partial")
                except Exception as exc:
                    outer_lifecycle.__exit__(type(exc), exc, exc.__traceback__)
                    raise

    if delete_files and operation_id and outer_lifecycle is None:
        outcome_claim = claim_nonmutating_camera_delete_outcome(
            db,
            camera=camera,
            segments=segments,
            actor=current_user,
            operation_id=operation_id,
        )
        if outcome_claim.get("state") == "terminal":
            return camera_delete_replay_response(
                camera=camera,
                delete_files=delete_files,
                terminal=terminal_replay_result(outcome_claim),
                operation_status=(outcome_claim.get("operation") or {}).get("status"),
            )
        if outcome_claim.get("state") != "claimed":
            raise HTTPException(status_code=409, detail=claim_state_detail(outcome_claim))
        outer_handle = outcome_claim["handle"]
        outer_lifecycle = StorageOperationLifecycle(
            db,
            outer_handle,
            failure_reason="camera_delete_outcome_persistence_failed",
        )

    try:
        preview_deleted = delete_camera_preview(camera.id)
        status_value = "deleted" if recordings.get("ok") else "deleted_archive_cleanup_partial"
        original_camera_name = camera.name
        response = camera_delete_response(
            camera=camera,
            delete_files=delete_files,
            status_value=status_value,
            recordings=recordings,
            preview_deleted=preview_deleted,
        )
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
        retained_archive_exists = (
            db.query(RecordingSegment.id)
            .filter(
                RecordingSegment.camera_id == camera.id,
                RecordingSegment.deleted_at.is_(None),
                RecordingSegment.status != "deleted",
            )
            .first()
            is not None
        )
        if retained_archive_exists:
            camera.retention_policy_version = int(getattr(camera, "retention_policy_version", 1) or 1) + 1
            advance_retention_signal(db, commit=False)
        db.commit()
        if outer_lifecycle is not None:
            terminal_recordings = {
                **recordings,
                "camera_delete_status": status_value,
                "camera_removed": True,
                "camera_id": camera.id,
                "camera_name": original_camera_name,
                "camera_preview_deleted": bool(preview_deleted),
                "delete_files": bool(delete_files),
            }
            outer_lifecycle.mark_inner_persisted(terminal_recordings)
            outer_lifecycle.finish(
                status="completed" if status_value == "deleted" else "partial",
                result=terminal_result_summary(terminal_recordings),
                progress={
                    "planned_count": int(recordings.get("planned_count") or 0),
                    "completed_count": int(recordings.get("deleted_count") or 0),
                    "failed_count": int(recordings.get("failed_count") or 0),
                    "skipped_count": int(recordings.get("skipped_count") or 0),
                    "completed_bytes": int(recordings.get("bytes_freed") or 0),
                },
                reason_code=safe_reason_code((recordings.get("warnings") or [None])[0]),
                retry_allowed=not bool(recordings.get("ok")),
                retry_mode="refresh" if not recordings.get("ok") else None,
            )
        return response
    except Exception as exc:
        if outer_lifecycle is not None and not outer_lifecycle.terminalized:
            outer_lifecycle.__exit__(type(exc), exc, exc.__traceback__)
        raise
