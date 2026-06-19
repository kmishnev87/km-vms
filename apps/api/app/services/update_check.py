from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.version import installed_build_metadata
from app.models.system_settings import SystemSettings


UPDATE_REPORT_VERSION = "stage608.update_status.v1"
UPDATE_INTERVAL = timedelta(hours=24)
MANUAL_RATE_LIMIT = timedelta(minutes=15)
MAX_METADATA_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TEXT = 300
SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,119}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9._-]+)?$")
SENSITIVE_KEY_RE = re.compile(r"(password|passwd|secret|token|authorization|jwt|credential|private[_-]?key|cookie|session)", re.IGNORECASE)
SENSITIVE_VALUE_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9_]+|Bearer\s+[A-Za-z0-9._~+/=-]+|rtsp://[^@\s]+@|postgresql://[^:\s]+:[^@\s]+@|-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)

_LAST_RESULT: dict[str, Any] | None = None
_LAST_MANUAL_CHECK_AT: datetime | None = None
_CHECK_IN_PROGRESS = False


class UpdateCheckBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("summary") or diagnostics.get("message") or status)


@dataclass
class UpdateMetadataWarning:
    code: str
    message: str
    severity: str = "info"
    field: str | None = None


@dataclass
class UpdateBlocker:
    code: str
    message: str
    severity: str = "high"


@dataclass
class UpdateInstalledState:
    status: str
    installed_version: str | None
    installed_commit: str | None
    source_kind: str | None
    repo: str | None
    ref: str | None
    channel: str | None
    last_update_status: str | None
    last_update_finished_at: str | None
    last_failed_phase: str | None
    metadata_validity: str
    warnings: list[UpdateMetadataWarning] = field(default_factory=list)


@dataclass
class UpdateManifestSummary:
    schema_version: int
    channel: str
    version: str
    git_ref: str | None
    commit: str | None
    published_at: str | None
    title: str | None
    summary: str | None
    release_notes_url: str | None
    requires_backup: bool
    requires_manual_action: bool
    requires_migration: bool
    minimum_current_version: str | None
    source_type: str | None
    source_repo: str | None
    source_ref: str | None
    breaking_changes: list[str] = field(default_factory=list)


@dataclass
class UpdateCheckResult:
    status: str
    installed: UpdateInstalledState
    latest: UpdateManifestSummary | None
    blockers: list[UpdateBlocker]
    warnings: list[UpdateMetadataWarning]
    checked_at: str
    manifest_source_status: str
    can_apply_from_ui: bool = False


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat() + "Z"


def _sanitize_text(value: Any, *, max_length: int = MAX_TEXT) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = SENSITIVE_VALUE_RE.sub("***", str(value).strip())
    return text[:max_length] or None


def _safe_field(key: str, value: Any, *, max_length: int = MAX_TEXT) -> str | None:
    if SENSITIVE_KEY_RE.search(str(key)):
        return None
    text = _sanitize_text(value, max_length=max_length)
    if text and SENSITIVE_VALUE_RE.search(text):
        return None
    return text


def _safe_timestamp(value: Any) -> str | None:
    text = _sanitize_text(value, max_length=80)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return text


def _safe_url(value: Any) -> str | None:
    text = _sanitize_text(value, max_length=300)
    if text is None:
        return None
    if text.startswith(("https://", "http://")) and not SENSITIVE_VALUE_RE.search(text):
        return text
    return None


def _read_json_file(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        if not path.exists():
            return None, "missing"
        if path.stat().st_size > MAX_METADATA_BYTES:
            return None, "too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "invalid_shape"
    return payload, "valid"


def _app_root() -> Path:
    return Path(os.getenv("KMVMS_APP_ROOT") or os.getenv("KM_VMS_APP_DIR") or Path.cwd())


def _metadata_paths(root: Path | None = None) -> tuple[Path, Path]:
    base = root or _app_root()
    return base / ".km-vms-source.json", base / ".km-vms-update.json"


def read_installed_update_state(*, app_root: Path | None = None) -> UpdateInstalledState:
    build = installed_build_metadata()
    source_path, update_path = _metadata_paths(app_root)
    source_payload, source_validity = _read_json_file(source_path)
    update_payload, update_validity = _read_json_file(update_path)
    warnings: list[UpdateMetadataWarning] = []
    if source_validity != "valid":
        warnings.append(UpdateMetadataWarning("source_metadata_" + source_validity, "Installed source metadata is unavailable or invalid.", field=".km-vms-source.json"))
    if update_validity != "valid":
        warnings.append(UpdateMetadataWarning("update_metadata_" + update_validity, "Last update metadata is unavailable or invalid.", field=".km-vms-update.json"))

    source = source_payload or {}
    update = update_payload or {}
    source_schema = source.get("schema_version")
    update_schema = update.get("schema_version")
    if source_payload and source_schema != 1:
        warnings.append(UpdateMetadataWarning("source_metadata_unsupported_schema", "Installed source metadata schema is unsupported.", field="schema_version"))
    if update_payload and update_schema != 1:
        warnings.append(UpdateMetadataWarning("update_metadata_unsupported_schema", "Last update metadata schema is unsupported.", field="schema_version"))

    repo = _safe_field("github_repo", source.get("github_repo") or update.get("github_repo"), max_length=160)
    ref = _safe_field("ref", source.get("ref") or update.get("ref"), max_length=120)
    commit = _safe_field("commit_sha", source.get("commit_sha") or update.get("commit_sha") or build.get("git_commit"), max_length=40)
    if commit and not SHA_RE.fullmatch(commit):
        warnings.append(UpdateMetadataWarning("installed_commit_invalid", "Installed commit value is not a valid SHA-like value.", field="commit_sha"))
        commit = None
    version = _safe_field("app_version", build.get("app_version"), max_length=80)
    last_status = _safe_field("status", update.get("status"), max_length=40)
    last_failed_phase = _safe_field("failed_phase", update.get("failed_phase"), max_length=80)
    metadata_validity = "valid" if source_validity == "valid" and update_validity in {"valid", "missing"} else ("missing" if source_validity == "missing" and update_validity == "missing" else "invalid")
    if metadata_validity == "missing":
        status = "metadata_missing"
    elif metadata_validity == "invalid":
        status = "metadata_invalid"
    else:
        status = "unknown"
    return UpdateInstalledState(
        status=status,
        installed_version=version,
        installed_commit=commit,
        source_kind=_safe_field("source_kind", source.get("source_kind") or update.get("source_kind") or build.get("install_source"), max_length=80),
        repo=repo,
        ref=ref,
        channel=_safe_field("channel", os.getenv("KMVMS_UPDATE_CHANNEL_ID") or build.get("source_channel_id") or ref, max_length=80),
        last_update_status=last_status,
        last_update_finished_at=_safe_timestamp(update.get("finished_at")),
        last_failed_phase=last_failed_phase,
        metadata_validity=metadata_validity,
        warnings=warnings,
    )


def _asdict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def _semver(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = SEMVER_RE.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def compare_versions(installed: Any, latest: Any) -> dict[str, Any]:
    current = _semver(_sanitize_text(installed, max_length=80))
    target = _semver(_sanitize_text(latest, max_length=80))
    if current is None or target is None:
        return {"ordering": "unknown_ordering", "reason": "non_semver_or_unknown_version"}
    if target > current:
        return {"ordering": "newer_available"}
    if target == current:
        return {"ordering": "same_version"}
    return {"ordering": "installed_newer_than_channel"}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:20]:
        text = _sanitize_text(item, max_length=160)
        if text:
            result.append(text)
    return result


def _bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise UpdateCheckBlocked("check_failed", {"summary": f"Release manifest field {key} must be boolean.", "error_category": "manifest_schema_invalid"})
    return value


def _manifest_text(payload: dict[str, Any], key: str, *, required: bool = False, max_length: int = MAX_TEXT) -> str | None:
    value = _safe_field(key, payload.get(key), max_length=max_length)
    if required and not value:
        raise UpdateCheckBlocked("check_failed", {"summary": f"Release manifest field {key} is required.", "error_category": "manifest_schema_invalid"})
    return value


def _normalize_manifest(payload: dict[str, Any]) -> UpdateManifestSummary:
    if payload.get("schema_version") != 1:
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest schema_version is unsupported.", "error_category": "manifest_schema_invalid"})
    channel = _manifest_text(payload, "channel", required=True, max_length=80) or "stable"
    version = _manifest_text(payload, "version", required=True, max_length=80) or "unknown"
    git_ref = _manifest_text(payload, "git_ref", max_length=120)
    commit = _manifest_text(payload, "commit", max_length=40)
    if git_ref and not SAFE_REF_RE.fullmatch(git_ref):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest git_ref is invalid.", "error_category": "manifest_schema_invalid"})
    if commit and not SHA_RE.fullmatch(commit):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest commit is invalid.", "error_category": "manifest_schema_invalid"})
    published_at = _safe_timestamp(payload.get("published_at"))
    if payload.get("published_at") is not None and not published_at:
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest published_at is invalid.", "error_category": "manifest_schema_invalid"})
    minimum = _manifest_text(payload, "minimum_current_version", max_length=80)
    if minimum and not _semver(minimum):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest minimum_current_version is invalid.", "error_category": "manifest_schema_invalid"})
    artifacts = payload.get("artifacts")
    source = artifacts.get("source") if isinstance(artifacts, dict) and isinstance(artifacts.get("source"), dict) else {}
    source_type = _safe_field("type", source.get("type"), max_length=80)
    source_repo = _safe_field("repo", source.get("repo"), max_length=160)
    source_ref = _safe_field("ref", source.get("ref"), max_length=120)
    if source_repo and not SAFE_REPO_RE.fullmatch(source_repo):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest source repo is invalid.", "error_category": "manifest_schema_invalid"})
    if source_ref and not SAFE_REF_RE.fullmatch(source_ref):
        raise UpdateCheckBlocked("check_failed", {"summary": "Release manifest source ref is invalid.", "error_category": "manifest_schema_invalid"})
    return UpdateManifestSummary(
        schema_version=1,
        channel=channel,
        version=version,
        git_ref=git_ref,
        commit=commit,
        published_at=published_at,
        title=_manifest_text(payload, "title", max_length=160),
        summary=_manifest_text(payload, "summary", max_length=800),
        release_notes_url=_safe_url(payload.get("release_notes_url")),
        requires_backup=_bool(payload, "requires_backup"),
        requires_manual_action=_bool(payload, "requires_manual_action"),
        requires_migration=_bool(payload, "requires_migration"),
        minimum_current_version=minimum,
        source_type=source_type,
        source_repo=source_repo,
        source_ref=source_ref,
        breaking_changes=_string_list(payload.get("breaking_changes")),
    )


def read_trusted_local_manifest(path: Path) -> UpdateManifestSummary:
    resolved = path.expanduser()
    try:
        if not resolved.exists():
            raise UpdateCheckBlocked("not_configured", {"summary": "Trusted release manifest is unavailable.", "error_category": "manifest_missing"})
        if resolved.stat().st_size > MAX_MANIFEST_BYTES:
            raise UpdateCheckBlocked("check_failed", {"summary": "Trusted release manifest exceeds size limit.", "error_category": "manifest_too_large"})
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except UpdateCheckBlocked:
        raise
    except Exception as exc:
        raise UpdateCheckBlocked("check_failed", {"summary": type(exc).__name__, "error_category": "manifest_invalid_json"}) from exc
    if not isinstance(payload, dict):
        raise UpdateCheckBlocked("check_failed", {"summary": "Trusted release manifest must be a JSON object.", "error_category": "manifest_not_object"})
    return _normalize_manifest(payload)


def _manifest_path() -> Path | None:
    raw = os.getenv("KMVMS_UPDATE_MANIFEST_PATH")
    return Path(raw) if raw else None


def _system_row(db: Session) -> SystemSettings | None:
    try:
        return db.query(SystemSettings).first()
    except Exception:
        return None


def _schedule(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    row = _system_row(db)
    anchor = getattr(row, "created_at", None) or now
    return {
        "automatic_checks_enabled": False,
        "schedule_source": "manual_only_stage608",
        "cache_persistence": "in_memory_last_result_only",
        "last_update_check_at": _LAST_RESULT.get("checked_at") if _LAST_RESULT else None,
        "next_update_check_at": None,
        "automatic_due_now": False,
        "anchor_created_at": _iso(anchor) if isinstance(anchor, datetime) else None,
    }


def _blocker(code: str, message: str, severity: str = "high") -> UpdateBlocker:
    return UpdateBlocker(code=code, message=message, severity=severity)


def _warning(code: str, message: str, severity: str = "info") -> UpdateMetadataWarning:
    return UpdateMetadataWarning(code=code, message=message, severity=severity)


def _compare(installed: UpdateInstalledState, latest: UpdateManifestSummary) -> tuple[str, list[UpdateBlocker], list[UpdateMetadataWarning]]:
    blockers: list[UpdateBlocker] = []
    warnings: list[UpdateMetadataWarning] = []
    if installed.installed_commit and latest.commit and installed.installed_commit.lower() == latest.commit.lower():
        return "current", blockers, warnings
    if latest.minimum_current_version:
        current = _semver(installed.installed_version)
        minimum = _semver(latest.minimum_current_version)
        if current is None or minimum is None or current < minimum:
            blockers.append(_blocker("minimum_current_version_not_satisfied", "Installed version does not satisfy the release minimum_current_version."))
            return "blocked", blockers, warnings
    if latest.requires_backup:
        blockers.append(_blocker("requires_backup", "Release requires backup before any future apply."))
    if latest.requires_manual_action:
        blockers.append(_blocker("requires_manual_action", "Release requires manual operator action."))
    if latest.requires_migration:
        blockers.append(_blocker("requires_migration", "Release requires migration support outside Stage 6.0.8."))
    if blockers:
        return "blocked", blockers, warnings
    if installed.installed_version and latest.version and installed.installed_version == latest.version and not latest.commit:
        warnings.append(_warning("commit_evidence_missing", "Version matches but commit evidence is unavailable."))
        return "current_or_unknown", blockers, warnings
    ordering = compare_versions(installed.installed_version, latest.version)["ordering"]
    if ordering == "newer_available":
        return "update_available", blockers, warnings
    if ordering == "same_version":
        return "current_or_unknown", blockers, warnings
    if ordering == "installed_newer_than_channel":
        blockers.append(_blocker("installed_newer_than_manifest", "Installed version is newer than trusted manifest version."))
        return "blocked", blockers, warnings
    warnings.append(_warning("comparison_evidence_insufficient", "Installed/latest comparison evidence is insufficient."))
    return "unknown", blockers, warnings


def _result_payload(result: UpdateCheckResult) -> dict[str, Any]:
    payload = _asdict(result)
    installed = payload["installed"]
    latest = payload["latest"]
    payload.update(
        {
            "report_version": UPDATE_REPORT_VERSION,
            "installed_build": {
                "status": installed["status"],
                "app_version": installed["installed_version"],
                "git_commit": installed["installed_commit"],
                "install_source": installed["source_kind"],
                "source_channel_id": installed["channel"],
                "metadata_source": installed["metadata_validity"],
            },
            "latest_release": None
            if latest is None
            else {
                "latest_version": latest["version"],
                "version": latest["version"],
                "release_id": latest["commit"] or latest["version"],
                "build_id": latest["commit"],
                "git_ref": latest["git_ref"],
                "commit": latest["commit"],
                "release_notes_summary": latest["summary"],
                "requires_backup": latest["requires_backup"],
                "requires_manual_action": latest["requires_manual_action"],
                "requires_migration": latest["requires_migration"],
            },
            "source_channel": {
                "status": payload["manifest_source_status"],
                "source_channel_id": installed["channel"],
                "trusted_source_type": "local_static_manifest" if payload["manifest_source_status"] == "configured" else "not_configured",
                "arbitrary_url_supported": False,
                "remote_check_status": "remote_check_not_implemented",
            },
            "classification": {
                "availability": payload["status"],
                "classification": "blocked" if payload["blockers"] else payload["status"],
                "ordering": compare_versions(installed["installed_version"], latest["version"] if latest else None)["ordering"] if latest else "unknown_ordering",
                "severity": "unknown",
            },
            "preflight": {
                "status": "blocked" if payload["blockers"] else "ok",
                "blockers": [item["code"] for item in payload["blockers"]],
                "warnings": [item["code"] for item in payload["warnings"]],
                "side_effects": {
                    "update_applied": False,
                    "artifact_downloaded": False,
                    "containers_restarted": False,
                    "migration_executed": False,
                    "backup_created": False,
                    "restore_executed": False,
                },
            },
            "side_effects": {
                "update_applied": False,
                "artifact_downloaded": False,
                "containers_restarted": False,
                "migration_executed": False,
                "backup_created": False,
                "restore_executed": False,
            },
            "raw_manifest_exposed": False,
            "next_recommended_action": "future_stage_609_apply_helper_required" if payload["status"] == "update_available" else "no_update_apply_action",
        }
    )
    helper_enabled = str(os.getenv("KMVMS_UPDATE_HELPER_ENABLED") or os.getenv("KM_VMS_UPDATE_HELPER_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    payload["can_apply_from_ui"] = bool(payload["status"] == "update_available" and not payload["blockers"] and helper_enabled)
    return payload


def build_update_status(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    installed = read_installed_update_state()
    manifest_configured = _manifest_path() is not None
    warnings = list(installed.warnings)
    if not manifest_configured:
        warnings.append(_warning("trusted_manifest_not_configured", "No trusted release manifest source is configured."))
    status = "not_configured" if not manifest_configured else installed.status
    result = UpdateCheckResult(
        status=status,
        installed=installed,
        latest=None,
        blockers=[],
        warnings=warnings,
        checked_at=_iso(now),
        manifest_source_status="configured" if manifest_configured else "not_configured",
    )
    payload = _result_payload(result)
    payload["schedule"] = _schedule(db, now=now)
    payload["cache"] = {"has_last_result": _LAST_RESULT is not None, "last_result_status": _LAST_RESULT.get("status") if _LAST_RESULT else None}
    payload["last_update_check"] = _LAST_RESULT
    return payload


def run_update_check(db: Session, *, manual: bool = False, manifest_path_for_test_only: str | Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    global _LAST_RESULT, _LAST_MANUAL_CHECK_AT, _CHECK_IN_PROGRESS
    now = now or _utcnow()
    if _CHECK_IN_PROGRESS:
        raise UpdateCheckBlocked("blocked", {"summary": "An update check is already in progress."})
    if manual and _LAST_MANUAL_CHECK_AT and now - _LAST_MANUAL_CHECK_AT < MANUAL_RATE_LIMIT:
        raise UpdateCheckBlocked("manual_update_check_rate_limited", {"summary": "Manual update check is rate-limited.", "retry_after_seconds": int((MANUAL_RATE_LIMIT - (now - _LAST_MANUAL_CHECK_AT)).total_seconds())})
    installed = read_installed_update_state()
    source_path = Path(manifest_path_for_test_only) if manifest_path_for_test_only else _manifest_path()
    if not source_path:
        result = UpdateCheckResult(
            status="not_configured",
            installed=installed,
            latest=None,
            blockers=[],
            warnings=[*installed.warnings, _warning("trusted_manifest_not_configured", "No trusted release manifest source is configured.")],
            checked_at=_iso(now),
            manifest_source_status="not_configured",
        )
        payload = _result_payload(result)
        payload["schedule"] = _schedule(db, now=now)
        _LAST_RESULT = payload
        if manual:
            _LAST_MANUAL_CHECK_AT = now
        return payload
    try:
        _CHECK_IN_PROGRESS = True
        latest = read_trusted_local_manifest(source_path)
        status, blockers, compare_warnings = _compare(installed, latest)
        result = UpdateCheckResult(
            status=status,
            installed=installed,
            latest=latest,
            blockers=blockers,
            warnings=[*installed.warnings, *compare_warnings],
            checked_at=_iso(now),
            manifest_source_status="configured",
        )
        payload = _result_payload(result)
        payload["schedule"] = _schedule(db, now=now)
        payload["last_success_at"] = _iso(now)
        _LAST_RESULT = payload
        return payload
    except UpdateCheckBlocked as exc:
        result = UpdateCheckResult(
            status=exc.status if exc.status in {"not_configured", "blocked"} else "check_failed",
            installed=installed,
            latest=None,
            blockers=[],
            warnings=installed.warnings,
            checked_at=_iso(now),
            manifest_source_status="check_failed",
        )
        payload = _result_payload(result)
        payload["errors"] = [{"code": exc.status, "summary": _sanitize_text(exc.diagnostics.get("summary"), max_length=200), "error_category": exc.diagnostics.get("error_category")}]
        payload["schedule"] = _schedule(db, now=now)
        _LAST_RESULT = payload
        return payload
    finally:
        if manual:
            _LAST_MANUAL_CHECK_AT = now
        _CHECK_IN_PROGRESS = False


def run_startup_due_check(db: Session) -> dict[str, Any]:
    return build_update_status(db)


def reset_update_check_cache_for_tests() -> None:
    global _LAST_RESULT, _LAST_MANUAL_CHECK_AT, _CHECK_IN_PROGRESS
    _LAST_RESULT = None
    _LAST_MANUAL_CHECK_AT = None
    _CHECK_IN_PROGRESS = False
