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
from datetime import datetime
from pathlib import Path
from typing import Any

import fcntl

APP_DIR = Path(os.getenv("KM_VMS_UPDATE_APP_DIR") or "/host-app")
HOST_APP_DIR_RAW = os.getenv("KM_VMS_UPDATE_HOST_APP_DIR") or ""
HOST_APP_DIR = Path(HOST_APP_DIR_RAW) if HOST_APP_DIR_RAW else None
CONTROL_DIR = APP_DIR / "data" / "update-control"
REQUEST_FILE = CONTROL_DIR / "update-request.json"
STATUS_FILE = CONTROL_DIR / "update-status.json"
PROGRESS_FILE = CONTROL_DIR / "update-progress.json"
APPLY_HISTORY_FILE = CONTROL_DIR / "update-apply-history.json"
ADMISSION_LOCK_FILE = CONTROL_DIR / "update-admission.lock"
HELPER_LEASE_FILE = CONTROL_DIR / "update-helper-claim.lock"

POLL_SECONDS = int(os.getenv("KM_VMS_UPDATE_HELPER_POLL_SECONDS") or "2")
MAX_CONTROL_BYTES = 64 * 1024
REQUEST_SCHEMA_VERSION = 3
REQUEST_DOCUMENT_TYPE = "update_apply_request"
REQUEST_STATES = {"admitted", "claimed", "terminal"}
TERMINAL = {"completed", "failed", "cancelled", "blocked"}
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
    try:
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > MAX_CONTROL_BYTES:
            raise HelperError(
                "control_file_invalid",
                "Update control file is invalid.",
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except HelperError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise HelperError(
            "control_file_invalid",
            "Update control file is invalid.",
        ) from exc
    if not isinstance(payload, dict):
        raise HelperError(
            "control_file_invalid",
            "Update control file must contain a JSON object.",
        )
    if path.parent == CONTROL_DIR and contains_sensitive_content(payload):
        raise HelperError(
            "control_payload_sensitive",
            "Update control payload contains sensitive content.",
        )
    return payload


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
        raise HelperError(
            "control_write_failed",
            "Update control payload could not be persisted.",
        ) from exc


@contextmanager
def admission_guard():
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with ADMISSION_LOCK_FILE.open("a+", encoding="utf-8") as lock_file:
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
    }
    return {
        "category": category,
        "message": messages.get(category, "Update apply failed."),
        "operator_action": "Review update status and retry only after the cause is resolved.",
    }


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
    }:
        return "applying"
    if value == "health_check":
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
    if current_step and not MACHINE_CODE_RE.fullmatch(current_step):
        current_step = None
    if phase and not MACHINE_CODE_RE.fullmatch(phase):
        phase = None
    return {"current_step": current_step, "phase": phase}


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
        not (HOST_APP_DIR / "docker-compose.yml").is_file()
        or not (HOST_APP_DIR / "scripts" / "update.sh").is_file()
    ):
        raise HelperError(
            "helper_host_app_dir_unmounted",
            "KM_VMS_UPDATE_HOST_APP_DIR is unavailable.",
        )
    return HOST_APP_DIR


def update_child_env(request: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["KM_VMS_UPDATE_HELPER_MODE"] = "1"
    env["KM_VMS_UPDATE_CONTROL_REQUEST_ID"] = str(request["request_id"])
    env["KM_VMS_UPDATE_PROGRESS_FILE"] = str(PROGRESS_FILE)
    helper_compose = os.getenv(
        "KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE",
        "",
    ).strip()
    if helper_compose:
        env["KM_VMS_DOCKER_COMPOSE"] = helper_compose
    else:
        inherited = env.get("KM_VMS_DOCKER_COMPOSE", "").strip()
        if inherited.startswith("/") and not Path(inherited).exists():
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
    if failed_phase == "schema_preflight":
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
) -> tuple[str, str, dict[str, Any]]:
    update_metadata = read_json(update_dir / ".km-vms-update.json")
    if not update_metadata:
        raise HelperError(
            "commit_missing",
            "Update metadata is missing.",
            phase="commit_verification",
        )
    if update_metadata.get("status") != "success":
        raise HelperError(
            "metadata_invalid",
            "Update metadata did not record success.",
            phase="commit_verification",
        )
    installed_commit = safe_text(update_metadata.get("commit_sha"), 40)
    if installed_commit != expected_commit:
        raise HelperError(
            "commit_mismatch",
            "Installed commit does not match the trusted target.",
            phase="commit_verification",
            diagnostics={"installed_commit": installed_commit or "missing"},
        )
    source_metadata = read_json(update_dir / ".km-vms-source.json")
    if source_metadata:
        source_commit = safe_text(source_metadata.get("commit_sha"), 40)
        if source_commit and source_commit != expected_commit:
            raise HelperError(
                "commit_mismatch",
                "Installed source commit does not match the trusted target.",
                phase="commit_verification",
                diagnostics={"installed_commit": source_commit},
            )
    release_metadata = read_json(update_dir / ".km-vms-release.json")
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
        if isinstance(update_metadata.get("validation_summary"), dict)
        else {}
    )
    host_status = safe_text(
        validation.get("release_identity_host_metadata_status"),
        40,
    ) or safe_text(release_metadata.get("metadata_status"), 40)
    api_status = safe_text(
        validation.get("release_identity_api_metadata_status"),
        40,
    )
    api_visible = validation.get("release_identity_api_visible") is True
    commit_verified = (
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


def run_update(request: dict[str, Any]) -> int:
    source = request["source"]
    update_dir = compose_app_dir()
    expected_commit = str(source["commit"])
    command = [
        "sh",
        "scripts/update.sh",
        "--github-repo",
        source["repo"],
        "--branch",
        source["apply_ref"],
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
    dry = run_child_with_progress(
        [*command, "--dry-run"],
        request,
        update_dir,
        env,
        timeout_seconds=1800,
        default_step="preflight",
        status_value="preflight",
    )
    if dry.returncode != 0:
        raise HelperError("preflight_failed", "Update preflight failed.")
    write_json(
        STATUS_FILE,
        base_status(
            request,
            "applying",
            "acquire_source",
            steps_for("acquire_source"),
        ),
    )
    applied = run_child_with_progress(
        command,
        request,
        update_dir,
        env,
        timeout_seconds=7200,
        default_step="acquire_source",
        status_value="applying",
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
                    status_value,
                    phase,
                    steps_for(step),
                )
                status_payload["current_step"] = step
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


def main() -> int:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    while True:
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
