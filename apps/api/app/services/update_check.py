from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.version import installed_build_metadata
from app.models.system_settings import SystemSettings
from app.services.backup_before_upgrade import backup_precondition_status
from app.services.schema_migrations import PRODUCTION_ADOPTION_DEFERRED, build_migration_plan
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION, schema_version_status


UPDATE_REPORT_VERSION = "stage7.update_check.v1"
UPDATE_INTERVAL = timedelta(hours=24)
MANUAL_RATE_LIMIT = timedelta(minutes=15)
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ITEMS = 20
MAX_SCHEMA_VERSION_VALUE = 1_000_000
SAFE_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:/@+ -]{0,200}$")
SENSITIVE_RE = re.compile(
    r"(password|passwd|secret|token|authorization|jwt|rtsp://|postgresql://|sqlite:///|-----BEGIN)[^,\s\"']*",
    re.IGNORECASE,
)

_LAST_RESULT: dict[str, Any] | None = None
_LAST_MANUAL_CHECK_AT: datetime | None = None
_CHECK_IN_PROGRESS = False


class UpdateCheckBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("summary") or status)


@dataclass(frozen=True)
class ReleaseSourceConfig:
    source_channel_id: str | None = None
    local_manifest_path: Path | None = None


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _sanitize(value: Any, *, max_length: int = 500) -> str:
    return SENSITIVE_RE.sub("redacted=***", str(value or ""))[:max_length]


def _safe_text(value: Any, *, max_length: int = 300) -> str | None:
    text = _sanitize(value, max_length=max_length).strip()
    if not text:
        return None
    return text


def _safe_list(value: Any, *, max_items: int = MAX_ITEMS) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value[:max_items]:
        text = _safe_text(item, max_length=120)
        if text:
            items.append(text)
    return items


def _manifest_schema_invalid(summary: str) -> UpdateCheckBlocked:
    return UpdateCheckBlocked(
        "invalid_manifest",
        {
            "status": "invalid_manifest",
            "error_category": "manifest_schema_invalid",
            "summary": summary,
        },
    )


def _schema_version_field(payload: dict[str, Any], key: str) -> int | None:
    if key not in payload or payload.get(key) is None:
        return None
    value = payload.get(key)
    if isinstance(value, bool):
        raise _manifest_schema_invalid(f"Release manifest field {key} must be a non-negative integer.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"\d{1,7}", value.strip()):
        parsed = int(value.strip())
    else:
        raise _manifest_schema_invalid(f"Release manifest field {key} must be a non-negative integer.")
    if parsed < 0 or parsed > MAX_SCHEMA_VERSION_VALUE:
        raise _manifest_schema_invalid(f"Release manifest field {key} is outside the supported range.")
    return parsed


def _bool_field(payload: dict[str, Any], key: str) -> bool:
    if key not in payload or payload.get(key) is None:
        return False
    if not isinstance(payload.get(key), bool):
        raise _manifest_schema_invalid(f"Release manifest field {key} must be boolean.")
    return bool(payload.get(key))


def _list_field(payload: dict[str, Any], key: str, *, max_items: int = MAX_ITEMS) -> list[str]:
    if key not in payload or payload.get(key) is None:
        return []
    if not isinstance(payload.get(key), list):
        raise _manifest_schema_invalid(f"Release manifest field {key} must be a list.")
    return _safe_list(payload.get(key), max_items=max_items)


def _source_config() -> ReleaseSourceConfig:
    raw_path = os.getenv("KMVMS_UPDATE_MANIFEST_PATH")
    channel = _safe_text(os.getenv("KMVMS_UPDATE_CHANNEL_ID"), max_length=80)
    return ReleaseSourceConfig(source_channel_id=channel, local_manifest_path=Path(raw_path) if raw_path else None)


def _system_row(db: Session) -> SystemSettings | None:
    try:
        return db.query(SystemSettings).first()
    except Exception:
        return None


def _schedule(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    system = _system_row(db)
    anchor = getattr(system, "created_at", None) or now
    identity = f"{getattr(system, 'id', 0)}:{anchor.isoformat()}:{getattr(system, 'system_name', '')}"
    jitter_minutes = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8], 16) % 1440
    daily_anchor = anchor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=jitter_minutes)
    while daily_anchor <= now:
        daily_anchor += UPDATE_INTERVAL
    previous = daily_anchor - UPDATE_INTERVAL
    due = _LAST_RESULT is None and previous <= now
    return {
        "automatic_checks_enabled": True,
        "schedule_source": "system_settings_created_at_plus_deterministic_jitter",
        "cache_persistence": "in_memory_last_result_only",
        "cache_limitation": "Last update-check result does not survive API process restart; schedule anchor and jitter derive from existing SystemSettings metadata.",
        "interval_seconds": int(UPDATE_INTERVAL.total_seconds()),
        "jitter_minutes": jitter_minutes,
        "last_update_check_at": _LAST_RESULT.get("checked_at") if _LAST_RESULT else None,
        "last_success_at": _LAST_RESULT.get("last_success_at") if _LAST_RESULT else None,
        "next_update_check_at": _iso(daily_anchor),
        "automatic_due_now": bool(due),
        "failed_retry_policy": "next_planned_daily_slot_except_manual_owner_admin_check",
    }


def _semver(value: Any) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?\s*$", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def compare_versions(installed: Any, latest: Any) -> dict[str, Any]:
    current = _semver(installed)
    target = _semver(latest)
    if current is None or target is None:
        return {"ordering": "unknown_ordering", "reason": "non_semver_or_unknown_version"}
    if target > current:
        return {"ordering": "newer_available"}
    if target == current:
        return {"ordering": "same_version"}
    return {"ordering": "installed_newer_than_channel"}


def _normalize_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    latest_version = _safe_text(payload.get("latest_version") or payload.get("version"), max_length=80)
    release_id = _safe_text(payload.get("release_id") or payload.get("build_id"), max_length=120)
    if not latest_version or not release_id:
        raise _manifest_schema_invalid("Release manifest requires latest_version and release_id.")
    severity = _safe_text(payload.get("severity"), max_length=40) or "unknown"
    if severity not in {"ordinary", "recommended", "critical", "security", "risky", "unknown"}:
        severity = "unknown"
    affected_domains = _list_field(payload, "affected_domains")
    migration_risk = _safe_text(payload.get("migration_risk"), max_length=60) or "unknown"
    if "compatibility" in payload and payload.get("compatibility") is not None and not isinstance(payload.get("compatibility"), dict):
        raise _manifest_schema_invalid("Release manifest compatibility section must be an object.")
    compatibility = payload.get("compatibility") if isinstance(payload.get("compatibility"), dict) else {}
    schema_fields = {
        "required_schema_version": _schema_version_field(payload, "required_schema_version"),
        "minimum_supported_schema_version": _schema_version_field(payload, "minimum_supported_schema_version"),
        "target_schema_version": _schema_version_field(payload, "target_schema_version"),
        "schema_compatibility_min": _schema_version_field(payload, "schema_compatibility_min"),
        "schema_compatibility_max": _schema_version_field(payload, "schema_compatibility_max"),
        "compatibility_schema_min": _schema_version_field(compatibility, "schema_min"),
        "compatibility_schema_max": _schema_version_field(compatibility, "schema_max"),
    }
    return {
        "latest_version": latest_version,
        "release_id": release_id,
        "build_id": _safe_text(payload.get("build_id"), max_length=120) or release_id,
        "release_date": _safe_text(payload.get("release_date"), max_length=80),
        "severity": severity,
        "release_notes_summary": _safe_text(payload.get("release_notes") or payload.get("changelog"), max_length=800),
        "affected_domains": affected_domains,
        "required_schema_version": schema_fields["required_schema_version"],
        "minimum_supported_schema_version": schema_fields["minimum_supported_schema_version"],
        "target_schema_version": schema_fields["target_schema_version"],
        "schema_compatibility_min": schema_fields["compatibility_schema_min"] if schema_fields["compatibility_schema_min"] is not None else schema_fields["schema_compatibility_min"],
        "schema_compatibility_max": schema_fields["compatibility_schema_max"] if schema_fields["compatibility_schema_max"] is not None else schema_fields["schema_compatibility_max"],
        "migration_risk": migration_risk,
        "backup_required": _bool_field(payload, "backup_required"),
        "restore_validation_required": _bool_field(payload, "restore_validation_required"),
        "restore_validation_recommended": _bool_field(payload, "restore_validation_recommended"),
        "recording_stop_required": _bool_field(payload, "recording_stop_required"),
        "recording_stop_recommended": _bool_field(payload, "recording_stop_recommended"),
        "manual_steps": _list_field(payload, "manual_steps", max_items=10),
    }


def read_trusted_local_manifest(path: Path) -> dict[str, Any]:
    resolved = path.expanduser()
    if not resolved.exists():
        raise UpdateCheckBlocked("release_manifest_unavailable", {"status": "failed", "error_category": "manifest_missing", "summary": "Trusted release manifest is unavailable."})
    if resolved.stat().st_size > MAX_MANIFEST_BYTES:
        raise UpdateCheckBlocked("release_manifest_too_large", {"status": "failed", "error_category": "manifest_too_large", "summary": "Trusted release manifest exceeds size limit."})
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UpdateCheckBlocked("release_manifest_invalid", {"status": "failed", "error_category": "manifest_invalid_json", "summary": _sanitize(type(exc).__name__)}) from exc
    if not isinstance(payload, dict):
        raise UpdateCheckBlocked("release_manifest_invalid", {"status": "failed", "error_category": "manifest_not_object", "summary": "Trusted release manifest must be a JSON object."})
    return _normalize_manifest(payload)


def _schema_compatibility(db: Session, release: dict[str, Any] | None) -> dict[str, Any]:
    schema_status = schema_version_status(db)
    current = schema_status.get("schema_version")
    required = release.get("required_schema_version") if release else None
    max_supported = release.get("schema_compatibility_max") if release else None
    blocked = False
    reasons: list[str] = []
    current_int = current if isinstance(current, int) and not isinstance(current, bool) else None
    if required is not None and current_int is not None and required > CURRENT_SCHEMA_VERSION:
        blocked = True
        reasons.append("target_schema_version_above_supported")
    if max_supported is not None and current_int is not None and current_int > max_supported:
        blocked = True
        reasons.append("current_schema_above_release_compatibility")
    return {
        "status": "blocked" if blocked else "compatible_or_unknown",
        "current_schema_version": current,
        "supported_schema_version": CURRENT_SCHEMA_VERSION,
        "required_schema_version": required,
        "schema_compatibility_max": max_supported,
        "blocked_reasons": reasons,
        "source": "schema_version_status_read_only",
    }


def _preflight(db: Session, release: dict[str, Any] | None) -> dict[str, Any]:
    plan = build_migration_plan(db)
    schema = _schema_compatibility(db, release)
    pending = list(plan.get("pending_migrations") or [])
    by_risk: dict[str, int] = {}
    for item in pending:
        risk = str(item.get("risk") or "unknown")
        by_risk[risk] = by_risk.get(risk, 0) + 1
    risky = bool(release and (release.get("migration_risk") in {"risky", "manual_only", "risky_requires_backup"} or release.get("backup_required") or release.get("restore_validation_required")))
    backup = backup_precondition_status(manifest_path=None, required=bool(risky))
    restore_status = "restore_status_source_unavailable"
    blockers: list[str] = []
    warnings: list[str] = []
    if schema["status"] == "blocked":
        blockers.extend(schema["blocked_reasons"])
    if risky and backup.get("status") != "satisfied":
        blockers.append("verified_backup_required_before_apply")
    if release and release.get("restore_validation_required") and restore_status != "restore_validated":
        blockers.append("restore_validation_required_before_apply")
    if release and (release.get("recording_stop_required") or release.get("recording_stop_recommended")):
        warnings.append("recording_stop_or_maintenance_window_required_by_release")
    if release and any(domain in set(release.get("affected_domains") or []) for domain in {"archive", "media", "recordings"}):
        warnings.append("video_archive_compatibility_caution_db_backup_does_not_cover_video_files")
    if plan.get("production_adoption_status") == PRODUCTION_ADOPTION_DEFERRED:
        warnings.append("production_adoption_deferred")
    return {
        "status": "blocked" if blockers else ("warning" if warnings else "ok"),
        "schema_compatibility": schema,
        "migration_plan": {
            "status": plan.get("status"),
            "pending_count": len(pending),
            "pending_by_risk": by_risk,
            "mutates_database": False,
        },
        "backup_requirement": {"required": bool(risky), "status": backup.get("status"), "source": "backup_precondition_read_only_no_backup_created"},
        "restore_validation_requirement": {"status": restore_status, "source": "source_unavailable_no_restore_executed"},
        "manual_steps": release.get("manual_steps", []) if release else [],
        "recording_stop_required": bool(release and release.get("recording_stop_required")),
        "warnings": warnings,
        "blockers": blockers,
        "side_effects": {
            "update_applied": False,
            "artifact_downloaded": False,
            "containers_restarted": False,
            "migration_executed": False,
            "backup_created": False,
            "restore_executed": False,
        },
    }


def _classify(installed: dict[str, Any], release: dict[str, Any] | None, preflight: dict[str, Any]) -> dict[str, Any]:
    if not release:
        return {"availability": "unknown", "severity": "unknown", "classification": "source_not_configured", "ordering": "unknown_ordering"}
    comparison = compare_versions(installed.get("app_version"), release.get("latest_version"))
    severity = release.get("severity") or "unknown"
    classification = severity if severity in {"critical", "security", "risky", "recommended"} else "ordinary"
    if preflight["status"] == "blocked":
        classification = "risky"
    return {
        "availability": "update_available" if comparison["ordering"] == "newer_available" else ("up_to_date" if comparison["ordering"] == "same_version" else "unknown"),
        "severity": severity,
        "classification": classification,
        "ordering": comparison["ordering"],
        "comparison": comparison,
    }


def _base_status(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    source = _source_config()
    installed = installed_build_metadata()
    schedule = _schedule(db, now=now)
    return {
        "report_version": UPDATE_REPORT_VERSION,
        "generated_at": _iso(now or _utcnow()),
        "status": "not_configured" if not source.local_manifest_path else "configured",
        "installed_build": installed,
        "source_channel": {
            "status": "source_channel_not_configured" if not source.local_manifest_path else "configured_local_manifest",
            "source_channel_id": source.source_channel_id,
            "trusted_source_type": "local_static_manifest" if source.local_manifest_path else "not_configured",
            "raw_source_exposed": False,
            "remote_check_status": "remote_check_not_configured",
            "arbitrary_url_supported": False,
            "redirect_policy": "remote_http_not_implemented",
        },
        "schedule": schedule,
        "cache": {
            "has_last_result": _LAST_RESULT is not None,
            "last_result_status": _LAST_RESULT.get("status") if _LAST_RESULT else None,
            "cache_persistence": schedule["cache_persistence"],
            "limitation": schedule["cache_limitation"],
        },
        "data_sources": {
            "installed_build": installed.get("metadata_source"),
            "release_source": "server_configured_local_manifest" if source.local_manifest_path else "not_configured",
            "schema_status": "read_only",
            "migration_plan": "read_only",
            "backup_status": "read_only_no_backup_created",
            "restore_validation_status": "source_unavailable_no_restore_executed",
        },
        "side_effects": {
            "update_applied": False,
            "artifact_downloaded": False,
            "containers_restarted": False,
            "migration_executed": False,
            "backup_created": False,
            "restore_executed": False,
        },
    }


def build_update_status(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    status = _base_status(db, now=now)
    if _LAST_RESULT:
        status["last_update_check"] = _LAST_RESULT
    status["next_recommended_action"] = "configure_trusted_release_manifest" if status["status"] == "not_configured" else "manual_check_available"
    return status


def run_update_check(db: Session, *, manual: bool = False, manifest_path_for_test_only: str | Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    global _LAST_RESULT, _LAST_MANUAL_CHECK_AT, _CHECK_IN_PROGRESS
    now = now or _utcnow()
    if _CHECK_IN_PROGRESS:
        raise UpdateCheckBlocked("update_check_in_progress", {"status": "blocked", "summary": "An update check is already in progress."})
    if manual and _LAST_MANUAL_CHECK_AT and now - _LAST_MANUAL_CHECK_AT < MANUAL_RATE_LIMIT:
        raise UpdateCheckBlocked(
            "manual_update_check_rate_limited",
            {"status": "blocked", "summary": "Manual update check is rate-limited.", "retry_after_seconds": int((MANUAL_RATE_LIMIT - (now - _LAST_MANUAL_CHECK_AT)).total_seconds())},
        )
    source = _source_config()
    source_path = Path(manifest_path_for_test_only) if manifest_path_for_test_only else source.local_manifest_path
    base = _base_status(db, now=now)
    if not source_path:
        result = {
            **base,
            "status": "not_configured",
            "latest_release": None,
            "classification": {"availability": "unknown", "severity": "unknown", "classification": "source_not_configured", "ordering": "unknown_ordering"},
            "preflight": _preflight(db, None),
            "warnings": [{"code": "source_channel_not_configured", "severity": "info", "message": "No trusted release source is configured."}],
            "errors": [],
            "checked_at": _iso(now),
            "last_success_at": None,
            "next_recommended_action": "configure_trusted_release_manifest",
            "raw_manifest_exposed": False,
        }
        _LAST_RESULT = result
        if manual:
            _LAST_MANUAL_CHECK_AT = now
        return result
    try:
        _CHECK_IN_PROGRESS = True
        release = read_trusted_local_manifest(source_path)
        preflight = _preflight(db, release)
        classification = _classify(base["installed_build"], release, preflight)
        result = {
            **base,
            "status": classification["availability"] if preflight["status"] != "blocked" else "blocked",
            "source_channel": {**base["source_channel"], "status": "configured_local_manifest", "trusted_source_type": "local_static_manifest"},
            "latest_release": release,
            "classification": classification,
            "preflight": preflight,
            "warnings": [{"code": item, "severity": "medium", "message": item} for item in preflight["warnings"]],
            "errors": [],
            "checked_at": _iso(now),
            "last_success_at": _iso(now),
            "next_recommended_action": "review_preflight_before_any_future_apply" if classification["availability"] == "update_available" else "no_update_apply_action",
            "raw_manifest_exposed": False,
        }
        _LAST_RESULT = result
        return result
    except UpdateCheckBlocked as exc:
        is_invalid_manifest = exc.status == "invalid_manifest" or exc.diagnostics.get("error_category") in {"manifest_schema_invalid", "manifest_invalid_json", "manifest_not_object"}
        result = {
            **base,
            "status": "invalid_manifest" if is_invalid_manifest else "failed",
            "latest_release": None,
            "classification": {"availability": "unknown", "severity": "unknown", "classification": "invalid_manifest" if is_invalid_manifest else "failed", "ordering": "unknown_ordering"},
            "preflight": _preflight(db, None),
            "warnings": [],
            "errors": [{"code": exc.status, "summary": _sanitize(exc.diagnostics.get("summary")), "error_category": exc.diagnostics.get("error_category")}],
            "checked_at": _iso(now),
            "last_success_at": None,
            "next_recommended_action": "retry_next_planned_daily_slot_or_manual_after_rate_limit",
            "raw_manifest_exposed": False,
        }
        _LAST_RESULT = result
        return result
    finally:
        if manual:
            _LAST_MANUAL_CHECK_AT = now
        _CHECK_IN_PROGRESS = False


def run_startup_due_check(db: Session) -> dict[str, Any]:
    status = build_update_status(db)
    if status["schedule"]["automatic_due_now"]:
        return run_update_check(db, manual=False)
    return status


def reset_update_check_cache_for_tests() -> None:
    global _LAST_RESULT, _LAST_MANUAL_CHECK_AT, _CHECK_IN_PROGRESS
    _LAST_RESULT = None
    _LAST_MANUAL_CHECK_AT = None
    _CHECK_IN_PROGRESS = False

