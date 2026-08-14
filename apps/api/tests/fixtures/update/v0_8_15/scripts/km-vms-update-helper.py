#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import importlib.util
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import fcntl

APP_DIR = Path(os.getenv("KM_VMS_UPDATE_APP_DIR") or "/host-app")
HOST_APP_DIR_RAW = os.getenv("KM_VMS_UPDATE_HOST_APP_DIR") or ""
HOST_APP_DIR = Path(HOST_APP_DIR_RAW) if HOST_APP_DIR_RAW else None
CONTROL_DIR = APP_DIR / "data" / "update-control"
RESTORE_CONTROL_DIR = APP_DIR / "data" / "restore-control"
RESTORE_PUBLIC_DIR = APP_DIR / "data" / "restore-public"
MAINTENANCE_CONTROL_DIR = APP_DIR / "data" / "maintenance-control"
REQUEST_FILE = CONTROL_DIR / "update-request.json"
STATUS_FILE = CONTROL_DIR / "update-status.json"
PROGRESS_FILE = CONTROL_DIR / "update-progress.json"
APPLY_HISTORY_FILE = CONTROL_DIR / "update-apply-history.json"
ACTIVATION_JOURNAL_FILE = CONTROL_DIR / "activation-journal.json"
ADMISSION_LOCK_FILE = (
    MAINTENANCE_CONTROL_DIR / "maintenance-admission.lock"
)
HELPER_LEASE_FILE = CONTROL_DIR / "update-helper-claim.lock"
RESTORE_REQUEST_FILE = RESTORE_CONTROL_DIR / "restore-request.json"
RESTORE_PUBLIC_STATUS_FILE = RESTORE_PUBLIC_DIR / "restore-status.json"
RESTORE_HELPER_HEALTH_FILE = RESTORE_PUBLIC_DIR / "helper-health.json"
RESTORE_JOURNAL_FILE = RESTORE_CONTROL_DIR / "restore-journal.json"
RESTORE_JOURNAL_DIR = RESTORE_CONTROL_DIR / "journal"
RESTORE_EXECUTOR_RESULT_FILE = (
    RESTORE_CONTROL_DIR / "restore-executor-result.json"
)
RESTORE_INTEGRITY_CONVERGENCE_FILE = (
    RESTORE_CONTROL_DIR / "restore-integrity-convergence.json"
)
RESTORE_DESTRUCTIVE_MARKER_FILE = (
    RESTORE_CONTROL_DIR / "restore-destructive-started.json"
)
RESTORE_HELPER_LEASE_FILE = (
    RESTORE_CONTROL_DIR / "restore-helper-claim.lock"
)
RESTORE_RECEIPT_DIR = RESTORE_CONTROL_DIR / "receipts"

POLL_SECONDS = int(os.getenv("KM_VMS_UPDATE_HELPER_POLL_SECONDS") or "2")
MAX_CONTROL_BYTES = 64 * 1024
REQUEST_SCHEMA_VERSION = 3
CURRENT_PRODUCT_DB_SCHEMA_VERSION = 9
REQUEST_DOCUMENT_TYPE = "update_apply_request"
REQUEST_STATES = {"admitted", "claimed", "terminal"}
TERMINAL = {
    "completed",
    "failed",
    "failed_rolled_back",
    "cancelled",
    "blocked",
}
RUNNING = {
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
    "preparing",
    "staging",
    "activating",
    "reconnecting",
    "rolling_back",
}
MACRO_STEPS = [
    "request",
    "preflight",
    "applying",
    "health_check",
    "commit_verification",
]

AUDIT_NAMESPACE = uuid.UUID("abf15e22-71b8-5af5-b9ee-ef808127c780")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
REQUEST_ID_RE = re.compile(r"^update-[0-9a-f]{32}$", re.IGNORECASE)
SUBMISSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
MIGRATION_ATTEMPT_RE = re.compile(
    r"^migration-attempt-[0-9a-f]{32}$",
    re.IGNORECASE,
)
MACHINE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SLOT_ID_RE = re.compile(
    r"^(?:release-[0-9a-f]{40}|adopted-[0-9a-f]{64}|initial-[0-9a-f]{64})$"
)
VERSION_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,119}$")
SENSITIVE_VALUE_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9_]+|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]+|rtsp://[^@\s]+@|"
    r"postgresql://[^:\s]+:[^@\s]+@|"
    r"-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
RESTORE_REQUEST_SCHEMA = "stage13.7.8.current-restore-request.v1"
RESTORE_PUBLIC_SCHEMA = "stage13.7.8.current-restore-public.v1"
RESTORE_REQUEST_ID_RE = re.compile(r"^restore-[0-9a-f]{32}$")
RESTORE_ARTIFACT_ID_RE = re.compile(
    r"^kmvms-db-\d{8}T\d{6}Z-[0-9a-f]{12}$"
)
RESTORE_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
RESTORE_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,100}$")
RESTORE_ACTIVE_STATES = {"admitted", "claimed"}
RESTORE_TERMINAL_RESULTS = {
    "completed",
    "blocked",
    "failed_rolled_back",
    "failed_recovery_required",
}
RESTORE_PHASES = {
    "preflight",
    "pre_restore_backup",
    "writers_paused",
    "restore_running",
    "services_starting",
    "post_restore_check",
    *RESTORE_TERMINAL_RESULTS,
}
RESTORE_OPERATIONAL_PHASES = RESTORE_PHASES - RESTORE_TERMINAL_RESULTS
RESTORE_SERVICE_ALLOWLIST = frozenset({"api", "recorder"})
RESTORE_INTEGRITY_CONVERGENCE_SCHEMA = (
    "archive-integrity-post-restore-convergence.v1"
)
RESTORE_INTEGRITY_MAX_ATTEMPTS = 3
RESTORE_INTEGRITY_RETRY_SECONDS = 30

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
LEGACY_ACTOR_KEYS = {"user_id", "role"}
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
SAFE_PREFLIGHT_FAILURE_CATEGORIES = frozenset({"slot_adoption_conflict"})
SAFE_ACQUISITION_FAILURE_CATEGORIES = frozenset(
    {"source_acquisition_failed", "source_temporarily_unavailable"}
)
SAFE_FAILURE_LINE_RE = re.compile(
    r"^ERROR \[([a-z][a-z0-9_]*)\]:[^\r\n]*$"
)
SCHEMA_RETRY_KEYS = {
    "schema_version",
    "request_id",
    "requested_at",
    "requested_by",
    "intent",
    "confirmed",
    "source",
    "preflight_required",
    "status_path",
    "retry_of_request_id",
    "migration_attempt_id",
}


class HelperError(RuntimeError):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        phase: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ):
        self.category = category
        self.phase = phase or category
        self.diagnostics = diagnostics or {}
        super().__init__(message)


def utcnow() -> str:
    return datetime.utcnow().isoformat() + "Z"


def parsed_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def safe_text(value: Any, limit: int = 300) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    return SENSITIVE_VALUE_RE.sub("***", str(value).strip())[:limit] or None


def contains_sensitive_content(value: Any) -> bool:
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


def read_json(path: Path) -> dict[str, Any] | None:
    descriptor: int | None = None
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size <= 1
            or info.st_size > MAX_CONTROL_BYTES
        ):
            raise HelperError(
                "control_file_invalid",
                "Update control file is invalid.",
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(MAX_CONTROL_BYTES + 1)
        if len(raw) > MAX_CONTROL_BYTES:
            raise HelperError(
                "control_file_invalid",
                "Update control file is invalid.",
            )
        payload = json.loads(raw.decode("utf-8"))
    except HelperError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise HelperError(
            "control_file_invalid",
            "Update control file is invalid.",
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not isinstance(payload, dict):
        raise HelperError(
            "control_file_invalid",
            "Update control file must contain a JSON object.",
        )
    if path.parent in {
        CONTROL_DIR,
        RESTORE_CONTROL_DIR,
        RESTORE_PUBLIC_DIR,
        RESTORE_RECEIPT_DIR,
    } and contains_sensitive_content(payload):
        raise HelperError(
            "control_payload_sensitive",
            "Update control payload contains sensitive content.",
        )
    return payload


def activation_journal(
    request_id: str | None = None,
) -> dict[str, Any] | None:
    payload = read_json(ACTIVATION_JOURNAL_FILE)
    if payload is None:
        return None
    previous = payload.get("previous")
    target = payload.get("target")
    phase = payload.get("phase")
    observed_request = str(payload.get("request_id") or "").lower()
    failure_category = payload.get("failure_category")
    rollback_trigger = payload.get("rollback_trigger")
    pointer_slot_id = payload.get("pointer_slot_id")
    target_verified = payload.get("target_verified")
    previous_verified = payload.get("previous_verified")
    if (
        payload.get("schema_version") != 1
        or payload.get("document_type") != "release_slot_activation"
        or not REQUEST_ID_RE.fullmatch(observed_request)
        or phase
        not in {
            "target_prepared",
            "quiescing",
            "schema_preparing",
            "activating",
            "verifying_target",
            "committing_target",
            "rolling_back",
            "completed",
            "failed_rolled_back",
            "blocked",
        }
        or not isinstance(previous, dict)
        or not isinstance(target, dict)
        or not SLOT_ID_RE.fullmatch(
            str(previous.get("slot_id") or "")
        )
        or (
            str(previous.get("slot_id") or "").startswith("initial-")
            and (
                previous.get("commit") is not None
                or previous.get("kind") != "initial_install_snapshot"
            )
        )
        or (
            not str(previous.get("slot_id") or "").startswith("initial-")
            and not COMMIT_SHA_RE.fullmatch(
                str(previous.get("commit") or "")
            )
        )
        or not VERSION_TEXT_RE.fullmatch(
            str(previous.get("version") or "")
        )
        or not SLOT_ID_RE.fullmatch(
            str(target.get("slot_id") or "")
        )
        or not COMMIT_SHA_RE.fullmatch(
            str(target.get("commit") or "")
        )
        or not VERSION_TEXT_RE.fullmatch(
            str(target.get("version") or "")
        )
        or pointer_slot_id
        not in {
            None,
            str(previous.get("slot_id") or ""),
            str(target.get("slot_id") or ""),
        }
        or not isinstance(target_verified, bool)
        or not isinstance(previous_verified, bool)
        or (
            failure_category is not None
            and (
                not isinstance(failure_category, str)
                or not MACHINE_CODE_RE.fullmatch(failure_category)
            )
        )
        or (
            rollback_trigger is not None
            and (
                not isinstance(rollback_trigger, str)
                or not MACHINE_CODE_RE.fullmatch(rollback_trigger)
            )
        )
    ):
        raise HelperError(
            "activation_journal_invalid",
            "Release activation state is invalid.",
        )
    if phase == "completed" and (
        pointer_slot_id != target["slot_id"]
        or target_verified is not True
        or failure_category is not None
        or rollback_trigger is not None
    ):
        raise HelperError(
            "activation_journal_invalid",
            "Completed release activation evidence is contradictory.",
        )
    if phase == "failed_rolled_back" and (
        pointer_slot_id != previous["slot_id"]
        or previous_verified is not True
        or failure_category is None
        or rollback_trigger is None
    ):
        raise HelperError(
            "activation_journal_invalid",
            "Rollback evidence is contradictory.",
        )
    if phase == "blocked" and failure_category is None:
        raise HelperError(
            "activation_journal_invalid",
            "Blocked activation lacks a failure category.",
        )
    if request_id is not None and observed_request != request_id.lower():
        return None
    return {
        "request_id": observed_request,
        "phase": phase,
        "previous_slot": previous["slot_id"],
        "previous_version": str(previous["version"]),
        "previous_commit": (
            str(previous["commit"]).lower()
            if previous.get("commit") is not None
            else None
        ),
        "target_slot": target["slot_id"],
        "target_version": str(target["version"]),
        "target_commit": str(target["commit"]).lower(),
        "failure_category": failure_category,
        "rollback_trigger": rollback_trigger,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if contains_sensitive_content(payload):
        raise HelperError(
            "control_payload_sensitive",
            "Update control payload contains sensitive content.",
        )
    if len(rendered.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise HelperError(
            "control_payload_too_large",
            "Update control payload is too large.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent_info = path.parent.lstat()
        target_info = path.lstat()
    except FileNotFoundError:
        target_info = None
        parent_info = path.parent.lstat()
    except OSError as exc:
        raise HelperError(
            "control_write_failed",
            "Update control payload could not be persisted.",
        ) from exc
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or (
            target_info is not None
            and (
                stat.S_ISLNK(target_info.st_mode)
                or not stat.S_ISREG(target_info.st_mode)
            )
        )
    ):
        raise HelperError(
            "control_write_unsafe",
            "Update control output path is unsafe.",
        )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise HelperError(
            "control_write_failed",
            "Update control payload could not be persisted.",
        ) from exc


@contextmanager
def admission_guard():
    try:
        MAINTENANCE_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        root_info = MAINTENANCE_CONTROL_DIR.lstat()
    except OSError as exc:
        raise HelperError(
            "maintenance_control_root_unavailable",
            "Maintenance admission root is unavailable.",
        ) from exc
    if (
        MAINTENANCE_CONTROL_DIR.is_symlink()
        or not stat.S_ISDIR(root_info.st_mode)
    ):
        raise HelperError(
            "maintenance_control_root_unsafe",
            "Maintenance admission root is unsafe.",
        )
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd: int | None = None
    try:
        lock_fd = os.open(ADMISSION_LOCK_FILE, flags, 0o600)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise OSError("admission lock is not a regular file")
    except OSError as exc:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        raise HelperError(
            "maintenance_admission_lock_unsafe",
            "Maintenance admission lock is unavailable.",
        ) from exc
    with os.fdopen(lock_fd, "r+", encoding="utf-8") as lock_file:
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
            fcntl.flock(
                lease_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lease_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def restore_execution_lease():
    RESTORE_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with RESTORE_HELPER_LEASE_FILE.open(
        "a+",
        encoding="utf-8",
    ) as lease_file:
        try:
            fcntl.flock(
                lease_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lease_file.fileno(), fcntl.LOCK_UN)


def deterministic_audit_event_id(request_id: str) -> str:
    return str(uuid.uuid5(AUDIT_NAMESPACE, request_id))


def strict_source(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != REQUEST_SOURCE_KEYS:
        return None
    if (
        value.get("kind") != "trusted_manifest"
        or value.get("source_type") != "github_tarball"
        or not isinstance(value.get("channel"), str)
        or not MACHINE_CODE_RE.fullmatch(value["channel"])
        or not isinstance(value.get("version"), str)
        or not VERSION_TEXT_RE.fullmatch(value["version"])
        or not isinstance(value.get("commit"), str)
        or not COMMIT_SHA_RE.fullmatch(value["commit"])
        or not isinstance(value.get("apply_ref"), str)
        or value["apply_ref"].lower() != value["commit"].lower()
        or not isinstance(value.get("ref"), str)
        or not GIT_REF_RE.fullmatch(value["ref"])
        or ".." in value["ref"]
        or "@{" in value["ref"]
        or not isinstance(value.get("repo"), str)
        or not GITHUB_REPO_RE.fullmatch(value["repo"])
    ):
        return None
    normalized = json.loads(json.dumps(value))
    normalized["commit"] = value["commit"].lower()
    normalized["apply_ref"] = value["apply_ref"].lower()
    return normalized


def strict_candidate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != APPLY_CANDIDATE_KEYS:
        return None
    if value.get("source") not in {"trusted_snapshot", "live_check"}:
        return None
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != APPLY_FRESHNESS_KEYS:
        return None
    for key, expected_type in {
        "available": bool,
        "fresh": bool,
        "age_seconds": int,
        "fresh_for_seconds": int,
        "version": str,
        "commit_short": str,
        "provider": str,
    }.items():
        if snapshot.get(key) is not None and type(snapshot[key]) is not expected_type:
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


def strict_actor(value: Any, *, allow_legacy: bool = False) -> bool:
    if not isinstance(value, dict):
        return False
    if allow_legacy and set(value) == LEGACY_ACTOR_KEYS:
        return (
            (
                (
                    isinstance(value.get("user_id"), int)
                    and not isinstance(value.get("user_id"), bool)
                    and value["user_id"] >= 1
                )
                or (
                    isinstance(value.get("user_id"), str)
                    and value["user_id"].isdigit()
                    and 0 < len(value["user_id"]) <= 20
                    and int(value["user_id"]) >= 1
                )
            )
            and isinstance(value.get("role"), str)
            and bool(MACHINE_CODE_RE.fullmatch(value["role"]))
        )
    if set(value) != CURRENT_ACTOR_KEYS:
        return False
    return (
        isinstance(value.get("user_id"), int)
        and not isinstance(value.get("user_id"), bool)
        and value["user_id"] >= 1
        and isinstance(value.get("username"), str)
        and 1 <= len(value["username"]) <= 150
        and isinstance(value.get("role"), str)
        and bool(MACHINE_CODE_RE.fullmatch(value["role"]))
        and (
            value.get("ip_address") is None
            or (
                isinstance(value["ip_address"], str)
                and len(value["ip_address"]) <= 80
            )
        )
        and (
            value.get("user_agent") is None
            or (
                isinstance(value["user_agent"], str)
                and len(value["user_agent"]) <= 300
            )
        )
    )


def strict_terminal_summary(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == TERMINAL_SUMMARY_KEYS
        and value.get("status") in TERMINAL
        and parsed_timestamp(value.get("finished_at")) is not None
        and (
            value.get("error_category") is None
            or (
                isinstance(value.get("error_category"), str)
                and MACHINE_CODE_RE.fullmatch(value["error_category"])
            )
        )
    )


def validate_current_request(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != CURRENT_REQUEST_KEYS:
        return None
    if (
        value.get("schema_version") != REQUEST_SCHEMA_VERSION
        or value.get("document_type") != REQUEST_DOCUMENT_TYPE
        or value.get("intent") != "apply_update"
        or value.get("confirmed") is not True
        or value.get("preflight_required") is not True
        or value.get("status_path")
        != "data/update-control/update-status.json"
        or value.get("state") not in REQUEST_STATES
        or not isinstance(value.get("request_id"), str)
        or not REQUEST_ID_RE.fullmatch(value["request_id"])
        or not isinstance(value.get("submission_id"), str)
        or not SUBMISSION_ID_RE.fullmatch(value["submission_id"])
        or parsed_timestamp(value.get("requested_at")) is None
        or parsed_timestamp(value.get("updated_at")) is None
        or value.get("audit_event_id")
        != deterministic_audit_event_id(value["request_id"])
        or strict_source(value.get("source")) is None
        or strict_candidate(value.get("apply_candidate")) is None
        or not strict_actor(value.get("requested_by"))
    ):
        return None
    if value["state"] == "admitted":
        if value.get("claimed_at") is not None or value.get("terminal") is not None:
            return None
    elif value["state"] == "claimed":
        if (
            parsed_timestamp(value.get("claimed_at")) is None
            or value.get("terminal") is not None
        ):
            return None
    elif (
        value.get("claimed_at") is not None
        and parsed_timestamp(value.get("claimed_at")) is None
    ) or not strict_terminal_summary(value.get("terminal")):
        return None
    normalized = json.loads(json.dumps(value))
    normalized["request_id"] = value["request_id"].lower()
    normalized["submission_id"] = value["submission_id"].lower()
    normalized["source"] = strict_source(value["source"])
    normalized["apply_candidate"] = strict_candidate(value["apply_candidate"])
    return normalized


def validate_schema_retry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != SCHEMA_RETRY_KEYS:
        return None
    source = strict_source(value.get("source"))
    if (
        value.get("schema_version") != 1
        or value.get("intent") != "apply_update"
        or value.get("confirmed") is not True
        or value.get("preflight_required") is not True
        or value.get("status_path")
        != "data/update-control/update-status.json"
        or not isinstance(value.get("request_id"), str)
        or not REQUEST_ID_RE.fullmatch(value["request_id"])
        or not isinstance(value.get("retry_of_request_id"), str)
        or not REQUEST_ID_RE.fullmatch(value["retry_of_request_id"])
        or value["retry_of_request_id"] == value["request_id"]
        or not isinstance(value.get("migration_attempt_id"), str)
        or not MIGRATION_ATTEMPT_RE.fullmatch(value["migration_attempt_id"])
        or parsed_timestamp(value.get("requested_at")) is None
        or not strict_actor(value.get("requested_by"), allow_legacy=True)
        or source is None
    ):
        return None
    normalized = json.loads(json.dumps(value))
    normalized["source"] = source
    normalized["_request_kind"] = "schema_retry"
    return normalized


def write_current_request(request: dict[str, Any]) -> None:
    validated = validate_current_request(request)
    if not validated:
        raise HelperError(
            "admission_contract_invalid",
            "Update admission contract is invalid.",
        )
    write_json(REQUEST_FILE, validated)


def error_payload(category: str) -> dict[str, str]:
    messages = {
        "cancelled_before_start": "Queued update was cancelled before helper execution.",
        "helper_restart_interrupted": "Update helper restarted before completion.",
        "helper_host_app_dir_missing": "Update helper application directory is not configured.",
        "helper_host_app_dir_invalid": "Update helper application directory is invalid.",
        "helper_host_app_dir_unmounted": "Update helper application directory is unavailable.",
        "preflight_failed": "Update preflight failed.",
        "source_acquisition_failed": "Trusted update source acquisition failed.",
        "source_temporarily_unavailable": "Trusted update source is temporarily unavailable.",
        "slot_adoption_conflict": "The preserved previous release no longer matches the current installation.",
        "compose_config_failed": "Docker Compose configuration validation failed.",
        "jellyfin_ffmpeg_repo_unavailable": "External FFmpeg repository was unavailable during image build.",
        "build_network_dependency_failed": "A network dependency failed during image build.",
        "docker_build_failed": "Docker image rebuild failed.",
        "schema_update_failed": "Database schema preparation failed.",
        "health_check_failed": "Updated services did not become healthy.",
        "commit_mismatch": "Installed commit did not match the trusted target.",
        "commit_missing": "Installed commit evidence is missing.",
        "metadata_invalid": "Installed release metadata is invalid.",
        "apply_timeout": "Update execution timed out.",
        "apply_failed": "Update apply failed.",
        "helper_exception": "Unexpected update helper failure.",
        "schema_previous_runtime_incompatible": "The previous release cannot safely run after the planned database migration.",
        "schema_migration_interrupted": "Database migration completion could not be proven after restart.",
        "target_health_failed": "The target release failed its runtime health check.",
        "target_identity_mismatch": "The target release identity did not match the trusted release.",
        "helper_handoff_failed": "The target update helper could not be handed off safely.",
        "rollback_verification_failed": "The previous release could not be verified after rollback.",
        "previous_recovery_failed": "The previous release could not be restored after a pre-activation failure.",
        "activation_pointer_conflict": "Release activation state is contradictory.",
        "activation_journal_invalid": "Release activation state is unavailable or invalid.",
    }
    if category in {
        "target_health_failed",
        "target_identity_mismatch",
        "helper_handoff_failed",
    }:
        operator_action = (
            "The previous release was restored. Review the target failure before retrying."
        )
    elif category == "slot_adoption_conflict":
        operator_action = (
            "Verify the installed source and runtime state before retrying."
        )
    elif category == "source_temporarily_unavailable":
        operator_action = "Wait briefly, then run the update again."
    elif category == "source_acquisition_failed":
        operator_action = "Verify access to the trusted release source, then retry."
    else:
        operator_action = (
            "Review update status and retry only after the cause is resolved."
        )
    return {
        "category": category,
        "message": messages.get(category, "Update apply failed."),
        "operator_action": operator_action,
    }


def _allowlisted_preflight_failure_category(
    metadata: dict[str, Any] | None,
    stderr: str,
) -> str | None:
    metadata_category = (
        metadata.get("error_category")
        if isinstance(metadata, dict)
        else None
    )
    if metadata_category in SAFE_PREFLIGHT_FAILURE_CATEGORIES:
        return str(metadata_category)
    for line in str(stderr or "").splitlines():
        match = SAFE_FAILURE_LINE_RE.fullmatch(line.strip())
        if match and match.group(1) in SAFE_PREFLIGHT_FAILURE_CATEGORIES:
            return match.group(1)
    return None


def classify_preflight_failure(stderr: str) -> HelperError:
    category = _allowlisted_preflight_failure_category(None, stderr)
    if category:
        return HelperError(
            category,
            error_payload(category)["message"],
        )
    return HelperError("preflight_failed", "Update preflight failed.")


def macro_step(value: str) -> str:
    if value in {"queued", "starting_helper", "request"}:
        return "request"
    if value in {
        "preflight",
        "acquire_source",
        "downloading",
        "extracting",
        "validating_source",
    }:
        return "preflight"
    if value in {
        "overlay",
        "applying",
        "compose_config",
        "rebuilding",
        "restarting",
        "preparing",
        "staging",
        "activating",
    }:
        return "applying"
    if value in {"health_check", "reconnecting", "rolling_back"}:
        return "health_check"
    if value in {"commit_verification", "completed"}:
        return "commit_verification"
    return "preflight"


def steps_for(value: str, failed: bool = False) -> list[dict[str, str]]:
    active = macro_step(value)
    active_index = MACRO_STEPS.index(active)
    result = []
    for index, name in enumerate(MACRO_STEPS):
        if index < active_index:
            state = "completed"
        elif index == active_index:
            state = "failed" if failed else "running"
        else:
            state = "pending"
        result.append({"name": name, "status": state})
    if value == "completed":
        for item in result:
            item["status"] = "completed"
    return result


def base_status(
    request: dict[str, Any],
    status: str,
    phase: str,
    steps: list[dict[str, str]],
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = request["source"]
    submission_id = request.get("submission_id")
    now = utcnow()
    return {
        "schema_version": 1,
        "request_id": request["request_id"],
        "submission_id": submission_id,
        "target_version": source.get("version"),
        "status": status,
        "phase": phase,
        "current_step": phase,
        "progress_percent": None,
        "progress_current": None,
        "progress_total": None,
        "progress_unit": None,
        "started_at": request["requested_at"],
        "updated_at": now,
        "finished_at": None,
        "source": {
            "kind": "github-tarball",
            "repo": source["repo"],
            "ref": source["ref"],
            "commit": source["commit"],
            "apply_ref": source["apply_ref"],
            "version": source["version"],
            "channel": source["channel"],
            "source_type": source["source_type"],
        },
        "apply_candidate": request.get("apply_candidate"),
        "expected_commit": source["commit"],
        "installed_commit": None,
        "commit_verified": False,
        "steps": steps,
        "can_cancel": False,
        "rollback_supported": True,
        "side_effects": {
            "api_docker_socket": False,
            "api_shell_execution": False,
            "request_controlled_source": False,
            "helper_has_docker_socket": True,
            "helper_public_ports": False,
        },
        "error": error,
    }


def read_progress(request_id: str | None = None) -> dict[str, Any] | None:
    try:
        payload = read_json(PROGRESS_FILE)
    except HelperError:
        return None
    if not payload:
        return None
    if request_id and payload.get("request_id") not in {None, request_id}:
        return None
    current_step = safe_text(payload.get("current_step"), 80)
    phase = safe_text(payload.get("phase"), 80)
    status = safe_text(payload.get("status"), 40)
    if current_step and not MACHINE_CODE_RE.fullmatch(current_step):
        current_step = None
    if phase and not MACHINE_CODE_RE.fullmatch(phase):
        phase = None
    if status not in RUNNING:
        status = None
    progress_percent = payload.get("progress_percent")
    progress_current = payload.get("progress_current")
    progress_total = payload.get("progress_total")
    progress_unit = payload.get("progress_unit")
    progress_fields = {
        "progress_percent": None,
        "progress_current": None,
        "progress_total": None,
        "progress_unit": None,
    }
    if any(
        value is not None
        for value in (
            progress_percent,
            progress_current,
            progress_total,
            progress_unit,
        )
    ):
        valid_progress = (
            type(progress_percent) is int
            and type(progress_current) is int
            and type(progress_total) is int
            and 0 <= progress_percent <= 100
            and 0 <= progress_current <= progress_total
            and progress_total > 0
            and progress_unit in {"bytes", "items"}
            and progress_percent
            == (progress_current * 100 // progress_total)
        )
        if valid_progress:
            progress_fields = {
                "progress_percent": progress_percent,
                "progress_current": progress_current,
                "progress_total": progress_total,
                "progress_unit": progress_unit,
            }
    return {
        "current_step": current_step,
        "phase": phase,
        "status": status,
        **progress_fields,
    }


def append_last_history(status_payload: dict[str, Any]) -> None:
    item = {
        key: status_payload.get(key)
        for key in (
            "request_id",
            "submission_id",
            "target_version",
            "status",
            "phase",
            "started_at",
            "updated_at",
            "finished_at",
            "source",
            "apply_candidate",
            "expected_commit",
            "installed_commit",
            "commit_verified",
            "steps",
            "error",
        )
    }
    write_json(
        APPLY_HISTORY_FILE,
        {
            "schema_version": 1,
            "max_items": 1,
            "items": [item],
            "updated_at": utcnow(),
        },
    )


def publish_terminal(
    request: dict[str, Any],
    status_payload: dict[str, Any],
) -> None:
    if (
        status_payload.get("status") not in TERMINAL
        or status_payload.get("request_id") != request.get("request_id")
    ):
        raise HelperError(
            "terminal_status_invalid",
            "Terminal update status is invalid.",
        )
    write_json(STATUS_FILE, status_payload)
    append_last_history(status_payload)
    if request.get("_request_kind") == "schema_retry":
        return
    with admission_guard():
        current = validate_current_request(read_json(REQUEST_FILE))
        if (
            not current
            or current["request_id"] != request["request_id"]
            or current["state"] not in {"claimed", "terminal"}
        ):
            raise HelperError(
                "admission_request_changed",
                "Current update admission changed during helper execution.",
            )
        if current["state"] == "terminal":
            return
        finished_at = (
            safe_text(
                status_payload.get("finished_at")
                or status_payload.get("updated_at"),
                80,
            )
            or utcnow()
        )
        category = (
            status_payload.get("error", {}).get("category")
            if isinstance(status_payload.get("error"), dict)
            else None
        )
        current["state"] = "terminal"
        current["updated_at"] = finished_at
        current["terminal"] = {
            "status": status_payload["status"],
            "finished_at": finished_at,
            "error_category": category,
        }
        write_current_request(current)


def claim_current_request() -> dict[str, Any] | None:
    with admission_guard():
        raw = read_json(REQUEST_FILE)
        current = validate_current_request(raw)
        if current:
            if current["state"] == "terminal":
                return None
            if current["state"] == "claimed":
                journal = activation_journal(current["request_id"])
                if journal:
                    current["_resume_activation"] = True
                    return current
                failed = base_status(
                    current,
                    "failed",
                    "helper_restart_interrupted",
                    steps_for("preflight", failed=True),
                    error_payload("helper_restart_interrupted"),
                )
                failed["finished_at"] = failed["updated_at"]
                write_json(STATUS_FILE, failed)
                append_last_history(failed)
                current["state"] = "terminal"
                current["updated_at"] = failed["finished_at"]
                current["terminal"] = {
                    "status": "failed",
                    "finished_at": failed["finished_at"],
                    "error_category": "helper_restart_interrupted",
                }
                write_current_request(current)
                return None
            claimed_at = utcnow()
            current["state"] = "claimed"
            current["claimed_at"] = claimed_at
            current["updated_at"] = claimed_at
            write_current_request(current)
            write_json(
                STATUS_FILE,
                base_status(
                    current,
                    "starting_helper",
                    "starting_helper",
                    steps_for("starting_helper"),
                ),
            )
            return current
        retry = validate_schema_retry(raw)
        if retry:
            status = read_json(STATUS_FILE)
            if (
                status
                and status.get("request_id") == retry["request_id"]
                and status.get("status") in TERMINAL
            ):
                return None
            return retry
        return None


def request_may_need_execution() -> bool:
    raw = read_json(REQUEST_FILE)
    current = validate_current_request(raw)
    if current:
        return current["state"] in {"admitted", "claimed"}
    retry = validate_schema_retry(raw)
    if not retry:
        return False
    status = read_json(STATUS_FILE)
    return not (
        status
        and status.get("request_id") == retry["request_id"]
        and status.get("status") in TERMINAL
    )


def compose_app_dir() -> Path:
    if HOST_APP_DIR is None:
        raise HelperError(
            "helper_host_app_dir_missing",
            "KM_VMS_UPDATE_HOST_APP_DIR must be configured.",
        )
    if not HOST_APP_DIR.is_absolute():
        raise HelperError(
            "helper_host_app_dir_invalid",
            "KM_VMS_UPDATE_HOST_APP_DIR must be absolute.",
        )
    if (
        not (HOST_APP_DIR / ".env").is_file()
        or not (HOST_APP_DIR / "data").is_dir()
        or not (
            HOST_APP_DIR
            / "data/update-runtime/bootstrap/current/km-vms-bootstrap.py"
        ).is_file()
    ):
        raise HelperError(
            "helper_host_app_dir_unmounted",
            "KM_VMS_UPDATE_HOST_APP_DIR is unavailable.",
        )
    return HOST_APP_DIR


def load_stable_bootstrap():
    stable_dir = compose_app_dir()
    module_path = (
        stable_dir
        / "data/update-runtime/bootstrap/current/km-vms-bootstrap.py"
    )
    if module_path.is_symlink() or not module_path.is_file():
        raise HelperError(
            "stable_bootstrap_missing",
            "Stable bootstrap authority is unavailable.",
        )
    spec = importlib.util.spec_from_file_location(
        "km_vms_restore_bootstrap_runtime",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise HelperError(
            "stable_bootstrap_invalid",
            "Stable bootstrap authority cannot be loaded.",
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise HelperError(
            getattr(exc, "code", "stable_bootstrap_invalid"),
            "Stable bootstrap authority is invalid.",
        ) from exc
    return stable_dir, module


def set_restore_writer_fence(*, enabled: bool) -> None:
    stable_dir, bootstrap = load_stable_bootstrap()
    try:
        bootstrap.set_writer_fence(
            stable_dir,
            str(os.getenv("KM_VMS_PROJECT_NAME") or "tnas-vms"),
            enabled=enabled,
        )
    except Exception as exc:
        raise HelperError(
            getattr(exc, "code", "restore_writer_fence_failed"),
            "Restore writer restart policy could not be fenced safely.",
        ) from exc


def resolve_update_source_dir(app_dir: Path) -> Path:
    bootstrap = (
        app_dir
        / "data/update-runtime/bootstrap/current/km-vms-bootstrap.py"
    )
    if not bootstrap.is_file() or bootstrap.is_symlink():
        raise HelperError(
            "helper_active_source_resolver_missing",
            "Active release source resolver is unavailable.",
        )
    try:
        resolved = subprocess.run(
            [
                "python3",
                "-B",
                str(bootstrap),
                "resolve",
                "--app-dir",
                str(app_dir),
                "--project-name",
                str(os.getenv("KM_VMS_PROJECT_NAME") or ""),
                "--repair",
            ],
            cwd=app_dir,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError(
            "helper_active_source_resolver_failed",
            "Active release source could not be resolved.",
        ) from exc
    try:
        source_text = str(json.loads(resolved.stdout).get("source_path") or "")
    except (TypeError, json.JSONDecodeError):
        source_text = ""
    if (
        resolved.returncode != 0
        or not source_text
        or "\n" in source_text
        or "\r" in source_text
    ):
        raise HelperError(
            "helper_active_source_resolver_failed",
            "Active release source could not be resolved.",
        )
    source_dir = Path(source_text)
    update_script = source_dir / "scripts" / "update.sh"
    if (
        not source_dir.is_absolute()
        or not update_script.is_file()
        or update_script.is_symlink()
    ):
        raise HelperError(
            "helper_active_source_invalid",
            "Active release source is incomplete or unsafe.",
        )
    return source_dir


def update_child_env(request: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["KM_VMS_UPDATE_HELPER_MODE"] = "1"
    env["KM_VMS_UPDATE_CONTROL_REQUEST_ID"] = str(request["request_id"])
    env["KM_VMS_UPDATE_PROGRESS_FILE"] = str(PROGRESS_FILE)
    helper_compose = os.getenv(
        "KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE",
        "",
    ).strip()
    helper_executable = (
        "docker" if helper_compose == "docker compose" else helper_compose
    )
    if helper_compose and shutil.which(helper_executable) is not None:
        env["KM_VMS_DOCKER_COMPOSE"] = helper_compose
    else:
        inherited = env.get("KM_VMS_DOCKER_COMPOSE", "").strip()
        inherited_executable = (
            "docker" if inherited == "docker compose" else inherited
        )
        if inherited and shutil.which(inherited_executable) is None:
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
        for part in (
            stderr,
            metadata.get("error_message") if metadata else "",
            failed_phase or "",
        )
    ).lower()
    if failed_phase == "health_check":
        return HelperError("health_check_failed", "Update health check failed.")
    if failed_phase == "compose_config":
        return HelperError(
            "compose_config_failed",
            "Docker Compose configuration validation failed.",
        )
    if failed_phase == "acquire":
        metadata_category = (
            metadata.get("error_category")
            if isinstance(metadata, dict)
            else None
        )
        category = (
            str(metadata_category)
            if metadata_category in SAFE_ACQUISITION_FAILURE_CATEGORIES
            else "preflight_failed"
        )
        attempts = (
            metadata.get("source_acquisition_attempts")
            if isinstance(metadata, dict)
            else None
        )
        diagnostics = {}
        if type(attempts) is int and 1 <= attempts <= 3:
            diagnostics["source_acquisition_attempts"] = attempts
        return HelperError(
            category,
            error_payload(category)["message"],
            phase="acquire_source",
            diagnostics=diagnostics,
        )
    if failed_phase in {
        "init",
        "validate_app_dir",
        "compose_detection",
        "extract",
        "validate_source_tree",
        "preflight_preservation",
        "permission_preflight",
    }:
        return HelperError(
            "preflight_failed",
            "Update preflight failed.",
        )
    if failed_phase == "schema_preflight":
        safe_category = _allowlisted_preflight_failure_category(
            metadata,
            stderr,
        )
        if safe_category:
            return HelperError(
                safe_category,
                error_payload(safe_category)["message"],
            )
        return HelperError(
            "preflight_failed",
            "Update preflight failed.",
        )
    if failed_phase == "schema_update":
        return HelperError(
            "schema_update_failed",
            "Database schema preparation failed.",
        )
    if failed_phase == "rebuild_recreate":
        if any(
            token in error_text
            for token in (
                "jellyfin",
                "repo.jellyfin.org",
                "jellyfin_team.gpg.key",
                "jellyfin-ffmpeg",
            )
        ):
            return HelperError(
                "jellyfin_ffmpeg_repo_unavailable",
                "External FFmpeg repository was unavailable.",
            )
        if any(
            token in error_text
            for token in (
                "curl",
                "apt-get",
                "timeout",
                "timed out",
                "temporary failure",
                "could not resolve",
                "connection",
            )
        ):
            return HelperError(
                "build_network_dependency_failed",
                "A network dependency failed during image build.",
            )
        return HelperError(
            "docker_build_failed",
            "Docker image rebuild failed.",
        )
    return HelperError("apply_failed", "Update apply failed.")


def verify_installed_commit(
    update_dir: Path,
    expected_commit: str,
    *,
    identity_dir: Path | None = None,
    slot_verified: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    update_metadata = read_json(update_dir / ".km-vms-update.json")
    if not update_metadata and not slot_verified:
        raise HelperError(
            "commit_missing",
            "Update metadata is missing.",
            phase="commit_verification",
        )
    if (
        update_metadata
        and not slot_verified
        and update_metadata.get("status") != "success"
    ):
        raise HelperError(
            "metadata_invalid",
            "Update metadata did not record success.",
            phase="commit_verification",
        )
    installed_commit = (
        expected_commit
        if slot_verified
        else safe_text(update_metadata.get("commit_sha"), 40)
        if update_metadata
        else None
    )
    if installed_commit and installed_commit != expected_commit:
        raise HelperError(
            "commit_mismatch",
            "Installed commit does not match the trusted target.",
            phase="commit_verification",
            diagnostics={"installed_commit": installed_commit or "missing"},
        )
    identity_root = identity_dir or update_dir
    source_metadata = read_json(identity_root / ".km-vms-source.json")
    if source_metadata:
        source_commit = safe_text(source_metadata.get("commit_sha"), 40)
        if source_commit and source_commit != expected_commit:
            raise HelperError(
                "commit_mismatch",
                "Installed source commit does not match the trusted target.",
                phase="commit_verification",
                diagnostics={"installed_commit": source_commit},
            )
    release_metadata = read_json(identity_root / ".km-vms-release.json")
    if not release_metadata:
        raise HelperError(
            "commit_missing",
            "Installed release identity is missing.",
            phase="commit_verification",
        )
    release_commit = safe_text(release_metadata.get("commit_sha"), 40)
    if release_commit != expected_commit:
        raise HelperError(
            "commit_mismatch",
            "Installed release identity does not match the trusted target.",
            phase="commit_verification",
        )
    validation = (
        update_metadata.get("validation_summary")
        if update_metadata
        and isinstance(update_metadata.get("validation_summary"), dict)
        else {}
    )
    host_status = (
        safe_text(release_metadata.get("metadata_status"), 40)
        if slot_verified
        else safe_text(
            validation.get("release_identity_host_metadata_status"),
            40,
        )
        or safe_text(release_metadata.get("metadata_status"), 40)
    )
    api_status = (
        "complete"
        if slot_verified
        else safe_text(
            validation.get("release_identity_api_metadata_status"),
            40,
        )
    )
    api_visible = slot_verified or (
        validation.get("release_identity_api_visible") is True
    )
    commit_verified = slot_verified or (
        validation.get("release_identity_commit_verified") is True
    )
    if (
        host_status != "complete"
        or api_status != "complete"
        or not api_visible
        or not commit_verified
    ):
        raise HelperError(
            "metadata_invalid",
            "Release identity verification is incomplete.",
            phase="commit_verification",
        )
    return installed_commit, expected_commit, {
        "host_metadata_status": host_status,
        "api_metadata_status": api_status,
        "api_visible": api_visible,
        "commit_verified": commit_verified,
    }


def publish_activation_outcome(
    request: dict[str, Any],
    journal: dict[str, Any],
    update_dir: Path,
) -> bool:
    source = request["source"]
    expected_commit = str(source["commit"]).lower()
    if (
        journal["request_id"] != request["request_id"]
        or journal["target_commit"] != expected_commit
        or journal["target_version"] != source["version"]
    ):
        raise HelperError(
            "activation_journal_invalid",
            "Release activation does not match the admitted target.",
        )
    phase = journal["phase"]
    if phase == "completed":
        active_source = resolve_update_source_dir(update_dir)
        expected_source = (
            update_dir
            / "data/update-runtime/slots"
            / journal["target_slot"]
            / "source"
        ).resolve()
        if active_source.resolve() != expected_source:
            raise HelperError(
                "activation_pointer_conflict",
                "Completed activation does not resolve to its target.",
            )
        installed, expected, release_identity = (
            verify_installed_commit(
                update_dir,
                expected_commit,
                identity_dir=active_source,
                slot_verified=True,
            )
        )
        completed = base_status(
            request,
            "completed",
            "completed",
            steps_for("completed"),
        )
        completed["commit_verified"] = True
        completed["installed_commit"] = installed
        completed["expected_commit"] = expected
        completed["release_identity"] = release_identity
        completed["rollback"] = {
            "status": "not_needed",
            "trigger": None,
            "restored_version": None,
        }
        completed["finished_at"] = completed["updated_at"]
        publish_terminal(request, completed)
        return True
    if phase == "failed_rolled_back":
        category = str(
            journal.get("rollback_trigger")
            or journal.get("failure_category")
            or "target_health_failed"
        )
        rolled_back = base_status(
            request,
            "failed_rolled_back",
            "failed_rolled_back",
            steps_for("rolling_back", failed=True),
            error_payload(category),
        )
        rolled_back["installed_commit"] = journal[
            "previous_commit"
        ]
        rolled_back["rollback"] = {
            "status": "completed",
            "trigger": category,
            "restored_version": journal["previous_version"],
        }
        rolled_back["finished_at"] = rolled_back["updated_at"]
        publish_terminal(request, rolled_back)
        return True
    if phase == "blocked":
        category = str(
            journal.get("failure_category")
            or "activation_journal_invalid"
        )
        blocked = base_status(
            request,
            "blocked",
            "blocked",
            steps_for("applying", failed=True),
            error_payload(category),
        )
        blocked["rollback"] = {
            "status": (
                "failed"
                if category
                in {
                    "rollback_verification_failed",
                    "previous_recovery_failed",
                }
                else "not_started"
            ),
            "trigger": journal.get("rollback_trigger"),
            "restored_version": None,
        }
        blocked["finished_at"] = blocked["updated_at"]
        publish_terminal(request, blocked)
        return True
    return False


def resume_activation(
    request: dict[str, Any],
    update_dir: Path,
) -> int:
    journal = activation_journal(request["request_id"])
    if journal is None:
        raise HelperError(
            "activation_journal_invalid",
            "Unfinished activation journal is missing.",
        )
    target_source = (
        update_dir
        / "data/update-runtime/slots"
        / journal["target_slot"]
        / "source"
    )
    bridge = target_source / "scripts/km-vms-update-helper-bridge.py"
    if bridge.is_symlink() or not bridge.is_file():
        raise HelperError(
            "activation_journal_invalid",
            "Trusted activation bridge is unavailable.",
        )
    if journal["phase"] not in {
        "completed",
        "failed_rolled_back",
        "blocked",
    }:
        command = [
            "python3",
            str(bridge),
            "resume-activation",
            "--app-dir",
            str(update_dir),
            "--project-name",
            os.getenv("KM_VMS_PROJECT_NAME", "tnas-vms"),
            "--request-id",
            request["request_id"],
        ]
        result = subprocess.run(
            command,
            cwd=update_dir,
            env=update_child_env(request),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=4200,
            check=False,
        )
        if result.returncode != 0:
            raise HelperError(
                "activation_journal_invalid",
                "Release activation could not converge after restart.",
            )
        journal = activation_journal(request["request_id"])
        if journal is None:
            raise HelperError(
                "activation_journal_invalid",
                "Release activation result is unavailable.",
            )
    if not publish_activation_outcome(request, journal, update_dir):
        raise HelperError(
            "activation_journal_invalid",
            "Release activation did not reach a terminal state.",
        )
    return 0


def run_update(request: dict[str, Any]) -> int:
    source = request["source"]
    update_dir = compose_app_dir()
    update_source_dir = resolve_update_source_dir(update_dir)
    expected_commit = str(source["commit"])
    command = [
        "sh",
        str(update_source_dir / "scripts" / "update.sh"),
        "--github-repo",
        source["repo"],
        "--branch",
        source["apply_ref"],
        "--trusted-commit",
        source["commit"],
        "--yes",
    ]
    if (
        os.getenv("KM_VMS_GITHUB_PRIVATE", "0") == "1"
        or os.getenv("KMVMS_UPDATE_SOURCE_PRIVATE", "0") == "1"
    ):
        command.append("--github-private")
    env = update_child_env(request)
    try:
        PROGRESS_FILE.unlink()
    except FileNotFoundError:
        pass
    write_json(
        STATUS_FILE,
        base_status(
            request,
            "preflight",
            "preflight",
            steps_for("preflight"),
        ),
    )
    applied = run_child_with_progress(
        command,
        request,
        update_dir,
        env,
        timeout_seconds=7200,
        default_step="preflight",
        status_value="preflight",
    )
    journal = activation_journal(request["request_id"])
    if journal:
        if publish_activation_outcome(
            request,
            journal,
            update_dir,
        ):
            return 0
        raise HelperError(
            "activation_journal_invalid",
            "Release activation did not reach terminal state.",
        )
    if applied.returncode != 0:
        raise classify_apply_failure(update_dir, applied.stderr.strip())
    write_json(
        STATUS_FILE,
        base_status(
            request,
            "applying",
            "commit_verification",
            steps_for("commit_verification"),
        ),
    )
    installed, expected, release_identity = verify_installed_commit(
        update_dir,
        expected_commit,
    )
    completed = base_status(
        request,
        "completed",
        "completed",
        steps_for("completed"),
    )
    completed["commit_verified"] = True
    completed["installed_commit"] = installed
    completed["expected_commit"] = expected
    completed["release_identity"] = release_identity
    completed["finished_at"] = completed["updated_at"]
    publish_terminal(request, completed)
    return 0


def run_child_with_progress(
    command: list[str],
    request: dict[str, Any],
    update_dir: Path,
    env: dict[str, str],
    *,
    timeout_seconds: int,
    default_step: str,
    status_value: str,
) -> subprocess.CompletedProcess[str]:
    request_id = str(request.get("request_id") or "")
    started = time.monotonic()
    stderr_path: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    last_step = default_step
    try:
        with tempfile.NamedTemporaryFile(
            "w+b",
            prefix="km-vms-update-stderr-",
            delete=False,
        ) as stderr_file:
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
                    raise HelperError(
                        "apply_timeout",
                        "Update helper child process timed out.",
                        phase=last_step,
                    )
                progress = read_progress(request_id)
                step = (
                    safe_text(
                        progress.get("current_step")
                        if progress
                        else default_step,
                        80,
                    )
                    or default_step
                )
                phase = (
                    safe_text(
                        progress.get("phase") if progress else step,
                        80,
                    )
                    or step
                )
                last_step = step
                status_payload = base_status(
                    request,
                    (
                        progress.get("status")
                        if progress and progress.get("status")
                        else status_value
                    ),
                    phase,
                    steps_for(step),
                )
                status_payload["current_step"] = step
                if progress:
                    for field in (
                        "progress_percent",
                        "progress_current",
                        "progress_total",
                        "progress_unit",
                    ):
                        status_payload[field] = progress.get(field)
                write_json(STATUS_FILE, status_payload)
                time.sleep(POLL_SECONDS)
        stderr_tail = read_stderr_tail(stderr_path)
        return subprocess.CompletedProcess(
            command,
            process.returncode if process else 1,
            "",
            stderr_tail,
        )
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


def validate_restore_request(value: Any) -> dict[str, Any] | None:
    expected = {
        "schema",
        "operation_id",
        "submission_id",
        "intent",
        "requested_at",
        "updated_at",
        "requested_by",
        "artifact",
        "confirmed",
        "confirmation_phrase",
        "state",
        "claimed_at",
        "terminal",
        "video_archive_scope",
        "migration_auto_apply",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return None
    actor = value.get("requested_by")
    artifact = value.get("artifact")
    if (
        value.get("schema") != RESTORE_REQUEST_SCHEMA
        or value.get("intent") != "restore_current_database"
        or value.get("confirmed") is not True
        or value.get("confirmation_phrase") != "RESTORE KM VMS"
        or value.get("state") not in {
            "admitted",
            "claimed",
            "terminal",
        }
        or value.get("video_archive_scope") != "excluded"
        or value.get("migration_auto_apply") is not False
        or not isinstance(value.get("operation_id"), str)
        or not RESTORE_REQUEST_ID_RE.fullmatch(
            value["operation_id"]
        )
        or not isinstance(value.get("submission_id"), str)
        or not SUBMISSION_ID_RE.fullmatch(value["submission_id"])
        or parsed_timestamp(value.get("requested_at")) is None
        or parsed_timestamp(value.get("updated_at")) is None
        or not isinstance(actor, dict)
        or set(actor)
        != {"user_id", "subject", "role", "binding"}
        or not isinstance(actor.get("user_id"), int)
        or isinstance(actor.get("user_id"), bool)
        or actor["user_id"] < 1
        or not isinstance(actor.get("subject"), str)
        or not RESTORE_SUBJECT_RE.fullmatch(actor["subject"])
        or actor.get("role") not in {"owner", "admin"}
        or not isinstance(actor.get("binding"), str)
        or not RESTORE_FINGERPRINT_RE.fullmatch(actor["binding"])
        or not isinstance(artifact, dict)
        or set(artifact)
        != {
            "artifact_id",
            "artifact_created_at",
            "artifact_schema_version",
            "db_backend",
            "file_size",
            "fingerprint",
        }
        or not isinstance(artifact.get("artifact_id"), str)
        or not RESTORE_ARTIFACT_ID_RE.fullmatch(
            artifact["artifact_id"]
        )
        or artifact.get("artifact_schema_version")
        != CURRENT_PRODUCT_DB_SCHEMA_VERSION
        or artifact.get("db_backend") != "postgresql"
        or not isinstance(artifact.get("file_size"), int)
        or artifact["file_size"] < 1
        or not isinstance(artifact.get("fingerprint"), str)
        or not RESTORE_FINGERPRINT_RE.fullmatch(
            artifact["fingerprint"]
        )
    ):
        return None
    if value["state"] == "admitted":
        if (
            value.get("claimed_at") is not None
            or value.get("terminal") is not None
        ):
            return None
    elif value["state"] == "claimed":
        if (
            parsed_timestamp(value.get("claimed_at")) is None
            or value.get("terminal") is not None
        ):
            return None
    else:
        terminal = value.get("terminal")
        terminal_keys = (
            set(terminal)
            if isinstance(terminal, dict)
            else set()
        )
        failed_phase = (
            terminal.get("failed_phase")
            if isinstance(terminal, dict)
            else None
        )
        if (
            not isinstance(terminal, dict)
            or not {
                "status",
                "finished_at",
                "reason_code",
            }.issubset(terminal_keys)
            or terminal_keys
            - {
                "status",
                "finished_at",
                "reason_code",
                "failed_phase",
            }
            or terminal.get("status")
            not in RESTORE_TERMINAL_RESULTS
            or parsed_timestamp(value.get("claimed_at")) is None
            or parsed_timestamp(terminal.get("finished_at")) is None
            or (
                terminal.get("reason_code") is not None
                and (
                    not isinstance(terminal.get("reason_code"), str)
                    or not MACHINE_CODE_RE.fullmatch(
                        terminal["reason_code"]
                    )
                )
            )
            or (
                terminal.get("status") == "completed"
                and terminal.get("reason_code") is not None
            )
            or (
                terminal.get("status") != "completed"
                and terminal.get("reason_code") is None
            )
            or (
                failed_phase is not None
                and failed_phase
                not in RESTORE_OPERATIONAL_PHASES
            )
            or (
                terminal.get("status") == "completed"
                and failed_phase is not None
            )
        ):
            return None
    return json.loads(json.dumps(value))


def restore_request_may_need_execution() -> bool:
    request = validate_restore_request(
        read_json(RESTORE_REQUEST_FILE)
    )
    return bool(
        request
        and request.get("state") in RESTORE_ACTIVE_STATES
    )


def restore_receipt_path(submission_id: str) -> Path:
    if not SUBMISSION_ID_RE.fullmatch(submission_id):
        raise HelperError(
            "restore_submission_invalid",
            "Restore submission identity is invalid.",
        )
    return RESTORE_RECEIPT_DIR / f"{submission_id}.json"


def restore_destructive_started(operation_id: str) -> bool:
    marker = read_json(RESTORE_DESTRUCTIVE_MARKER_FILE)
    return bool(
        isinstance(marker, dict)
        and marker.get("operation_id") == operation_id
        and marker.get("mutation_started") is True
    )


def publish_restore_helper_health() -> None:
    write_json(
        RESTORE_HELPER_HEALTH_FILE,
        {
            "schema_version": 1,
            "role": "update-helper-restore-dispatch",
            "updated_at": utcnow(),
        },
    )


def _restore_public_payload(
    request: dict[str, Any],
    *,
    phase: str,
    status: str = "running",
    pre_restore_backup_id: str | None = None,
    terminal_result: str | None = None,
    reason_code: str | None = None,
    failed_phase: str | None = None,
) -> dict[str, Any]:
    if phase not in RESTORE_PHASES:
        phase = "preflight"
    artifact = request["artifact"]
    now = utcnow()
    terminal = (
        request.get("terminal")
        if isinstance(request.get("terminal"), dict)
        else {}
    )
    terminal_finished_at = (
        terminal.get("finished_at")
        if terminal_result
        and parsed_timestamp(terminal.get("finished_at")) is not None
        else now
        if terminal_result
        else None
    )
    terminal_failed_phase = (
        failed_phase
        if failed_phase in RESTORE_OPERATIONAL_PHASES
        else terminal.get("failed_phase")
        if terminal.get("failed_phase")
        in RESTORE_OPERATIONAL_PHASES
        else None
    )
    if terminal_result == "completed":
        terminal_failed_phase = None
    return {
        "schema": RESTORE_PUBLIC_SCHEMA,
        "operation_id": request["operation_id"],
        "submission_id": request["submission_id"],
        "actor_subject": request["requested_by"]["subject"],
        "status": terminal_result or status,
        "phase": phase,
        "artifact": {
            "artifact_id": artifact["artifact_id"],
            "artifact_created_at": artifact.get(
                "artifact_created_at"
            ),
            "artifact_schema_version": artifact[
                "artifact_schema_version"
            ],
            "db_backend": artifact["db_backend"],
        },
        "pre_restore_backup_id": pre_restore_backup_id,
        "accepted_at": request["requested_at"],
        "started_at": request.get("claimed_at"),
        "updated_at": terminal_finished_at or now,
        "finished_at": terminal_finished_at,
        "terminal_result": terminal_result,
        "reason_code": reason_code,
        "failed_phase": terminal_failed_phase,
        "next_action": (
            "sign_in_again"
            if terminal_result == "completed"
            else "current_database_restored"
            if terminal_result == "failed_rolled_back"
            else "contact_support"
            if terminal_result == "failed_recovery_required"
            else "review_restore_status"
            if terminal_result
            else "wait"
        ),
        "video_archive_modified": False,
    }


def _journal_event(
    request: dict[str, Any],
    *,
    phase: str,
    pre_restore_backup_id: str | None,
    destructive_started: bool,
    terminal_result: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    event = {
        "schema_version": 1,
        "operation_id": request["operation_id"],
        "submission_id": request["submission_id"],
        "phase": phase,
        "recorded_at": utcnow(),
        "source_artifact_id": request["artifact"]["artifact_id"],
        "pre_restore_backup_id": pre_restore_backup_id,
        "destructive_started": bool(destructive_started),
        "terminal_result": terminal_result,
        "reason_code": reason_code,
        "video_archive_modified": False,
    }
    write_json(RESTORE_JOURNAL_FILE, event)
    event_dir = (
        RESTORE_JOURNAL_DIR / request["operation_id"]
    )
    event_path = (
        event_dir
        / (
            f"{int(time.time() * 1000):013d}-"
            f"{phase}-{uuid.uuid4().hex[:8]}.json"
        )
    )
    write_json(event_path, event)
    return event


def publish_restore_phase(
    request: dict[str, Any],
    *,
    phase: str,
    pre_restore_backup_id: str | None = None,
    destructive_started: bool = False,
) -> None:
    _journal_event(
        request,
        phase=phase,
        pre_restore_backup_id=pre_restore_backup_id,
        destructive_started=destructive_started,
    )
    write_json(
        RESTORE_PUBLIC_STATUS_FILE,
        _restore_public_payload(
            request,
            phase=phase,
            pre_restore_backup_id=pre_restore_backup_id,
        ),
    )


def finish_restore_request(
    request: dict[str, Any],
    *,
    result: str,
    reason_code: str | None,
    pre_restore_backup_id: str | None,
    destructive_started: bool,
    failed_phase: str | None = None,
) -> None:
    if result not in RESTORE_TERMINAL_RESULTS:
        raise HelperError(
            "restore_terminal_invalid",
            "Restore terminal result is invalid.",
        )
    finished = utcnow()
    safe_failed_phase = (
        failed_phase
        if result != "completed"
        and failed_phase in RESTORE_OPERATIONAL_PHASES
        else None
    )
    terminal = {
        "status": result,
        "finished_at": finished,
        "reason_code": (
            safe_text(reason_code, 80)
            if reason_code
            and MACHINE_CODE_RE.fullmatch(reason_code)
            else None
        ),
        "failed_phase": safe_failed_phase,
    }
    request = {
        **request,
        "state": "terminal",
        "updated_at": finished,
        "terminal": terminal,
    }
    with admission_guard():
        current = validate_restore_request(
            read_json(RESTORE_REQUEST_FILE)
        )
        if (
            current is None
            or current["operation_id"]
            != request["operation_id"]
            or current["state"] not in RESTORE_ACTIVE_STATES
        ):
            raise HelperError(
                "restore_request_changed",
                "Restore request changed during execution.",
            )
        try:
            write_json(RESTORE_REQUEST_FILE, request)
        except HelperError:
            observed = validate_restore_request(
                read_json(RESTORE_REQUEST_FILE)
            )
            if (
                observed is None
                or observed.get("operation_id")
                != request["operation_id"]
                or observed.get("state") != "terminal"
                or observed.get("terminal") != terminal
            ):
                raise
        try:
            write_json(
                restore_receipt_path(request["submission_id"]),
                request,
            )
        except HelperError:
            pass
    try:
        _journal_event(
            request,
            phase=result,
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=destructive_started,
            terminal_result=result,
            reason_code=terminal["reason_code"],
        )
    except HelperError:
        pass
    try:
        write_json(
            RESTORE_PUBLIC_STATUS_FILE,
            _restore_public_payload(
                request,
                phase=result,
                status=result,
                pre_restore_backup_id=pre_restore_backup_id,
                terminal_result=result,
                reason_code=terminal["reason_code"],
                failed_phase=terminal["failed_phase"],
            ),
        )
    except HelperError:
        pass
    if result in {"completed", "failed_rolled_back", "blocked"}:
        try:
            set_restore_writer_fence(enabled=False)
        except HelperError as exc:
            # The safe terminal DB outcome is already durable.  Leaving the
            # containers at restart=no is fail-safe; stable startup
            # reconciliation will retry the normal policy convergence.
            print(
                f"WARNING [{exc.category}]: {exc}",
                file=sys.stderr,
            )


def reconcile_restore_terminal_projection() -> None:
    request = validate_restore_request(
        read_json(RESTORE_REQUEST_FILE)
    )
    if request is None or request.get("state") != "terminal":
        return
    terminal = request["terminal"]
    result = terminal["status"]
    try:
        journal = read_json(RESTORE_JOURNAL_FILE)
    except HelperError:
        journal = None
    journal_matches = bool(
        isinstance(journal, dict)
        and journal.get("operation_id") == request["operation_id"]
    )
    pre_restore_backup_id = (
        journal.get("pre_restore_backup_id")
        if journal_matches
        else None
    )
    try:
        destructive_started = restore_destructive_started(
            request["operation_id"]
        )
    except HelperError:
        destructive_started = result in {
            "completed",
            "failed_rolled_back",
            "failed_recovery_required",
        }
    try:
        receipt = validate_restore_request(
            read_json(restore_receipt_path(request["submission_id"]))
        )
    except HelperError:
        receipt = None
    if (
        receipt is None
        or receipt.get("operation_id") != request["operation_id"]
        or receipt.get("terminal") != terminal
    ):
        try:
            write_json(
                restore_receipt_path(request["submission_id"]),
                request,
            )
        except HelperError:
            pass
    if not (
        journal_matches
        and journal.get("terminal_result") == result
        and journal.get("reason_code") == terminal.get("reason_code")
    ):
        try:
            _journal_event(
                request,
                phase=result,
                pre_restore_backup_id=pre_restore_backup_id,
                destructive_started=destructive_started,
                terminal_result=result,
                reason_code=terminal.get("reason_code"),
            )
        except HelperError:
            pass
    try:
        public = read_json(RESTORE_PUBLIC_STATUS_FILE)
    except HelperError:
        public = None
    if not (
        isinstance(public, dict)
        and public.get("operation_id") == request["operation_id"]
        and public.get("terminal_result") == result
        and public.get("finished_at") == terminal["finished_at"]
    ):
        try:
            write_json(
                RESTORE_PUBLIC_STATUS_FILE,
                _restore_public_payload(
                    request,
                    phase=result,
                    status=result,
                    pre_restore_backup_id=pre_restore_backup_id,
                    terminal_result=result,
                    reason_code=terminal.get("reason_code"),
                    failed_phase=terminal.get("failed_phase"),
                ),
            )
        except HelperError:
            pass


def claim_restore_request() -> dict[str, Any] | None:
    with admission_guard():
        request = validate_restore_request(
            read_json(RESTORE_REQUEST_FILE)
        )
        if (
            request is None
            or request["state"] == "terminal"
        ):
            return None
        if request["state"] == "admitted":
            claimed_at = utcnow()
            request["state"] = "claimed"
            request["claimed_at"] = claimed_at
            request["updated_at"] = claimed_at
            write_json(RESTORE_REQUEST_FILE, request)
            write_json(
                restore_receipt_path(
                    request["submission_id"]
                ),
                request,
            )
        return request


def _compose_override() -> str:
    override = str(
        os.getenv("KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE")
        or os.getenv("KM_VMS_DOCKER_COMPOSE")
        or ""
    ).strip()
    if override.startswith("/") and not Path(override).exists():
        return ""
    if (
        override == "docker-compose"
        and shutil.which("docker-compose") is None
    ):
        return ""
    return override


def restore_compose_command(
    *arguments: str,
    timeout_seconds: int = 900,
) -> subprocess.CompletedProcess[str]:
    stable_dir = compose_app_dir()
    source_dir = resolve_update_source_dir(stable_dir)
    common = source_dir / "scripts" / "km-vms-compose-common.sh"
    if not common.is_file() or common.is_symlink():
        raise HelperError(
            "restore_compose_contract_missing",
            "Restore Compose contract is unavailable.",
        )
    shell = (
        '. "$1"; override="$2"; stable="$3"; source="$4"; '
        "shift 4; "
        'km_vms_detect_compose "$override" || exit 91; '
        'km_vms_compose_for_source "$stable" "$source" "$@"'
    )
    try:
        result = subprocess.run(
            [
                "sh",
                "-c",
                shell,
                "km-vms-restore-compose",
                str(common),
                _compose_override(),
                str(stable_dir),
                str(source_dir),
                *arguments,
            ],
            cwd=stable_dir,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError(
            "restore_compose_failed",
            "Restore Compose command failed.",
        ) from exc
    return result


def run_restore_executor(
    request: dict[str, Any],
    action: str,
    *,
    artifact_id: str | None = None,
    mode: str = "source",
    recorder_not_before_epoch: float | None = None,
    final_db_outcome: str | None = None,
    timeout_seconds: int = 1200,
) -> dict[str, Any]:
    command = [
        "run",
        "--rm",
        "--no-deps",
        "restore-executor",
        action,
        "--operation-id",
        request["operation_id"],
    ]
    if artifact_id:
        command.extend(["--artifact-id", artifact_id])
    if action == "restore":
        command.extend(["--mode", mode])
    if action == "invalidate-integrity":
        if final_db_outcome not in {"source", "rollback"}:
            raise HelperError(
                "restore_integrity_outcome_invalid",
                "Restore integrity outcome is invalid.",
            )
        command.extend(
            ["--final-db-outcome", final_db_outcome]
        )
    if action == "recorder-proof":
        if recorder_not_before_epoch is None:
            raise HelperError(
                "restore_recorder_proof_boundary_missing",
                "Recorder proof boundary is unavailable.",
            )
        command.extend(
            [
                "--recorder-not-before-epoch",
                f"{float(recorder_not_before_epoch):.6f}",
            ]
        )
    result = restore_compose_command(
        *command,
        timeout_seconds=timeout_seconds,
    )
    payload = read_json(RESTORE_EXECUTOR_RESULT_FILE)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("operation_id")
        != request["operation_id"]
        or payload.get("action") != action
        or payload.get("status")
        not in {"completed", "failed"}
    ):
        raise HelperError(
            "restore_executor_result_invalid",
            "Restore executor result is unavailable.",
            diagnostics={
                "mutation_started": bool(
                    restore_destructive_started(
                        request["operation_id"]
                    )
                )
            },
        )
    if result.returncode != 0 or payload["status"] != "completed":
        code = safe_text(
            payload.get("reason_code"),
            80,
        ) or "restore_executor_failed"
        raise HelperError(
            code
            if MACHINE_CODE_RE.fullmatch(code)
            else "restore_executor_failed",
            "Restore executor failed.",
            diagnostics={
                "mutation_started": bool(
                    payload.get("mutation_started")
                )
            },
        )
    details = payload.get("details")
    return details if isinstance(details, dict) else {}


def _restore_integrity_idempotency_key(
    request: dict[str, Any],
    final_db_outcome: str,
) -> str:
    if final_db_outcome not in {"source", "rollback"}:
        raise HelperError(
            "restore_integrity_outcome_invalid",
            "Restore integrity outcome is invalid.",
        )
    identity = (
        "archive-integrity-post-restore:v1:"
        f"{request['operation_id']}:{final_db_outcome}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _restore_integrity_convergence_status(
    request: dict[str, Any],
    final_db_outcome: str,
) -> dict[str, Any] | None:
    try:
        payload = read_json(RESTORE_INTEGRITY_CONVERGENCE_FILE)
    except HelperError:
        return None
    if not isinstance(payload, dict):
        return None
    expected_key = _restore_integrity_idempotency_key(
        request,
        final_db_outcome,
    )
    if (
        payload.get("schema")
        != RESTORE_INTEGRITY_CONVERGENCE_SCHEMA
        or payload.get("operation_id") != request["operation_id"]
        or payload.get("final_db_outcome") != final_db_outcome
        or payload.get("idempotency_key") != expected_key
        or payload.get("state")
        not in {"invalidated", "scheduled", "retry_required"}
        or not isinstance(payload.get("attempt_count"), int)
        or isinstance(payload.get("attempt_count"), bool)
        or payload["attempt_count"] < 0
    ):
        return None
    return payload


def _write_restore_integrity_convergence(
    request: dict[str, Any],
    *,
    final_db_outcome: str,
    state: str,
    attempt_count: int,
    scan_id: str | None = None,
    reason_code: str | None = None,
    next_retry_at_epoch: float | None = None,
) -> None:
    if state not in {"invalidated", "scheduled", "retry_required"}:
        raise HelperError(
            "restore_integrity_state_invalid",
            "Restore integrity convergence state is invalid.",
        )
    write_json(
        RESTORE_INTEGRITY_CONVERGENCE_FILE,
        {
            "schema": RESTORE_INTEGRITY_CONVERGENCE_SCHEMA,
            "operation_id": request["operation_id"],
            "final_db_outcome": final_db_outcome,
            "idempotency_key": _restore_integrity_idempotency_key(
                request,
                final_db_outcome,
            ),
            "state": state,
            "attempt_count": max(0, int(attempt_count)),
            "scan_id": safe_text(scan_id, 64) if scan_id else None,
            "reason_code": (
                reason_code
                if reason_code
                and MACHINE_CODE_RE.fullmatch(reason_code)
                else None
            ),
            "next_action": (
                "retry_integrity_scan"
                if state == "retry_required"
                else None
            ),
            "next_retry_at_epoch": (
                float(next_retry_at_epoch)
                if next_retry_at_epoch is not None
                else None
            ),
            "updated_at": utcnow(),
        },
    )


def invalidate_post_restore_integrity(
    request: dict[str, Any],
    *,
    final_db_outcome: str,
) -> dict[str, Any]:
    details = run_restore_executor(
        request,
        "invalidate-integrity",
        final_db_outcome=final_db_outcome,
        timeout_seconds=120,
    )
    _write_restore_integrity_convergence(
        request,
        final_db_outcome=final_db_outcome,
        state="invalidated",
        attempt_count=0,
    )
    return details


def run_restore_api_integrity_executor(
    request: dict[str, Any],
    *,
    final_db_outcome: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    command = [
        "exec",
        "-T",
        "api",
        "python3",
        "-m",
        "app.services.current_db_restore_executor",
        "enqueue-integrity",
        "--operation-id",
        request["operation_id"],
        "--final-db-outcome",
        final_db_outcome,
    ]
    result = restore_compose_command(
        *command,
        timeout_seconds=timeout_seconds,
    )
    payload = read_json(RESTORE_EXECUTOR_RESULT_FILE)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("operation_id") != request["operation_id"]
        or payload.get("action") != "enqueue-integrity"
        or payload.get("status") not in {"completed", "failed"}
    ):
        raise HelperError(
            "restore_integrity_enqueue_result_invalid",
            "Archive integrity scheduling result is unavailable.",
        )
    if result.returncode != 0 or payload["status"] != "completed":
        raise HelperError(
            "restore_integrity_enqueue_failed",
            "Archive integrity scan could not be scheduled.",
        )
    details = payload.get("details")
    if not isinstance(details, dict) or not safe_text(
        details.get("scan_id"),
        64,
    ):
        raise HelperError(
            "restore_integrity_enqueue_result_invalid",
            "Archive integrity scheduling result is unavailable.",
        )
    return details


def schedule_post_restore_integrity(
    request: dict[str, Any],
    *,
    final_db_outcome: str,
) -> bool:
    previous = _restore_integrity_convergence_status(
        request,
        final_db_outcome,
    )
    attempts = int((previous or {}).get("attempt_count") or 0) + 1
    try:
        details = run_restore_api_integrity_executor(
            request,
            final_db_outcome=final_db_outcome,
        )
    except Exception:
        try:
            _write_restore_integrity_convergence(
                request,
                final_db_outcome=final_db_outcome,
                state="retry_required",
                attempt_count=attempts,
                reason_code="restore_integrity_enqueue_failed",
                next_retry_at_epoch=(
                    time.time() + RESTORE_INTEGRITY_RETRY_SECONDS
                    if attempts < RESTORE_INTEGRITY_MAX_ATTEMPTS
                    else None
                ),
            )
        except Exception:
            pass
        return False
    try:
        _write_restore_integrity_convergence(
            request,
            final_db_outcome=final_db_outcome,
            state="scheduled",
            attempt_count=attempts,
            scan_id=safe_text(details.get("scan_id"), 64),
        )
    except Exception:
        pass
    return True


def reconcile_restore_integrity_convergence() -> None:
    request = validate_restore_request(
        read_json(RESTORE_REQUEST_FILE)
    )
    if request is None or request.get("state") != "terminal":
        return
    terminal_status = str(
        (request.get("terminal") or {}).get("status") or ""
    )
    final_db_outcome = {
        "completed": "source",
        "failed_rolled_back": "rollback",
    }.get(terminal_status)
    if final_db_outcome is None:
        return
    convergence = _restore_integrity_convergence_status(
        request,
        final_db_outcome,
    )
    if convergence and convergence.get("state") == "scheduled":
        return
    if not convergence or convergence.get("state") == "invalidated":
        schedule_post_restore_integrity(
            request,
            final_db_outcome=final_db_outcome,
        )
        return
    if convergence.get("state") != "retry_required":
        return
    attempts = int(convergence.get("attempt_count") or 0)
    retry_at = convergence.get("next_retry_at_epoch")
    if (
        attempts >= RESTORE_INTEGRITY_MAX_ATTEMPTS
        or not isinstance(retry_at, (int, float))
        or isinstance(retry_at, bool)
        or float(retry_at) > time.time()
    ):
        return
    schedule_post_restore_integrity(
        request,
        final_db_outcome=final_db_outcome,
    )


def _service_container_id(service: str) -> str | None:
    if service not in RESTORE_SERVICE_ALLOWLIST:
        return None
    result = restore_compose_command(
        "ps",
        "-q",
        service,
        timeout_seconds=30,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(
        r"[0-9a-f]{12,64}",
        value,
    ) else None


def _existing_restore_service_container_id(service: str) -> str:
    if service not in RESTORE_SERVICE_ALLOWLIST:
        raise HelperError(
            "restore_service_not_allowed",
            "Restore service is not allowlisted.",
        )
    try:
        result = restore_compose_command(
            "ps",
            "-q",
            "--all",
            service,
            timeout_seconds=30,
        )
    except HelperError as exc:
        raise HelperError(
            "restore_service_discovery_failed",
            "Restore service container discovery failed.",
        ) from exc
    container_ids = [
        value.strip()
        for value in result.stdout.splitlines()
        if value.strip()
    ]
    if (
        result.returncode != 0
        or len(container_ids) != 1
        or not re.fullmatch(r"[0-9a-f]{12,64}", container_ids[0])
    ):
        raise HelperError(
            "restore_service_container_unavailable",
            "Exact restore service container is unavailable.",
        )
    container_id = container_ids[0]
    try:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "com.docker.compose.service" }}',
                container_id,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError(
            "restore_service_discovery_failed",
            "Restore service container validation failed.",
        ) from exc
    if (
        inspected.returncode != 0
        or inspected.stdout.strip() != service
    ):
        raise HelperError(
            "restore_service_identity_mismatch",
            "Restore service container identity did not match.",
        )
    return container_id


def _run_existing_restore_service_action(
    service: str,
    action: str,
) -> str:
    if action not in {"start", "restart"}:
        raise HelperError(
            "restore_service_action_invalid",
            "Restore service action is invalid.",
        )
    container_id = _existing_restore_service_container_id(service)
    try:
        result = subprocess.run(
            ["docker", action, container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError(
            "restore_service_action_failed",
            "Restore service action failed.",
        ) from exc
    if result.returncode != 0:
        raise HelperError(
            "restore_service_action_failed",
            "Restore service action failed.",
        )
    return container_id


def _service_running_state(service: str) -> bool | None:
    if service not in RESTORE_SERVICE_ALLOWLIST:
        return None
    try:
        result = restore_compose_command(
            "ps",
            "-q",
            "--all",
            service,
            timeout_seconds=30,
        )
    except HelperError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return False
    if not re.fullmatch(r"[0-9a-f]{12,64}", value):
        return None
    try:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                value,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    observed = inspected.stdout.strip().lower()
    if inspected.returncode != 0 or observed not in {"true", "false"}:
        return None
    return observed == "true"


def wait_for_restore_writers_stopped(
    *,
    timeout_seconds: int = 60,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        states = {
            service: _service_running_state(service)
            for service in ("api", "recorder")
        }
        if all(state is False for state in states.values()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def wait_for_service(
    service: str,
    *,
    require_health: bool,
    timeout_seconds: int,
    expected_container_id: str | None = None,
) -> bool:
    if service not in RESTORE_SERVICE_ALLOWLIST:
        return False
    if (
        expected_container_id is not None
        and not re.fullmatch(
            r"[0-9a-f]{12,64}",
            expected_container_id,
        )
    ):
        return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container_id = (
            expected_container_id
            or _service_container_id(service)
        )
        if container_id:
            template = (
                "{{.State.Health.Status}}"
                if require_health
                else "{{.State.Running}}"
            )
            inspected = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    template,
                    container_id,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
                check=False,
            )
            observed = inspected.stdout.strip().lower()
            if (
                inspected.returncode == 0
                and (
                    observed == "healthy"
                    if require_health
                    else observed == "true"
                )
            ):
                return True
        time.sleep(2)
    return False


def stop_restore_writers() -> None:
    set_restore_writer_fence(enabled=True)
    try:
        restore_compose_command(
            "stop",
            "recorder",
            "api",
            timeout_seconds=180,
        )
    except HelperError:
        pass
    if wait_for_restore_writers_stopped():
        return
    raise HelperError(
        "restore_writer_isolation_failed",
        "Database writers could not be isolated.",
    )


def start_restore_api() -> str:
    try:
        container_id = _run_existing_restore_service_action(
            "api",
            "start",
        )
    except HelperError as exc:
        raise HelperError(
            "restore_api_start_failed",
            "API container did not start after restore.",
        ) from exc
    if not wait_for_service(
        "api",
        require_health=True,
        timeout_seconds=120,
        expected_container_id=container_id,
    ):
        raise HelperError(
            "restore_api_health_failed",
            "API did not become healthy after restore.",
        )
    return container_id


def start_restore_recorder() -> str:
    try:
        container_id = _run_existing_restore_service_action(
            "recorder",
            "start",
        )
    except HelperError as exc:
        raise HelperError(
            "restore_recorder_start_failed",
            "Recorder container did not start after restore.",
        ) from exc
    if not wait_for_service(
        "recorder",
        require_health=False,
        timeout_seconds=60,
        expected_container_id=container_id,
    ):
        raise HelperError(
            "restore_recorder_start_failed",
            "Recorder did not start after restore.",
        )
    return container_id


def require_restore_recorder_start_proof(
    request: dict[str, Any],
    *,
    not_before_epoch: float,
    expected_container_id: str | None = None,
) -> None:
    try:
        run_restore_executor(
            request,
            "recorder-proof",
            recorder_not_before_epoch=not_before_epoch,
            timeout_seconds=120,
        )
    except HelperError as exc:
        raise HelperError(
            "restore_recorder_start_failed",
            "Recorder did not become operational after restore.",
            diagnostics=exc.diagnostics,
        ) from exc
    if not wait_for_service(
        "recorder",
        require_health=False,
        timeout_seconds=10,
        expected_container_id=expected_container_id,
    ):
        raise HelperError(
            "restore_recorder_start_failed",
            "Recorder stopped before operational proof completed.",
        )


def start_restore_recorder_with_proof(
    request: dict[str, Any],
) -> None:
    not_before_epoch = time.time()
    container_id = start_restore_recorder()
    require_restore_recorder_start_proof(
        request,
        not_before_epoch=not_before_epoch,
        expected_container_id=container_id,
    )


def restart_restore_recorder_with_proof(
    request: dict[str, Any],
) -> None:
    not_before_epoch = time.time()
    try:
        container_id = _run_existing_restore_service_action(
            "recorder",
            "restart",
        )
    except HelperError as exc:
        raise HelperError(
            "restore_recorder_start_failed",
            "Recorder did not restart during restore recovery.",
        ) from exc
    if not wait_for_service(
        "recorder",
        require_health=False,
        timeout_seconds=60,
        expected_container_id=container_id,
    ):
        raise HelperError(
            "restore_recorder_start_failed",
            "Recorder did not restart during restore recovery.",
        )
    require_restore_recorder_start_proof(
        request,
        not_before_epoch=not_before_epoch,
        expected_container_id=container_id,
    )


def restart_restore_writers_best_effort(
    request: dict[str, Any],
) -> bool:
    try:
        recorder_state_before_start = _service_running_state(
            "recorder"
        )
    except Exception:
        recorder_state_before_start = None

    api_recovered = False
    try:
        start_restore_api()
        api_recovered = True
    except Exception:
        pass

    recorder_recovered = False
    try:
        if recorder_state_before_start is False:
            start_restore_recorder_with_proof(request)
        elif recorder_state_before_start is None:
            restart_restore_recorder_with_proof(request)
        else:
            start_restore_recorder()
            run_restore_executor(
                request,
                "recorder-live-proof",
                timeout_seconds=120,
            )
        if not wait_for_service(
            "recorder",
            require_health=False,
            timeout_seconds=10,
        ):
            return False
        recorder_recovered = True
    except Exception:
        pass
    return api_recovered and recorder_recovered


def rollback_current_restore(
    request: dict[str, Any],
    *,
    pre_restore_backup_id: str | None,
    reason_code: str,
    failed_phase: str | None = None,
) -> None:
    source_failed_phase = (
        failed_phase
        if failed_phase in RESTORE_OPERATIONAL_PHASES
        else "restore_running"
    )

    def recovery_required(
        recovery_reason: str,
        recovery_phase: str,
    ) -> None:
        finish_restore_request(
            request,
            result="failed_recovery_required",
            reason_code=recovery_reason,
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=True,
            failed_phase=recovery_phase,
        )

    if (
        not pre_restore_backup_id
        or not RESTORE_ARTIFACT_ID_RE.fullmatch(
            pre_restore_backup_id
        )
    ):
        recovery_required(
            "pre_restore_backup_missing",
            "restore_running",
        )
        return
    try:
        stop_restore_writers()
    except HelperError:
        recovery_required(
            "automatic_rollback_isolation_failed",
            "writers_paused",
        )
        return
    try:
        run_restore_executor(
            request,
            "restore",
            artifact_id=pre_restore_backup_id,
            mode="rollback",
        )
        invalidate_post_restore_integrity(
            request,
            final_db_outcome="rollback",
        )
    except Exception:
        recovery_required(
            "automatic_rollback_database_failed",
            "restore_running",
        )
        return
    publish_restore_phase(
        request,
        phase="services_starting",
        pre_restore_backup_id=pre_restore_backup_id,
        destructive_started=True,
    )
    try:
        start_restore_api()
    except Exception:
        recovery_required(
            "automatic_rollback_api_recovery_failed",
            "services_starting",
        )
        return
    publish_restore_phase(
        request,
        phase="post_restore_check",
        pre_restore_backup_id=pre_restore_backup_id,
        destructive_started=True,
    )
    try:
        run_restore_executor(request, "post-check")
    except Exception:
        recovery_required(
            "automatic_rollback_validation_failed",
            "post_restore_check",
        )
        return
    try:
        start_restore_recorder_with_proof(request)
    except Exception:
        recovery_required(
            "automatic_rollback_recorder_recovery_failed",
            "post_restore_check",
        )
        return
    finish_restore_request(
        request,
        result="failed_rolled_back",
        reason_code=reason_code,
        pre_restore_backup_id=pre_restore_backup_id,
        destructive_started=True,
        failed_phase=source_failed_phase,
    )
    schedule_post_restore_integrity(
        request,
        final_db_outcome="rollback",
    )


def run_current_restore(request: dict[str, Any]) -> None:
    prior_journal = read_json(RESTORE_JOURNAL_FILE)
    prior_marker = read_json(
        RESTORE_DESTRUCTIVE_MARKER_FILE
    )
    if (
        isinstance(prior_marker, dict)
        and prior_marker.get("operation_id")
        == request["operation_id"]
        and prior_marker.get("mutation_started") is True
    ):
        pre_restore_backup_id = (
            prior_journal.get("pre_restore_backup_id")
            if isinstance(prior_journal, dict)
            else None
        )
        rollback_current_restore(
            request,
            pre_restore_backup_id=pre_restore_backup_id,
            reason_code="restore_interrupted_after_mutation",
            failed_phase=(
                prior_journal.get("phase")
                if isinstance(prior_journal, dict)
                and prior_journal.get("phase")
                in RESTORE_OPERATIONAL_PHASES
                else "restore_running"
            ),
        )
        return
    if (
        request["state"] == "claimed"
        and isinstance(prior_journal, dict)
        and prior_journal.get("operation_id")
        == request["operation_id"]
    ):
        writers_recovered = restart_restore_writers_best_effort(
            request
        )
        finish_restore_request(
            request,
            result="blocked",
            reason_code=(
                "restore_interrupted_before_mutation"
                if writers_recovered
                else "restore_writer_isolation_failed"
            ),
            pre_restore_backup_id=prior_journal.get(
                "pre_restore_backup_id"
            ),
            destructive_started=False,
            failed_phase=(
                prior_journal.get("phase")
                if prior_journal.get("phase")
                in RESTORE_OPERATIONAL_PHASES
                else "preflight"
            ),
        )
        return

    pre_restore_backup_id: str | None = None
    writers_isolation_attempted = False
    destructive_started = False
    current_phase = "preflight"
    try:
        current_phase = "preflight"
        publish_restore_phase(request, phase="preflight")
        run_restore_executor(request, "preflight")
        current_phase = "pre_restore_backup"
        publish_restore_phase(
            request,
            phase="pre_restore_backup",
        )
        writers_isolation_attempted = True
        current_phase = "writers_paused"
        publish_restore_phase(
            request,
            phase="writers_paused",
        )
        stop_restore_writers()
        current_phase = "pre_restore_backup"
        backup = run_restore_executor(
            request,
            "pre-restore-backup",
        )
        pre_restore_backup_id = safe_text(
            backup.get("pre_restore_backup_id"),
            80,
        )
        if (
            not pre_restore_backup_id
            or not RESTORE_ARTIFACT_ID_RE.fullmatch(
                pre_restore_backup_id
            )
            or backup.get("verified") is not True
        ):
            raise HelperError(
                "pre_restore_backup_verification_failed",
                "Pre-restore backup verification failed.",
            )
        current_phase = "restore_running"
        publish_restore_phase(
            request,
            phase="restore_running",
            pre_restore_backup_id=pre_restore_backup_id,
        )
        destructive_started = True
        run_restore_executor(
            request,
            "restore",
            artifact_id=request["artifact"]["artifact_id"],
            mode="source",
        )
        invalidate_post_restore_integrity(
            request,
            final_db_outcome="source",
        )
        current_phase = "services_starting"
        publish_restore_phase(
            request,
            phase="services_starting",
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=True,
        )
        start_restore_api()
        current_phase = "post_restore_check"
        publish_restore_phase(
            request,
            phase="post_restore_check",
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=True,
        )
        run_restore_executor(request, "post-check")
        start_restore_recorder_with_proof(request)
        finish_restore_request(
            request,
            result="completed",
            reason_code=None,
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=True,
        )
        schedule_post_restore_integrity(
            request,
            final_db_outcome="source",
        )
        return
    except HelperError as exc:
        destructive_started = bool(
            destructive_started
            or exc.diagnostics.get("mutation_started")
            or restore_destructive_started(
                request["operation_id"]
            )
        )
        if destructive_started:
            rollback_current_restore(
                request,
                pre_restore_backup_id=pre_restore_backup_id,
                reason_code=exc.category,
                failed_phase=current_phase,
            )
            return
        writers_recovered = bool(
            not writers_isolation_attempted
            or restart_restore_writers_best_effort(request)
        )
        finish_restore_request(
            request,
            result="blocked",
            reason_code=(
                exc.category
                if writers_recovered
                else "restore_writer_isolation_failed"
            ),
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=False,
            failed_phase=current_phase,
        )
        return
    except Exception:
        destructive_started = bool(
            destructive_started
            or restore_destructive_started(
                request["operation_id"]
            )
        )
        if destructive_started:
            rollback_current_restore(
                request,
                pre_restore_backup_id=pre_restore_backup_id,
                reason_code="restore_helper_exception",
                failed_phase=current_phase,
            )
            return
        writers_recovered = bool(
            not writers_isolation_attempted
            or restart_restore_writers_best_effort(request)
        )
        finish_restore_request(
            request,
            result="blocked",
            reason_code=(
                "restore_helper_exception"
                if writers_recovered
                else "restore_writer_isolation_failed"
            ),
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=False,
            failed_phase=current_phase,
        )
        return


def converge_unhandled_restore_failure(
    request: dict[str, Any],
    *,
    reason_code: str,
) -> None:
    journal = read_json(RESTORE_JOURNAL_FILE)
    pre_restore_backup_id = (
        journal.get("pre_restore_backup_id")
        if isinstance(journal, dict)
        and journal.get("operation_id") == request["operation_id"]
        else None
    )
    if restore_destructive_started(request["operation_id"]):
        rollback_current_restore(
            request,
            pre_restore_backup_id=pre_restore_backup_id,
            reason_code=reason_code,
            failed_phase=(
                journal.get("phase")
                if isinstance(journal, dict)
                and journal.get("phase")
                in RESTORE_OPERATIONAL_PHASES
                else "restore_running"
            ),
        )
        return
    writers_recovered = restart_restore_writers_best_effort(
        request
    )
    finish_restore_request(
        request,
        result="blocked",
        reason_code=(
            reason_code
            if writers_recovered
            else "restore_writer_isolation_failed"
        ),
        pre_restore_backup_id=pre_restore_backup_id,
        destructive_started=False,
        failed_phase=(
            journal.get("phase")
            if isinstance(journal, dict)
            and journal.get("phase")
            in RESTORE_OPERATIONAL_PHASES
            else "preflight"
        ),
    )


def main() -> int:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    RESTORE_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    RESTORE_PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    MAINTENANCE_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            publish_restore_helper_health()
        except HelperError:
            pass
        try:
            reconcile_restore_terminal_projection()
        except HelperError:
            pass
        try:
            reconcile_restore_integrity_convergence()
        except HelperError:
            pass
        try:
            restore_pending = restore_request_may_need_execution()
        except HelperError:
            restore_pending = False
        if restore_pending:
            with restore_execution_lease() as restore_acquired:
                if restore_acquired:
                    restore_request: dict[str, Any] | None = None
                    try:
                        restore_request = claim_restore_request()
                        if restore_request:
                            run_current_restore(restore_request)
                    except HelperError as exc:
                        if restore_request:
                            try:
                                converge_unhandled_restore_failure(
                                    restore_request,
                                    reason_code=exc.category,
                                )
                            except Exception:
                                pass
                    except Exception:
                        if restore_request:
                            try:
                                converge_unhandled_restore_failure(
                                    restore_request,
                                    reason_code="restore_helper_exception",
                                )
                            except Exception:
                                pass
            time.sleep(POLL_SECONDS)
            continue
        try:
            if not request_may_need_execution():
                time.sleep(POLL_SECONDS)
                continue
        except HelperError:
            time.sleep(POLL_SECONDS)
            continue
        with helper_execution_lease() as acquired:
            if not acquired:
                time.sleep(POLL_SECONDS)
                continue
            request: dict[str, Any] | None = None
            try:
                request = claim_current_request()
                if request:
                    if request.pop("_resume_activation", False):
                        resume_activation(request, compose_app_dir())
                    else:
                        run_update(request)
            except HelperError as exc:
                if request:
                    failed = base_status(
                        request,
                        "failed",
                        exc.phase,
                        steps_for(exc.phase, failed=True),
                        error_payload(exc.category),
                    )
                    if exc.diagnostics.get("installed_commit"):
                        failed["installed_commit"] = safe_text(
                            exc.diagnostics["installed_commit"],
                            40,
                        )
                    source_attempts = exc.diagnostics.get(
                        "source_acquisition_attempts"
                    )
                    if type(source_attempts) is int and 1 <= source_attempts <= 3:
                        failed["source_acquisition_attempts"] = source_attempts
                    failed["finished_at"] = failed["updated_at"]
                    try:
                        publish_terminal(request, failed)
                    except HelperError:
                        pass
            except Exception:
                if request:
                    failed = base_status(
                        request,
                        "failed",
                        "helper_exception",
                        steps_for("preflight", failed=True),
                        error_payload("helper_exception"),
                    )
                    failed["finished_at"] = failed["updated_at"]
                    try:
                        publish_terminal(request, failed)
                    except HelperError:
                        pass
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
