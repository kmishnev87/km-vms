from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import fcntl
import jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.audit_event import AuditEvent
from app.services.update_check import (
    get_trusted_apply_snapshot,
    read_installed_update_state,
    run_update_check,
    trusted_apply_snapshot_status,
)

REQUEST_SCHEMA_VERSION = 2
LEGACY_REQUEST_SCHEMA_VERSION = 1
STATUS_SCHEMA_VERSION = 1
MAX_CONTROL_BYTES = 64 * 1024
MAX_ADMISSION_BYTES = 512 * 1024
MAX_LINEAGE_BYTES = 4 * 1024
MAX_ADMISSION_ENTRIES = 256
MAX_TERMINAL_STEPS = 12
TERMINAL_RETENTION_DAYS = 90
SUBMISSION_PROOF_TTL_SECONDS = 15 * 60
SUBMISSION_PROOF_LEEWAY_SECONDS = 30
MAX_SUBMISSION_PROOF_BYTES = 2048
SUBMISSION_PROOF_TYPE = "km_vms_update_apply_submission"
SUBMISSION_PROOF_AUDIENCE = "km-vms-update-apply"
SUBMISSION_PROOF_PURPOSE = "apply-submission"
SUBMISSION_PROOF_VERSION = 1
SUBMISSION_PROOF_HEADER = "X-KM-VMS-Update-Submission-Proof"
ADMISSION_DOCUMENT_TYPE = "update_apply_admission"
ADMISSION_STATES = {"audit_pending", "admitted_unclaimed", "claimed", "terminal"}
NON_TERMINAL_ADMISSION_STATES = ADMISSION_STATES - {"terminal"}
LINEAGE_SCHEMA_VERSION = 1
LINEAGE_DOCUMENT_TYPE = "update_apply_admission_lineage"
LINEAGE_MARKER_PAYLOAD = {
    "schema_version": LINEAGE_SCHEMA_VERSION,
    "document_type": LINEAGE_DOCUMENT_TYPE,
    "initialized": True,
}
AUDIT_NAMESPACE = uuid.UUID("abf15e22-71b8-5af5-b9ee-ef808127c780")
AUDIT_EVENT_TYPE = "system.update_apply_requested"
AUDIT_TARGET_TYPE = "update_apply"
MAX_APPLY_HISTORY_ITEMS = 10
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked"}
RUNNING_STATUSES = {"queued", "starting_helper", "preflight", "acquire_source", "downloading", "extracting", "validating_source", "overlay", "applying", "compose_config", "rebuilding", "restarting", "health_check", "commit_verification"}
STALE_AFTER_SECONDS = 180
FORBIDDEN_REQUEST_FIELDS = {
    "url",
    "repo",
    "repository",
    "ref",
    "branch",
    "command",
    "shell",
    "docker",
    "compose",
    "token",
    "token_env",
    "token_file",
    "path",
    "backup_path",
    "database_url",
    "db_url",
    "image",
    "env",
}
SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{0,200}$")
MACHINE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,119}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
CANONICAL_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
REQUEST_ID_RE = re.compile(r"^update-[0-9a-f]{32}$", re.IGNORECASE)
LEGACY_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,79}$")
SUBMISSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
SENSITIVE_VALUE_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9_]+|Bearer\s+[A-Za-z0-9._~+/=-]+|rtsp://[^@\s]+@|postgresql://[^:\s]+:[^@\s]+@|-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
UNSAFE_PUBLIC_TEXT_RE = re.compile(
    r"([A-Za-z]:\\|/(?:volume|var|etc|tmp|home|root|mnt|run|proc|sys|dev)/|traceback|stack\s+trace|authorization\s*:|cookie\s*:)",
    re.IGNORECASE,
)
ADMISSION_DOCUMENT_KEYS = {
    "schema_version",
    "document_type",
    "current_submission_id",
    "entries",
    "updated_at",
}
ADMISSION_ENTRY_KEYS = {
    "submission_id",
    "request_id",
    "target_version",
    "target_commit",
    "requested_at",
    "updated_at",
    "state",
    "request",
    "audit",
    "claimed_at",
    "terminal",
}
CURRENT_REQUEST_KEYS = {
    "schema_version",
    "request_id",
    "submission_id",
    "requested_at",
    "requested_by",
    "intent",
    "source",
    "apply_candidate",
    "confirmed",
    "preflight_required",
    "status_path",
}
CURRENT_ACTOR_KEYS = {"user_id", "username", "role", "ip_address", "user_agent"}
REQUEST_SOURCE_KEYS = {"kind", "channel", "version", "commit", "apply_ref", "ref", "repo", "source_type"}
APPLY_CANDIDATE_KEYS = {"source", "snapshot"}
APPLY_CANDIDATE_PROFILE_CANONICAL_CURRENT = "canonical_current"
APPLY_CANDIDATE_PROFILE_COMPACT_READ_ONLY = "compact_read_only"
APPLY_FRESHNESS_KEYS = {
    "available",
    "fresh",
    "age_seconds",
    "fresh_for_seconds",
    "version",
    "commit_short",
    "provider",
}
AUDIT_KEYS = {"state", "event_id", "confirmed_at"}
TERMINAL_SOURCE_KEYS = {"kind", "repo", "ref", "commit", "apply_ref"}
TERMINAL_STEP_KEYS = {"name", "status"}
TERMINAL_ERROR_KEYS = {"category", "message", "operator_action"}
TERMINAL_RELEASE_IDENTITY_KEYS = {
    "host_metadata_status",
    "api_metadata_status",
    "api_visible",
    "commit_verified",
}
TERMINAL_SIDE_EFFECT_KEYS = {
    "api_docker_socket",
    "api_shell_execution",
    "request_controlled_source",
    "helper_has_docker_socket",
    "helper_public_ports",
}
TERMINAL_COMMON_KEYS = {
    "schema_version",
    "request_id",
    "submission_id",
    "target_version",
    "status",
    "phase",
    "current_step",
    "started_at",
    "updated_at",
    "finished_at",
    "source",
    "expected_commit",
    "commit_verified",
    "steps",
    "can_cancel",
    "rollback_supported",
    "side_effects",
    "error",
}
PRE_CLOSEOUT_CANCEL_KEYS = {
    "schema_version",
    "request_id",
    "submission_id",
    "target_version",
    "status",
    "phase",
    "current_step",
    "started_at",
    "updated_at",
    "finished_at",
    "source",
    "apply_candidate",
    "steps",
    "can_cancel",
    "rollback_supported",
    "expected_commit",
    "installed_commit",
    "commit_verified",
    "error",
}
LEGACY_MINIMAL_REQUEST_KEYS = {"schema_version", "request_id", "requested_at", "intent", "confirmed", "source"}
LEGACY_HISTORICAL_REQUEST_KEYS = LEGACY_MINIMAL_REQUEST_KEYS | {
    "requested_by",
    "preflight_required",
    "status_path",
}
LEGACY_SNAPSHOT_REQUEST_KEYS = LEGACY_HISTORICAL_REQUEST_KEYS | {"apply_candidate"}
LEGACY_TRANSITIONAL_REQUEST_KEYS = LEGACY_SNAPSHOT_REQUEST_KEYS | {"submission_id"}
LEGACY_MINIMAL_SOURCE_KEYS = {"version", "commit"}
LEGACY_ACTOR_KEYS = {"user_id", "role"}
LEGACY_MINIMAL_TERMINAL_KEYS = {
    "schema_version",
    "request_id",
    "status",
    "phase",
    "current_step",
    "started_at",
    "updated_at",
    "finished_at",
    "expected_commit",
    "installed_commit",
    "commit_verified",
    "error",
}
LEGACY_HISTORICAL_COMPLETED_TERMINAL_KEYS = {
    "schema_version",
    "request_id",
    "status",
    "phase",
    "current_step",
    "started_at",
    "updated_at",
    "source",
    "expected_commit",
    "installed_commit",
    "commit_verified",
    "steps",
    "can_cancel",
    "rollback_supported",
    "side_effects",
    "error",
}
LEGACY_HISTORICAL_COMPLETED_STEP_NAMES = (
    "request",
    "preflight",
    "apply",
    "health_check",
    "commit_verification",
)
TERMINAL_STEP_NAMES = {
    "request",
    "queued",
    "preflight",
    "acquire_source",
    "extracting",
    "validating_source",
    "overlay",
    "compose_config",
    "rebuilding",
    "restarting",
    "health_check",
    "commit_verification",
}
TERMINAL_STEP_STATUSES = {"pending", "running", "completed", "failed"}
TERMINAL_FAILURE_PHASES = {
    "helper_restart_interrupted": {"helper_restart_interrupted"},
    "helper_host_app_dir_missing": {"helper_host_app_dir_missing"},
    "helper_host_app_dir_invalid": {"helper_host_app_dir_invalid"},
    "helper_host_app_dir_unmounted": {"helper_host_app_dir_unmounted"},
    "preflight_failed": {"preflight_failed"},
    "compose_config_failed": {"compose_config_failed"},
    "jellyfin_ffmpeg_repo_unavailable": {"jellyfin_ffmpeg_repo_unavailable"},
    "build_network_dependency_failed": {"build_network_dependency_failed"},
    "docker_build_failed": {"docker_build_failed"},
    "health_check_failed": {"health_check_failed"},
    "commit_mismatch": {"commit_verification"},
    "commit_missing": {"commit_verification"},
    "metadata_invalid": {"commit_verification"},
    "apply_timeout": TERMINAL_STEP_NAMES - {"request"},
    "apply_failed": {"apply_failed"},
    "helper_exception": {"helper_exception"},
}
_ADMISSION_THREAD_LOCK = threading.RLock()
_AUDIT_COORDINATOR_STOP = threading.Event()
_AUDIT_COORDINATOR_THREAD: threading.Thread | None = None
_STARTUP_LINEAGE_LOCK = threading.RLock()
_STARTUP_LINEAGE_STATE: dict[str, Any] = {
    "status": "not_run",
    "classification": "unknown",
    "terminal": False,
    "marker_created": False,
}


class UpdateApplyBlocked(RuntimeError):
    def __init__(self, code: str, message: str, *, diagnostics: dict[str, Any] | None = None):
        self.code = code
        self.diagnostics = diagnostics or {}
        super().__init__(message)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat() + "Z"


def _parse_iso(value: Any) -> datetime | None:
    text = _safe_string(value, max_length=80)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _control_root() -> Path:
    return Path(settings.update_control_root)


def _request_path() -> Path:
    return _control_root() / "update-request.json"


def _status_path() -> Path:
    return _control_root() / "update-status.json"


def _apply_history_path() -> Path:
    return _control_root() / "update-apply-history.json"


def _lock_path() -> Path:
    return _control_root() / "update.lock"


def _admission_lock_path() -> Path:
    return _control_root() / "update-admission.lock"


def _lineage_marker_path() -> Path:
    return _control_root() / "update-admission-lineage.json"


def _progress_path() -> Path:
    return _control_root() / "update-progress.json"


def _helper_history_path() -> Path:
    return _control_root() / "update-helper-history.json"


@contextmanager
def _admission_guard():
    """Serialize Apply admission snapshots across API threads and processes."""
    root = _control_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = _admission_lock_path()
    with _ADMISSION_THREAD_LOCK:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _safe_string(value: Any, *, max_length: int = 300) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = SENSITIVE_VALUE_RE.sub("***", str(value).strip())
    return text[:max_length] or None


def _is_bounded_string(value: Any, *, minimum: int = 1, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value) <= maximum


def _is_nullable_bounded_string(value: Any, *, maximum: int) -> bool:
    return value is None or _is_bounded_string(value, maximum=maximum)


def _is_bounded_int(value: Any, *, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _is_exact_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def _is_allowed_string(value: Any, allowed: set[str] | frozenset[str]) -> bool:
    return type(value) is str and value in allowed


def _is_nullable_bounded_int(value: Any, *, minimum: int, maximum: int) -> bool:
    return value is None or _is_bounded_int(value, minimum=minimum, maximum=maximum)


def _has_exact_keys(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _contains_sensitive_content(value: Any) -> bool:
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return True
    return bool(SENSITIVE_VALUE_RE.search(rendered))


def _safe_error(code: str, message: str, action: str = "Review the update status and use terminal recovery if needed.") -> dict[str, str]:
    return {
        "category": _safe_string(code, max_length=80) or "update_apply_error",
        "message": _safe_string(message, max_length=300) or "Update apply is unavailable.",
        "operator_action": _safe_string(action, max_length=300) or "Review update status.",
    }


def _safe_machine_code(value: Any, *, max_length: int = 80) -> str | None:
    text = _safe_string(value, max_length=max_length)
    if not text or len(text) > max_length or not MACHINE_CODE_RE.fullmatch(text):
        return None
    return text


def _safe_public_timestamp(value: Any) -> str | None:
    text = _safe_string(value, max_length=80)
    return text if text and _parse_iso(text) is not None else None


def _safe_public_source(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    repo = _safe_string(value.get("repo"), max_length=160)
    if not repo or not GITHUB_REPO_RE.fullmatch(repo):
        repo = None
    ref = _safe_string(value.get("ref"), max_length=120)
    if (
        not ref
        or not GIT_REF_RE.fullmatch(ref)
        or ".." in ref
        or "@{" in ref
        or ref.endswith(".")
    ):
        ref = None
    version = _safe_string(value.get("version"), max_length=80)
    if not version or not VERSION_TEXT_RE.fullmatch(version):
        version = None
    commit = _safe_string(value.get("commit"), max_length=40)
    if not commit or not COMMIT_SHA_RE.fullmatch(commit):
        commit = None
    apply_ref = _safe_string(value.get("apply_ref"), max_length=40)
    if not apply_ref or not COMMIT_SHA_RE.fullmatch(apply_ref):
        apply_ref = None
    kind = _safe_string(value.get("kind"), max_length=40)
    if kind not in {"trusted_manifest", "github-tarball"}:
        kind = None
    source_type = _safe_machine_code(value.get("source_type"), max_length=40)
    if source_type not in {None, "github_tarball"}:
        source_type = None
    sanitized = {
        "kind": kind,
        "channel": _safe_machine_code(value.get("channel"), max_length=40),
        "version": version,
        "commit": commit.lower() if commit else None,
        "apply_ref": apply_ref.lower() if apply_ref else None,
        "ref": ref,
        "repo": repo,
        "source_type": source_type,
    }
    return sanitized if any(item is not None for item in sanitized.values()) else None


def _safe_public_apply_candidate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = _safe_machine_code(value.get("source"), max_length=40)
    raw_snapshot = value.get("snapshot") if isinstance(value.get("snapshot"), dict) else None
    snapshot: dict[str, Any] | None = None
    if raw_snapshot is not None:
        snapshot = {
            "available": raw_snapshot.get("available") if isinstance(raw_snapshot.get("available"), bool) else None,
            "fresh": raw_snapshot.get("fresh") if isinstance(raw_snapshot.get("fresh"), bool) else None,
            "age_seconds": raw_snapshot.get("age_seconds")
            if isinstance(raw_snapshot.get("age_seconds"), int)
            and not isinstance(raw_snapshot.get("age_seconds"), bool)
            and 0 <= raw_snapshot.get("age_seconds") <= 315_360_000
            else None,
            "fresh_for_seconds": raw_snapshot.get("fresh_for_seconds")
            if isinstance(raw_snapshot.get("fresh_for_seconds"), int)
            and not isinstance(raw_snapshot.get("fresh_for_seconds"), bool)
            and 0 <= raw_snapshot.get("fresh_for_seconds") <= 86_400
            else None,
        }
        if not any(item is not None for item in snapshot.values()):
            snapshot = None
    return {"source": source, "snapshot": snapshot} if source or snapshot else None


def _public_error_for_category(category: str) -> dict[str, str]:
    messages = {
        "cancelled_before_start": "Queued update apply was cancelled before helper started.",
        "helper_restart_interrupted": "Update execution was interrupted before terminal persistence.",
        "helper_host_app_dir_missing": "Update helper application directory is not configured.",
        "helper_host_app_dir_invalid": "Update helper application directory configuration is invalid.",
        "helper_host_app_dir_unmounted": "Update helper application directory is unavailable.",
        "preflight_failed": "Update preflight failed.",
        "compose_config_failed": "Docker Compose configuration validation failed.",
        "jellyfin_ffmpeg_repo_unavailable": "The external FFmpeg repository was unavailable during the update build.",
        "build_network_dependency_failed": "A required network dependency was unavailable during the update build.",
        "docker_build_failed": "Docker image rebuild failed during update apply.",
        "health_check_failed": "Update health check failed.",
        "commit_mismatch": "Installed commit did not match the trusted release commit.",
        "commit_missing": "Installed commit evidence is unavailable.",
        "metadata_invalid": "Installed release metadata is invalid.",
        "apply_timeout": "Update execution exceeded the bounded timeout.",
        "apply_failed": "Update apply failed.",
        "helper_exception": "Unexpected update helper failure.",
    }
    actions = {
        "cancelled_before_start": "No update was applied.",
        "helper_restart_interrupted": "Refresh update status and explicitly retry after the current state is resolved.",
        "helper_host_app_dir_missing": "Configure the update helper application directory before retrying.",
        "helper_host_app_dir_invalid": "Correct the update helper application directory configuration before retrying.",
        "helper_host_app_dir_unmounted": "Restore the update helper application directory mount before retrying.",
        "jellyfin_ffmpeg_repo_unavailable": "Restore repository connectivity, then explicitly retry the update.",
        "build_network_dependency_failed": "Restore network connectivity, then explicitly retry the update.",
        "compose_config_failed": "Review the server Compose configuration before retrying.",
        "docker_build_failed": "Review the sanitized build status before retrying.",
        "health_check_failed": "Review service health before retrying.",
        "commit_mismatch": "Treat the update as failed and verify the trusted release source before retrying.",
        "commit_missing": "Verify installed release identity before retrying.",
        "metadata_invalid": "Repair installed release identity before retrying.",
        "apply_timeout": "Refresh update status before deciding whether to retry.",
    }
    return {
        "category": category,
        "message": messages.get(category, "Update apply is unavailable."),
        "operator_action": actions.get(category, "Review update status and explicitly retry only after the cause is resolved."),
    }


def _safe_public_error(value: Any, *, require_complete: bool = False) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    category = _safe_machine_code(value.get("category"), max_length=80)
    message = _safe_string(value.get("message"), max_length=300)
    action = _safe_string(value.get("operator_action"), max_length=300)
    if require_complete and (not category or not message or not action):
        return None
    return _public_error_for_category(category or "update_apply_error")


def _json_limit(path: Path) -> int:
    if path == _request_path():
        return MAX_ADMISSION_BYTES
    if path == _lineage_marker_path():
        return MAX_LINEAGE_BYTES
    return MAX_CONTROL_BYTES


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if SENSITIVE_VALUE_RE.search(rendered):
        raise UpdateApplyBlocked("control_payload_sensitive", "Update control payload contains sensitive content.")
    if len(rendered.encode("utf-8")) > _json_limit(path):
        raise UpdateApplyBlocked(
            "submission_ledger_capacity" if path == _request_path() else "control_payload_too_large",
            "Update admission capacity is unavailable." if path == _request_path() else "Update control payload is too large.",
            diagnostics={"retry_allowed": False, "next_action": "wait_for_retention"},
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _decode_authority_json(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise TypeError
    return payload


def _is_authority_json_path(path: Path) -> bool:
    return path in {_request_path(), _lineage_marker_path(), _status_path()}


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        if not path.exists():
            return None, "missing"
        if path.stat().st_size > _json_limit(path):
            return None, "too_large"
        text = path.read_text(encoding="utf-8")
        payload = _decode_authority_json(text) if _is_authority_json_path(path) else json.loads(text)
    except (json.JSONDecodeError, UnicodeError, OSError, RecursionError, TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "invalid_shape"
    if _is_authority_json_path(path) and _contains_sensitive_content(payload):
        return None, "sensitive_content"
    return payload, "valid"


def _lineage_marker_contract(payload: dict[str, Any] | None, state: str) -> str:
    if state == "missing":
        return "missing"
    if (
        state != "valid"
        or not _has_exact_keys(payload, set(LINEAGE_MARKER_PAYLOAD))
        or not _is_exact_int(payload.get("schema_version"), LINEAGE_SCHEMA_VERSION)
        or payload.get("document_type") != LINEAGE_DOCUMENT_TYPE
        or payload.get("initialized") is not True
    ):
        return "invalid"
    return "valid"


def _read_lineage_marker_unlocked() -> str:
    payload, state = _read_json(_lineage_marker_path())
    return _lineage_marker_contract(payload, state)


def _write_lineage_marker_locked() -> dict[str, Any]:
    marker_state = _read_lineage_marker_unlocked()
    if marker_state == "valid":
        return {"created": False, "mode_0600": (_lineage_marker_path().stat().st_mode & 0o777) == 0o600}
    if marker_state != "missing":
        raise UpdateApplyBlocked("update_lineage_invalid", "Update admission lineage is invalid.")
    try:
        _atomic_write_json(_lineage_marker_path(), LINEAGE_MARKER_PAYLOAD)
    except UpdateApplyBlocked:
        raise
    except Exception as exc:
        raise UpdateApplyBlocked(
            "update_lineage_unavailable",
            "Update admission lineage could not be persisted.",
        ) from exc
    if _read_lineage_marker_unlocked() != "valid":
        raise UpdateApplyBlocked("update_lineage_unavailable", "Update admission lineage could not be persisted.")
    try:
        mode_0600 = (_lineage_marker_path().stat().st_mode & 0o777) == 0o600
    except OSError:
        mode_0600 = False
    return {"created": True, "mode_0600": mode_0600}


def _startup_lineage_snapshot() -> dict[str, Any]:
    with _STARTUP_LINEAGE_LOCK:
        return dict(_STARTUP_LINEAGE_STATE)


def _set_startup_lineage_state(
    status: str,
    classification: str,
    *,
    marker_created: bool = False,
) -> dict[str, Any]:
    snapshot = {
        "status": _safe_machine_code(status, max_length=40) or "error",
        "classification": _safe_machine_code(classification, max_length=80) or "unknown",
        "terminal": True,
        "marker_created": bool(marker_created),
    }
    with _STARTUP_LINEAGE_LOCK:
        _STARTUP_LINEAGE_STATE.clear()
        _STARTUP_LINEAGE_STATE.update(snapshot)
    return dict(snapshot)


def _normalized_submission_id(value: Any) -> str:
    submission_id = _safe_string(value, max_length=80)
    if not submission_id:
        raise UpdateApplyBlocked(
            "submission_id_required",
            "Refresh update status and confirm Apply again.",
            diagnostics={"retry_allowed": True, "next_action": "refresh_and_confirm"},
        )
    if not SUBMISSION_ID_RE.fullmatch(submission_id):
        raise UpdateApplyBlocked(
            "submission_id_invalid",
            "Refresh update status and confirm Apply again.",
            diagnostics={"retry_allowed": True, "next_action": "refresh_and_confirm"},
        )
    return submission_id.lower()


def _normalized_target_version(value: Any) -> str:
    version = value if type(value) is str else None
    if not version:
        raise UpdateApplyBlocked(
            "update_check_required",
            "Run Check update again before applying this release.",
            diagnostics={"reason": "expected_version_or_commit_missing"},
        )
    if (
        not _is_bounded_string(version, maximum=80)
        or not VERSION_TEXT_RE.fullmatch(version)
        or SENSITIVE_VALUE_RE.search(version)
    ):
        raise UpdateApplyBlocked(
            "submission_target_invalid",
            "The selected update target is invalid. Refresh update status.",
            diagnostics={"retry_allowed": True, "next_action": "refresh_status"},
        )
    return version


def _normalized_target_commit(value: Any) -> str:
    commit = value if type(value) is str else None
    if not commit:
        raise UpdateApplyBlocked(
            "update_check_required",
            "Run Check update again before applying this release.",
            diagnostics={"reason": "expected_version_or_commit_missing"},
        )
    if not COMMIT_SHA_RE.fullmatch(commit):
        raise UpdateApplyBlocked(
            "submission_target_invalid",
            "The selected update target is invalid. Refresh update status.",
            diagnostics={"retry_allowed": True, "next_action": "refresh_status"},
        )
    return commit.lower()


def _actor_snapshot(actor: Any, *, ip_address: str | None, user_agent: str | None) -> dict[str, Any]:
    actor_id = getattr(actor, "id", None)
    if not isinstance(actor_id, int) or actor_id <= 0:
        raise UpdateApplyBlocked("submission_actor_invalid", "Authenticated update actor is unavailable.")
    username = _safe_string(getattr(actor, "username", None), max_length=100)
    role = _safe_string(getattr(actor, "role", None), max_length=50)
    if not username or not role:
        raise UpdateApplyBlocked("submission_actor_invalid", "Authenticated update actor is unavailable.")
    return {
        "user_id": actor_id,
        "username": username,
        "role": role,
        "ip_address": _safe_string(ip_address, max_length=100),
        "user_agent": _safe_string(user_agent, max_length=300),
    }


def _proof_payload(
    *,
    submission_id: str,
    target_version: str,
    target_commit: str,
    actor_id: int,
    issued_at: datetime,
) -> dict[str, Any]:
    issued_epoch = int(issued_at.replace(tzinfo=timezone.utc).timestamp())
    return {
        "typ": SUBMISSION_PROOF_TYPE,
        "aud": SUBMISSION_PROOF_AUDIENCE,
        "purpose": SUBMISSION_PROOF_PURPOSE,
        "version": SUBMISSION_PROOF_VERSION,
        "jti": submission_id,
        "sub": str(actor_id),
        "target_version": target_version,
        "target_commit": target_commit,
        "iat": issued_epoch,
        "nbf": issued_epoch,
        "exp": issued_epoch + SUBMISSION_PROOF_TTL_SECONDS,
    }


def _issue_submission_proof(
    *,
    submission_id: str,
    target_version: str,
    target_commit: str,
    actor_id: int,
    issued_at: datetime,
) -> tuple[str, dict[str, Any]]:
    payload = _proof_payload(
        submission_id=submission_id,
        target_version=target_version,
        target_commit=target_commit,
        actor_id=actor_id,
        issued_at=issued_at,
    )
    proof = jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm="HS256",
        headers={"typ": SUBMISSION_PROOF_TYPE},
    )
    if not isinstance(proof, str) or len(proof.encode("utf-8")) > MAX_SUBMISSION_PROOF_BYTES:
        raise UpdateApplyBlocked("submission_proof_unavailable", "Update confirmation could not be prepared.")
    return proof, payload


def verify_update_apply_submission_proof(
    proof: Any,
    *,
    actor_id: int,
    submission_id: str | None = None,
    target_version: str | None = None,
    target_commit: str | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(proof, str) or not proof or len(proof.encode("utf-8")) > MAX_SUBMISSION_PROOF_BYTES:
        return "invalid", None
    try:
        header = jwt.get_unverified_header(proof)
        if header.get("alg") != "HS256" or header.get("typ") != SUBMISSION_PROOF_TYPE:
            return "invalid", None
        payload = jwt.decode(
            proof,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=SUBMISSION_PROOF_AUDIENCE,
            options={
                "require": ["typ", "aud", "purpose", "version", "jti", "sub", "target_version", "target_commit", "iat", "nbf", "exp"],
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError:
        return "invalid", None
    if (
        not _is_allowed_string(payload.get("aud"), {SUBMISSION_PROOF_AUDIENCE})
        or payload.get("typ") != SUBMISSION_PROOF_TYPE
        or payload.get("purpose") != SUBMISSION_PROOF_PURPOSE
        or not _is_exact_int(payload.get("version"), SUBMISSION_PROOF_VERSION)
        or payload.get("sub") != str(actor_id)
    ):
        return "invalid", None
    if not all(type(payload.get(key)) is int for key in ("iat", "nbf", "exp")):
        return "invalid", None
    try:
        normalized_id = _normalized_submission_id(payload.get("jti"))
        normalized_version = _normalized_target_version(payload.get("target_version"))
        normalized_commit = _normalized_target_commit(payload.get("target_commit"))
        issued_epoch = payload["iat"]
        not_before_epoch = payload["nbf"]
        expires_epoch = payload["exp"]
    except (TypeError, ValueError, UpdateApplyBlocked):
        return "invalid", None
    if not_before_epoch != issued_epoch or expires_epoch - issued_epoch != SUBMISSION_PROOF_TTL_SECONDS:
        return "invalid", None
    current_epoch = int((now or _utcnow()).replace(tzinfo=timezone.utc).timestamp())
    if issued_epoch > current_epoch + SUBMISSION_PROOF_LEEWAY_SECONDS:
        return "invalid", None
    if not_before_epoch > current_epoch + SUBMISSION_PROOF_LEEWAY_SECONDS:
        return "invalid", None
    if submission_id is not None and normalized_id != _normalized_submission_id(submission_id):
        return "invalid", None
    if target_version is not None and normalized_version != _normalized_target_version(target_version):
        return "invalid", None
    if target_commit is not None and normalized_commit != _normalized_target_commit(target_commit):
        return "invalid", None
    normalized = {
        "submission_id": normalized_id,
        "actor_id": actor_id,
        "target_version": normalized_version,
        "target_commit": normalized_commit,
        "issued_at": issued_epoch,
        "expires_at": expires_epoch,
    }
    state = "valid_expired" if expires_epoch < current_epoch - SUBMISSION_PROOF_LEEWAY_SECONDS else "valid_unexpired"
    return state, normalized


def _strict_timestamp(value: Any) -> bool:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 80
        or value != value.strip()
        or not CANONICAL_UTC_TIMESTAMP_RE.fullmatch(value)
    ):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _strict_request_source(value: Any, *, minimal_legacy: bool = False) -> dict[str, str] | None:
    expected_keys = LEGACY_MINIMAL_SOURCE_KEYS if minimal_legacy else REQUEST_SOURCE_KEYS
    if not _has_exact_keys(value, expected_keys) or _contains_sensitive_content(value):
        return None
    version = value.get("version")
    commit = value.get("commit")
    if (
        not _is_bounded_string(version, maximum=80)
        or not VERSION_TEXT_RE.fullmatch(version)
        or not _is_bounded_string(commit, minimum=40, maximum=40)
        or not COMMIT_SHA_RE.fullmatch(commit)
    ):
        return None
    normalized = {"version": version, "commit": commit.lower()}
    if minimal_legacy:
        return normalized
    channel = value.get("channel")
    apply_ref = value.get("apply_ref")
    ref = value.get("ref")
    repo = value.get("repo")
    if (
        value.get("kind") != "trusted_manifest"
        or value.get("source_type") != "github_tarball"
        or not _is_bounded_string(channel, maximum=80)
        or not SAFE_TEXT_RE.fullmatch(channel)
        or not _is_bounded_string(apply_ref, minimum=40, maximum=40)
        or not COMMIT_SHA_RE.fullmatch(apply_ref)
        or apply_ref.lower() != commit.lower()
        or not _is_bounded_string(ref, maximum=120)
        or not GIT_REF_RE.fullmatch(ref)
        or ".." in ref
        or "@{" in ref
        or ref.endswith(".")
        or not _is_bounded_string(repo, maximum=160)
        or not GITHUB_REPO_RE.fullmatch(repo)
    ):
        return None
    return {
        "kind": "trusted_manifest",
        "channel": channel,
        "version": version,
        "commit": commit.lower(),
        "apply_ref": apply_ref.lower(),
        "ref": ref,
        "repo": repo,
        "source_type": "github_tarball",
    }


def _strict_apply_candidate(value: Any) -> tuple[dict[str, Any], str] | None:
    if not _has_exact_keys(value, APPLY_CANDIDATE_KEYS) or _contains_sensitive_content(value):
        return None
    source = value.get("source")
    snapshot = value.get("snapshot")
    compact_keys = {"available", "fresh", "age_seconds", "fresh_for_seconds"}
    if not _is_allowed_string(source, {"trusted_snapshot", "live_check"}) or not isinstance(snapshot, dict):
        return None
    snapshot_keys = frozenset(snapshot)
    if snapshot_keys not in {frozenset(compact_keys), frozenset(APPLY_FRESHNESS_KEYS)}:
        return None
    if (
        not isinstance(snapshot.get("available"), bool)
        or not isinstance(snapshot.get("fresh"), bool)
        or not _is_nullable_bounded_int(snapshot.get("age_seconds"), minimum=0, maximum=315_360_000)
        or not _is_bounded_int(snapshot.get("fresh_for_seconds"), minimum=0, maximum=86_400)
    ):
        return None
    if snapshot_keys == frozenset(compact_keys):
        if snapshot.get("available") is not False or source != "live_check":
            return None
        normalized_snapshot = dict(snapshot)
        profile = APPLY_CANDIDATE_PROFILE_COMPACT_READ_ONLY
    else:
        version = snapshot.get("version")
        commit_short = snapshot.get("commit_short")
        provider = snapshot.get("provider")
        if (
            not (version is None or (_is_bounded_string(version, maximum=80) and VERSION_TEXT_RE.fullmatch(version)))
            or not (commit_short is None or (_is_bounded_string(commit_short, maximum=12) and re.fullmatch(r"[0-9a-fA-F]+", commit_short)))
            or not (provider is None or (_is_bounded_string(provider, maximum=80) and SAFE_TEXT_RE.fullmatch(provider)))
        ):
            return None
        if source == "trusted_snapshot" and snapshot.get("available") is not True:
            return None
        normalized_snapshot = dict(snapshot)
        if commit_short is not None:
            normalized_snapshot["commit_short"] = commit_short.lower()
        profile = APPLY_CANDIDATE_PROFILE_CANONICAL_CURRENT
    return {"source": source, "snapshot": normalized_snapshot}, profile


def _strict_current_actor(value: Any) -> dict[str, Any] | None:
    if not _has_exact_keys(value, CURRENT_ACTOR_KEYS) or _contains_sensitive_content(value):
        return None
    user_id = value.get("user_id")
    username = value.get("username")
    role = value.get("role")
    ip_address = value.get("ip_address")
    user_agent = value.get("user_agent")
    if (
        not _is_bounded_int(user_id, minimum=1, maximum=9_223_372_036_854_775_807)
        or not _is_bounded_string(username, maximum=100)
        or not _is_bounded_string(role, maximum=50)
        or not _is_nullable_bounded_string(ip_address, maximum=100)
        or not _is_nullable_bounded_string(user_agent, maximum=300)
    ):
        return None
    return dict(value)


def _strict_legacy_actor(value: Any) -> bool:
    if not _has_exact_keys(value, LEGACY_ACTOR_KEYS) or _contains_sensitive_content(value):
        return False
    user_id = value.get("user_id")
    valid_user_id = _is_bounded_int(user_id, minimum=1, maximum=9_223_372_036_854_775_807) or (
        _is_bounded_string(user_id, maximum=20) and user_id.isdigit() and int(user_id) > 0
    )
    return bool(valid_user_id and _is_bounded_string(value.get("role"), maximum=50))


def _legacy_request_contract(payload: dict[str, Any] | None, state: str) -> tuple[str, dict[str, Any] | None]:
    if state == "missing":
        return "missing", None
    if state != "valid" or not isinstance(payload, dict) or _contains_sensitive_content(payload):
        return "invalid", None
    keys = set(payload)
    if keys == LEGACY_MINIMAL_REQUEST_KEYS:
        profile = "minimal"
    elif keys == LEGACY_HISTORICAL_REQUEST_KEYS:
        profile = "historical"
    elif keys == LEGACY_SNAPSHOT_REQUEST_KEYS:
        profile = "snapshot"
    elif keys == LEGACY_TRANSITIONAL_REQUEST_KEYS:
        profile = "transitional"
    else:
        return "invalid", None
    source = _strict_request_source(payload.get("source"), minimal_legacy=profile == "minimal")
    request_id = payload.get("request_id")
    requested_at = payload.get("requested_at")
    if (
        not _is_exact_int(payload.get("schema_version"), LEGACY_REQUEST_SCHEMA_VERSION)
        or payload.get("intent") != "apply_update"
        or payload.get("confirmed") is not True
        or not _is_bounded_string(request_id, maximum=80)
        or not LEGACY_REQUEST_ID_RE.fullmatch(request_id)
        or not _strict_timestamp(requested_at)
        or source is None
    ):
        return "invalid", None
    if profile != "minimal" and (
        not _strict_legacy_actor(payload.get("requested_by"))
        or payload.get("preflight_required") is not True
        or payload.get("status_path") != "data/update-control/update-status.json"
    ):
        return "invalid", None
    if profile in {"snapshot", "transitional"} and _strict_apply_candidate(payload.get("apply_candidate")) is None:
        return "invalid", None
    submission_id = payload.get("submission_id") if profile == "transitional" else None
    if submission_id is not None and (not isinstance(submission_id, str) or not SUBMISSION_ID_RE.fullmatch(submission_id)):
        return "invalid", None
    return "legacy", {
        "request_id": request_id,
        "submission_id": submission_id.lower() if submission_id else None,
        "target_commit": source["commit"],
        "target_version": source["version"],
        "requested_at": requested_at,
        "payload": payload,
        "legacy_profile": profile,
    }


def _empty_admission_document() -> dict[str, Any]:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "document_type": ADMISSION_DOCUMENT_TYPE,
        "current_submission_id": None,
        "entries": [],
        "updated_at": _iso(),
    }


def _entry_request_contract(value: Any) -> dict[str, Any] | None:
    if not _has_exact_keys(value, CURRENT_REQUEST_KEYS) or _contains_sensitive_content(value):
        return None
    source = _strict_request_source(value.get("source"))
    actor = _strict_current_actor(value.get("requested_by"))
    candidate_contract = _strict_apply_candidate(value.get("apply_candidate"))
    request_id = value.get("request_id")
    submission_id = value.get("submission_id")
    requested_at = value.get("requested_at")
    if (
        not _is_exact_int(value.get("schema_version"), REQUEST_SCHEMA_VERSION)
        or value.get("intent") != "apply_update"
        or value.get("confirmed") is not True
        or value.get("preflight_required") is not True
        or value.get("status_path") != "data/update-control/update-status.json"
        or not isinstance(request_id, str)
        or not REQUEST_ID_RE.fullmatch(request_id)
        or not isinstance(submission_id, str)
        or not SUBMISSION_ID_RE.fullmatch(submission_id)
        or not _strict_timestamp(requested_at)
        or source is None
        or actor is None
        or candidate_contract is None
    ):
        return None
    _candidate, candidate_profile = candidate_contract
    return {
        "request_id": request_id,
        "submission_id": submission_id.lower(),
        "requested_at": requested_at,
        "target_version": source["version"],
        "target_commit": source["commit"],
        "payload": value,
        "candidate_profile": candidate_profile,
    }


def _admission_entry_contract(value: Any) -> dict[str, Any] | None:
    if (
        not _has_exact_keys(value, ADMISSION_ENTRY_KEYS)
        or not _is_allowed_string(value.get("state"), ADMISSION_STATES)
        or _contains_sensitive_content(value)
    ):
        return None
    request = _entry_request_contract(value.get("request"))
    if not request:
        return None
    candidate_profile = request.pop("candidate_profile")
    state = value.get("state")
    if state in NON_TERMINAL_ADMISSION_STATES and candidate_profile != APPLY_CANDIDATE_PROFILE_CANONICAL_CURRENT:
        return None
    if (
        not isinstance(value.get("submission_id"), str)
        or value.get("submission_id").lower() != request["submission_id"]
        or value.get("request_id") != request["request_id"]
        or value.get("target_version") != request["target_version"]
        or not isinstance(value.get("target_commit"), str)
        or value.get("target_commit").lower() != request["target_commit"]
        or value.get("requested_at") != request["requested_at"]
        or not _strict_timestamp(value.get("updated_at"))
    ):
        return None
    audit = value.get("audit")
    if not _has_exact_keys(audit, AUDIT_KEYS):
        return None
    event_id = audit.get("event_id")
    audit_state = audit.get("state")
    expected_event_id = _audit_event_id(request["request_id"])
    if not isinstance(event_id, str) or event_id != expected_event_id:
        return None
    if not _is_allowed_string(audit_state, {"pending", "confirmed"}):
        return None
    confirmed_at = audit.get("confirmed_at")
    if value.get("state") == "audit_pending":
        if audit_state != "pending" or confirmed_at is not None:
            return None
    elif audit_state != "confirmed" or not _strict_timestamp(confirmed_at):
        return None
    claimed_at = value.get("claimed_at")
    terminal = value.get("terminal")
    requested_time = _parse_iso(request["requested_at"])
    updated_time = _parse_iso(value.get("updated_at"))
    confirmed_time = _parse_iso(audit.get("confirmed_at")) if audit.get("confirmed_at") is not None else None
    claimed_time = _parse_iso(claimed_at) if claimed_at is not None and _strict_timestamp(claimed_at) else None
    if requested_time is None or updated_time is None or updated_time < requested_time:
        return None
    if confirmed_time is not None and (confirmed_time < requested_time or updated_time < confirmed_time):
        return None
    if state in {"audit_pending", "admitted_unclaimed"} and (claimed_at is not None or terminal is not None):
        return None
    if state == "claimed" and (claimed_time is None or terminal is not None):
        return None
    if claimed_time is not None and (claimed_time < (confirmed_time or requested_time) or updated_time < claimed_time):
        return None
    result = {
        **request,
        "state": state,
        "audit": audit,
        "claimed_at": claimed_at,
        "terminal": terminal if isinstance(terminal, dict) else None,
        "entry": value,
    }
    if state == "terminal":
        terminal_shape = _terminal_shape(terminal, result) if isinstance(terminal, dict) else None
        if (
            candidate_profile == APPLY_CANDIDATE_PROFILE_COMPACT_READ_ONLY
            and terminal_shape != "pre_closeout_cancel"
        ):
            return None
        strict_terminal = _strict_terminal_snapshot(terminal, result)
        if strict_terminal is None:
            return None
        terminal_status = strict_terminal.get("status")
        if terminal_status == "cancelled":
            if claimed_at is not None:
                return None
        elif _is_allowed_string(terminal_status, {"failed", "completed"}):
            if claimed_time is None:
                return None
        else:
            return None
        finished_time = _parse_iso(strict_terminal.get("finished_at"))
        if finished_time is None or updated_time < finished_time or (claimed_time is not None and finished_time < claimed_time):
            return None
    elif terminal is not None:
        return None
    return result


def _admission_document_contract(
    payload: dict[str, Any] | None,
    state: str,
) -> tuple[str, dict[str, Any] | None]:
    if state == "missing":
        return "missing", None
    if state != "valid" or not payload:
        return "invalid", None
    raw_schema_version = payload.get("schema_version")
    if _is_exact_int(raw_schema_version, LEGACY_REQUEST_SCHEMA_VERSION):
        legacy_contract, legacy = _legacy_request_contract(payload, state)
        return ("legacy", legacy) if legacy_contract in {"legacy", "current"} else ("invalid", None)
    if (
        not _has_exact_keys(payload, ADMISSION_DOCUMENT_KEYS)
        or _contains_sensitive_content(payload)
        or not _is_exact_int(raw_schema_version, REQUEST_SCHEMA_VERSION)
        or payload.get("document_type") != ADMISSION_DOCUMENT_TYPE
        or not _strict_timestamp(payload.get("updated_at"))
    ):
        return "invalid", None
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_ADMISSION_ENTRIES:
        return "invalid", None
    entries: list[dict[str, Any]] = []
    seen_submissions: set[str] = set()
    seen_requests: set[str] = set()
    for raw in raw_entries:
        entry = _admission_entry_contract(raw)
        if (
            not entry
            or entry["submission_id"] in seen_submissions
            or entry["request_id"] in seen_requests
        ):
            return "invalid", None
        seen_submissions.add(entry["submission_id"])
        seen_requests.add(entry["request_id"])
        entries.append(entry)
    current_id = payload.get("current_submission_id")
    if current_id is not None:
        if not isinstance(current_id, str):
            return "invalid", None
        try:
            current_id = _normalized_submission_id(current_id)
        except UpdateApplyBlocked:
            return "invalid", None
        if current_id not in seen_submissions:
            return "invalid", None
    non_terminal = [entry for entry in entries if entry.get("state") in NON_TERMINAL_ADMISSION_STATES]
    if len(non_terminal) > 1:
        return "invalid", None
    if non_terminal and current_id != non_terminal[0]["submission_id"]:
        return "invalid", None
    if current_id is None and non_terminal:
        return "invalid", None
    document_updated = _parse_iso(payload.get("updated_at"))
    if document_updated is None or any(document_updated < _parse_iso(entry["entry"].get("updated_at")) for entry in entries):
        return "invalid", None
    return "current", {
        "payload": payload,
        "entries": entries,
        "by_submission": {entry["submission_id"]: entry for entry in entries},
        "current_submission_id": current_id,
        "current": next((entry for entry in entries if entry["submission_id"] == current_id), None),
    }


def _read_admission_document_unlocked() -> tuple[str, dict[str, Any] | None]:
    payload, state = _read_json(_request_path())
    return _admission_document_contract(payload, state)


def _execution_footprints_unlocked() -> dict[str, bool]:
    return {
        "execution_lock": _lock_path().exists(),
        "status": _status_path().exists(),
        "progress": _progress_path().exists(),
        "apply_history": _apply_history_path().exists(),
        "helper_history": _helper_history_path().exists(),
    }


def _read_admission_authority_unlocked() -> dict[str, Any]:
    contract, document = _read_admission_document_unlocked()
    marker_state = _read_lineage_marker_unlocked()
    footprints = _execution_footprints_unlocked()
    if contract == "invalid" or marker_state == "invalid":
        classification = "invalid"
    elif contract == "current":
        classification = "current" if marker_state == "valid" else "lineage_incomplete"
    elif contract == "legacy":
        classification = "legacy"
    elif marker_state == "valid" or any(footprints.values()):
        classification = "missing_unexpected"
    else:
        classification = "pristine"
    return {
        "classification": classification,
        "contract": contract,
        "document": document,
        "marker_state": marker_state,
        "footprints": footprints,
    }


def adopt_update_apply_lineage_on_startup() -> dict[str, Any]:
    try:
        with _admission_guard():
            authority = _read_admission_authority_unlocked()
            classification = authority["classification"]
            marker_created = False
            if classification == "lineage_incomplete" or (
                classification == "legacy" and authority["marker_state"] == "missing"
            ):
                result = _write_lineage_marker_locked()
                marker_created = bool(result.get("created"))
                authority = _read_admission_authority_unlocked()
                classification = authority["classification"]
                if classification not in {"current", "legacy"}:
                    return _set_startup_lineage_state("blocked", classification, marker_created=marker_created)
                return _set_startup_lineage_state("adopted", classification, marker_created=marker_created)
            if classification in {"pristine", "current", "legacy"}:
                return _set_startup_lineage_state("not_required", classification)
            return _set_startup_lineage_state("blocked", classification)
    except Exception:
        return _set_startup_lineage_state("error", "startup_error")


def _admission_payload(
    authority: str,
    state: str,
    *,
    request: dict[str, Any] | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    request = request or {}
    request_id = _safe_string(request.get("request_id"), max_length=80)
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "authority": authority,
        "state": state,
        "linearizable": authority in {"active", "inactive"},
        "active": True if authority == "active" else False if authority == "inactive" else None,
        "submission_id": _safe_string(request.get("submission_id"), max_length=80),
        "request_id": request_id,
        "target_version": _safe_string(request.get("target_version"), max_length=80),
        "target_commit": _safe_string(request.get("target_commit"), max_length=40),
        "generation": request_id,
        "reason_code": _safe_string(reason_code, max_length=80),
        "retry_allowed": authority == "inactive",
        "next_action": "confirm_apply" if authority == "inactive" else "wait_for_status" if authority == "active" else "refresh_status",
        "startup_adoption": _startup_lineage_snapshot(),
    }


def _sanitized_source(latest: dict[str, Any]) -> dict[str, Any]:
    commit = _safe_string(latest.get("commit"), max_length=40)
    return {
        "kind": "trusted_manifest",
        "channel": _safe_string(latest.get("channel"), max_length=80) or "stable",
        "version": _safe_string(latest.get("version"), max_length=80),
        "commit": commit,
        "apply_ref": commit,
        "ref": _safe_string(latest.get("source_ref") or latest.get("git_ref"), max_length=120),
        "repo": _safe_string(latest.get("source_repo"), max_length=160),
        "source_type": _safe_string(latest.get("source_type"), max_length=80),
    }


def _validate_latest_for_apply(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("manifest_source_status") == "not_configured" or result.get("status") == "not_configured":
        raise UpdateApplyBlocked("manifest_not_configured", "Trusted release manifest source is not configured.")
    if result.get("status") in {"check_failed", "invalid_manifest", "failed"}:
        raise UpdateApplyBlocked("manifest_check_failed", "Trusted release manifest check failed.")
    blockers = result.get("blockers") or []
    if blockers:
        code = _safe_string(blockers[0].get("code") if isinstance(blockers[0], dict) else blockers[0], max_length=80) or "release_blocked"
        raise UpdateApplyBlocked(code, "Release has blockers that cannot be applied from the UI.", diagnostics={"blockers": blockers})
    if result.get("status") != "update_available":
        raise UpdateApplyBlocked("no_update_available", "No trusted compatible update is available.")
    latest = result.get("latest")
    if not isinstance(latest, dict):
        raise UpdateApplyBlocked("latest_release_missing", "Latest trusted release metadata is missing.")
    if latest.get("requires_backup") or latest.get("requires_manual_action") or latest.get("requires_migration"):
        raise UpdateApplyBlocked("unsupported_release_requirements", "Release requires backup, manual action or migration support that is outside this stage.")
    if latest.get("source_type") != "github_tarball" or not latest.get("source_repo") or not (latest.get("source_ref") or latest.get("git_ref")):
        raise UpdateApplyBlocked("trusted_source_incomplete", "Trusted release source must be a GitHub tarball repo/ref.")
    commit = str(latest.get("commit") or "")
    if not COMMIT_SHA_RE.fullmatch(commit):
        raise UpdateApplyBlocked("trusted_commit_missing", "Trusted release manifest must include a full commit SHA before in-app apply.")
    return latest


def _check_token_precondition() -> None:
    if not settings.kmvms_update_source_private:
        return
    if settings.kmvms_update_token_configured:
        return
    if os.getenv("KM_VMS_GITHUB_TOKEN"):
        return
    token_file = os.getenv("KM_VMS_GITHUB_TOKEN_FILE")
    if token_file and Path(token_file).is_file():
        return
    raise UpdateApplyBlocked("token_not_configured", "Private trusted source requires a server-side GitHub token source.")


def _validate_expected(latest: dict[str, Any], *, expected_version: str | None, expected_commit: str | None) -> None:
    if not expected_version or not expected_commit:
        raise UpdateApplyBlocked("update_check_required", "Run Check update again before applying this release.", diagnostics={"reason": "expected_version_or_commit_missing"})
    if expected_version and expected_version != latest.get("version"):
        raise UpdateApplyBlocked("manifest_version_changed", "Trusted manifest version changed. Refresh update status and retry.")
    if expected_commit and expected_commit != latest.get("commit"):
        raise UpdateApplyBlocked("manifest_commit_changed", "Trusted manifest commit changed. Refresh update status and retry.")


def _installed_matches_snapshot(snapshot: dict[str, Any]) -> bool:
    fingerprint = snapshot.get("installed_fingerprint") if isinstance(snapshot.get("installed_fingerprint"), dict) else {}
    installed = read_installed_update_state()
    expected_commit = _safe_string(fingerprint.get("installed_commit"), max_length=40)
    expected_git_head = _safe_string(fingerprint.get("git_head"), max_length=40)
    if (fingerprint.get("installed_version") or None) != (installed.installed_version or None):
        return False
    if (expected_commit or None) != (installed.installed_commit or None):
        return False
    if (expected_git_head or None) != (installed.git_head or None):
        return False
    if (fingerprint.get("identity_validity") or None) != (installed.identity_validity or None):
        return False
    return True


def _snapshot_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    latest = snapshot.get("latest") if isinstance(snapshot.get("latest"), dict) else {}
    return {
        "status": "update_available",
        "blockers": [],
        "latest": latest,
        "manifest_source_status": snapshot.get("manifest_source_status"),
    }


def _canonical_freshness_snapshot(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "available": raw.get("available") if isinstance(raw.get("available"), bool) else False,
        "fresh": raw.get("fresh") if isinstance(raw.get("fresh"), bool) else False,
        "age_seconds": raw.get("age_seconds"),
        "fresh_for_seconds": raw.get("fresh_for_seconds"),
        "version": raw.get("version"),
        "commit_short": raw.get("commit_short"),
        "provider": raw.get("provider"),
    }


def _select_apply_candidate(db: Session, *, expected_version: str | None, expected_commit: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if not expected_version or not expected_commit:
        raise UpdateApplyBlocked("update_check_required", "Run Check update again before applying this release.", diagnostics={"reason": "expected_version_or_commit_missing"})
    snapshot_status = trusted_apply_snapshot_status()
    if snapshot_status.get("available") and not snapshot_status.get("fresh"):
        raise UpdateApplyBlocked("trusted_snapshot_stale", "Update check is too old. Run Check update again.", diagnostics={"snapshot": snapshot_status})
    snapshot = get_trusted_apply_snapshot()
    if snapshot:
        latest = _validate_latest_for_apply(_snapshot_result(snapshot))
        _validate_expected(latest, expected_version=expected_version, expected_commit=expected_commit)
        if not _installed_matches_snapshot(snapshot):
            raise UpdateApplyBlocked("trusted_snapshot_invalidated", "Installed release identity changed after update check. Run Check update again.", diagnostics={"snapshot": snapshot.get("freshness")})
        return latest, {"source": "trusted_snapshot", "snapshot": _canonical_freshness_snapshot(snapshot.get("freshness"))}
    update = run_update_check(db, manual=False)
    latest = _validate_latest_for_apply(update)
    _validate_expected(latest, expected_version=expected_version, expected_commit=expected_commit)
    return latest, {"source": "live_check", "snapshot": _canonical_freshness_snapshot(snapshot_status)}


def _running_status(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    return str(payload.get("status") or "") in RUNNING_STATUSES


def _safe_steps(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    steps: list[dict[str, str]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        steps.append(
            {
                "name": _safe_machine_code(item.get("name"), max_length=80) or "unknown",
                "status": _safe_machine_code(item.get("status"), max_length=40) or "pending",
            }
        )
    return steps


def _sanitize_apply_history_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    request_id = _safe_string(item.get("request_id"), max_length=80)
    submission_id = _safe_string(item.get("submission_id"), max_length=80)
    target_version = _safe_string(item.get("target_version"), max_length=80)
    expected_commit = _safe_string(item.get("expected_commit"), max_length=40)
    installed_commit = _safe_string(item.get("installed_commit"), max_length=40)
    sanitized = {
        "request_id": request_id if request_id and REQUEST_ID_RE.fullmatch(request_id) else None,
        "submission_id": submission_id.lower() if submission_id and SUBMISSION_ID_RE.fullmatch(submission_id) else None,
        "target_version": target_version if target_version and VERSION_TEXT_RE.fullmatch(target_version) else None,
        "status": _safe_machine_code(item.get("status"), max_length=40),
        "phase": _safe_machine_code(item.get("phase"), max_length=80),
        "started_at": _safe_public_timestamp(item.get("started_at")),
        "finished_at": _safe_public_timestamp(item.get("finished_at") or item.get("updated_at")),
        "updated_at": _safe_public_timestamp(item.get("updated_at")),
        "expected_commit": expected_commit.lower() if expected_commit and COMMIT_SHA_RE.fullmatch(expected_commit) else None,
        "installed_commit": installed_commit.lower() if installed_commit and COMMIT_SHA_RE.fullmatch(installed_commit) else None,
        "commit_verified": item.get("commit_verified") is True,
        "source": _safe_public_source(item.get("source")),
        "apply_candidate": _safe_public_apply_candidate(item.get("apply_candidate")),
        "steps": _safe_steps(item.get("steps")),
        "error": _safe_public_error(item.get("error")),
        "history_detail_status": _safe_machine_code(item.get("history_detail_status"), max_length=80) or "step_timestamps_unavailable",
    }
    rendered = json.dumps(sanitized, ensure_ascii=False)
    if SENSITIVE_VALUE_RE.search(rendered):
        return None
    return sanitized


def _read_apply_history() -> dict[str, Any]:
    payload, state = _read_json(_apply_history_path())
    if state == "missing":
        return {"available": False, "state": "missing", "items": [], "last": None, "max_items": MAX_APPLY_HISTORY_ITEMS}
    if state != "valid" or not payload:
        return {"available": False, "state": state, "items": [], "last": None, "max_items": MAX_APPLY_HISTORY_ITEMS}
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    items = [item for item in (_sanitize_apply_history_item(raw) for raw in raw_items[-MAX_APPLY_HISTORY_ITEMS:]) if item]
    return {"available": bool(items), "state": "valid", "items": items, "last": items[-1] if items else None, "max_items": MAX_APPLY_HISTORY_ITEMS}


def _base_status(
    status_value: str = "idle",
    phase: str = "idle",
    *,
    request_id: str | None = None,
    submission_id: str | None = None,
) -> dict[str, Any]:
    now = _iso()
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "request_id": request_id,
        "submission_id": submission_id,
        "target_version": None,
        "status": status_value,
        "phase": phase,
        "current_step": phase,
        "started_at": None,
        "updated_at": now,
        "elapsed_seconds": None,
        "last_progress_age_seconds": None,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "is_stale": False,
        "effective_status": status_value,
        "release_identity": None,
        "source": None,
        "apply_candidate": None,
        "steps": [],
        "can_cancel": status_value == "queued",
        "rollback_supported": False,
        "side_effects": {
            "api_docker_socket": False,
            "api_shell_execution": False,
            "request_controlled_source": False,
            "helper_has_docker_socket": True,
            "helper_public_ports": False,
        },
        "error": None,
        "admission": _admission_payload("unknown", "not_evaluated"),
        "last_apply_summary": None,
        "apply_history": {"available": False, "state": "missing", "items": [], "last": None, "max_items": MAX_APPLY_HISTORY_ITEMS},
    }


def _read_update_apply_status_without_admission() -> dict[str, Any]:
    apply_history = _read_apply_history()
    payload, state = _read_json(_status_path())
    if state == "missing":
        base = _base_status()
        base["apply_history"] = apply_history
        base["last_apply_summary"] = apply_history["last"]
        return base
    if state != "valid":
        status_payload = _base_status("blocked", "status_read")
        status_payload["error"] = _safe_error("status_" + state, "Update status file is unavailable or invalid.")
        status_payload["apply_history"] = apply_history
        status_payload["last_apply_summary"] = apply_history["last"]
        return status_payload
    status_value = _safe_machine_code(payload.get("status"), max_length=40) or "unknown"
    phase = _safe_machine_code(payload.get("phase") or payload.get("current_step"), max_length=80) or "unknown"
    current_step = _safe_machine_code(payload.get("current_step") or payload.get("phase"), max_length=80) or "unknown"
    request_id = _safe_string(payload.get("request_id"), max_length=80)
    submission_id = _safe_string(payload.get("submission_id"), max_length=80)
    sanitized = _base_status(
        status_value,
        phase,
        request_id=request_id if request_id and REQUEST_ID_RE.fullmatch(request_id) else None,
        submission_id=submission_id.lower() if submission_id and SUBMISSION_ID_RE.fullmatch(submission_id) else None,
    )
    sanitized.update(
        {
            "schema_version": 1,
            "current_step": current_step,
            "started_at": _safe_public_timestamp(payload.get("started_at")),
            "target_version": _safe_string(payload.get("target_version"), max_length=80)
            if VERSION_TEXT_RE.fullmatch(_safe_string(payload.get("target_version"), max_length=80) or "")
            else None,
            "updated_at": _safe_public_timestamp(payload.get("updated_at")) or _iso(),
            "source": _safe_public_source(payload.get("source")),
            "apply_candidate": _safe_public_apply_candidate(payload.get("apply_candidate")),
            "steps": _safe_steps(payload.get("steps")),
            "can_cancel": bool(payload.get("can_cancel")) and str(payload.get("status")) == "queued",
            "rollback_supported": False,
            "error": _safe_public_error(payload.get("error")),
            "expected_commit": (_safe_string(payload.get("expected_commit"), max_length=40) or "").lower()
            if COMMIT_SHA_RE.fullmatch(_safe_string(payload.get("expected_commit"), max_length=40) or "")
            else None,
            "installed_commit": (_safe_string(payload.get("installed_commit"), max_length=40) or "").lower()
            if COMMIT_SHA_RE.fullmatch(_safe_string(payload.get("installed_commit"), max_length=40) or "")
            else None,
            "commit_verified": payload.get("commit_verified") is True,
            "apply_history": apply_history,
            "last_apply_summary": apply_history["last"],
        }
    )
    now = _utcnow()
    started_at = _parse_iso(sanitized.get("started_at"))
    updated_at = _parse_iso(sanitized.get("updated_at"))
    elapsed_seconds = int((now - started_at).total_seconds()) if started_at else None
    last_progress_age_seconds = int((now - updated_at).total_seconds()) if updated_at else None
    is_stale = bool(str(sanitized.get("status")) in RUNNING_STATUSES and last_progress_age_seconds is not None and last_progress_age_seconds > STALE_AFTER_SECONDS)
    sanitized["elapsed_seconds"] = elapsed_seconds
    sanitized["last_progress_age_seconds"] = last_progress_age_seconds
    sanitized["stale_after_seconds"] = STALE_AFTER_SECONDS
    sanitized["is_stale"] = is_stale
    sanitized["effective_status"] = "stalled" if is_stale else sanitized.get("status")
    release_payload, release_state = _read_json(Path(os.getenv("KMVMS_APP_ROOT") or os.getenv("KM_VMS_APP_DIR") or Path.cwd()) / ".km-vms-release.json")
    if release_state == "valid" and release_payload:
        commit_sha = _safe_string(release_payload.get("commit_sha"), max_length=40)
        sanitized["release_identity"] = {
            "metadata_status": _safe_machine_code(release_payload.get("metadata_status"), max_length=40),
            "metadata_source": _safe_machine_code(release_payload.get("metadata_source"), max_length=80),
            "commit_sha": commit_sha.lower() if commit_sha and COMMIT_SHA_RE.fullmatch(commit_sha) else None,
        }
    elif release_state != "missing":
        sanitized["release_identity"] = {"metadata_status": release_state}
    rendered = json.dumps(sanitized, ensure_ascii=False)
    if SENSITIVE_VALUE_RE.search(rendered):
        blocked = _base_status("blocked", "status_redaction", request_id=sanitized.get("request_id"))
        blocked["error"] = _safe_error("status_sensitive_content", "Update status contained sensitive content and was suppressed.")
        return blocked
    return sanitized


def _queued_status_from_request(request: dict[str, Any]) -> dict[str, Any]:
    payload = request["payload"]
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    queued = _base_status(
        "queued",
        "queued",
        request_id=request.get("request_id"),
        submission_id=request.get("submission_id"),
    )
    queued.update(
        {
            "started_at": request.get("requested_at"),
            "updated_at": request.get("requested_at") or _iso(),
            "target_version": request.get("target_version"),
            "source": _safe_public_source({**source, "kind": "github-tarball"}),
            "apply_candidate": _safe_public_apply_candidate(payload.get("apply_candidate")),
            "expected_commit": request.get("target_commit"),
            "commit_verified": False,
            "steps": [
                {"name": "request", "status": "completed"},
                {"name": "preflight", "status": "pending"},
                {"name": "apply", "status": "pending"},
                {"name": "health_check", "status": "pending"},
            ],
            "can_cancel": True,
        }
    )
    return queued


def _status_identity_matches(payload: dict[str, Any], entry: dict[str, Any]) -> bool:
    legacy = isinstance(entry.get("payload"), dict) and _is_exact_int(
        entry["payload"].get("schema_version"), LEGACY_REQUEST_SCHEMA_VERSION
    )
    payload_submission_value = payload.get("submission_id")
    entry_submission_value = entry.get("submission_id")
    if payload_submission_value is not None and not isinstance(payload_submission_value, str):
        return False
    if entry_submission_value is not None and not isinstance(entry_submission_value, str):
        return False
    payload_submission = (payload_submission_value or "").lower()
    entry_submission = (entry_submission_value or "").lower()
    payload_version = payload.get("target_version")
    version_matches = payload_version == entry.get("target_version") or (legacy and payload_version is None)
    request_id = payload.get("request_id")
    expected_commit = payload.get("expected_commit")
    return (
        isinstance(request_id, str)
        and request_id == entry.get("request_id")
        and payload_submission == entry_submission
        and version_matches
        and isinstance(expected_commit, str)
        and expected_commit.lower() == entry.get("target_commit")
    )


def _safe_terminal_error(value: Any) -> dict[str, str] | None:
    if not _has_exact_keys(value, TERMINAL_ERROR_KEYS):
        return None
    category = _safe_machine_code(value.get("category"), max_length=80)
    raw_message = value.get("message")
    raw_action = value.get("operator_action")
    if (
        not category
        or not isinstance(raw_message, str)
        or not isinstance(raw_action, str)
        or not raw_message.strip()
        or not raw_action.strip()
        or len(raw_message) > 300
        or len(raw_action) > 300
        or SENSITIVE_VALUE_RE.search(raw_message)
        or SENSITIVE_VALUE_RE.search(raw_action)
        or UNSAFE_PUBLIC_TEXT_RE.search(raw_message)
        or UNSAFE_PUBLIC_TEXT_RE.search(raw_action)
    ):
        return None
    return _public_error_for_category(category)


def _strict_terminal_source(value: Any, entry: dict[str, Any]) -> bool:
    if not _has_exact_keys(value, TERMINAL_SOURCE_KEYS) or _contains_sensitive_content(value):
        return False
    request_source = entry.get("payload", {}).get("source")
    if not isinstance(request_source, dict):
        return False
    commit = value.get("commit")
    apply_ref = value.get("apply_ref")
    return bool(
        value.get("kind") == "github-tarball"
        and value.get("repo") == request_source.get("repo")
        and value.get("ref") == request_source.get("ref")
        and isinstance(commit, str)
        and COMMIT_SHA_RE.fullmatch(commit)
        and commit.lower() == entry.get("target_commit")
        and isinstance(apply_ref, str)
        and COMMIT_SHA_RE.fullmatch(apply_ref)
        and apply_ref.lower() == entry.get("target_commit")
    )


def _strict_terminal_steps(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or not value or len(value) > MAX_TERMINAL_STEPS:
        return None
    result: list[dict[str, str]] = []
    names: set[str] = set()
    for item in value:
        if not _has_exact_keys(item, TERMINAL_STEP_KEYS):
            return None
        name = item.get("name")
        status = item.get("status")
        if (
            not _is_allowed_string(name, TERMINAL_STEP_NAMES)
            or not _is_allowed_string(status, TERMINAL_STEP_STATUSES)
            or name in names
        ):
            return None
        names.add(name)
        result.append({"name": name, "status": status})
    return result


def _strict_legacy_historical_completed_steps(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or len(value) != len(LEGACY_HISTORICAL_COMPLETED_STEP_NAMES):
        return None
    result: list[dict[str, str]] = []
    for item, expected_name in zip(value, LEGACY_HISTORICAL_COMPLETED_STEP_NAMES):
        if (
            not _has_exact_keys(item, TERMINAL_STEP_KEYS)
            or item.get("name") != expected_name
            or item.get("status") != "completed"
        ):
            return None
        result.append({"name": expected_name, "status": "completed"})
    return result


def _strict_terminal_side_effects(value: Any) -> bool:
    return bool(
        _has_exact_keys(value, TERMINAL_SIDE_EFFECT_KEYS)
        and value.get("api_docker_socket") is False
        and value.get("api_shell_execution") is False
        and value.get("request_controlled_source") is False
        and value.get("helper_has_docker_socket") is True
        and value.get("helper_public_ports") is False
    )


def _strict_terminal_release_identity(value: Any) -> bool:
    return bool(
        _has_exact_keys(value, TERMINAL_RELEASE_IDENTITY_KEYS)
        and value.get("host_metadata_status") == "complete"
        and value.get("api_metadata_status") == "complete"
        and value.get("api_visible") is True
        and value.get("commit_verified") is True
    )


def _terminal_shape(payload: dict[str, Any], entry: dict[str, Any]) -> str | None:
    keys = set(payload)
    legacy = _is_exact_int(entry.get("payload", {}).get("schema_version"), LEGACY_REQUEST_SCHEMA_VERSION)
    status_value = payload.get("status")
    if legacy and keys == LEGACY_MINIMAL_TERMINAL_KEYS:
        return "legacy_minimal"
    if legacy and status_value == "completed" and keys == LEGACY_HISTORICAL_COMPLETED_TERMINAL_KEYS:
        return "legacy_historical_completed"
    if keys == PRE_CLOSEOUT_CANCEL_KEYS and status_value == "cancelled":
        return "pre_closeout_cancel"
    common = set(TERMINAL_COMMON_KEYS)
    variants = [common]
    if legacy:
        variants.append(common - {"submission_id"})
    if status_value == "failed":
        allowed = variants + [variant | {"installed_commit"} for variant in variants]
    elif status_value == "completed":
        allowed = [variant | {"installed_commit", "release_identity"} for variant in variants]
    elif status_value == "cancelled":
        allowed = variants
    else:
        return None
    return "helper" if keys in allowed else None


def _terminal_failure_phase_valid(category: str, phase: str | None, current_step: str | None) -> bool:
    allowed = TERMINAL_FAILURE_PHASES.get(category)
    return bool(allowed and phase == current_step and phase in allowed)


def _strict_terminal_snapshot(payload: Any, entry: dict[str, Any]) -> dict[str, Any] | None:
    if (
        not isinstance(payload, dict)
        or _contains_sensitive_content(payload)
        or not _is_exact_int(payload.get("schema_version"), STATUS_SCHEMA_VERSION)
    ):
        return None
    shape = _terminal_shape(payload, entry)
    if shape is None:
        return None
    if _is_exact_int(entry.get("payload", {}).get("schema_version"), REQUEST_SCHEMA_VERSION):
        candidate_contract = _strict_apply_candidate(entry.get("payload", {}).get("apply_candidate"))
        if candidate_contract is None:
            return None
        _candidate, candidate_profile = candidate_contract
        if candidate_profile == APPLY_CANDIDATE_PROFILE_COMPACT_READ_ONLY and shape != "pre_closeout_cancel":
            return None
    status_value = payload.get("status")
    phase = payload.get("phase")
    current_step = payload.get("current_step")
    if (
        not _is_allowed_string(status_value, {"completed", "failed", "cancelled"})
        or not _is_bounded_string(phase, maximum=80)
        or not _is_bounded_string(current_step, maximum=80)
    ):
        return None
    if not _status_identity_matches(payload, entry):
        return None
    started_at = payload.get("started_at")
    updated_at = payload.get("updated_at")
    finished_at = payload.get("updated_at") if shape == "legacy_historical_completed" else payload.get("finished_at")
    started_time = _parse_iso(started_at)
    updated_time = _parse_iso(updated_at)
    finished_time = _parse_iso(finished_at)
    requested_time = _parse_iso(entry.get("requested_at"))
    if (
        not _strict_timestamp(started_at)
        or not _strict_timestamp(updated_at)
        or not _strict_timestamp(finished_at)
        or started_time is None
        or updated_time is None
        or finished_time is None
        or requested_time is None
        or started_time < requested_time
        or updated_time < started_time
        or finished_time < started_time
        or finished_time != updated_time
    ):
        return None
    if shape == "legacy_minimal":
        steps = []
    elif shape == "legacy_historical_completed":
        steps = _strict_legacy_historical_completed_steps(payload.get("steps"))
    else:
        steps = _strict_terminal_steps(payload.get("steps"))
    if steps is None:
        return None
    if shape == "helper" and (
        not _strict_terminal_source(payload.get("source"), entry)
        or not _strict_terminal_side_effects(payload.get("side_effects"))
        or payload.get("can_cancel") is not False
        or payload.get("rollback_supported") is not False
    ):
        return None
    if shape == "legacy_historical_completed" and (
        not _strict_terminal_source(payload.get("source"), entry)
        or not _strict_terminal_side_effects(payload.get("side_effects"))
        or payload.get("can_cancel") is not False
        or payload.get("rollback_supported") is not False
    ):
        return None
    if shape == "pre_closeout_cancel" and (
        _strict_request_source(payload.get("source")) is None
        or _strict_apply_candidate(payload.get("apply_candidate")) is None
        or payload.get("source") != entry.get("payload", {}).get("source")
        or payload.get("apply_candidate") != entry.get("payload", {}).get("apply_candidate")
        or payload.get("can_cancel") is not False
        or payload.get("rollback_supported") is not False
    ):
        return None
    installed_commit = payload.get("installed_commit")
    if installed_commit is not None and (not isinstance(installed_commit, str) or not COMMIT_SHA_RE.fullmatch(installed_commit)):
        return None
    raw_commit_verified = payload.get("commit_verified")
    commit_verified = raw_commit_verified is True
    if status_value == "completed":
        error = None
        if (
            phase != current_step
            or phase not in {"completed", "commit_verification"}
            or not isinstance(installed_commit, str)
            or installed_commit.lower() != entry.get("target_commit")
            or raw_commit_verified is not True
            or payload.get("error") is not None
            or (shape == "helper" and not _strict_terminal_release_identity(payload.get("release_identity")))
        ):
            return None
    elif status_value == "cancelled":
        error = _safe_terminal_error(payload.get("error"))
        if (
            phase != "cancelled"
            or current_step != "cancelled"
            or raw_commit_verified is not False
            or ("installed_commit" in payload and installed_commit is not None)
            or not error
            or error.get("category") != "cancelled_before_start"
        ):
            return None
    else:
        error = _safe_terminal_error(payload.get("error"))
        if (
            raw_commit_verified is not False
            or not error
            or not _terminal_failure_phase_valid(error.get("category"), phase, current_step)
        ):
            return None
    source = _safe_public_source(entry["payload"].get("source"))
    candidate = _safe_public_apply_candidate(entry["payload"].get("apply_candidate"))
    snapshot = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "request_id": entry["request_id"],
        "submission_id": entry["submission_id"],
        "target_version": entry["target_version"],
        "status": status_value,
        "phase": phase,
        "current_step": current_step,
        "started_at": started_at,
        "updated_at": updated_at,
        "finished_at": finished_at,
        "source": source,
        "apply_candidate": candidate,
        "steps": steps,
        "can_cancel": False,
        "rollback_supported": False,
        "expected_commit": entry["target_commit"],
        "installed_commit": installed_commit.lower() if installed_commit else None,
        "commit_verified": commit_verified,
        "error": error,
    }
    rendered = json.dumps(snapshot, ensure_ascii=False)
    if len(rendered.encode("utf-8")) > MAX_CONTROL_BYTES or SENSITIVE_VALUE_RE.search(rendered):
        return None
    return snapshot


def _active_status_matches(payload: Any, entry: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not _is_exact_int(payload.get("schema_version"), STATUS_SCHEMA_VERSION):
        return False
    status_value = _safe_machine_code(payload.get("status"), max_length=40)
    if status_value not in RUNNING_STATUSES or not _status_identity_matches(payload, entry):
        return False
    phase = _safe_machine_code(payload.get("phase"), max_length=80)
    current_step = _safe_machine_code(payload.get("current_step"), max_length=80)
    return bool(phase and current_step and phase not in {"unknown", "idle"} and current_step not in {"unknown", "idle"})


def _status_is_older_than_entry(payload: Any, entry: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return True
    updated = _parse_iso(payload.get("updated_at"))
    boundary = _parse_iso(entry.get("claimed_at") or entry.get("requested_at"))
    return bool(boundary and (updated is None or updated <= boundary))


def _classify_entry_authority(
    entry: dict[str, Any],
    raw_status: dict[str, Any] | None,
    status_state: str,
) -> dict[str, Any]:
    state = entry.get("state")
    if state == "unknown":
        return {"authority": "unknown", "reason": "entry_unknown", "terminal": None}
    if state == "terminal":
        terminal = _strict_terminal_snapshot(entry.get("terminal"), entry)
        return (
            {
                "authority": "authoritative_terminal",
                "reason": "ledger_terminal",
                "terminal": terminal,
                "raw_terminal": entry.get("terminal"),
            }
            if terminal
            else {"authority": "unknown", "reason": "ledger_terminal_invalid", "terminal": None}
        )
    if state == "audit_pending":
        return {"authority": "authoritative_active", "reason": "audit_pending", "terminal": None}
    if state == "admitted_unclaimed":
        if status_state == "missing" or _status_is_older_than_entry(raw_status, entry):
            return {"authority": "authoritative_active", "reason": "admitted_unclaimed", "terminal": None}
        if _active_status_matches(raw_status, entry) and raw_status.get("status") == "queued":
            return {"authority": "authoritative_active", "reason": "admitted_unclaimed", "terminal": None}
        return {"authority": "unknown", "reason": "unclaimed_status_contradictory", "terminal": None}
    if state == "claimed":
        terminal = _strict_terminal_snapshot(raw_status, entry) if status_state == "valid" else None
        if terminal:
            return {
                "authority": "authoritative_terminal",
                "reason": "claimed_terminal_evidence",
                "terminal": terminal,
                "raw_terminal": raw_status,
            }
        if status_state == "missing" or _active_status_matches(raw_status, entry) or _status_is_older_than_entry(raw_status, entry):
            return {"authority": "authoritative_active", "reason": "helper_claimed", "terminal": None}
        return {"authority": "unknown", "reason": "claimed_status_contradictory", "terminal": None}
    return {"authority": "unknown", "reason": "entry_state_invalid", "terminal": None}


def _attach_presentation_context(status: dict[str, Any], presentation: dict[str, Any]) -> dict[str, Any]:
    status["apply_history"] = presentation.get("apply_history")
    status["last_apply_summary"] = presentation.get("last_apply_summary")
    status["release_identity"] = presentation.get("release_identity")
    now = _utcnow()
    started = _parse_iso(status.get("started_at"))
    updated = _parse_iso(status.get("updated_at"))
    status["elapsed_seconds"] = int((now - started).total_seconds()) if started else None
    status["last_progress_age_seconds"] = int((now - updated).total_seconds()) if updated else None
    status["stale_after_seconds"] = STALE_AFTER_SECONDS
    status["is_stale"] = bool(
        status.get("status") in RUNNING_STATUSES
        and status.get("last_progress_age_seconds") is not None
        and status["last_progress_age_seconds"] > STALE_AFTER_SECONDS
    )
    status["effective_status"] = "stalled" if status["is_stale"] else status.get("status")
    status["side_effects"] = {
        "api_docker_socket": False,
        "api_shell_execution": False,
        "request_controlled_source": False,
        "helper_has_docker_socket": True,
        "helper_public_ports": False,
    }
    return status


def _public_status_for_entry(
    entry: dict[str, Any],
    classification: dict[str, Any],
    raw_status: dict[str, Any] | None,
    presentation: dict[str, Any],
) -> dict[str, Any]:
    authority = classification.get("authority")
    if authority == "authoritative_terminal":
        status = dict(classification["terminal"])
        admission_authority = "inactive"
    elif authority == "authoritative_active":
        if entry.get("state") == "claimed" and _active_status_matches(raw_status, entry):
            status = dict(presentation)
        else:
            status = _queued_status_from_request(entry)
            if entry.get("state") == "audit_pending":
                status["phase"] = "audit_pending"
                status["current_step"] = "audit_pending"
                status["can_cancel"] = False
            elif entry.get("state") == "claimed":
                status["status"] = "starting_helper"
                status["phase"] = "starting_helper"
                status["current_step"] = "starting_helper"
                status["can_cancel"] = False
        admission_authority = "active"
    else:
        status = _base_status("blocked", "admission_unknown", request_id=entry.get("request_id"), submission_id=entry.get("submission_id"))
        status["target_version"] = entry.get("target_version")
        status["expected_commit"] = entry.get("target_commit")
        status["error"] = _safe_error("update_admission_unknown", "Update admission state is unavailable.")
        admission_authority = "unknown"
    status["request_id"] = entry.get("request_id")
    status["submission_id"] = entry.get("submission_id")
    status["target_version"] = entry.get("target_version")
    status["expected_commit"] = entry.get("target_commit")
    status["can_cancel"] = authority == "authoritative_active" and entry.get("state") == "admitted_unclaimed"
    status["admission"] = _admission_payload(
        admission_authority,
        classification.get("reason") or "unknown",
        request=entry,
        reason_code=None if admission_authority in {"active", "inactive"} else classification.get("reason"),
    )
    return _attach_presentation_context(status, presentation)


def _legacy_authority(
    legacy: dict[str, Any],
    raw_status: dict[str, Any] | None,
    status_state: str,
) -> dict[str, Any]:
    terminal = _strict_terminal_snapshot(raw_status, legacy) if status_state == "valid" else None
    if terminal:
        return {
            "authority": "authoritative_terminal",
            "reason": "legacy_exact_terminal",
            "terminal": terminal,
            "raw_terminal": raw_status,
        }
    if status_state == "missing" or _active_status_matches(raw_status, legacy) or _status_is_older_than_entry(raw_status, legacy):
        return {"authority": "authoritative_active", "reason": "legacy_request_active", "terminal": None}
    return {"authority": "unknown", "reason": "legacy_control_unknown", "terminal": None}


def _read_update_apply_status_unlocked() -> dict[str, Any]:
    presentation = _read_update_apply_status_without_admission()
    raw_status, status_state = _read_json(_status_path())
    authority = _read_admission_authority_unlocked()
    classification = authority["classification"]
    document = authority["document"]
    if classification == "invalid":
        reason = "lineage_invalid" if authority["marker_state"] == "invalid" else "request_invalid"
        presentation["admission"] = _admission_payload("unknown", reason, reason_code=reason)
        return presentation
    if classification == "lineage_incomplete":
        presentation["admission"] = _admission_payload(
            "unknown",
            "lineage_incomplete",
            reason_code="lineage_incomplete",
        )
        return presentation
    if classification == "missing_unexpected":
        presentation["admission"] = _admission_payload(
            "unknown",
            "admission_missing_unexpected",
            reason_code="admission_missing_unexpected",
        )
        return presentation
    if classification == "legacy" and document:
        classification = _legacy_authority(document, raw_status, status_state)
        return _public_status_for_entry(document, classification, raw_status, presentation)
    if classification == "current" and document and document.get("current"):
        entry = document["current"]
        classification = _classify_entry_authority(entry, raw_status, status_state)
        return _public_status_for_entry(entry, classification, raw_status, presentation)
    status_value = str(presentation.get("status") or "unknown")
    if _lock_path().exists() or status_value in RUNNING_STATUSES:
        presentation["admission"] = _admission_payload("active", "legacy_operation_active")
    elif status_value in {"idle", "completed", "failed", "cancelled"}:
        presentation["admission"] = _admission_payload("inactive", "no_active_admission")
    else:
        presentation["admission"] = _admission_payload("unknown", "status_non_authoritative", reason_code="status_non_authoritative")
    return presentation


def read_update_apply_status() -> dict[str, Any]:
    with _admission_guard():
        return _read_update_apply_status_unlocked()


def _document_copy(document: dict[str, Any] | None) -> dict[str, Any]:
    if not document:
        return _empty_admission_document()
    return json.loads(json.dumps(document["payload"], ensure_ascii=False))


def _replace_raw_entry(payload: dict[str, Any], replacement: dict[str, Any]) -> None:
    submission_id = replacement["submission_id"]
    for index, entry in enumerate(payload.get("entries") or []):
        if entry.get("submission_id") == submission_id:
            payload["entries"][index] = replacement
            payload["updated_at"] = _iso()
            return
    raise UpdateApplyBlocked("update_admission_unknown", "Update admission entry is unavailable.")


def _validated_document_for_write(payload: dict[str, Any]) -> dict[str, Any]:
    payload["updated_at"] = _iso()
    contract, document = _admission_document_contract(payload, "valid")
    if contract != "current" or not document:
        raise UpdateApplyBlocked("update_admission_unknown", "Update admission document is invalid.")
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(rendered.encode("utf-8")) > MAX_ADMISSION_BYTES:
        raise UpdateApplyBlocked(
            "submission_ledger_capacity",
            "Update submission history is full.",
            diagnostics={"retry_allowed": False, "next_action": "wait_for_retention"},
        )
    return document


def _write_admission_document(payload: dict[str, Any]) -> dict[str, Any]:
    if _read_lineage_marker_unlocked() != "valid":
        raise UpdateApplyBlocked("update_lineage_unavailable", "Update admission lineage is unavailable.")
    _validated_document_for_write(payload)
    _atomic_write_json(_request_path(), payload)
    contract, document = _admission_document_contract(payload, "valid")
    if contract != "current" or not document:
        raise UpdateApplyBlocked("update_admission_unknown", "Update admission document is invalid.")
    return document


def _prune_eligible_terminal_entries(payload: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    contract, document = _admission_document_contract(payload, "valid")
    if contract != "current" or not document:
        raise UpdateApplyBlocked("update_admission_unknown", "Update admission document is invalid.")
    cutoff = now - timedelta(days=TERMINAL_RETENTION_DAYS)
    retained: list[dict[str, Any]] = []
    removed: set[str] = set()
    for raw in payload.get("entries") or []:
        terminal = raw.get("terminal") if isinstance(raw.get("terminal"), dict) else {}
        finished = _parse_iso(terminal.get("finished_at") or terminal.get("updated_at"))
        if raw.get("state") == "terminal" and finished and finished < cutoff:
            removed.add(str(raw.get("submission_id") or ""))
        else:
            retained.append(raw)
    payload["entries"] = retained
    if payload.get("current_submission_id") in removed:
        payload["current_submission_id"] = None
    contract, document = _admission_document_contract(payload, "valid")
    if contract != "current" or not document:
        raise UpdateApplyBlocked("update_admission_unknown", "Update admission pruning produced invalid topology.")
    return payload


def _ensure_new_entry_capacity(payload: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    candidate = _prune_eligible_terminal_entries(json.loads(json.dumps(payload)), now=_utcnow())
    if len(candidate.get("entries") or []) >= MAX_ADMISSION_ENTRIES:
        raise UpdateApplyBlocked(
            "submission_ledger_capacity",
            "Update submission history is full.",
            diagnostics={"retry_allowed": False, "next_action": "wait_for_retention"},
        )
    candidate.setdefault("entries", []).append(entry)
    candidate["current_submission_id"] = entry["submission_id"]
    _validated_document_for_write(candidate)
    return candidate


def _ticket_capacity_available(document: dict[str, Any] | None) -> bool:
    if not document:
        return True
    candidate = _prune_eligible_terminal_entries(_document_copy(document), now=_utcnow())
    return len(candidate.get("entries") or []) < MAX_ADMISSION_ENTRIES and len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) < MAX_ADMISSION_BYTES - 8192


def _audit_event_id(request_id: str) -> str:
    return str(uuid.uuid5(AUDIT_NAMESPACE, request_id))


def _audit_event_matches(event: AuditEvent | None, entry: dict[str, Any]) -> bool:
    if not event:
        return False
    actor = entry["payload"].get("requested_by") or {}
    expected_event_id = _audit_event_id(entry["request_id"])
    return (
        entry["audit"].get("event_id") == expected_event_id
        and event.id == expected_event_id
        and event.event_type == AUDIT_EVENT_TYPE
        and event.target_type == AUDIT_TARGET_TYPE
        and event.target_id == entry["request_id"]
        and event.actor_user_id == actor.get("user_id")
        and event.actor_username == actor.get("username")
        and event.actor_role == actor.get("role")
    )


def _ensure_deterministic_accepted_audit(session_factory: Any, entry: dict[str, Any]) -> None:
    event_id = _audit_event_id(entry["request_id"])
    if entry["audit"].get("event_id") != event_id:
        raise UpdateApplyBlocked("accepted_audit_corrupt", "Accepted update audit identity is contradictory.")
    requested_by = entry["payload"].get("requested_by") or {}
    db = session_factory()
    try:
        existing = db.get(AuditEvent, event_id)
        if existing:
            if not _audit_event_matches(existing, entry):
                raise UpdateApplyBlocked("accepted_audit_corrupt", "Accepted update audit identity is contradictory.")
            db.rollback()
            return
        db.add(
            AuditEvent(
                id=event_id,
                actor_user_id=requested_by.get("user_id"),
                actor_username=requested_by.get("username"),
                actor_role=requested_by.get("role"),
                category="system",
                event_type=AUDIT_EVENT_TYPE,
                severity="warning",
                message_ru="Product update apply request was queued for helper execution.",
                message_en="Product update apply request was queued for helper execution.",
                target_type=AUDIT_TARGET_TYPE,
                target_id=entry["request_id"],
                event_metadata={"request_id": entry["request_id"], "api_docker_socket": False, "api_shell_execution": False},
                ip_address=requested_by.get("ip_address"),
                user_agent=requested_by.get("user_agent"),
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.get(AuditEvent, event_id)
            if not _audit_event_matches(existing, entry):
                raise UpdateApplyBlocked("accepted_audit_corrupt", "Accepted update audit identity is contradictory.")
    except UpdateApplyBlocked:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise UpdateApplyBlocked(
            "accepted_audit_unavailable",
            "The update request is reserved while audit confirmation recovers.",
            diagnostics={"retry_allowed": True, "next_action": "wait_for_status"},
        ) from exc
    finally:
        db.close()


def _confirm_pending_audit_locked(
    document: dict[str, Any],
    entry: dict[str, Any],
    *,
    session_factory: Any,
) -> dict[str, Any]:
    if entry.get("state") != "audit_pending":
        return document
    _ensure_deterministic_accepted_audit(session_factory, entry)
    payload = _document_copy(document)
    raw = json.loads(json.dumps(entry["entry"]))
    raw["audit"]["state"] = "confirmed"
    raw["audit"]["confirmed_at"] = _iso()
    raw["state"] = "admitted_unclaimed"
    raw["updated_at"] = _iso()
    _replace_raw_entry(payload, raw)
    return _write_admission_document(payload)


def reconcile_update_apply_audit_once(*, session_factory: Any | None = None) -> dict[str, Any]:
    factory = session_factory or SessionLocal
    with _admission_guard():
        authority = _read_admission_authority_unlocked()
        if authority["classification"] != "current" or not authority["document"]:
            return {"status": authority["classification"], "repaired": False}
        document = authority["document"]
        pending = [entry for entry in document["entries"] if entry.get("state") == "audit_pending"]
        if not pending:
            return {"status": "idle", "repaired": False}
        entry = sorted(pending, key=lambda item: (item.get("requested_at") or "", item.get("submission_id") or ""))[0]
        confirmed = _confirm_pending_audit_locked(document, entry, session_factory=factory)
        return {"status": "confirmed", "repaired": True, "submission_id": entry["submission_id"], "document": confirmed}


def _audit_coordinator_loop() -> None:
    while not _AUDIT_COORDINATOR_STOP.is_set():
        try:
            reconcile_update_apply_audit_once()
        except Exception:
            pass
        _AUDIT_COORDINATOR_STOP.wait(5.0)


def start_update_apply_audit_coordinator() -> None:
    global _AUDIT_COORDINATOR_THREAD
    if _AUDIT_COORDINATOR_THREAD and _AUDIT_COORDINATOR_THREAD.is_alive():
        return
    _AUDIT_COORDINATOR_STOP.clear()
    _AUDIT_COORDINATOR_THREAD = threading.Thread(target=_audit_coordinator_loop, name="update-apply-audit", daemon=True)
    _AUDIT_COORDINATOR_THREAD.start()


def stop_update_apply_audit_coordinator() -> None:
    global _AUDIT_COORDINATOR_THREAD
    _AUDIT_COORDINATOR_STOP.set()
    thread = _AUDIT_COORDINATOR_THREAD
    if thread:
        thread.join(timeout=5)
    _AUDIT_COORDINATOR_THREAD = None


def _session_factory_from_request_db(db: Session) -> Any:
    bind = db.get_bind()
    db.rollback()
    return sessionmaker(bind=bind, autoflush=False, autocommit=False)


def _canonical_apply_response(entry: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
    raw_status, status_state = _read_json(_status_path())
    presentation = _read_update_apply_status_without_admission()
    classification = _classify_entry_authority(entry, raw_status, status_state)
    status_payload = _public_status_for_entry(entry, classification, raw_status, presentation)
    return {
        "accepted": True,
        "status": status_payload.get("status"),
        "submission_id": entry.get("submission_id"),
        "request_id": entry.get("request_id"),
        "replayed": replayed,
        "apply_status": status_payload,
        "can_cancel": bool(status_payload.get("can_cancel")),
    }


def _existing_entry_response(
    document: dict[str, Any] | None,
    *,
    submission_id: str,
    target_version: str,
    target_commit: str,
    actor_id: int,
) -> dict[str, Any] | None:
    if not document:
        return None
    entry = document["by_submission"].get(submission_id)
    if not entry:
        return None
    requested_by = entry["payload"].get("requested_by") or {}
    if requested_by.get("user_id") != actor_id:
        raise UpdateApplyBlocked("submission_proof_invalid", "Update confirmation proof is invalid.")
    if entry.get("target_version") != target_version or entry.get("target_commit") != target_commit:
        raise UpdateApplyBlocked(
            "submission_target_mismatch",
            "This update confirmation is bound to a different release target.",
            diagnostics={"retry_allowed": False, "next_action": "refresh_status"},
        )
    return _canonical_apply_response(entry, replayed=True)


def _legacy_can_retire(legacy: dict[str, Any]) -> bool:
    raw_status, status_state = _read_json(_status_path())
    return _legacy_authority(legacy, raw_status, status_state).get("authority") == "authoritative_terminal" and not _lock_path().exists()


def _admission_gate(document: dict[str, Any] | None) -> dict[str, Any]:
    if _lock_path().exists():
        raise UpdateApplyBlocked(
            "update_already_running",
            "Another update apply is already running.",
            diagnostics={"retry_allowed": True, "next_action": "wait_for_status"},
        )
    if not document or not document.get("current"):
        return _empty_admission_document() if not document else _document_copy(document)
    raw_status, status_state = _read_json(_status_path())
    current = document["current"]
    classification = _classify_entry_authority(current, raw_status, status_state)
    if classification.get("authority") == "authoritative_terminal":
        if current.get("state") != "terminal":
            payload = _document_copy(document)
            raw = json.loads(json.dumps(current["entry"]))
            raw["state"] = "terminal"
            raw_terminal = classification.get("raw_terminal")
            if not isinstance(raw_terminal, dict):
                raise UpdateApplyBlocked("update_admission_unknown", "Terminal update evidence is unavailable.")
            raw["terminal"] = json.loads(json.dumps(raw_terminal))
            raw["updated_at"] = _iso()
            _replace_raw_entry(payload, raw)
            document = _write_admission_document(payload)
        return _document_copy(document)
    if classification.get("authority") == "authoritative_active":
        raise UpdateApplyBlocked(
            "update_already_running",
            "Another update apply is already admitted or running.",
            diagnostics={"retry_allowed": True, "next_action": "wait_for_status"},
        )
    raise UpdateApplyBlocked(
        "update_admission_unknown",
        "Update admission state is unavailable. Refresh status before retrying.",
        diagnostics={"retry_allowed": True, "next_action": "refresh_status"},
    )


def _assert_ticket_gate(document: dict[str, Any] | None) -> None:
    if _lock_path().exists():
        raise UpdateApplyBlocked(
            "update_already_running",
            "Another update apply is already running.",
            diagnostics={"retry_allowed": True, "next_action": "wait_for_status"},
        )
    if not document or not document.get("current"):
        return
    raw_status, status_state = _read_json(_status_path())
    classification = _classify_entry_authority(document["current"], raw_status, status_state)
    if classification.get("authority") == "authoritative_terminal":
        return
    if classification.get("authority") == "authoritative_active":
        raise UpdateApplyBlocked(
            "update_already_running",
            "Another update apply is already admitted or running.",
            diagnostics={"retry_allowed": True, "next_action": "wait_for_status"},
        )
    raise UpdateApplyBlocked(
        "update_admission_unknown",
        "Update admission state is unavailable. Refresh status before retrying.",
        diagnostics={"retry_allowed": True, "next_action": "refresh_status"},
    )


def issue_update_apply_submission_ticket(
    db: Session,
    *,
    expected_manifest_version: str | None,
    expected_manifest_commit: str | None,
    actor: Any,
) -> dict[str, Any]:
    if not settings.kmvms_update_helper_enabled:
        raise UpdateApplyBlocked("helper_not_configured", "Update helper service is not enabled for this installation.")
    target_version = _normalized_target_version(expected_manifest_version)
    target_commit = _normalized_target_commit(expected_manifest_commit)
    actor_snapshot = _actor_snapshot(actor, ip_address=None, user_agent=None)
    db.rollback()
    with _admission_guard():
        authority = _read_admission_authority_unlocked()
        classification = authority["classification"]
        document = authority["document"]
        if classification in {"invalid", "lineage_incomplete", "missing_unexpected"}:
            raise UpdateApplyBlocked("update_admission_unknown", "Update admission state is unavailable.")
        if classification == "legacy":
            if not document or not _legacy_can_retire(document):
                raise UpdateApplyBlocked("update_already_running", "Another update apply is active or unresolved.")
            capacity_document = None
        elif classification == "current":
            _assert_ticket_gate(document)
            capacity_document = document
        elif classification == "pristine":
            capacity_document = None
        else:
            raise UpdateApplyBlocked("update_admission_unknown", "Update admission state is unavailable.")
        if not _ticket_capacity_available(capacity_document):
            raise UpdateApplyBlocked(
                "submission_ledger_capacity",
                "Update submission history is full.",
                diagnostics={"retry_allowed": False, "next_action": "wait_for_retention"},
            )
    latest, _candidate = _select_apply_candidate(db, expected_version=target_version, expected_commit=target_commit)
    _check_token_precondition()
    _validate_expected(latest, expected_version=target_version, expected_commit=target_commit)
    submission_id = str(uuid.uuid4())
    issued_at = _utcnow()
    proof, claims = _issue_submission_proof(
        submission_id=submission_id,
        target_version=target_version,
        target_commit=target_commit,
        actor_id=actor_snapshot["user_id"],
        issued_at=issued_at,
    )
    return {
        "schema_version": 1,
        "submission_id": submission_id,
        "submission_proof": proof,
        "target_version": target_version,
        "target_commit": target_commit,
        "issued_at": _iso(datetime.fromtimestamp(claims["iat"], tz=timezone.utc).replace(tzinfo=None)),
        "expires_at": _iso(datetime.fromtimestamp(claims["exp"], tz=timezone.utc).replace(tzinfo=None)),
    }


def request_update_apply(
    db: Session,
    *,
    confirm: bool,
    submission_id: str | None,
    submission_proof: str | None,
    expected_manifest_version: str | None,
    expected_manifest_commit: str | None,
    actor: Any,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise UpdateApplyBlocked("confirmation_required", "Explicit confirmation is required before update apply.")
    if not settings.kmvms_update_helper_enabled:
        raise UpdateApplyBlocked("helper_not_configured", "Update helper service is not enabled for this installation.")
    normalized_submission_id = _normalized_submission_id(submission_id)
    target_version = _normalized_target_version(expected_manifest_version)
    target_commit = _normalized_target_commit(expected_manifest_commit)
    actor_snapshot = _actor_snapshot(actor, ip_address=ip_address, user_agent=user_agent)
    session_factory = _session_factory_from_request_db(db)
    proof_state, proof_claims = verify_update_apply_submission_proof(
        submission_proof,
        actor_id=actor_snapshot["user_id"],
        submission_id=normalized_submission_id,
    )
    if proof_state == "invalid" or not proof_claims:
        raise UpdateApplyBlocked("submission_proof_invalid", "Update confirmation proof is invalid.")
    if proof_claims["target_version"] != target_version or proof_claims["target_commit"] != target_commit:
        raise UpdateApplyBlocked(
            "submission_target_mismatch",
            "This update confirmation is bound to a different release target.",
            diagnostics={"retry_allowed": False, "next_action": "refresh_status"},
        )

    with _admission_guard():
        authority = _read_admission_authority_unlocked()
        classification = authority["classification"]
        document = authority["document"]
        if classification in {"invalid", "lineage_incomplete", "missing_unexpected"}:
            raise UpdateApplyBlocked("update_admission_unknown", "Update admission state is unavailable.")
        replay = _existing_entry_response(
            document if classification == "current" else None,
            submission_id=normalized_submission_id,
            target_version=target_version,
            target_commit=target_commit,
            actor_id=actor_snapshot["user_id"],
        )
        if replay:
            return replay
        if proof_state == "valid_expired":
            raise UpdateApplyBlocked(
                "submission_expired",
                "This update confirmation expired. Confirm the update again.",
                diagnostics={"retry_allowed": True, "next_action": "confirm_apply"},
            )

    latest, apply_candidate = _select_apply_candidate(db, expected_version=target_version, expected_commit=target_commit)
    _check_token_precondition()
    source = _sanitized_source(latest)
    if not source["repo"] or not source["ref"] or not source["source_type"] or not source["commit"] or not source["apply_ref"]:
        raise UpdateApplyBlocked("trusted_source_incomplete", "Trusted release source is incomplete.")
    db.rollback()

    with _admission_guard():
        authority = _read_admission_authority_unlocked()
        classification = authority["classification"]
        document = authority["document"]
        if classification in {"invalid", "lineage_incomplete", "missing_unexpected"}:
            raise UpdateApplyBlocked("update_admission_unknown", "Update admission state is unavailable.")
        replay = _existing_entry_response(
            document if classification == "current" else None,
            submission_id=normalized_submission_id,
            target_version=target_version,
            target_commit=target_commit,
            actor_id=actor_snapshot["user_id"],
        )
        if replay:
            return replay
        if classification == "legacy":
            if not document or not _legacy_can_retire(document):
                raise UpdateApplyBlocked("update_admission_unknown", "Legacy update admission is not safely terminal.")
            payload = _empty_admission_document()
        elif classification == "current":
            payload = _admission_gate(document)
        elif classification == "pristine":
            payload = _empty_admission_document()
        else:
            raise UpdateApplyBlocked("update_admission_unknown", "Update admission state is unavailable.")
        now = _iso()
        request_id = "update-" + uuid.uuid4().hex
        request = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "request_id": request_id,
            "submission_id": normalized_submission_id,
            "requested_at": now,
            "requested_by": actor_snapshot,
            "intent": "apply_update",
            "source": source,
            "apply_candidate": apply_candidate,
            "confirmed": True,
            "preflight_required": True,
            "status_path": "data/update-control/update-status.json",
        }
        entry = {
            "submission_id": normalized_submission_id,
            "request_id": request_id,
            "target_version": target_version,
            "target_commit": target_commit,
            "requested_at": now,
            "updated_at": now,
            "state": "audit_pending",
            "request": request,
            "audit": {"state": "pending", "event_id": _audit_event_id(request_id), "confirmed_at": None},
            "claimed_at": None,
            "terminal": None,
        }
        payload = _ensure_new_entry_capacity(payload, entry)
        if authority["marker_state"] == "missing":
            _write_lineage_marker_locked()
        document = _write_admission_document(payload)
        reserved = document["by_submission"][normalized_submission_id]
        document = _confirm_pending_audit_locked(document, reserved, session_factory=session_factory)
        admitted = document["by_submission"][normalized_submission_id]
        return _canonical_apply_response(admitted, replayed=False)


def read_update_apply_reconciliation(
    *,
    submission_id: str,
    submission_proof: str | None,
    actor_id: int,
) -> dict[str, Any]:
    normalized_submission_id = _normalized_submission_id(submission_id)
    if not isinstance(actor_id, int) or actor_id <= 0:
        raise UpdateApplyBlocked("submission_proof_invalid", "Update confirmation proof is invalid.")
    proof_state, claims = verify_update_apply_submission_proof(
        submission_proof,
        actor_id=actor_id,
        submission_id=normalized_submission_id,
    )
    if proof_state == "invalid" or not claims:
        raise UpdateApplyBlocked("submission_proof_invalid", "Update confirmation proof is invalid.")
    with _admission_guard():
        authority = _read_admission_authority_unlocked()
        classification = authority["classification"]
        document = authority["document"]
        if classification in {"invalid", "lineage_incomplete", "missing_unexpected"}:
            raise UpdateApplyBlocked("update_admission_unknown", "Update admission state is unavailable.")
        if classification == "current" and document:
            entry = document["by_submission"].get(normalized_submission_id)
            if entry:
                response = _existing_entry_response(
                    document,
                    submission_id=normalized_submission_id,
                    target_version=claims["target_version"],
                    target_commit=claims["target_commit"],
                    actor_id=actor_id,
                )
                return {
                    "schema_version": 1,
                    "found": True,
                    "status": "found",
                    "submission_id": normalized_submission_id,
                    "request_id": response.get("request_id") if response else None,
                    "apply_status": response.get("apply_status") if response else None,
                    "replayed": True,
                }
    absence_status = "submission_expired" if proof_state == "valid_expired" else "not_found"
    return {
        "schema_version": 1,
        "found": False,
        "status": absence_status,
        "submission_id": normalized_submission_id,
        "request_id": None,
        "apply_status": None,
        "replayed": False,
    }


def cancel_update_apply() -> dict[str, Any]:
    with _admission_guard():
        authority = _read_admission_authority_unlocked()
        classification = authority["classification"]
        document = authority["document"]
        if classification in {"invalid", "lineage_incomplete", "missing_unexpected"}:
            raise UpdateApplyBlocked("update_admission_unknown", "Update admission state is unavailable.")
        if classification != "current" or not document or not document.get("current"):
            return {"status": "not_cancelable", "submission_id": None, "request_id": None, "can_cancel": False, "reason": "No queued update apply is available."}
        entry = document["current"]
        raw_status, status_state = _read_json(_status_path())
        entry_authority = _classify_entry_authority(entry, raw_status, status_state)
        if entry.get("state") != "admitted_unclaimed" or entry_authority.get("authority") != "authoritative_active":
            return {
                "status": "not_cancelable",
                "submission_id": entry.get("submission_id"),
                "request_id": entry.get("request_id"),
                "can_cancel": False,
                "reason": "Update apply can only be cancelled before helper claim.",
            }
        now = _iso()
        cancelled = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "request_id": entry["request_id"],
            "submission_id": entry["submission_id"],
            "target_version": entry["target_version"],
            "status": "cancelled",
            "phase": "cancelled",
            "current_step": "cancelled",
            "started_at": entry["requested_at"],
            "updated_at": now,
            "finished_at": now,
            "source": {
                "kind": "github-tarball",
                "repo": entry["payload"]["source"]["repo"],
                "ref": entry["payload"]["source"]["ref"],
                "commit": entry["target_commit"],
                "apply_ref": entry["target_commit"],
            },
            "steps": [{"name": "request", "status": "completed"}],
            "can_cancel": False,
            "rollback_supported": False,
            "side_effects": {
                "api_docker_socket": False,
                "api_shell_execution": False,
                "request_controlled_source": False,
                "helper_has_docker_socket": True,
                "helper_public_ports": False,
            },
            "expected_commit": entry["target_commit"],
            "commit_verified": False,
            "error": _safe_error("cancelled_before_start", "Queued update apply was cancelled before helper started.", "No update was applied."),
        }
        if not _strict_terminal_snapshot(cancelled, entry):
            raise UpdateApplyBlocked("update_admission_unknown", "Cancellation terminal truth is invalid.")
        payload = _document_copy(document)
        raw = json.loads(json.dumps(entry["entry"]))
        raw["state"] = "terminal"
        raw["terminal"] = cancelled
        raw["updated_at"] = now
        _replace_raw_entry(payload, raw)
        _write_admission_document(payload)
        _atomic_write_json(_status_path(), cancelled)
        return {
            "status": "cancelled",
            "submission_id": entry.get("submission_id"),
            "request_id": entry.get("request_id"),
            "can_cancel": False,
        }


def reject_forbidden_apply_fields(payload: dict[str, Any]) -> None:
    for key in payload:
        if key in FORBIDDEN_REQUEST_FIELDS or any(token in key.lower() for token in ("token", "secret", "command", "url", "path")):
            raise UpdateApplyBlocked("forbidden_request_field", f"Forbidden update apply request field: {key}")
        value = payload[key]
        if isinstance(value, str) and (SENSITIVE_VALUE_RE.search(value) or not SAFE_TEXT_RE.fullmatch(value)):
            raise UpdateApplyBlocked("unsafe_request_value", f"Unsafe update apply request value for field: {key}")
