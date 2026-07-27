#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

APP_DIR = Path(os.getenv("KM_VMS_UPDATE_APP_DIR") or "/host-app")
HOST_APP_DIR_RAW = os.getenv("KM_VMS_UPDATE_HOST_APP_DIR") or ""
HOST_APP_DIR = Path(HOST_APP_DIR_RAW) if HOST_APP_DIR_RAW else None
CONTROL_DIR = APP_DIR / "data" / "update-control"
REQUEST_FILE = CONTROL_DIR / "update-request.json"
LINEAGE_FILE = CONTROL_DIR / "update-admission-lineage.json"
STATUS_FILE = CONTROL_DIR / "update-status.json"
HISTORY_FILE = CONTROL_DIR / "update-helper-history.json"
PROGRESS_FILE = CONTROL_DIR / "update-progress.json"
APPLY_HISTORY_FILE = CONTROL_DIR / "update-apply-history.json"
ADMISSION_LOCK_FILE = CONTROL_DIR / "update-admission.lock"
HELPER_LEASE_FILE = CONTROL_DIR / "update-helper-claim.lock"
POLL_SECONDS = int(os.getenv("KM_VMS_UPDATE_HELPER_POLL_SECONDS") or "2")
MAX_CONTROL_BYTES = 64 * 1024
MAX_ADMISSION_BYTES = 512 * 1024
MAX_LINEAGE_BYTES = 4 * 1024
MAX_ADMISSION_ENTRIES = 256
MAX_TERMINAL_STEPS = 12
ADMISSION_SCHEMA_VERSION = 2
ADMISSION_DOCUMENT_TYPE = "update_apply_admission"
ADMISSION_STATES = {"audit_pending", "admitted_unclaimed", "claimed", "terminal"}
NON_TERMINAL_ADMISSION_STATES = ADMISSION_STATES - {"terminal"}
LINEAGE_PAYLOAD = {
    "schema_version": 1,
    "document_type": "update_apply_admission_lineage",
    "initialized": True,
}
AUDIT_NAMESPACE = uuid.UUID("abf15e22-71b8-5af5-b9ee-ef808127c780")
MAX_APPLY_HISTORY_ITEMS = 10
TERMINAL = {"completed", "failed", "cancelled", "blocked"}
ACTIVE = {"starting_helper", "preflight", "acquire_source", "downloading", "extracting", "validating_source", "overlay", "applying", "compose_config", "rebuilding", "restarting", "health_check", "commit_verification"}
STEP_ORDER = ["queued", "preflight", "acquire_source", "extracting", "validating_source", "overlay", "compose_config", "rebuilding", "restarting", "health_check", "commit_verification"]
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
CANONICAL_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
REQUEST_ID_RE = re.compile(r"^update-[0-9a-f]{32}$", re.IGNORECASE)
LEGACY_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,79}$")
MIGRATION_ATTEMPT_RE = re.compile(
    r"^migration-attempt-[0-9a-f]{32}$",
    re.IGNORECASE,
)
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
MACHINE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{0,200}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,119}$")
FORBIDDEN_KEYS = {"url", "repo_url", "command", "docker", "compose", "token", "token_env", "token_file", "path", "backup_path", "database_url", "image", "env"}
ADMISSION_DOCUMENT_KEYS = {"schema_version", "document_type", "current_submission_id", "entries", "updated_at"}
ADMISSION_ENTRY_KEYS = {
    "submission_id", "request_id", "target_version", "target_commit", "requested_at", "updated_at",
    "state", "request", "audit", "claimed_at", "terminal",
}
CURRENT_REQUEST_KEYS = {
    "schema_version", "request_id", "submission_id", "requested_at", "requested_by", "intent",
    "source", "apply_candidate", "confirmed", "preflight_required", "status_path",
}
CURRENT_ACTOR_KEYS = {"user_id", "username", "role", "ip_address", "user_agent"}
REQUEST_SOURCE_KEYS = {"kind", "channel", "version", "commit", "apply_ref", "ref", "repo", "source_type"}
APPLY_CANDIDATE_KEYS = {"source", "snapshot"}
APPLY_CANDIDATE_PROFILE_CANONICAL_CURRENT = "canonical_current"
APPLY_CANDIDATE_PROFILE_COMPACT_READ_ONLY = "compact_read_only"
APPLY_FRESHNESS_KEYS = {"available", "fresh", "age_seconds", "fresh_for_seconds", "version", "commit_short", "provider"}
AUDIT_KEYS = {"state", "event_id", "confirmed_at"}
TERMINAL_SOURCE_KEYS = {"kind", "repo", "ref", "commit", "apply_ref"}
TERMINAL_STEP_KEYS = {"name", "status"}
TERMINAL_ERROR_KEYS = {"category", "message", "operator_action"}
TERMINAL_RELEASE_IDENTITY_KEYS = {"host_metadata_status", "api_metadata_status", "api_visible", "commit_verified"}
TERMINAL_SIDE_EFFECT_KEYS = {"api_docker_socket", "api_shell_execution", "request_controlled_source", "helper_has_docker_socket", "helper_public_ports"}
TERMINAL_COMMON_KEYS = {
    "schema_version", "request_id", "submission_id", "target_version", "status", "phase", "current_step",
    "started_at", "updated_at", "finished_at", "source", "expected_commit", "commit_verified", "steps",
    "can_cancel", "rollback_supported", "side_effects", "error",
}
PRE_CLOSEOUT_CANCEL_KEYS = {
    "schema_version", "request_id", "submission_id", "target_version", "status", "phase", "current_step",
    "started_at", "updated_at", "finished_at", "source", "apply_candidate", "steps", "can_cancel",
    "rollback_supported", "expected_commit", "installed_commit", "commit_verified", "error",
}
LEGACY_MINIMAL_REQUEST_KEYS = {"schema_version", "request_id", "requested_at", "intent", "confirmed", "source"}
LEGACY_HISTORICAL_REQUEST_KEYS = LEGACY_MINIMAL_REQUEST_KEYS | {"requested_by", "preflight_required", "status_path"}
LEGACY_SNAPSHOT_REQUEST_KEYS = LEGACY_HISTORICAL_REQUEST_KEYS | {"apply_candidate"}
LEGACY_TRANSITIONAL_REQUEST_KEYS = LEGACY_SNAPSHOT_REQUEST_KEYS | {"submission_id"}
SCHEMA_RETRY_REQUEST_KEYS = LEGACY_HISTORICAL_REQUEST_KEYS | {
    "retry_of_request_id",
    "migration_attempt_id",
}
LEGACY_MINIMAL_SOURCE_KEYS = {"version", "commit"}
LEGACY_ACTOR_KEYS = {"user_id", "role"}
LEGACY_MINIMAL_TERMINAL_KEYS = {
    "schema_version", "request_id", "status", "phase", "current_step", "started_at", "updated_at",
    "finished_at", "expected_commit", "installed_commit", "commit_verified", "error",
}
LEGACY_HISTORICAL_COMPLETED_TERMINAL_KEYS = {
    "schema_version", "request_id", "status", "phase", "current_step", "started_at", "updated_at",
    "source", "expected_commit", "installed_commit", "commit_verified", "steps", "can_cancel",
    "rollback_supported", "side_effects", "error",
}
LEGACY_VERIFIED_COMPLETED_TERMINAL_KEYS = LEGACY_HISTORICAL_COMPLETED_TERMINAL_KEYS | {
    "release_identity",
}
LEGACY_HISTORICAL_COMPLETED_STEP_NAMES = (
    "request", "preflight", "apply", "health_check", "commit_verification",
)
LEGACY_VERIFIED_COMPLETED_STEP_NAMES = tuple(STEP_ORDER)
TERMINAL_STEP_NAMES = {"request", *STEP_ORDER}
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
    "schema_update_failed": {"schema_update_failed"},
    "health_check_failed": {"health_check_failed"},
    "commit_mismatch": {"commit_verification"},
    "commit_missing": {"commit_verification"},
    "metadata_invalid": {"commit_verification"},
    "apply_timeout": set(STEP_ORDER),
    "apply_failed": {"apply_failed"},
    "helper_exception": {"helper_exception"},
}


class HelperError(RuntimeError):
    def __init__(self, category: str, message: str, *, phase: str | None = None, diagnostics: dict[str, Any] | None = None):
        self.category = category
        self.phase = phase or category
        self.diagnostics = diagnostics or {}
        super().__init__(message)


def utcnow() -> str:
    return datetime.utcnow().isoformat() + "Z"


def safe_text(value: Any, limit: int = 300) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    return SENSITIVE_VALUE_RE.sub("***", str(value).strip())[:limit] or None


def valid_timestamp(value: Any) -> bool:
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


def parsed_timestamp(value: Any) -> datetime | None:
    if not valid_timestamp(value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def is_bounded_string(value: Any, *, minimum: int = 1, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value) <= maximum


def is_nullable_bounded_string(value: Any, *, maximum: int) -> bool:
    return value is None or is_bounded_string(value, maximum=maximum)


def is_bounded_int(value: Any, *, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def is_exact_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def is_allowed_string(value: Any, allowed: set[str] | frozenset[str]) -> bool:
    return type(value) is str and value in allowed


def is_nullable_bounded_int(value: Any, *, minimum: int, maximum: int) -> bool:
    return value is None or is_bounded_int(value, minimum=minimum, maximum=maximum)


def has_exact_keys(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def contains_sensitive_content(value: Any) -> bool:
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return True
    return bool(SENSITIVE_VALUE_RE.search(rendered))


def decode_authority_json(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise HelperError("control_file_invalid", "Update control data is unavailable or invalid.") from exc
    if not isinstance(payload, dict):
        raise HelperError("control_file_invalid", "Update control data must be a JSON object.")
    return payload


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        limit = MAX_ADMISSION_BYTES if path == REQUEST_FILE else MAX_LINEAGE_BYTES if path == LINEAGE_FILE else MAX_CONTROL_BYTES
        if path.stat().st_size > limit:
            raise HelperError("control_file_too_large", "Update control data exceeds its size limit.")
        text = path.read_text(encoding="utf-8")
        payload = decode_authority_json(text) if path in {REQUEST_FILE, LINEAGE_FILE, STATUS_FILE} else json.loads(text)
    except HelperError:
        raise
    except Exception as exc:
        raise HelperError("control_file_invalid", "Update control data is unavailable or invalid.") from exc
    if not isinstance(payload, dict):
        raise HelperError("control_file_invalid", "Update control data must be a JSON object.")
    if contains_sensitive_content(payload):
        raise HelperError("control_file_sensitive", "Update control data contains sensitive content.")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if SENSITIVE_VALUE_RE.search(rendered):
        raise HelperError("status_sensitive", "Refusing to write sensitive helper status.")
    limit = MAX_ADMISSION_BYTES if path == REQUEST_FILE else MAX_LINEAGE_BYTES if path == LINEAGE_FILE else MAX_CONTROL_BYTES
    if len(rendered.encode("utf-8")) > limit:
        raise HelperError("control_file_too_large", f"{path.name} exceeds the update-control size limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


@contextmanager
def admission_guard():
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with ADMISSION_LOCK_FILE.open("a+", encoding="utf-8") as lock_file:
        try:
            os.chmod(ADMISSION_LOCK_FILE, 0o600)
        except OSError:
            pass
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def helper_execution_lease():
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with HELPER_LEASE_FILE.open("a+", encoding="utf-8") as lease_file:
        try:
            os.chmod(HELPER_LEASE_FILE, 0o600)
        except OSError:
            pass
        try:
            fcntl.flock(lease_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lease_file.fileno(), fcntl.LOCK_UN)


def load_history() -> set[str]:
    payload = read_json(HISTORY_FILE) or {"processed_request_ids": []}
    values = payload.get("processed_request_ids")
    return {str(item) for item in values if isinstance(item, str)} if isinstance(values, list) else set()


def save_history(processed: set[str]) -> None:
    write_json(HISTORY_FILE, {"schema_version": 1, "updated_at": utcnow(), "processed_request_ids": sorted(processed)[-100:]})


def append_apply_history(status_payload: dict[str, Any]) -> None:
    try:
        existing = read_json(APPLY_HISTORY_FILE) or {"items": []}
        items = existing.get("items") if isinstance(existing.get("items"), list) else []
        entry = {
            "request_id": safe_text(status_payload.get("request_id"), 80),
            "submission_id": safe_text(status_payload.get("submission_id"), 80),
            "target_version": safe_text(status_payload.get("target_version"), 80),
            "status": safe_text(status_payload.get("status"), 40),
            "phase": safe_text(status_payload.get("phase"), 80),
            "started_at": safe_text(status_payload.get("started_at"), 80),
            "finished_at": safe_text(status_payload.get("updated_at"), 80),
            "updated_at": safe_text(status_payload.get("updated_at"), 80),
            "expected_commit": safe_text(status_payload.get("expected_commit"), 40),
            "installed_commit": safe_text(status_payload.get("installed_commit"), 40),
            "commit_verified": bool(status_payload.get("commit_verified")),
            "source": status_payload.get("source") if isinstance(status_payload.get("source"), dict) else None,
            "steps": status_payload.get("steps")[:12] if isinstance(status_payload.get("steps"), list) else [],
            "error": status_payload.get("error") if isinstance(status_payload.get("error"), dict) else None,
            "history_detail_status": "step_timestamps_unavailable",
        }
        deduped = [item for item in items if not isinstance(item, dict) or item.get("request_id") != entry["request_id"]]
        deduped.append(entry)
        write_json(APPLY_HISTORY_FILE, {"schema_version": 1, "updated_at": utcnow(), "max_items": MAX_APPLY_HISTORY_ITEMS, "items": deduped[-MAX_APPLY_HISTORY_ITEMS:]})
    except Exception:
        return


def error_payload(category: str, message: str) -> dict[str, str]:
    messages = {
        "cancelled_before_start": "Queued update apply was cancelled before helper started.",
        "helper_restart_interrupted": "Update execution was interrupted before terminal persistence.",
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
    action = "Review sanitized update status and use terminal recovery if needed."
    if category in {"build_network_dependency_failed", "jellyfin_ffmpeg_repo_unavailable"}:
        action = "External FFmpeg repository or network dependency failed during API image build. Retry after repository connectivity is restored or use the documented terminal recovery path."
    elif category == "docker_build_failed":
        action = "Docker image rebuild failed. Review sanitized update status and retry after the build cause is fixed."
    elif category == "schema_update_failed":
        action = "Review the database schema preparation failure before retrying the update."
    elif category == "compose_config_failed":
        action = "Compose configuration failed. Review server-side compose configuration before retrying."
    elif category == "health_check_failed":
        action = "Containers were recreated but API health did not recover. Review service status before retrying."
    elif category == "commit_mismatch":
        action = "Installed commit did not match trusted release evidence. Treat the update as failed and retry only after checking the release source."
    return {
        "category": safe_text(category, 80) or "helper_error",
        "message": messages.get(category, "Update helper failed."),
        "operator_action": action,
    }


def base_status(request: dict[str, Any], status: str, phase: str, steps: list[dict[str, str]], error: dict[str, str] | None = None) -> dict[str, Any]:
    source = request.get("source") if isinstance(request.get("source"), dict) else {}
    expected_commit = safe_text(source.get("commit"), 40)
    return {
        "schema_version": 1,
        "request_id": safe_text(request.get("request_id"), 80),
        "submission_id": safe_text(request.get("submission_id"), 80),
        "target_version": safe_text(source.get("version"), 80),
        "status": status,
        "phase": phase,
        "current_step": phase,
        "started_at": safe_text(request.get("requested_at"), 80),
        "updated_at": utcnow(),
        "source": {
            "kind": "github-tarball",
            "repo": safe_text(source.get("repo"), 160),
            "ref": safe_text(source.get("ref"), 120),
            "commit": expected_commit,
            "apply_ref": safe_text(source.get("apply_ref"), 40),
        },
        "expected_commit": expected_commit,
        "commit_verified": False,
        "steps": steps,
        "can_cancel": status == "queued",
        "rollback_supported": False,
        "side_effects": {
            "api_docker_socket": False,
            "api_shell_execution": False,
            "request_controlled_source": False,
            "helper_has_docker_socket": True,
            "helper_public_ports": False,
        },
        "error": error,
    }


def failed_steps(category: str, phase: str | None = None) -> list[dict[str, str]]:
    if category == "preflight_failed":
        return steps_for("preflight", failed=True)
    if category == "compose_config_failed":
        return steps_for("compose_config", failed=True)
    if category in {
        "jellyfin_ffmpeg_repo_unavailable",
        "build_network_dependency_failed",
        "docker_build_failed",
        "schema_update_failed",
    }:
        return steps_for("rebuilding", failed=True)
    if category == "apply_timeout":
        timeout_phase = phase if phase in STEP_ORDER else "rebuilding"
        return steps_for(timeout_phase, failed=True)
    if category == "apply_failed":
        return steps_for("overlay", failed=True)
    if category == "health_check_failed":
        return steps_for("health_check", failed=True)
    if category in {"commit_mismatch", "commit_missing", "metadata_invalid"}:
        return steps_for("commit_verification", failed=True)
    return steps_for("queued", failed=True)


def steps_for(current_step: str, failed: bool = False) -> list[dict[str, str]]:
    normalized = "rebuilding" if current_step == "restarting" else current_step
    if normalized not in STEP_ORDER:
        normalized = "preflight"
    current_index = STEP_ORDER.index(normalized)
    steps: list[dict[str, str]] = []
    for index, name in enumerate(STEP_ORDER):
        if index < current_index:
            state = "completed"
        elif index == current_index:
            state = "failed" if failed else "running"
        else:
            state = "pending"
        steps.append({"name": name, "status": state})
    return steps


def read_progress(request_id: str | None = None) -> dict[str, Any] | None:
    try:
        payload = read_json(PROGRESS_FILE)
    except Exception:
        return None
    if not payload:
        return None
    progress_request_id = payload.get("request_id")
    if request_id is not None:
        if type(request_id) is not str or not request_id:
            return None
        if type(progress_request_id) is not str or progress_request_id != request_id:
            return None
    return payload


def strict_request_source(value: Any, *, minimal_legacy: bool = False) -> dict[str, str] | None:
    expected_keys = LEGACY_MINIMAL_SOURCE_KEYS if minimal_legacy else REQUEST_SOURCE_KEYS
    if not has_exact_keys(value, expected_keys) or contains_sensitive_content(value):
        return None
    version = value.get("version")
    commit = value.get("commit")
    if (
        not is_bounded_string(version, maximum=80)
        or not VERSION_TEXT_RE.fullmatch(version)
        or not is_bounded_string(commit, minimum=40, maximum=40)
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
        or not is_bounded_string(channel, maximum=80)
        or not SAFE_TEXT_RE.fullmatch(channel)
        or not is_bounded_string(apply_ref, minimum=40, maximum=40)
        or not COMMIT_SHA_RE.fullmatch(apply_ref)
        or apply_ref.lower() != commit.lower()
        or not is_bounded_string(ref, maximum=120)
        or not GIT_REF_RE.fullmatch(ref)
        or ".." in ref
        or "@{" in ref
        or ref.endswith(".")
        or not is_bounded_string(repo, maximum=160)
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


def strict_apply_candidate(value: Any) -> tuple[dict[str, Any], str] | None:
    if not has_exact_keys(value, APPLY_CANDIDATE_KEYS) or contains_sensitive_content(value):
        return None
    source = value.get("source")
    snapshot = value.get("snapshot")
    compact_keys = {"available", "fresh", "age_seconds", "fresh_for_seconds"}
    if not is_allowed_string(source, {"trusted_snapshot", "live_check"}) or not isinstance(snapshot, dict):
        return None
    snapshot_keys = frozenset(snapshot)
    if snapshot_keys not in {frozenset(compact_keys), frozenset(APPLY_FRESHNESS_KEYS)}:
        return None
    if (
        not isinstance(snapshot.get("available"), bool)
        or not isinstance(snapshot.get("fresh"), bool)
        or not is_nullable_bounded_int(snapshot.get("age_seconds"), minimum=0, maximum=315_360_000)
        or not is_bounded_int(snapshot.get("fresh_for_seconds"), minimum=0, maximum=86_400)
    ):
        return None
    if snapshot_keys == frozenset(compact_keys):
        if snapshot.get("available") is not False or source != "live_check":
            return None
        profile = APPLY_CANDIDATE_PROFILE_COMPACT_READ_ONLY
    else:
        version = snapshot.get("version")
        commit_short = snapshot.get("commit_short")
        provider = snapshot.get("provider")
        if (
            not (version is None or (is_bounded_string(version, maximum=80) and VERSION_TEXT_RE.fullmatch(version)))
            or not (commit_short is None or (is_bounded_string(commit_short, maximum=12) and re.fullmatch(r"[0-9a-fA-F]+", commit_short)))
            or not (provider is None or (is_bounded_string(provider, maximum=80) and SAFE_TEXT_RE.fullmatch(provider)))
            or (source == "trusted_snapshot" and snapshot.get("available") is not True)
        ):
            return None
        profile = APPLY_CANDIDATE_PROFILE_CANONICAL_CURRENT
    return {"source": source, "snapshot": dict(snapshot)}, profile


def strict_current_actor(value: Any) -> bool:
    if not has_exact_keys(value, CURRENT_ACTOR_KEYS) or contains_sensitive_content(value):
        return False
    return bool(
        is_bounded_int(value.get("user_id"), minimum=1, maximum=9_223_372_036_854_775_807)
        and is_bounded_string(value.get("username"), maximum=100)
        and is_bounded_string(value.get("role"), maximum=50)
        and is_nullable_bounded_string(value.get("ip_address"), maximum=100)
        and is_nullable_bounded_string(value.get("user_agent"), maximum=300)
    )


def strict_legacy_actor(value: Any) -> bool:
    if not has_exact_keys(value, LEGACY_ACTOR_KEYS) or contains_sensitive_content(value):
        return False
    user_id = value.get("user_id")
    valid_user_id = is_bounded_int(user_id, minimum=1, maximum=9_223_372_036_854_775_807) or (
        is_bounded_string(user_id, maximum=20) and user_id.isdigit() and int(user_id) > 0
    )
    return bool(valid_user_id and is_bounded_string(value.get("role"), maximum=50))


def validate_request(request: dict[str, Any]) -> str:
    source = strict_request_source(request.get("source")) if isinstance(request, dict) else None
    candidate_contract = strict_apply_candidate(request.get("apply_candidate")) if isinstance(request, dict) else None
    if (
        not has_exact_keys(request, CURRENT_REQUEST_KEYS)
        or contains_sensitive_content(request)
        or not is_exact_int(request.get("schema_version"), ADMISSION_SCHEMA_VERSION)
        or request.get("intent") != "apply_update"
        or request.get("confirmed") is not True
        or request.get("preflight_required") is not True
        or request.get("status_path") != "data/update-control/update-status.json"
        or not isinstance(request.get("request_id"), str)
        or not REQUEST_ID_RE.fullmatch(request.get("request_id"))
        or not isinstance(request.get("submission_id"), str)
        or not SUBMISSION_ID_RE.fullmatch(request.get("submission_id"))
        or not valid_timestamp(request.get("requested_at"))
        or not strict_current_actor(request.get("requested_by"))
        or source is None
        or candidate_contract is None
    ):
        raise HelperError("request_contract_invalid", "Update request contract is invalid.")
    return candidate_contract[1]


def validate_legacy_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict) or contains_sensitive_content(request):
        raise HelperError("legacy_admission_invalid", "Legacy update admission is malformed.")
    keys = set(request)
    if keys == LEGACY_MINIMAL_REQUEST_KEYS:
        profile = "minimal"
    elif keys == LEGACY_HISTORICAL_REQUEST_KEYS:
        profile = "historical"
    elif keys == LEGACY_SNAPSHOT_REQUEST_KEYS:
        profile = "snapshot"
    elif keys == LEGACY_TRANSITIONAL_REQUEST_KEYS:
        profile = "transitional"
    elif keys == SCHEMA_RETRY_REQUEST_KEYS:
        profile = "schema_retry"
    else:
        raise HelperError("legacy_admission_invalid", "Legacy update admission is malformed.")
    source = strict_request_source(request.get("source"), minimal_legacy=profile == "minimal")
    request_id = request.get("request_id")
    if (
        not is_exact_int(request.get("schema_version"), 1)
        or request.get("intent") != "apply_update"
        or request.get("confirmed") is not True
        or not isinstance(request_id, str)
        or not LEGACY_REQUEST_ID_RE.fullmatch(request_id)
        or not valid_timestamp(request.get("requested_at"))
        or source is None
    ):
        raise HelperError("legacy_admission_invalid", "Legacy update admission is malformed.")
    if profile != "minimal" and (
        not (
            strict_legacy_actor(request.get("requested_by"))
            or (
                profile == "schema_retry"
                and strict_current_actor(request.get("requested_by"))
            )
        )
        or request.get("preflight_required") is not True
        or request.get("status_path") != "data/update-control/update-status.json"
    ):
        raise HelperError("legacy_admission_invalid", "Legacy update admission is malformed.")
    if profile in {"snapshot", "transitional"} and strict_apply_candidate(request.get("apply_candidate")) is None:
        raise HelperError("legacy_admission_invalid", "Legacy update admission is malformed.")
    if profile == "schema_retry" and (
        not REQUEST_ID_RE.fullmatch(request_id)
        or not isinstance(request.get("retry_of_request_id"), str)
        or not LEGACY_REQUEST_ID_RE.fullmatch(
            request.get("retry_of_request_id")
        )
        or request.get("retry_of_request_id") == request_id
        or not isinstance(request.get("migration_attempt_id"), str)
        or not MIGRATION_ATTEMPT_RE.fullmatch(
            request.get("migration_attempt_id")
        )
    ):
        raise HelperError(
            "legacy_admission_invalid",
            "Schema retry admission is malformed.",
        )
    submission_id = request.get("submission_id") if profile == "transitional" else None
    if submission_id is not None and (not isinstance(submission_id, str) or not SUBMISSION_ID_RE.fullmatch(submission_id)):
        raise HelperError("legacy_admission_invalid", "Legacy update admission is malformed.")
    return {
        "request_id": request_id,
        "submission_id": submission_id.lower() if submission_id else None,
        "target_version": source["version"],
        "target_commit": source["commit"],
        "requested_at": request["requested_at"],
        "payload": request,
        "legacy_profile": profile,
    }


def validate_lineage_marker() -> str:
    marker = read_json(LINEAGE_FILE)
    if marker is None:
        return "missing"
    if (
        not has_exact_keys(marker, set(LINEAGE_PAYLOAD))
        or not is_exact_int(marker.get("schema_version"), 1)
        or marker.get("document_type") != LINEAGE_PAYLOAD["document_type"]
        or marker.get("initialized") is not True
    ):
        raise HelperError("admission_lineage_invalid", "Update admission lineage is invalid.")
    return "valid"


def deterministic_audit_event_id(request_id: str) -> str:
    return str(uuid.uuid5(AUDIT_NAMESPACE, request_id))


def validate_admission_document(payload: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    if payload is None:
        return "missing", None
    raw_schema_version = payload.get("schema_version")
    if is_exact_int(raw_schema_version, 1):
        validate_legacy_request(payload)
        return "legacy", None
    if (
        not has_exact_keys(payload, ADMISSION_DOCUMENT_KEYS)
        or contains_sensitive_content(payload)
        or not is_exact_int(raw_schema_version, ADMISSION_SCHEMA_VERSION)
        or payload.get("document_type") != ADMISSION_DOCUMENT_TYPE
        or not valid_timestamp(payload.get("updated_at"))
    ):
        raise HelperError("admission_schema_unsupported", "Update admission document schema is unsupported.")
    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ADMISSION_ENTRIES:
        raise HelperError("admission_entries_invalid", "Update admission entries are invalid.")
    by_submission: dict[str, dict[str, Any]] = {}
    request_ids: set[str] = set()
    for raw in entries:
        if (
            not has_exact_keys(raw, ADMISSION_ENTRY_KEYS)
            or contains_sensitive_content(raw)
            or not is_allowed_string(raw.get("state"), ADMISSION_STATES)
        ):
            raise HelperError("admission_entry_invalid", "Update admission entry is invalid.")
        request = raw.get("request")
        if not isinstance(request, dict):
            raise HelperError("admission_request_missing", "Update admission request is missing.")
        candidate_profile = validate_request(request)
        submission_value = raw.get("submission_id")
        request_id = raw.get("request_id")
        target_commit = raw.get("target_commit")
        submission_id = submission_value.lower() if isinstance(submission_value, str) else ""
        source = request.get("source") if isinstance(request.get("source"), dict) else {}
        if (
            not SUBMISSION_ID_RE.fullmatch(submission_id)
            or not isinstance(request.get("submission_id"), str)
            or submission_id != request.get("submission_id").lower()
            or not isinstance(request_id, str)
            or not REQUEST_ID_RE.fullmatch(request_id)
            or request_id != request.get("request_id")
            or raw.get("target_version") != source.get("version")
            or not isinstance(target_commit, str)
            or target_commit.lower() != str(source.get("commit") or "").lower()
            or raw.get("requested_at") != request.get("requested_at")
            or not valid_timestamp(raw.get("updated_at"))
            or submission_id in by_submission
            or request_id in request_ids
        ):
            raise HelperError("admission_identity_invalid", "Update admission identity is contradictory.")
        audit = raw.get("audit")
        if not has_exact_keys(audit, AUDIT_KEYS):
            raise HelperError("admission_audit_invalid", "Update admission audit state is invalid.")
        audit_state = audit.get("state")
        if audit.get("event_id") != deterministic_audit_event_id(request_id):
            raise HelperError("admission_audit_invalid", "Update admission audit identity is contradictory.")
        if not is_allowed_string(audit_state, {"pending", "confirmed"}):
            raise HelperError("admission_audit_invalid", "Update admission audit state is invalid.")
        if raw.get("state") == "audit_pending" and (audit_state != "pending" or audit.get("confirmed_at") is not None):
            raise HelperError("admission_audit_invalid", "Pending admission audit state is contradictory.")
        if raw.get("state") != "audit_pending" and (
            audit_state != "confirmed" or not valid_timestamp(audit.get("confirmed_at"))
        ):
            raise HelperError("admission_audit_invalid", "Executable admission lacks accepted audit confirmation.")
        state = raw.get("state")
        if state in NON_TERMINAL_ADMISSION_STATES and candidate_profile != APPLY_CANDIDATE_PROFILE_CANONICAL_CURRENT:
            raise HelperError("admission_candidate_profile_invalid", "Update admission candidate is read-only.")
        claimed_at = raw.get("claimed_at")
        terminal = raw.get("terminal")
        requested_time = parsed_timestamp(request.get("requested_at"))
        updated_time = parsed_timestamp(raw.get("updated_at"))
        confirmed_time = parsed_timestamp(audit.get("confirmed_at")) if audit.get("confirmed_at") is not None else None
        claimed_time = parsed_timestamp(claimed_at) if claimed_at is not None else None
        if requested_time is None or updated_time is None or updated_time < requested_time:
            raise HelperError("admission_timestamp_invalid", "Update admission timestamps are contradictory.")
        if confirmed_time is not None and (confirmed_time < requested_time or updated_time < confirmed_time):
            raise HelperError("admission_timestamp_invalid", "Update admission timestamps are contradictory.")
        if state in {"audit_pending", "admitted_unclaimed"} and (claimed_at is not None or terminal is not None):
            raise HelperError("admission_claim_invalid", "Unclaimed admission contains claim or terminal truth.")
        if state == "claimed" and (claimed_time is None or terminal is not None):
            raise HelperError("admission_claim_invalid", "Claimed admission lacks exact claim state.")
        if claimed_time is not None and (claimed_time < (confirmed_time or requested_time) or updated_time < claimed_time):
            raise HelperError("admission_timestamp_invalid", "Update admission timestamps are contradictory.")
        if state == "terminal":
            if not isinstance(terminal, dict) or not terminal_status_for_request(terminal, request):
                raise HelperError("admission_terminal_invalid", "Terminal admission summary is malformed or unbound.")
            terminal_status = terminal.get("status")
            if terminal_status == "cancelled":
                if claimed_at is not None:
                    raise HelperError("admission_claim_invalid", "Cancelled admission cannot carry a helper claim.")
            elif is_allowed_string(terminal_status, {"failed", "completed"}):
                if claimed_time is None:
                    raise HelperError("admission_claim_invalid", "Helper terminal truth requires a prior claim.")
            else:
                raise HelperError("admission_terminal_invalid", "Terminal admission summary is malformed or unbound.")
            finished_time = parsed_timestamp(terminal.get("finished_at"))
            if finished_time is None or updated_time < finished_time or (claimed_time is not None and finished_time < claimed_time):
                raise HelperError("admission_timestamp_invalid", "Update admission timestamps are contradictory.")
        elif terminal is not None:
            raise HelperError("admission_terminal_invalid", "Non-terminal admission contains terminal truth.")
        by_submission[submission_id] = raw
        request_ids.add(request_id)
    current_id = payload.get("current_submission_id")
    if current_id is not None:
        if not isinstance(current_id, str) or not SUBMISSION_ID_RE.fullmatch(current_id):
            raise HelperError("admission_current_invalid", "Current update admission identity is invalid.")
        current_id = current_id.lower()
        if current_id not in by_submission:
            raise HelperError("admission_current_invalid", "Current update admission entry is missing.")
    non_terminal = [raw for raw in entries if raw.get("state") in NON_TERMINAL_ADMISSION_STATES]
    if len(non_terminal) > 1:
        raise HelperError("admission_topology_invalid", "Update admission topology is contradictory.")
    if non_terminal and current_id != str(non_terminal[0].get("submission_id") or "").lower():
        raise HelperError("admission_topology_invalid", "Update admission topology is contradictory.")
    if current_id is None and non_terminal:
        raise HelperError("admission_topology_invalid", "Update admission topology is contradictory.")
    document_updated = parsed_timestamp(payload.get("updated_at"))
    if document_updated is None or any(document_updated < parsed_timestamp(raw.get("updated_at")) for raw in entries):
        raise HelperError("admission_timestamp_invalid", "Update admission timestamps are contradictory.")
    return "current", {
        "payload": payload,
        "current_submission_id": current_id,
        "current": by_submission.get(current_id),
        "by_submission": by_submission,
    }


def read_admission_authority() -> tuple[str, dict[str, Any] | None]:
    marker_state = validate_lineage_marker()
    contract, document = validate_admission_document(read_json(REQUEST_FILE))
    if contract == "current":
        if marker_state != "valid":
            raise HelperError("admission_lineage_incomplete", "Update admission lineage is incomplete.")
        return contract, document
    if contract == "legacy":
        return contract, None
    footprints = any(
        path.exists()
        for path in (STATUS_FILE, PROGRESS_FILE, APPLY_HISTORY_FILE, HISTORY_FILE, CONTROL_DIR / "update.lock")
    )
    if marker_state == "valid" or footprints:
        raise HelperError("admission_missing_unexpected", "Update admission authority is unexpectedly missing.")
    return "missing", None


def write_admission_document(payload: dict[str, Any]) -> None:
    if validate_lineage_marker() != "valid":
        raise HelperError("admission_lineage_incomplete", "Update admission lineage is incomplete.")
    contract, document = validate_admission_document(payload)
    if contract != "current" or not document:
        raise HelperError("admission_topology_invalid", "Update admission topology is contradictory.")
    write_json(REQUEST_FILE, payload)


def replace_admission_entry(payload: dict[str, Any], replacement: dict[str, Any]) -> None:
    for index, raw in enumerate(payload.get("entries") or []):
        if raw.get("submission_id") == replacement.get("submission_id"):
            payload["entries"][index] = replacement
            payload["updated_at"] = utcnow()
            return
    raise HelperError("admission_entry_missing", "Current update admission entry is missing.")


def status_matches_request(payload: dict[str, Any] | None, request: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    source = request.get("source") if isinstance(request.get("source"), dict) else {}
    legacy = is_exact_int(request.get("schema_version"), 1)
    payload_submission = payload.get("submission_id")
    request_submission = request.get("submission_id")
    if payload_submission is not None and not isinstance(payload_submission, str):
        return False
    if request_submission is not None and not isinstance(request_submission, str):
        return False
    target_version = payload.get("target_version")
    expected_commit = payload.get("expected_commit")
    return (
        is_exact_int(payload.get("schema_version"), 1)
        and isinstance(payload.get("request_id"), str)
        and payload.get("request_id") == request.get("request_id")
        and (payload_submission or "").lower() == (request_submission or "").lower()
        and (target_version == source.get("version") or (legacy and target_version is None))
        and isinstance(expected_commit, str)
        and expected_commit.lower() == str(source.get("commit") or "").lower()
    )


def strict_terminal_source(value: Any, request: dict[str, Any]) -> bool:
    if not has_exact_keys(value, TERMINAL_SOURCE_KEYS) or contains_sensitive_content(value):
        return False
    request_source = request.get("source")
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
        and commit.lower() == str(request_source.get("commit") or "").lower()
        and isinstance(apply_ref, str)
        and COMMIT_SHA_RE.fullmatch(apply_ref)
        and apply_ref.lower() == str(request_source.get("commit") or "").lower()
    )


def strict_terminal_steps(value: Any) -> bool:
    if not isinstance(value, list) or not value or len(value) > MAX_TERMINAL_STEPS:
        return False
    names: set[str] = set()
    for item in value:
        if not has_exact_keys(item, TERMINAL_STEP_KEYS):
            return False
        name = item.get("name")
        status = item.get("status")
        if (
            not is_allowed_string(name, TERMINAL_STEP_NAMES)
            or not is_allowed_string(status, TERMINAL_STEP_STATUSES)
            or name in names
        ):
            return False
        names.add(name)
    return True


def strict_legacy_historical_completed_steps(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != len(LEGACY_HISTORICAL_COMPLETED_STEP_NAMES):
        return False
    return all(
        has_exact_keys(item, TERMINAL_STEP_KEYS)
        and item.get("name") == expected_name
        and item.get("status") == "completed"
        for item, expected_name in zip(value, LEGACY_HISTORICAL_COMPLETED_STEP_NAMES)
    )


def strict_legacy_verified_completed_steps(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != len(LEGACY_VERIFIED_COMPLETED_STEP_NAMES):
        return False
    return all(
        has_exact_keys(item, TERMINAL_STEP_KEYS)
        and item.get("name") == expected_name
        and item.get("status") == "completed"
        for item, expected_name in zip(value, LEGACY_VERIFIED_COMPLETED_STEP_NAMES)
    )


def strict_terminal_error(value: Any) -> str | None:
    if not has_exact_keys(value, TERMINAL_ERROR_KEYS):
        return None
    category = value.get("category")
    message = value.get("message")
    action = value.get("operator_action")
    if (
        not is_bounded_string(category, maximum=80)
        or not MACHINE_CODE_RE.fullmatch(category)
        or not is_bounded_string(message, maximum=300)
        or not is_bounded_string(action, maximum=300)
        or not message.strip()
        or not action.strip()
        or SENSITIVE_VALUE_RE.search(message)
        or SENSITIVE_VALUE_RE.search(action)
        or UNSAFE_PUBLIC_TEXT_RE.search(message)
        or UNSAFE_PUBLIC_TEXT_RE.search(action)
    ):
        return None
    return category


def strict_terminal_side_effects(value: Any) -> bool:
    return bool(
        has_exact_keys(value, TERMINAL_SIDE_EFFECT_KEYS)
        and value.get("api_docker_socket") is False
        and value.get("api_shell_execution") is False
        and value.get("request_controlled_source") is False
        and value.get("helper_has_docker_socket") is True
        and value.get("helper_public_ports") is False
    )


def strict_terminal_release_identity(value: Any) -> bool:
    return bool(
        has_exact_keys(value, TERMINAL_RELEASE_IDENTITY_KEYS)
        and value.get("host_metadata_status") == "complete"
        and value.get("api_metadata_status") == "complete"
        and value.get("api_visible") is True
        and value.get("commit_verified") is True
    )


def terminal_shape(payload: dict[str, Any], request: dict[str, Any]) -> str | None:
    keys = set(payload)
    legacy = is_exact_int(request.get("schema_version"), 1)
    status_value = payload.get("status")
    if legacy and keys == LEGACY_MINIMAL_TERMINAL_KEYS:
        return "legacy_minimal"
    if legacy and status_value == "completed" and keys == LEGACY_HISTORICAL_COMPLETED_TERMINAL_KEYS:
        return "legacy_historical_completed"
    if legacy and status_value == "completed" and keys == LEGACY_VERIFIED_COMPLETED_TERMINAL_KEYS:
        return "legacy_verified_completed"
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


def terminal_status_for_request(payload: dict[str, Any], request: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or contains_sensitive_content(payload) or not status_matches_request(payload, request):
        return False
    shape = terminal_shape(payload, request)
    if shape is None:
        return False
    if is_exact_int(request.get("schema_version"), ADMISSION_SCHEMA_VERSION):
        candidate_contract = strict_apply_candidate(request.get("apply_candidate"))
        if candidate_contract is None:
            return False
        _candidate, candidate_profile = candidate_contract
        if candidate_profile == APPLY_CANDIDATE_PROFILE_COMPACT_READ_ONLY and shape != "pre_closeout_cancel":
            return False
    status_value = payload.get("status")
    phase = payload.get("phase")
    current_step = payload.get("current_step")
    if not is_bounded_string(phase, maximum=80) or not is_bounded_string(current_step, maximum=80):
        return False
    started_at = payload.get("started_at")
    updated_at = payload.get("updated_at")
    finished_at = (
        payload.get("updated_at")
        if shape in {"legacy_historical_completed", "legacy_verified_completed"}
        else payload.get("finished_at")
    )
    started_time = parsed_timestamp(started_at)
    updated_time = parsed_timestamp(updated_at)
    finished_time = parsed_timestamp(finished_at)
    requested_time = parsed_timestamp(request.get("requested_at"))
    if (
        started_time is None
        or updated_time is None
        or finished_time is None
        or requested_time is None
        or started_time < requested_time
        or updated_time < started_time
        or finished_time != updated_time
    ):
        return False
    if shape == "helper" and (
        not strict_terminal_source(payload.get("source"), request)
        or not strict_terminal_steps(payload.get("steps"))
        or not strict_terminal_side_effects(payload.get("side_effects"))
        or payload.get("can_cancel") is not False
        or payload.get("rollback_supported") is not False
    ):
        return False
    if shape == "legacy_historical_completed" and (
        not strict_terminal_source(payload.get("source"), request)
        or not strict_legacy_historical_completed_steps(payload.get("steps"))
        or not strict_terminal_side_effects(payload.get("side_effects"))
        or payload.get("can_cancel") is not False
        or payload.get("rollback_supported") is not False
    ):
        return False
    if shape == "legacy_verified_completed" and (
        not strict_terminal_source(payload.get("source"), request)
        or not strict_legacy_verified_completed_steps(payload.get("steps"))
        or not strict_terminal_side_effects(payload.get("side_effects"))
        or not strict_terminal_release_identity(payload.get("release_identity"))
        or payload.get("can_cancel") is not False
        or payload.get("rollback_supported") is not False
    ):
        return False
    if shape == "pre_closeout_cancel" and (
        strict_request_source(payload.get("source")) is None
        or strict_apply_candidate(payload.get("apply_candidate")) is None
        or payload.get("source") != request.get("source")
        or payload.get("apply_candidate") != request.get("apply_candidate")
        or not strict_terminal_steps(payload.get("steps"))
        or payload.get("can_cancel") is not False
        or payload.get("rollback_supported") is not False
    ):
        return False
    installed_commit = payload.get("installed_commit")
    if installed_commit is not None and (not isinstance(installed_commit, str) or not COMMIT_SHA_RE.fullmatch(installed_commit)):
        return False
    error_category = strict_terminal_error(payload.get("error"))
    if status_value == "completed":
        source = request.get("source") or {}
        return bool(
            phase == current_step
            and phase in {"completed", "commit_verification"}
            and payload.get("commit_verified") is True
            and isinstance(installed_commit, str)
            and installed_commit.lower() == str(source.get("commit") or "").lower()
            and payload.get("error") is None
            and (shape != "helper" or strict_terminal_release_identity(payload.get("release_identity")))
        )
    if status_value == "cancelled":
        return bool(
            phase == "cancelled"
            and current_step == "cancelled"
            and payload.get("commit_verified") is False
            and ("installed_commit" not in payload or installed_commit is None)
            and error_category == "cancelled_before_start"
        )
    allowed_phases = TERMINAL_FAILURE_PHASES.get(error_category or "")
    return bool(
        status_value == "failed"
        and payload.get("commit_verified") is False
        and allowed_phases
        and phase == current_step
        and phase in allowed_phases
    )


def _publish_terminal_locked(document: dict[str, Any], entry: dict[str, Any], status_payload: dict[str, Any]) -> None:
    request = entry.get("request")
    if not isinstance(request, dict) or not terminal_status_for_request(status_payload, request):
        raise HelperError("terminal_truth_invalid", "Helper terminal truth is invalid or unbound.")
    if entry.get("state") == "terminal":
        existing = entry.get("terminal")
        if existing != status_payload:
            raise HelperError("terminal_truth_contradictory", "Helper terminal truth is already different.")
        return
    if entry.get("state") != "claimed":
        raise HelperError("terminal_without_claim", "Helper cannot publish terminal truth without an exact claim.")
    payload = json.loads(json.dumps(document["payload"]))
    replacement = json.loads(json.dumps(entry))
    replacement["state"] = "terminal"
    replacement["terminal"] = status_payload
    replacement["updated_at"] = status_payload.get("finished_at") or status_payload.get("updated_at") or utcnow()
    replace_admission_entry(payload, replacement)
    write_admission_document(payload)
    write_json(STATUS_FILE, status_payload)


def publish_terminal(request: dict[str, Any], status_payload: dict[str, Any]) -> None:
    status_payload = json.loads(json.dumps(status_payload))
    status_payload["can_cancel"] = False
    status_payload["finished_at"] = status_payload.get("finished_at") or status_payload.get("updated_at") or utcnow()
    with admission_guard():
        contract, document = read_admission_authority()
        if contract != "current" or not document or not document.get("current"):
            raise HelperError("admission_current_missing", "Claimed update admission is unavailable.")
        entry = document["current"]
        if entry.get("request_id") != request.get("request_id") or entry.get("submission_id") != request.get("submission_id"):
            raise HelperError("admission_claim_changed", "Claimed update admission identity changed.")
        _publish_terminal_locked(document, entry, status_payload)
    append_apply_history(status_payload)


def claim_current_request() -> dict[str, Any] | None:
    with admission_guard():
        contract, document = read_admission_authority()
        if contract in {"missing", "legacy"} or not document or not document.get("current"):
            return None
        entry = document["current"]
        state = entry.get("state")
        request = entry.get("request")
        if state in {"audit_pending", "terminal", "unknown"}:
            return None
        if state == "claimed":
            interrupted = base_status(
                request,
                "failed",
                "helper_restart_interrupted",
                steps_for("preflight", failed=True),
                error_payload(
                    "helper_restart_interrupted",
                    "Update helper ownership ended before terminal persistence. The claimed request was not run again.",
                ),
            )
            interrupted["finished_at"] = interrupted["updated_at"]
            _publish_terminal_locked(document, entry, interrupted)
            append_apply_history(interrupted)
            return None
        if state != "admitted_unclaimed":
            raise HelperError("admission_state_invalid", "Current update admission state is not executable.")
        candidate_profile = validate_request(request)
        if candidate_profile != APPLY_CANDIDATE_PROFILE_CANONICAL_CURRENT:
            raise HelperError("admission_candidate_profile_invalid", "Update admission candidate is read-only.")
        status = read_json(STATUS_FILE)
        if status_matches_request(status, request) and is_allowed_string(status.get("status"), TERMINAL):
            raise HelperError("unclaimed_terminal_contradiction", "Unclaimed admission has terminal status evidence.")
        payload = json.loads(json.dumps(document["payload"]))
        replacement = json.loads(json.dumps(entry))
        claimed_at = utcnow()
        replacement["state"] = "claimed"
        replacement["claimed_at"] = claimed_at
        replacement["updated_at"] = claimed_at
        replace_admission_entry(payload, replacement)
        write_admission_document(payload)
        starting = base_status(request, "starting_helper", "starting_helper", steps_for("queued"))
        starting["can_cancel"] = False
        write_json(STATUS_FILE, starting)
        return json.loads(json.dumps(request))


def compose_app_dir() -> Path:
    if HOST_APP_DIR is None:
        raise HelperError("helper_host_app_dir_missing", "KM_VMS_UPDATE_HOST_APP_DIR must be configured for Docker socket compose operations.")
    if not HOST_APP_DIR.is_absolute():
        raise HelperError("helper_host_app_dir_invalid", "KM_VMS_UPDATE_HOST_APP_DIR must be an absolute host path.")
    if not (HOST_APP_DIR / "docker-compose.yml").is_file() or not (HOST_APP_DIR / "scripts" / "update.sh").is_file():
        raise HelperError("helper_host_app_dir_unmounted", "KM_VMS_UPDATE_HOST_APP_DIR is not mounted inside update-helper.")
    return HOST_APP_DIR


def update_child_env(request: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["KM_VMS_UPDATE_HELPER_MODE"] = "1"
    env["KM_VMS_UPDATE_CONTROL_REQUEST_ID"] = str(request["request_id"])
    env["KM_VMS_UPDATE_PROGRESS_FILE"] = str(PROGRESS_FILE)
    helper_compose = os.getenv("KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE", "").strip()
    if helper_compose:
        env["KM_VMS_DOCKER_COMPOSE"] = helper_compose
    else:
        inherited = env.get("KM_VMS_DOCKER_COMPOSE", "").strip()
        if inherited.startswith("/") and not Path(inherited).exists():
            # Host-only NAS compose paths are valid for terminal update, but not
            # from inside update-helper. Let update.sh detect container compose.
            env.pop("KM_VMS_DOCKER_COMPOSE", None)
    return env


def classify_apply_failure(update_dir: Path, stderr: str) -> HelperError:
    try:
        metadata = read_json(update_dir / ".km-vms-update.json")
    except HelperError:
        metadata = None
    failed_phase = metadata.get("failed_phase") if metadata else None
    error_text = " ".join(
        str(part or "")
        for part in [
            stderr,
            metadata.get("error_message") if metadata else "",
            failed_phase or "",
        ]
    )
    lowered = error_text.lower()
    if failed_phase == "health_check":
        return HelperError("health_check_failed", "Update health check failed.")
    if failed_phase == "compose_config":
        return HelperError("compose_config_failed", "Docker Compose configuration validation failed.")
    if failed_phase == "schema_update":
        return HelperError(
            "schema_update_failed",
            "Database schema preparation failed during update apply.",
        )
    if failed_phase == "rebuild_recreate":
        if any(token in lowered for token in ("jellyfin", "repo.jellyfin.org", "jellyfin_team.gpg.key", "jellyfin-ffmpeg")):
            return HelperError(
                "jellyfin_ffmpeg_repo_unavailable",
                "External Jellyfin FFmpeg repository/key download or apt install failed or timed out during API image build. Source overlay may already have been applied if the failure happened after precompose.",
            )
        if any(token in lowered for token in ("curl", "apt-get", "timeout", "timed out", "temporary failure", "could not resolve", "connection")):
            return HelperError(
                "build_network_dependency_failed",
                "A network dependency failed or timed out during Docker image build. Source overlay may already have been applied if the failure happened after precompose.",
            )
        return HelperError("docker_build_failed", "Docker image rebuild failed during update apply.")
    if any(token in lowered for token in ("jellyfin", "repo.jellyfin.org", "jellyfin_team.gpg.key", "jellyfin-ffmpeg")):
        return HelperError("jellyfin_ffmpeg_repo_unavailable", "External Jellyfin FFmpeg repository/key download or apt install failed or timed out during API image build.")
    if any(token in lowered for token in ("docker build", "build failed", "compose rebuild")):
        return HelperError("docker_build_failed", "Docker image rebuild failed during update apply.")
    return HelperError("apply_failed", "Update apply failed.")


def verify_installed_commit(update_dir: Path, expected_commit: str) -> tuple[str, str, dict[str, Any]]:
    update_metadata = read_json(update_dir / ".km-vms-update.json")
    if not update_metadata:
        raise HelperError("commit_missing", "Update metadata is missing after successful apply.", phase="commit_verification")
    if update_metadata.get("status") != "success":
        raise HelperError("metadata_invalid", "Update metadata did not record a successful apply.", phase="commit_verification")
    installed_commit = safe_text(update_metadata.get("commit_sha"), 40)
    if installed_commit != expected_commit:
        raise HelperError("commit_mismatch", "Installed update commit does not match the trusted manifest commit.", phase="commit_verification", diagnostics={"installed_commit": installed_commit or "missing"})
    source_metadata = read_json(update_dir / ".km-vms-source.json")
    if source_metadata:
        source_commit = safe_text(source_metadata.get("commit_sha"), 40)
        if source_commit and source_commit != expected_commit:
            raise HelperError("commit_mismatch", "Installed source commit does not match the trusted manifest commit.", phase="commit_verification", diagnostics={"installed_commit": source_commit})
    release_metadata = read_json(update_dir / ".km-vms-release.json")
    if not release_metadata:
        raise HelperError("commit_missing", "Installed release identity is missing after successful apply.", phase="commit_verification")
    release_commit = safe_text(release_metadata.get("commit_sha"), 40)
    if release_commit != expected_commit:
        raise HelperError("commit_mismatch", "Installed release identity commit does not match the trusted manifest commit.", phase="commit_verification", diagnostics={"installed_commit": release_commit or "missing"})
    validation = update_metadata.get("validation_summary") if isinstance(update_metadata.get("validation_summary"), dict) else {}
    host_identity_status = safe_text(validation.get("release_identity_host_metadata_status"), 40) or safe_text(release_metadata.get("metadata_status"), 40)
    api_identity_status = safe_text(validation.get("release_identity_api_metadata_status"), 40)
    api_visible = validation.get("release_identity_api_visible") is True
    identity_commit_verified = validation.get("release_identity_commit_verified") is True
    if host_identity_status != "complete":
        raise HelperError("metadata_invalid", "Host release identity is not complete after successful apply.", phase="commit_verification", diagnostics={"installed_commit": release_commit or "missing"})
    if api_identity_status != "complete" or not api_visible or not identity_commit_verified:
        raise HelperError("metadata_invalid", "API-visible release identity was not confirmed complete after successful apply.", phase="commit_verification", diagnostics={"installed_commit": release_commit or "missing"})
    release_identity = {
        "host_metadata_status": host_identity_status,
        "api_metadata_status": api_identity_status,
        "api_visible": api_visible,
        "commit_verified": identity_commit_verified,
    }
    return installed_commit, expected_commit, release_identity


def run_update(request: dict[str, Any]) -> int:
    source = request["source"]
    update_dir = compose_app_dir()
    expected_commit = str(source["commit"])
    common = ["sh", "scripts/update.sh", "--github-repo", source["repo"], "--branch", source["apply_ref"], "--yes"]
    if os.getenv("KM_VMS_GITHUB_PRIVATE", "0") == "1" or os.getenv("KMVMS_UPDATE_SOURCE_PRIVATE", "0") == "1":
        common.append("--github-private")
    env = update_child_env(request)
    try:
        PROGRESS_FILE.unlink()
    except FileNotFoundError:
        pass
    steps = steps_for("preflight")
    write_json(STATUS_FILE, base_status(request, "preflight", "preflight", steps))
    dry = run_child_with_progress([*common, "--dry-run"], request, update_dir, env, timeout_seconds=1800, default_step="preflight", status_value="preflight")
    if dry.returncode != 0:
        raise HelperError("preflight_failed", "Update preflight failed.")
    write_json(STATUS_FILE, base_status(request, "applying", "acquire_source", steps_for("acquire_source")))
    apply = run_child_with_progress(common, request, update_dir, env, timeout_seconds=7200, default_step="acquire_source", status_value="applying")
    if apply.returncode != 0:
        raise classify_apply_failure(update_dir, apply.stderr.strip())
    write_json(STATUS_FILE, base_status(request, "applying", "commit_verification", steps_for("commit_verification")))
    installed_commit, expected_commit, release_identity = verify_installed_commit(update_dir, expected_commit)
    steps = [{"name": name, "status": "completed"} for name in STEP_ORDER]
    completed = base_status(request, "completed", "completed", steps)
    completed["commit_verified"] = True
    completed["installed_commit"] = installed_commit
    completed["expected_commit"] = expected_commit
    completed["release_identity"] = release_identity
    completed["finished_at"] = completed["updated_at"]
    publish_terminal(request, completed)
    return 0


def run_child_with_progress(command: list[str], request: dict[str, Any], update_dir: Path, env: dict[str, str], *, timeout_seconds: int, default_step: str, status_value: str) -> subprocess.CompletedProcess[str]:
    request_id = str(request.get("request_id") or "")
    started = time.monotonic()
    stderr_path: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    last_step = default_step if default_step in STEP_ORDER else "preflight"
    try:
        with tempfile.NamedTemporaryFile("w+b", prefix="km-vms-update-stderr-", delete=False) as stderr_file:
            stderr_path = Path(stderr_file.name)
            process = subprocess.Popen(
                command,
                cwd=update_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                start_new_session=True,
            )
            while True:
                if process.poll() is not None:
                    break
                if time.monotonic() - started > timeout_seconds:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        process.kill()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    try:
                        stderr_file.flush()
                    except OSError:
                        pass
                    raise HelperError(
                        "apply_timeout",
                        "Update helper child process exceeded the bounded timeout.",
                        phase=last_step,
                    )
                progress = read_progress(request_id)
                step = safe_text(progress.get("current_step") if progress else default_step, 80) or default_step
                phase = safe_text(progress.get("phase") if progress else step, 80) or step
                if step in STEP_ORDER:
                    last_step = step
                elif phase in STEP_ORDER:
                    last_step = phase
                status_payload = base_status(request, status_value, phase, steps_for(step))
                status_payload["current_step"] = step
                write_json(STATUS_FILE, status_payload)
                time.sleep(POLL_SECONDS)
        stderr_tail = read_stderr_tail(stderr_path)
        return subprocess.CompletedProcess(command, process.returncode if process else 1, "", stderr_tail)
    finally:
        if stderr_path:
            try:
                stderr_path.unlink()
            except FileNotFoundError:
                pass


def read_stderr_tail(path: Path, *, limit: int = 1200) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > limit * 4:
                stream.seek(max(0, size - limit * 4))
            data = stream.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return safe_text(text[-limit:], limit) or ""


def should_process(request: dict[str, Any], processed: set[str]) -> bool:
    """Compatibility predicate only; authoritative claim is claim_current_request()."""
    request_id = str(request.get("request_id") or "")
    if not request_id or request_id in processed:
        return False
    status = read_json(STATUS_FILE)
    if status and status.get("request_id") == request_id and is_allowed_string(status.get("status"), TERMINAL):
        processed.add(request_id)
        save_history(processed)
        return False
    return True


def main() -> int:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        with helper_execution_lease() as lease_acquired:
            if not lease_acquired:
                time.sleep(POLL_SECONDS)
                continue
            request: dict[str, Any] | None = None
            try:
                request = claim_current_request()
                if request:
                    run_update(request)
                    processed = load_history()
                    processed.add(str(request["request_id"]))
                    save_history(processed)
            except HelperError as exc:
                if request:
                    failed = base_status(request, "failed", exc.phase, failed_steps(exc.category, exc.phase), error_payload(exc.category, str(exc)))
                    if exc.diagnostics.get("installed_commit"):
                        failed["installed_commit"] = safe_text(exc.diagnostics.get("installed_commit"), 40)
                    failed["finished_at"] = failed["updated_at"]
                    try:
                        publish_terminal(request, failed)
                        processed = load_history()
                        processed.add(str(request["request_id"]))
                        save_history(processed)
                    except HelperError:
                        pass
            except Exception:
                if request:
                    failed = base_status(
                        request,
                        "failed",
                        "helper_exception",
                        steps_for("preflight", failed=True),
                        error_payload("helper_exception", "Unexpected helper execution failure."),
                    )
                    failed["finished_at"] = failed["updated_at"]
                    try:
                        publish_terminal(request, failed)
                    except HelperError:
                        pass
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
