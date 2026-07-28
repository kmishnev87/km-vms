from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_event import AuditEvent
from app.models.schema_migration_control import (
    SchemaMigrationAttempt,
    SchemaMigrationControl,
)
from app.services.update_check import (
    get_trusted_apply_snapshot,
    read_installed_update_state,
    run_update_check,
    trusted_apply_snapshot_status,
)

REQUEST_SCHEMA_VERSION = 3
STATUS_SCHEMA_VERSION = 1
REQUEST_DOCUMENT_TYPE = "update_apply_request"
MAX_CONTROL_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_HISTORY_ITEMS = 1
ADMISSION_CLAIM_TIMEOUT_SECONDS = 10 * 60
STALE_AFTER_SECONDS = 180

AUDIT_NAMESPACE = uuid.UUID("abf15e22-71b8-5af5-b9ee-ef808127c780")
AUDIT_EVENT_TYPE = "system.update_apply_requested"
AUDIT_TARGET_TYPE = "update_apply"

ACTIVE_REQUEST_STATES = {"admitted", "claimed"}
REQUEST_STATES = ACTIVE_REQUEST_STATES | {"terminal"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked"}
RUNNING_STATUSES = {
    "queued",
    "starting_helper",
    "preflight",
    "acquire_source",
    "downloading",
    "extracting",
    "validating_source",
    "overlay",
    "applying",
    "compose_config",
    "rebuilding",
    "restarting",
    "health_check",
    "commit_verification",
}
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

MACHINE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,119}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
REQUEST_ID_RE = re.compile(r"^update-[0-9a-f]{32}$", re.IGNORECASE)
LEGACY_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,79}$")
SUBMISSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
SENSITIVE_VALUE_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9_]+|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]+|rtsp://[^@\s]+@|"
    r"postgresql://[^:\s]+:[^@\s]+@|"
    r"-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)

CURRENT_REQUEST_KEYS = {
    "schema_version",
    "document_type",
    "request_id",
    "submission_id",
    "requested_at",
    "updated_at",
    "requested_by",
    "intent",
    "source",
    "apply_candidate",
    "confirmed",
    "preflight_required",
    "status_path",
    "state",
    "claimed_at",
    "terminal",
    "audit_event_id",
}
CURRENT_ACTOR_KEYS = {
    "user_id",
    "username",
    "role",
    "ip_address",
    "user_agent",
}
REQUEST_SOURCE_KEYS = {
    "kind",
    "channel",
    "version",
    "commit",
    "apply_ref",
    "ref",
    "repo",
    "source_type",
}
APPLY_CANDIDATE_KEYS = {"source", "snapshot"}
APPLY_FRESHNESS_KEYS = {
    "available",
    "fresh",
    "age_seconds",
    "fresh_for_seconds",
    "version",
    "commit_short",
    "provider",
}
TERMINAL_SUMMARY_KEYS = {"status", "finished_at", "error_category"}

_ADMISSION_THREAD_LOCK = threading.RLock()


class UpdateApplyBlocked(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
    ):
        self.code = code
        self.diagnostics = diagnostics or {}
        super().__init__(message)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).replace(tzinfo=timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
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


def _admission_lock_path() -> Path:
    return _control_root() / "update-admission.lock"


def _helper_lease_path() -> Path:
    return _control_root() / "update-helper-claim.lock"


@contextmanager
def _admission_guard():
    root = _control_root()
    root.mkdir(parents=True, exist_ok=True)
    with _ADMISSION_THREAD_LOCK:
        with _admission_lock_path().open("a+", encoding="utf-8") as lock_file:
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


def _safe_machine_code(value: Any, *, max_length: int = 80) -> str | None:
    text = _safe_string(value, max_length=max_length)
    if not text or not MACHINE_CODE_RE.fullmatch(text):
        return None
    return text


def _safe_timestamp(value: Any) -> str | None:
    text = _safe_string(value, max_length=80)
    return text if text and _parse_iso(text) is not None else None


def _contains_sensitive_content(value: Any) -> bool:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return True
    return bool(SENSITIVE_VALUE_RE.search(rendered))


def _safe_error(
    code: str,
    message: str,
    action: str = "Review update status before retrying.",
) -> dict[str, str]:
    return {
        "category": _safe_machine_code(code, max_length=80) or "update_apply_error",
        "message": _safe_string(message, max_length=300)
        or "Update apply is unavailable.",
        "operator_action": _safe_string(action, max_length=300)
        or "Review update status.",
    }


def _public_error_for_category(category: str) -> dict[str, str]:
    messages = {
        "cancelled_before_start": "Queued update apply was cancelled before helper started.",
        "helper_not_claimed": "Update helper did not claim the queued request in time.",
        "helper_restart_interrupted": "Update execution was interrupted before completion.",
        "helper_host_app_dir_missing": "Update helper application directory is not configured.",
        "helper_host_app_dir_invalid": "Update helper application directory configuration is invalid.",
        "helper_host_app_dir_unmounted": "Update helper application directory is unavailable.",
        "preflight_failed": "Update preflight failed.",
        "compose_config_failed": "Docker Compose configuration validation failed.",
        "jellyfin_ffmpeg_repo_unavailable": "The external FFmpeg repository was unavailable during the update build.",
        "build_network_dependency_failed": "A required network dependency was unavailable during the update build.",
        "docker_build_failed": "Docker image rebuild failed during update apply.",
        "schema_update_failed": "Database schema preparation failed during update apply.",
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
        "helper_not_claimed": "Verify that update-helper is running, then retry.",
        "helper_restart_interrupted": "Refresh status and retry after confirming no update is active.",
        "compose_config_failed": "Review the server Compose configuration before retrying.",
        "jellyfin_ffmpeg_repo_unavailable": "Restore repository connectivity, then retry.",
        "build_network_dependency_failed": "Restore network connectivity, then retry.",
        "docker_build_failed": "Review the sanitized build status before retrying.",
        "schema_update_failed": "Review the schema failure before retrying.",
        "health_check_failed": "Review service health before retrying.",
        "commit_mismatch": "Verify the trusted release source before retrying.",
        "commit_missing": "Verify installed release identity before retrying.",
        "metadata_invalid": "Repair installed release identity before retrying.",
        "apply_timeout": "Refresh status before deciding whether to retry.",
    }
    return {
        "category": category,
        "message": messages.get(category, "Update apply is unavailable."),
        "operator_action": actions.get(
            category,
            "Review update status and retry only after the cause is resolved.",
        ),
    }


def _safe_public_error(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    category = _safe_machine_code(value.get("category"), max_length=80)
    return _public_error_for_category(category or "update_apply_error")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if SENSITIVE_VALUE_RE.search(rendered):
        raise UpdateApplyBlocked(
            "control_payload_sensitive",
            "Update control payload contains sensitive content.",
        )
    limit = MAX_REQUEST_BYTES if path == _request_path() else MAX_CONTROL_BYTES
    if len(rendered.encode("utf-8")) > limit:
        raise UpdateApplyBlocked(
            "control_payload_too_large",
            "Update control payload is too large.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateApplyBlocked(
            "control_write_failed",
            "Update request could not be persisted.",
        ) from exc


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    limit = MAX_REQUEST_BYTES if path == _request_path() else MAX_CONTROL_BYTES
    try:
        if not path.exists():
            return None, "missing"
        if not path.is_file() or path.stat().st_size > limit:
            return None, "invalid_file"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError, OSError, RecursionError, ValueError):
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "invalid_shape"
    if _contains_sensitive_content(payload):
        return None, "sensitive_content"
    return payload, "valid"


def _normalized_submission_id(value: Any) -> str:
    submission_id = _safe_string(value, max_length=80)
    if not submission_id:
        raise UpdateApplyBlocked(
            "submission_id_required",
            "A client idempotency identifier is required.",
        )
    submission_id = submission_id.lower()
    if not SUBMISSION_ID_RE.fullmatch(submission_id):
        raise UpdateApplyBlocked(
            "submission_id_invalid",
            "The client idempotency identifier is invalid.",
        )
    return submission_id


def _normalized_target_version(value: Any) -> str:
    version = _safe_string(value, max_length=80)
    if not version or not VERSION_TEXT_RE.fullmatch(version):
        raise UpdateApplyBlocked(
            "update_check_required",
            "Run Check update again before applying this release.",
        )
    return version


def _normalized_target_commit(value: Any) -> str:
    commit = (_safe_string(value, max_length=40) or "").lower()
    if not COMMIT_SHA_RE.fullmatch(commit):
        raise UpdateApplyBlocked(
            "update_check_required",
            "Run Check update again before applying this release.",
        )
    return commit


def _actor_snapshot(
    actor: Any,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    user_id = getattr(actor, "id", None)
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1:
        raise UpdateApplyBlocked(
            "actor_identity_invalid",
            "Authenticated update actor identity is invalid.",
        )
    username = _safe_string(getattr(actor, "username", None), max_length=150)
    role = _safe_machine_code(getattr(actor, "role", None), max_length=50)
    if not username or not role:
        raise UpdateApplyBlocked(
            "actor_identity_invalid",
            "Authenticated update actor identity is invalid.",
        )
    return {
        "user_id": user_id,
        "username": username,
        "role": role,
        "ip_address": _safe_string(ip_address, max_length=80),
        "user_agent": _safe_string(user_agent, max_length=300),
    }


def _strict_source(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != REQUEST_SOURCE_KEYS:
        return None
    if value.get("kind") != "trusted_manifest":
        return None
    if value.get("source_type") != "github_tarball":
        return None
    channel = value.get("channel")
    version = value.get("version")
    commit = value.get("commit")
    apply_ref = value.get("apply_ref")
    ref = value.get("ref")
    repo = value.get("repo")
    if (
        not isinstance(channel, str)
        or not MACHINE_CODE_RE.fullmatch(channel)
        or not isinstance(version, str)
        or not VERSION_TEXT_RE.fullmatch(version)
        or not isinstance(commit, str)
        or not COMMIT_SHA_RE.fullmatch(commit)
        or not isinstance(apply_ref, str)
        or apply_ref.lower() != commit.lower()
        or not isinstance(ref, str)
        or not GIT_REF_RE.fullmatch(ref)
        or ".." in ref
        or "@{" in ref
        or not isinstance(repo, str)
        or not GITHUB_REPO_RE.fullmatch(repo)
    ):
        return None
    return {
        **value,
        "commit": commit.lower(),
        "apply_ref": apply_ref.lower(),
    }


def _strict_candidate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != APPLY_CANDIDATE_KEYS:
        return None
    if value.get("source") not in {"trusted_snapshot", "live_check"}:
        return None
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != APPLY_FRESHNESS_KEYS:
        return None
    if any(
        key in snapshot
        and snapshot[key] is not None
        and type(snapshot[key]) is not expected_type
        for key, expected_type in {
            "available": bool,
            "fresh": bool,
            "age_seconds": int,
            "fresh_for_seconds": int,
            "version": str,
            "commit_short": str,
            "provider": str,
        }.items()
    ):
        return None
    if (
        snapshot.get("age_seconds") is not None
        and snapshot["age_seconds"] < 0
    ) or (
        snapshot.get("fresh_for_seconds") is not None
        and snapshot["fresh_for_seconds"] < 0
    ):
        return None
    return json.loads(json.dumps(value))


def _strict_actor(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != CURRENT_ACTOR_KEYS:
        return None
    if (
        not isinstance(value.get("user_id"), int)
        or isinstance(value.get("user_id"), bool)
        or value["user_id"] < 1
        or not isinstance(value.get("username"), str)
        or not value["username"]
        or len(value["username"]) > 150
        or not isinstance(value.get("role"), str)
        or not MACHINE_CODE_RE.fullmatch(value["role"])
        or (
            value.get("ip_address") is not None
            and (
                not isinstance(value["ip_address"], str)
                or len(value["ip_address"]) > 80
            )
        )
        or (
            value.get("user_agent") is not None
            and (
                not isinstance(value["user_agent"], str)
                or len(value["user_agent"]) > 300
            )
        )
    ):
        return None
    return json.loads(json.dumps(value))


def _strict_terminal_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != TERMINAL_SUMMARY_KEYS:
        return None
    status_value = value.get("status")
    finished_at = value.get("finished_at")
    error_category = value.get("error_category")
    if (
        status_value not in TERMINAL_STATUSES
        or _parse_iso(finished_at) is None
        or (
            error_category is not None
            and (
                not isinstance(error_category, str)
                or not MACHINE_CODE_RE.fullmatch(error_category)
            )
        )
        or (status_value == "completed" and error_category is not None)
    ):
        return None
    return {
        "status": status_value,
        "finished_at": finished_at,
        "error_category": error_category,
    }


def _request_contract(
    payload: dict[str, Any] | None,
    state: str,
) -> dict[str, Any] | None:
    if state != "valid" or not isinstance(payload, dict):
        return None
    if (
        set(payload) != CURRENT_REQUEST_KEYS
        or payload.get("schema_version") != REQUEST_SCHEMA_VERSION
        or payload.get("document_type") != REQUEST_DOCUMENT_TYPE
        or payload.get("intent") != "apply_update"
        or payload.get("confirmed") is not True
        or payload.get("preflight_required") is not True
        or payload.get("status_path")
        != "data/update-control/update-status.json"
        or payload.get("state") not in REQUEST_STATES
    ):
        return None
    request_id = payload.get("request_id")
    submission_id = payload.get("submission_id")
    if (
        not isinstance(request_id, str)
        or not REQUEST_ID_RE.fullmatch(request_id)
        or not isinstance(submission_id, str)
        or not SUBMISSION_ID_RE.fullmatch(submission_id)
        or _parse_iso(payload.get("requested_at")) is None
        or _parse_iso(payload.get("updated_at")) is None
        or payload.get("audit_event_id") != _audit_event_id(request_id)
    ):
        return None
    source = _strict_source(payload.get("source"))
    candidate = _strict_candidate(payload.get("apply_candidate"))
    actor = _strict_actor(payload.get("requested_by"))
    if not source or not candidate or not actor:
        return None
    claimed_at = payload.get("claimed_at")
    terminal = payload.get("terminal")
    if payload["state"] == "admitted":
        if claimed_at is not None or terminal is not None:
            return None
    elif payload["state"] == "claimed":
        if _parse_iso(claimed_at) is None or terminal is not None:
            return None
    elif (
        claimed_at is not None
        and _parse_iso(claimed_at) is None
    ) or _strict_terminal_summary(terminal) is None:
        return None
    normalized = json.loads(json.dumps(payload))
    normalized["request_id"] = request_id.lower()
    normalized["submission_id"] = submission_id.lower()
    normalized["source"] = source
    normalized["apply_candidate"] = candidate
    normalized["requested_by"] = actor
    if terminal is not None:
        normalized["terminal"] = _strict_terminal_summary(terminal)
    return normalized


def _read_current_request_unlocked() -> tuple[str, dict[str, Any] | None]:
    payload, state = _read_json(_request_path())
    if state == "missing":
        return "missing", None
    current = _request_contract(payload, state)
    if current:
        return "current", current
    return "legacy_or_unknown", None


def _write_current_request(request: dict[str, Any]) -> dict[str, Any]:
    validated = _request_contract(request, "valid")
    if not validated:
        raise UpdateApplyBlocked(
            "admission_contract_invalid",
            "Update admission contract is invalid.",
        )
    _atomic_write_json(_request_path(), validated)
    return validated


def _helper_lease_active() -> bool:
    path = _helper_lease_path()
    try:
        if not path.exists() or not path.is_file():
            return False
        with path.open("a+", encoding="utf-8") as lease:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return True


def _schema_admission_blocker(db: Session) -> tuple[str, str] | None:
    try:
        bind = db.get_bind()
        if not inspect(bind).has_table("schema_migration_control"):
            return None
        control = db.get(SchemaMigrationControl, "current")
        if control is None or control.state == "completed":
            return None
        if control.state == "failed":
            attempt = (
                db.get(SchemaMigrationAttempt, control.owner_attempt_id)
                if control.owner_attempt_id
                else None
            )
            if attempt is not None and attempt.resumable is True:
                return None
            return (
                "schema_recovery_required",
                "A prior schema mutation requires recovery before another update.",
            )
        return (
            "schema_mutation_active",
            "A schema update is still active.",
        )
    except SQLAlchemyError as exc:
        db.rollback()
        raise UpdateApplyBlocked(
            "schema_state_unavailable",
            "Schema update state could not be verified.",
        ) from exc


def _safe_public_source(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = {
        "kind": _safe_machine_code(value.get("kind"), max_length=40),
        "channel": _safe_machine_code(value.get("channel"), max_length=40),
        "version": _safe_string(value.get("version"), max_length=80),
        "commit": (_safe_string(value.get("commit"), max_length=40) or "").lower()
        or None,
        "apply_ref": (
            _safe_string(value.get("apply_ref"), max_length=40) or ""
        ).lower()
        or None,
        "ref": _safe_string(value.get("ref"), max_length=120),
        "repo": _safe_string(value.get("repo"), max_length=160),
        "source_type": _safe_machine_code(value.get("source_type"), max_length=40),
    }
    if source["version"] and not VERSION_TEXT_RE.fullmatch(source["version"]):
        source["version"] = None
    for key in ("commit", "apply_ref"):
        if source[key] and not COMMIT_SHA_RE.fullmatch(source[key]):
            source[key] = None
    if source["repo"] and not GITHUB_REPO_RE.fullmatch(source["repo"]):
        source["repo"] = None
    if source["ref"] and (
        not GIT_REF_RE.fullmatch(source["ref"])
        or ".." in source["ref"]
        or "@{" in source["ref"]
    ):
        source["ref"] = None
    return source if any(value is not None for value in source.values()) else None


def _safe_public_candidate(value: Any) -> dict[str, Any] | None:
    candidate = _strict_candidate(value)
    if not candidate:
        return None
    snapshot = candidate["snapshot"]
    return {
        "source": candidate["source"],
        "snapshot": {
            "available": snapshot.get("available"),
            "fresh": snapshot.get("fresh"),
            "age_seconds": snapshot.get("age_seconds"),
            "fresh_for_seconds": snapshot.get("fresh_for_seconds"),
        },
    }


def _safe_steps(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        name = _safe_machine_code(item.get("name"), max_length=80)
        state = _safe_machine_code(item.get("status"), max_length=40)
        if name and state in {"pending", "running", "completed", "failed"}:
            result.append({"name": name, "status": state})
    return result


def _admission_payload(
    authority: str,
    state: str,
    request: dict[str, Any] | None = None,
    *,
    reason_code: str | None = None,
) -> dict[str, Any]:
    request = request or {}
    source = request.get("source") if isinstance(request.get("source"), dict) else {}
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "authority": authority,
        "state": state,
        "active": authority == "active",
        "submission_id": _safe_string(request.get("submission_id"), max_length=80),
        "request_id": _safe_string(request.get("request_id"), max_length=80),
        "target_version": _safe_string(source.get("version"), max_length=80),
        "target_commit": _safe_string(source.get("commit"), max_length=40),
        "reason_code": _safe_machine_code(reason_code, max_length=80),
        "retry_allowed": authority == "inactive",
        "next_action": (
            "wait_for_status"
            if authority == "active"
            else "confirm_apply"
            if authority == "inactive"
            else "refresh_status"
        ),
    }


def _base_status(
    status_value: str = "idle",
    phase: str = "idle",
    *,
    request_id: str | None = None,
    submission_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "request_id": request_id,
        "submission_id": submission_id,
        "target_version": None,
        "status": status_value,
        "phase": phase,
        "current_step": phase,
        "started_at": None,
        "updated_at": _iso(),
        "finished_at": None,
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
        "expected_commit": None,
        "installed_commit": None,
        "commit_verified": False,
        "admission": _admission_payload("inactive", "idle"),
        "last_apply_summary": None,
        "apply_history": {
            "available": False,
            "state": "missing",
            "items": [],
            "last": None,
            "max_items": MAX_HISTORY_ITEMS,
        },
    }


def _macro_steps(current: str, *, failed: bool = False) -> list[dict[str, str]]:
    order = [
        "request",
        "preflight",
        "applying",
        "health_check",
        "commit_verification",
    ]
    aliases = {
        "queued": "request",
        "starting_helper": "request",
        "acquire_source": "preflight",
        "downloading": "preflight",
        "extracting": "preflight",
        "validating_source": "preflight",
        "overlay": "applying",
        "compose_config": "applying",
        "rebuilding": "applying",
        "restarting": "applying",
        "completed": "commit_verification",
    }
    active = aliases.get(current, current)
    try:
        active_index = order.index(active)
    except ValueError:
        active_index = 0
    steps = []
    for index, name in enumerate(order):
        if index < active_index:
            status_value = "completed"
        elif index == active_index:
            status_value = "failed" if failed else "running"
        else:
            status_value = "pending"
        steps.append({"name": name, "status": status_value})
    if current == "completed":
        for item in steps:
            item["status"] = "completed"
    return steps


def _queued_status(request: dict[str, Any]) -> dict[str, Any]:
    status = _base_status(
        "queued",
        "queued",
        request_id=request["request_id"],
        submission_id=request["submission_id"],
    )
    status.update(
        {
            "target_version": request["source"]["version"],
            "started_at": request["requested_at"],
            "updated_at": request["updated_at"],
            "source": _safe_public_source(
                {**request["source"], "kind": "github-tarball"}
            ),
            "apply_candidate": _safe_public_candidate(
                request["apply_candidate"]
            ),
            "expected_commit": request["source"]["commit"],
            "steps": _macro_steps("queued"),
            "can_cancel": True,
            "admission": _admission_payload(
                "active",
                "admitted",
                request,
            ),
        }
    )
    return status


def _terminal_status(
    request: dict[str, Any],
    status_value: str,
    error_category: str | None,
    *,
    finished_at: str | None = None,
) -> dict[str, Any]:
    finished = finished_at or _iso()
    phase = "completed" if status_value == "completed" else (
        error_category or status_value
    )
    status = _base_status(
        status_value,
        phase,
        request_id=request["request_id"],
        submission_id=request["submission_id"],
    )
    status.update(
        {
            "target_version": request["source"]["version"],
            "started_at": request["requested_at"],
            "updated_at": finished,
            "finished_at": finished,
            "source": _safe_public_source(
                {**request["source"], "kind": "github-tarball"}
            ),
            "apply_candidate": _safe_public_candidate(
                request["apply_candidate"]
            ),
            "expected_commit": request["source"]["commit"],
            "steps": _macro_steps(
                "completed" if status_value == "completed" else phase,
                failed=status_value != "completed",
            ),
            "can_cancel": False,
            "error": (
                _public_error_for_category(error_category)
                if error_category
                else None
            ),
            "admission": _admission_payload(
                "inactive",
                "terminal",
                request,
            ),
        }
    )
    return status


def _sanitize_history_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    request_id = _safe_string(item.get("request_id"), max_length=80)
    status_value = _safe_machine_code(item.get("status"), max_length=40)
    if (
        not request_id
        or not LEGACY_REQUEST_ID_RE.fullmatch(request_id)
        or status_value not in TERMINAL_STATUSES
    ):
        return None
    return {
        "request_id": request_id,
        "submission_id": _safe_string(item.get("submission_id"), max_length=80),
        "target_version": _safe_string(item.get("target_version"), max_length=80),
        "status": status_value,
        "phase": _safe_machine_code(item.get("phase"), max_length=80),
        "started_at": _safe_timestamp(item.get("started_at")),
        "finished_at": _safe_timestamp(
            item.get("finished_at") or item.get("updated_at")
        ),
        "updated_at": _safe_timestamp(item.get("updated_at")),
        "expected_commit": (
            _safe_string(item.get("expected_commit"), max_length=40) or ""
        ).lower()
        or None,
        "installed_commit": (
            _safe_string(item.get("installed_commit"), max_length=40) or ""
        ).lower()
        or None,
        "commit_verified": item.get("commit_verified") is True,
        "source": _safe_public_source(item.get("source")),
        "apply_candidate": _safe_public_candidate(item.get("apply_candidate")),
        "steps": _safe_steps(item.get("steps")),
        "error": _safe_public_error(item.get("error")),
        "history_detail_status": "step_timestamps_unavailable",
    }


def _read_last_history() -> dict[str, Any]:
    payload, state = _read_json(_apply_history_path())
    if state != "valid" or not payload:
        return {
            "available": False,
            "state": state,
            "items": [],
            "last": None,
            "max_items": MAX_HISTORY_ITEMS,
        }
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    last = next(
        (
            item
            for item in (
                _sanitize_history_item(candidate)
                for candidate in reversed(raw_items)
            )
            if item is not None
        ),
        None,
    )
    return {
        "available": last is not None,
        "state": "valid",
        "items": [last] if last else [],
        "last": last,
        "max_items": MAX_HISTORY_ITEMS,
    }


def _sanitize_status_payload() -> tuple[dict[str, Any], str]:
    history = _read_last_history()
    payload, state = _read_json(_status_path())
    if state == "missing":
        result = _base_status()
        result["apply_history"] = history
        result["last_apply_summary"] = history["last"]
        return result, state
    if state != "valid" or not payload:
        result = _base_status("blocked", "status_read")
        result["error"] = _safe_error(
            f"status_{state}",
            "Update status is unavailable or invalid.",
        )
        result["apply_history"] = history
        result["last_apply_summary"] = history["last"]
        return result, state
    status_value = _safe_machine_code(payload.get("status"), max_length=40)
    phase = _safe_machine_code(
        payload.get("phase") or payload.get("current_step"),
        max_length=80,
    )
    request_id = _safe_string(payload.get("request_id"), max_length=80)
    submission_id = _safe_string(payload.get("submission_id"), max_length=80)
    if not status_value:
        status_value = "unknown"
    if not phase:
        phase = "unknown"
    if not request_id or not LEGACY_REQUEST_ID_RE.fullmatch(request_id):
        request_id = None
    if submission_id and not SUBMISSION_ID_RE.fullmatch(submission_id):
        submission_id = None
    result = _base_status(
        status_value,
        phase,
        request_id=request_id,
        submission_id=submission_id.lower() if submission_id else None,
    )
    expected_commit = (
        _safe_string(payload.get("expected_commit"), max_length=40) or ""
    ).lower()
    installed_commit = (
        _safe_string(payload.get("installed_commit"), max_length=40) or ""
    ).lower()
    result.update(
        {
            "target_version": _safe_string(
                payload.get("target_version"),
                max_length=80,
            ),
            "current_step": _safe_machine_code(
                payload.get("current_step") or payload.get("phase"),
                max_length=80,
            )
            or phase,
            "started_at": _safe_timestamp(payload.get("started_at")),
            "updated_at": _safe_timestamp(payload.get("updated_at")) or _iso(),
            "finished_at": _safe_timestamp(payload.get("finished_at")),
            "source": _safe_public_source(payload.get("source")),
            "apply_candidate": _safe_public_candidate(
                payload.get("apply_candidate")
            ),
            "steps": _safe_steps(payload.get("steps")),
            "can_cancel": (
                payload.get("can_cancel") is True
                and status_value == "queued"
            ),
            "error": _safe_public_error(payload.get("error")),
            "expected_commit": (
                expected_commit
                if COMMIT_SHA_RE.fullmatch(expected_commit)
                else None
            ),
            "installed_commit": (
                installed_commit
                if COMMIT_SHA_RE.fullmatch(installed_commit)
                else None
            ),
            "commit_verified": payload.get("commit_verified") is True,
            "apply_history": history,
            "last_apply_summary": history["last"],
        }
    )
    started = _parse_iso(result["started_at"])
    updated = _parse_iso(result["updated_at"])
    now = _utcnow()
    result["elapsed_seconds"] = (
        max(0, int((now - started).total_seconds()))
        if started
        else None
    )
    result["last_progress_age_seconds"] = (
        max(0, int((now - updated).total_seconds()))
        if updated
        else None
    )
    result["is_stale"] = bool(
        status_value in RUNNING_STATUSES
        and result["last_progress_age_seconds"] is not None
        and result["last_progress_age_seconds"] > STALE_AFTER_SECONDS
    )
    result["effective_status"] = (
        "stalled" if result["is_stale"] else status_value
    )
    release_path = (
        Path(
            os.getenv("KMVMS_APP_ROOT")
            or os.getenv("KM_VMS_APP_DIR")
            or Path.cwd()
        )
        / ".km-vms-release.json"
    )
    release, release_state = _read_json(release_path)
    if release_state == "valid" and release:
        release_commit = (
            _safe_string(release.get("commit_sha"), max_length=40) or ""
        ).lower()
        result["release_identity"] = {
            "metadata_status": _safe_machine_code(
                release.get("metadata_status"),
                max_length=40,
            ),
            "metadata_source": _safe_machine_code(
                release.get("metadata_source"),
                max_length=80,
            ),
            "commit_sha": (
                release_commit
                if COMMIT_SHA_RE.fullmatch(release_commit)
                else None
            ),
        }
    if _contains_sensitive_content(result):
        blocked = _base_status("blocked", "status_redaction")
        blocked["error"] = _safe_error(
            "status_sensitive_content",
            "Update status contained sensitive content and was suppressed.",
        )
        return blocked, "sensitive_content"
    return result, "valid"


def _status_matches_request(
    status: dict[str, Any],
    request: dict[str, Any],
) -> bool:
    return (
        status.get("request_id") == request.get("request_id")
        and status.get("expected_commit") == request["source"]["commit"]
        and (
            status.get("submission_id") in {None, request.get("submission_id")}
        )
    )


def _mark_terminal_unlocked(
    request: dict[str, Any],
    status_value: str,
    error_category: str | None,
    *,
    status_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finished_at = _iso()
    if status_payload is None:
        status_payload = _terminal_status(
            request,
            status_value,
            error_category,
            finished_at=finished_at,
        )
        _atomic_write_json(_status_path(), status_payload)
    else:
        finished_at = (
            _safe_timestamp(
                status_payload.get("finished_at")
                or status_payload.get("updated_at")
            )
            or finished_at
        )
    updated = json.loads(json.dumps(request))
    updated["state"] = "terminal"
    updated["updated_at"] = finished_at
    updated["terminal"] = {
        "status": status_value,
        "finished_at": finished_at,
        "error_category": error_category,
    }
    return _write_current_request(updated)


def _reconcile_current_unlocked(request: dict[str, Any]) -> dict[str, Any]:
    if request["state"] == "terminal":
        return request
    lease_active = _helper_lease_active()
    status, _ = _sanitize_status_payload()
    if (
        status.get("status") in TERMINAL_STATUSES
        and _status_matches_request(status, request)
    ):
        error_category = (
            (status.get("error") or {}).get("category")
            if status.get("status") != "completed"
            else None
        )
        return _mark_terminal_unlocked(
            request,
            status["status"],
            error_category,
            status_payload=status,
        )
    if request["state"] == "claimed" and not lease_active:
        return _mark_terminal_unlocked(
            request,
            "failed",
            "helper_restart_interrupted",
        )
    if request["state"] == "admitted" and not lease_active:
        requested_at = _parse_iso(request["requested_at"])
        if (
            requested_at is not None
            and (_utcnow() - requested_at).total_seconds()
            > ADMISSION_CLAIM_TIMEOUT_SECONDS
        ):
            return _mark_terminal_unlocked(
                request,
                "failed",
                "helper_not_claimed",
            )
    return request


def _status_for_current_unlocked(request: dict[str, Any]) -> dict[str, Any]:
    request = _reconcile_current_unlocked(request)
    if request["state"] == "admitted":
        return _queued_status(request)
    raw_status, _ = _sanitize_status_payload()
    if request["state"] == "claimed":
        if _status_matches_request(raw_status, request):
            raw_status["admission"] = _admission_payload(
                "active",
                "claimed",
                request,
            )
            return raw_status
        status = _base_status(
            "starting_helper",
            "starting_helper",
            request_id=request["request_id"],
            submission_id=request["submission_id"],
        )
        status.update(
            {
                "target_version": request["source"]["version"],
                "started_at": request["requested_at"],
                "source": _safe_public_source(
                    {**request["source"], "kind": "github-tarball"}
                ),
                "apply_candidate": _safe_public_candidate(
                    request["apply_candidate"]
                ),
                "expected_commit": request["source"]["commit"],
                "steps": _macro_steps("starting_helper"),
                "admission": _admission_payload(
                    "active",
                    "claimed",
                    request,
                ),
            }
        )
        return status
    terminal = request["terminal"]
    if (
        raw_status.get("status") in TERMINAL_STATUSES
        and _status_matches_request(raw_status, request)
    ):
        raw_status["admission"] = _admission_payload(
            "inactive",
            "terminal",
            request,
        )
        raw_status["is_stale"] = False
        raw_status["effective_status"] = raw_status["status"]
        return raw_status
    return _terminal_status(
        request,
        terminal["status"],
        terminal["error_category"],
        finished_at=terminal["finished_at"],
    )


def _legacy_status_unlocked() -> dict[str, Any]:
    status, state = _sanitize_status_payload()
    lease_active = _helper_lease_active()
    if state != "valid":
        status["admission"] = _admission_payload(
            "inactive",
            "legacy_ignored",
            reason_code=state,
        )
        return status
    if status.get("status") in RUNNING_STATUSES:
        if lease_active:
            status["admission"] = _admission_payload(
                "active",
                "legacy_claimed",
            )
            return status
        interrupted = _base_status(
            "failed",
            "helper_restart_interrupted",
            request_id=status.get("request_id"),
            submission_id=status.get("submission_id"),
        )
        interrupted.update(
            {
                "target_version": status.get("target_version"),
                "started_at": status.get("started_at"),
                "updated_at": _iso(),
                "finished_at": _iso(),
                "source": status.get("source"),
                "apply_candidate": status.get("apply_candidate"),
                "expected_commit": status.get("expected_commit"),
                "steps": _macro_steps(
                    "helper_restart_interrupted",
                    failed=True,
                ),
                "error": _public_error_for_category(
                    "helper_restart_interrupted"
                ),
                "last_apply_summary": status.get("last_apply_summary"),
                "apply_history": status.get("apply_history"),
                "admission": _admission_payload(
                    "inactive",
                    "legacy_interrupted",
                ),
            }
        )
        return interrupted
    status["admission"] = _admission_payload(
        "inactive",
        (
            "legacy_terminal"
            if status.get("status") in TERMINAL_STATUSES
            else "legacy_idle"
        ),
    )
    status["is_stale"] = False
    status["effective_status"] = status.get("status")
    return status


def _read_update_apply_status_unlocked() -> dict[str, Any]:
    classification, request = _read_current_request_unlocked()
    if classification == "current" and request:
        return _status_for_current_unlocked(request)
    return _legacy_status_unlocked()


def read_update_apply_status() -> dict[str, Any]:
    with _admission_guard():
        return _read_update_apply_status_unlocked()


def _sanitized_source(latest: dict[str, Any]) -> dict[str, Any]:
    commit = (_safe_string(latest.get("commit"), max_length=40) or "").lower()
    return {
        "kind": "trusted_manifest",
        "channel": _safe_machine_code(latest.get("channel"), max_length=40)
        or "stable",
        "version": _safe_string(latest.get("version"), max_length=80),
        "commit": commit,
        "apply_ref": commit,
        "ref": _safe_string(
            latest.get("source_ref") or latest.get("git_ref"),
            max_length=120,
        ),
        "repo": _safe_string(latest.get("source_repo"), max_length=160),
        "source_type": _safe_machine_code(
            latest.get("source_type"),
            max_length=40,
        ),
    }


def _validate_latest_for_apply(result: dict[str, Any]) -> dict[str, Any]:
    if (
        result.get("manifest_source_status") == "not_configured"
        or result.get("status") == "not_configured"
    ):
        raise UpdateApplyBlocked(
            "manifest_not_configured",
            "Trusted release manifest source is not configured.",
        )
    if result.get("status") in {"check_failed", "invalid_manifest", "failed"}:
        raise UpdateApplyBlocked(
            "manifest_check_failed",
            "Trusted release manifest check failed.",
        )
    blockers = result.get("blockers") or []
    if blockers:
        first = blockers[0]
        code = _safe_machine_code(
            first.get("code") if isinstance(first, dict) else first,
            max_length=80,
        ) or "release_blocked"
        raise UpdateApplyBlocked(
            code,
            "Release has blockers that cannot be applied from the UI.",
            diagnostics={"blockers": blockers},
        )
    if result.get("status") != "update_available":
        raise UpdateApplyBlocked(
            "no_update_available",
            "No trusted compatible update is available.",
        )
    latest = result.get("latest")
    if not isinstance(latest, dict):
        raise UpdateApplyBlocked(
            "latest_release_missing",
            "Latest trusted release metadata is missing.",
        )
    if (
        latest.get("requires_backup")
        or latest.get("requires_manual_action")
        or latest.get("requires_migration")
    ):
        raise UpdateApplyBlocked(
            "unsupported_release_requirements",
            "Release requirements are outside the supported in-app apply path.",
        )
    if (
        latest.get("source_type") != "github_tarball"
        or not latest.get("source_repo")
        or not (latest.get("source_ref") or latest.get("git_ref"))
    ):
        raise UpdateApplyBlocked(
            "trusted_source_incomplete",
            "Trusted release source must be a GitHub tarball repo/ref.",
        )
    commit = str(latest.get("commit") or "").lower()
    if not COMMIT_SHA_RE.fullmatch(commit):
        raise UpdateApplyBlocked(
            "trusted_commit_missing",
            "Trusted release manifest must include a full commit SHA.",
        )
    return latest


def _check_token_precondition() -> None:
    if not settings.kmvms_update_source_private:
        return
    if settings.kmvms_update_token_configured or os.getenv(
        "KM_VMS_GITHUB_TOKEN"
    ):
        return
    token_file = os.getenv("KM_VMS_GITHUB_TOKEN_FILE")
    if token_file and Path(token_file).is_file():
        return
    raise UpdateApplyBlocked(
        "token_not_configured",
        "Private trusted source requires a server-side GitHub token source.",
    )


def _validate_expected(
    latest: dict[str, Any],
    *,
    expected_version: str | None,
    expected_commit: str | None,
) -> None:
    if not expected_version or not expected_commit:
        raise UpdateApplyBlocked(
            "update_check_required",
            "Run Check update again before applying this release.",
        )
    if expected_version != latest.get("version"):
        raise UpdateApplyBlocked(
            "manifest_version_changed",
            "Trusted manifest version changed. Refresh update status and retry.",
        )
    if expected_commit.lower() != str(latest.get("commit") or "").lower():
        raise UpdateApplyBlocked(
            "manifest_commit_changed",
            "Trusted manifest commit changed. Refresh update status and retry.",
        )


def _installed_matches_snapshot(snapshot: dict[str, Any]) -> bool:
    fingerprint = (
        snapshot.get("installed_fingerprint")
        if isinstance(snapshot.get("installed_fingerprint"), dict)
        else {}
    )
    installed = read_installed_update_state()
    expected_commit = _safe_string(
        fingerprint.get("installed_commit"),
        max_length=40,
    )
    expected_git_head = _safe_string(
        fingerprint.get("git_head"),
        max_length=40,
    )
    return (
        (fingerprint.get("installed_version") or None)
        == (installed.installed_version or None)
        and (expected_commit or None) == (installed.installed_commit or None)
        and (expected_git_head or None) == (installed.git_head or None)
        and (fingerprint.get("identity_validity") or None)
        == (installed.identity_validity or None)
    )


def _snapshot_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "update_available",
        "blockers": [],
        "latest": (
            snapshot.get("latest")
            if isinstance(snapshot.get("latest"), dict)
            else {}
        ),
        "manifest_source_status": snapshot.get("manifest_source_status"),
    }


def _canonical_freshness_snapshot(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "available": (
            raw.get("available")
            if isinstance(raw.get("available"), bool)
            else False
        ),
        "fresh": (
            raw.get("fresh")
            if isinstance(raw.get("fresh"), bool)
            else False
        ),
        "age_seconds": raw.get("age_seconds"),
        "fresh_for_seconds": raw.get("fresh_for_seconds"),
        "version": raw.get("version"),
        "commit_short": raw.get("commit_short"),
        "provider": raw.get("provider"),
    }


def _select_apply_candidate(
    db: Session,
    *,
    expected_version: str | None,
    expected_commit: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not expected_version or not expected_commit:
        raise UpdateApplyBlocked(
            "update_check_required",
            "Run Check update again before applying this release.",
        )
    snapshot_status = trusted_apply_snapshot_status()
    if snapshot_status.get("available") and not snapshot_status.get("fresh"):
        raise UpdateApplyBlocked(
            "trusted_snapshot_stale",
            "Update check is too old. Run Check update again.",
            diagnostics={"snapshot": snapshot_status},
        )
    snapshot = get_trusted_apply_snapshot()
    if snapshot:
        latest = _validate_latest_for_apply(_snapshot_result(snapshot))
        _validate_expected(
            latest,
            expected_version=expected_version,
            expected_commit=expected_commit,
        )
        if not _installed_matches_snapshot(snapshot):
            raise UpdateApplyBlocked(
                "trusted_snapshot_invalidated",
                "Installed release identity changed after update check.",
            )
        return latest, {
            "source": "trusted_snapshot",
            "snapshot": _canonical_freshness_snapshot(
                snapshot.get("freshness")
            ),
        }
    update = run_update_check(db, manual=False)
    latest = _validate_latest_for_apply(update)
    _validate_expected(
        latest,
        expected_version=expected_version,
        expected_commit=expected_commit,
    )
    return latest, {
        "source": "live_check",
        "snapshot": _canonical_freshness_snapshot(snapshot_status),
    }


def _audit_event_id(request_id: str) -> str:
    return str(uuid.uuid5(AUDIT_NAMESPACE, request_id))


def _request_id(submission_id: str, target_commit: str) -> str:
    identity = uuid.uuid5(
        AUDIT_NAMESPACE,
        f"{submission_id}:{target_commit}",
    )
    return f"update-{identity.hex}"


def _audit_matches(event: AuditEvent | None, request: dict[str, Any]) -> bool:
    actor = request["requested_by"]
    return bool(
        event
        and event.id == request["audit_event_id"]
        and event.event_type == AUDIT_EVENT_TYPE
        and event.target_type == AUDIT_TARGET_TYPE
        and event.target_id == request["request_id"]
        and event.actor_user_id == actor["user_id"]
        and event.actor_username == actor["username"]
        and event.actor_role == actor["role"]
    )


def _ensure_audit(db: Session, request: dict[str, Any]) -> None:
    event_id = request["audit_event_id"]
    existing = db.get(AuditEvent, event_id)
    if existing:
        if not _audit_matches(existing, request):
            raise UpdateApplyBlocked(
                "accepted_audit_corrupt",
                "Update audit identity is contradictory.",
            )
        db.rollback()
        return
    actor = request["requested_by"]
    db.add(
        AuditEvent(
            id=event_id,
            actor_user_id=actor["user_id"],
            actor_username=actor["username"],
            actor_role=actor["role"],
            category="system",
            event_type=AUDIT_EVENT_TYPE,
            severity="warning",
            message_ru="Product update apply was requested.",
            message_en="Product update apply was requested.",
            target_type=AUDIT_TARGET_TYPE,
            target_id=request["request_id"],
            event_metadata={
                "request_id": request["request_id"],
                "idempotency": "client_uuid",
                "target_version": request["source"]["version"],
                "api_docker_socket": False,
                "api_shell_execution": False,
            },
            ip_address=actor.get("ip_address"),
            user_agent=actor.get("user_agent"),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(AuditEvent, event_id)
        if not _audit_matches(existing, request):
            raise UpdateApplyBlocked(
                "accepted_audit_corrupt",
                "Update audit identity is contradictory.",
            )
    except SQLAlchemyError as exc:
        db.rollback()
        raise UpdateApplyBlocked(
            "accepted_audit_unavailable",
            "Update admission was not written because audit is unavailable.",
            diagnostics={"retry_allowed": True},
        ) from exc


def _canonical_apply_response(
    request: dict[str, Any],
    *,
    replayed: bool,
) -> dict[str, Any]:
    status = _status_for_current_unlocked(request)
    return {
        "accepted": True,
        "status": status.get("status"),
        "submission_id": request["submission_id"],
        "request_id": request["request_id"],
        "replayed": replayed,
        "apply_status": status,
        "can_cancel": bool(status.get("can_cancel")),
    }


def request_update_apply(
    db: Session,
    *,
    confirm: bool,
    submission_id: str | None,
    expected_manifest_version: str | None,
    expected_manifest_commit: str | None,
    actor: Any,
    ip_address: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    if not settings.kmvms_update_helper_enabled:
        raise UpdateApplyBlocked(
            "helper_not_configured",
            "Update helper is not enabled.",
        )
    if confirm is not True:
        raise UpdateApplyBlocked(
            "confirmation_required",
            "Explicit update confirmation is required.",
        )
    normalized_submission = _normalized_submission_id(submission_id)
    expected_version = _normalized_target_version(expected_manifest_version)
    expected_commit = _normalized_target_commit(expected_manifest_commit)
    reject_forbidden_apply_fields(
        {
            "confirm": confirm,
            "submission_id": normalized_submission,
            "expected_manifest_version": expected_version,
            "expected_manifest_commit": expected_commit,
        }
    )
    _check_token_precondition()
    latest, apply_candidate = _select_apply_candidate(
        db,
        expected_version=expected_version,
        expected_commit=expected_commit,
    )
    source = _sanitized_source(latest)
    if not _strict_source(source):
        raise UpdateApplyBlocked(
            "trusted_source_incomplete",
            "Trusted release source is incomplete.",
        )
    target_commit = source["commit"]
    request_id = _request_id(normalized_submission, target_commit)
    now = _iso()
    candidate_request = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "document_type": REQUEST_DOCUMENT_TYPE,
        "request_id": request_id,
        "submission_id": normalized_submission,
        "requested_at": now,
        "updated_at": now,
        "requested_by": _actor_snapshot(
            actor,
            ip_address=ip_address,
            user_agent=user_agent,
        ),
        "intent": "apply_update",
        "source": source,
        "apply_candidate": apply_candidate,
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
        "state": "admitted",
        "claimed_at": None,
        "terminal": None,
        "audit_event_id": _audit_event_id(request_id),
    }
    if not _request_contract(candidate_request, "valid"):
        raise UpdateApplyBlocked(
            "admission_contract_invalid",
            "Update admission contract is invalid.",
        )
    with _admission_guard():
        classification, current = _read_current_request_unlocked()
        if classification == "current" and current:
            current = _reconcile_current_unlocked(current)
            if current["submission_id"] == normalized_submission:
                if current["source"]["commit"] != target_commit:
                    raise UpdateApplyBlocked(
                        "idempotency_conflict",
                        "The idempotency identifier is bound to another target.",
                    )
                return _canonical_apply_response(current, replayed=True)
            if current["state"] in ACTIVE_REQUEST_STATES:
                raise UpdateApplyBlocked(
                    "update_already_running",
                    "Another update is already admitted.",
                )
        if _helper_lease_active():
            raise UpdateApplyBlocked(
                "update_already_running",
                "An update helper is still active.",
            )
        schema_blocker = _schema_admission_blocker(db)
        if schema_blocker:
            raise UpdateApplyBlocked(*schema_blocker)
        _ensure_audit(db, candidate_request)
        written = _write_current_request(candidate_request)
        return _canonical_apply_response(written, replayed=False)


def cancel_update_apply() -> dict[str, Any]:
    with _admission_guard():
        classification, current = _read_current_request_unlocked()
        if classification != "current" or not current:
            return {
                "status": "not_cancelable",
                "request_id": None,
                "can_cancel": False,
            }
        current = _reconcile_current_unlocked(current)
        if current["state"] != "admitted":
            status = _status_for_current_unlocked(current)
            return {
                "status": "not_cancelable",
                "request_id": current["request_id"],
                "can_cancel": False,
                "apply_status": status,
            }
        terminal = _mark_terminal_unlocked(
            current,
            "cancelled",
            "cancelled_before_start",
        )
        status = _status_for_current_unlocked(terminal)
        return {
            "status": "cancelled",
            "request_id": current["request_id"],
            "can_cancel": False,
            "apply_status": status,
        }


def reject_forbidden_apply_fields(payload: dict[str, Any]) -> None:
    for key in payload:
        lowered = str(key).lower()
        if (
            key in FORBIDDEN_REQUEST_FIELDS
            or any(
                token in lowered
                for token in (
                    "token",
                    "secret",
                    "command",
                    "url",
                    "path",
                    "image",
                )
            )
        ):
            raise UpdateApplyBlocked(
                "request_controlled_source_forbidden",
                "Update source and execution authority are server controlled.",
            )
