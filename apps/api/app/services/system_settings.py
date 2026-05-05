from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.recording import RecordingJob
from app.models.system_settings import SystemSettings
from app.services.storage_contract import (
    RECORDING_FORMATS,
    recording_format_contract,
    storage_contract,
)

LANGUAGES = {"ru", "en"}
HARDWARE_BACKENDS = {"auto", "cpu", "qsv", "vaapi", "nvenc", "amf"}
ACTIVE_RECORDING_JOB_STATES = {"starting", "recording", "stopping", "restarting"}
AUTO_FREE_SPACE_WARNING_THRESHOLD_PERCENT = 10.0
AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT = 5.0
AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT = 1.0


def default_timezone() -> str:
    return "Asia/Yekaterinburg"


def get_system_settings(db: Session) -> SystemSettings:
    row = db.query(SystemSettings).order_by(SystemSettings.id.asc()).first()
    if row:
        return row

    row = SystemSettings(
        system_initialized=False,
        timezone=default_timezone(),
        language="ru",
        storage_path=settings.storage_root,
        recording_format="mkv",
        auto_free_space_cleanup_enabled=False,
        recording_suspended_by_low_disk=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def serialize_settings(row: SystemSettings) -> dict:
    contract = storage_contract(db_storage_path=row.storage_path)
    format_contract = recording_format_contract(row.recording_format)
    return {
        "system_initialized": row.system_initialized,
        "timezone": row.timezone,
        "language": row.language,
        "storage_path": row.storage_path,
        "storage_contract": contract,
        **contract,
        "recording_format": row.recording_format,
        "recording_profile": format_contract["recording_profile"],
        "recording_format_contract": format_contract,
        "hardware_preferred_backend": row.hardware_preferred_backend,
        "auto_free_space_cleanup_enabled": bool(getattr(row, "auto_free_space_cleanup_enabled", False)),
        "auto_free_space_warning_threshold_percent": AUTO_FREE_SPACE_WARNING_THRESHOLD_PERCENT,
        "auto_free_space_cleanup_threshold_percent": AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT,
        "auto_free_space_critical_threshold_percent": AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT,
        "recording_suspended_by_low_disk": bool(getattr(row, "recording_suspended_by_low_disk", False)),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def validate_settings_payload(payload: dict, partial: bool = False) -> dict:
    data = {}
    required = [] if partial else ["timezone", "language", "storage_path", "recording_format"]
    for key in required:
        if not payload.get(key):
            raise ValueError(f"{key} is required")

    if "timezone" in payload:
        timezone = str(payload.get("timezone") or "").strip()
        if not timezone:
            raise ValueError("timezone is required")
        try:
            ZoneInfo(timezone)
        except Exception:
            raise ValueError("timezone is invalid")
        data["timezone"] = timezone

    if "language" in payload:
        language = str(payload.get("language") or "").strip().lower()
        if language not in LANGUAGES:
            raise ValueError("language must be ru or en")
        data["language"] = language

    if "storage_path" in payload:
        storage_path = str(payload.get("storage_path") or "").strip()
        if not storage_path:
            raise ValueError("storage_path is required")
        data["storage_path"] = storage_path

    if "recording_format" in payload:
        recording_format = str(payload.get("recording_format") or "").strip().lower()
        if recording_format not in RECORDING_FORMATS:
            raise ValueError("recording_format must be mkv or mp4")
        data["recording_format"] = recording_format

    if "hardware_preferred_backend" in payload:
        value = payload.get("hardware_preferred_backend")
        normalized = str(value).strip().lower() if value else ""
        if normalized and normalized not in HARDWARE_BACKENDS:
            raise ValueError("hardware_preferred_backend is invalid")
        data["hardware_preferred_backend"] = None if normalized in {"", "auto"} else normalized

    if "auto_free_space_cleanup_enabled" in payload:
        data["auto_free_space_cleanup_enabled"] = bool(payload.get("auto_free_space_cleanup_enabled"))

    return data


def update_system_settings(db: Session, payload: dict) -> SystemSettings:
    row = get_system_settings(db)
    data = validate_settings_payload(payload, partial=True)
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def active_recording_jobs_count(db: Session) -> int:
    return (
        db.query(RecordingJob)
        .filter(RecordingJob.state.in_(ACTIVE_RECORDING_JOB_STATES))
        .count()
    )


def validate_storage_path(path_value: str, create: bool = True) -> dict:
    path = Path(str(path_value or "").strip())
    result = {
        "path": str(path),
        "path_role": "container_runtime_or_reference_path",
        "runtime_storage_source": "settings.storage_root_env",
        "host_mount_note": "Host storage path is deploy-managed; this validation does not remount host storage.",
        "exists": False,
        "created": False,
        "writable": False,
        "free_bytes": None,
        "ok": False,
        "error": None,
    }
    if not str(path):
        result["error"] = "storage_path is required"
        return result

    try:
        if not path.exists() and create:
            path.mkdir(parents=True, exist_ok=True)
            result["created"] = True
        result["exists"] = path.exists()
        if not result["exists"]:
            result["error"] = "path does not exist"
            return result

        probe = path / ".km_vms_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        result["writable"] = True

        usage = shutil.disk_usage(path)
        result["free_bytes"] = usage.free
        result["ok"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def get_hardware_preferred_backend() -> str:
    try:
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            row = get_system_settings(db)
            value = str(row.hardware_preferred_backend or "").strip().lower()
            return value if value in HARDWARE_BACKENDS else ""
        finally:
            db.close()
    except Exception:
        return ""


def effective_hardware_backend_setting() -> str:
    preferred = get_hardware_preferred_backend()
    if preferred:
        return preferred
    value = str(settings.live_hwaccel_backend or "auto").strip().lower()
    return value if value in HARDWARE_BACKENDS else "auto"
