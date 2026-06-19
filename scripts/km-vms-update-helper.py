#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

APP_DIR = Path(os.getenv("KM_VMS_UPDATE_APP_DIR") or "/host-app")
HOST_APP_DIR_RAW = os.getenv("KM_VMS_UPDATE_HOST_APP_DIR") or ""
HOST_APP_DIR = Path(HOST_APP_DIR_RAW) if HOST_APP_DIR_RAW else None
CONTROL_DIR = APP_DIR / "data" / "update-control"
REQUEST_FILE = CONTROL_DIR / "update-request.json"
STATUS_FILE = CONTROL_DIR / "update-status.json"
HISTORY_FILE = CONTROL_DIR / "update-helper-history.json"
POLL_SECONDS = int(os.getenv("KM_VMS_UPDATE_HELPER_POLL_SECONDS") or "2")
MAX_CONTROL_BYTES = 64 * 1024
TERMINAL = {"completed", "failed", "cancelled", "blocked"}
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SENSITIVE_VALUE_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9_]+|Bearer\s+[A-Za-z0-9._~+/=-]+|rtsp://[^@\s]+@|postgresql://[^:\s]+:[^@\s]+@|-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
FORBIDDEN_KEYS = {"url", "repo_url", "command", "docker", "compose", "token", "token_env", "token_file", "path", "backup_path", "database_url", "image", "env"}


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


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.stat().st_size > MAX_CONTROL_BYTES:
        raise HelperError("control_file_too_large", f"{path.name} exceeds the update-control size limit.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HelperError("control_file_invalid", f"{path.name} must be a JSON object.")
    rendered = json.dumps(payload, ensure_ascii=False)
    if SENSITIVE_VALUE_RE.search(rendered):
        raise HelperError("control_file_sensitive", f"{path.name} contains sensitive content.")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if SENSITIVE_VALUE_RE.search(rendered):
        raise HelperError("status_sensitive", "Refusing to write sensitive helper status.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def load_history() -> set[str]:
    payload = read_json(HISTORY_FILE) or {"processed_request_ids": []}
    values = payload.get("processed_request_ids")
    return {str(item) for item in values if isinstance(item, str)} if isinstance(values, list) else set()


def save_history(processed: set[str]) -> None:
    write_json(HISTORY_FILE, {"schema_version": 1, "updated_at": utcnow(), "processed_request_ids": sorted(processed)[-100:]})


def error_payload(category: str, message: str) -> dict[str, str]:
    return {
        "category": safe_text(category, 80) or "helper_error",
        "message": safe_text(message, 1000) or "Update helper failed.",
        "operator_action": "Review sanitized update status and use terminal recovery if needed.",
    }


def base_status(request: dict[str, Any], status: str, phase: str, steps: list[dict[str, str]], error: dict[str, str] | None = None) -> dict[str, Any]:
    source = request.get("source") if isinstance(request.get("source"), dict) else {}
    expected_commit = safe_text(source.get("commit"), 40)
    return {
        "schema_version": 1,
        "request_id": safe_text(request.get("request_id"), 80),
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


def failed_steps(category: str) -> list[dict[str, str]]:
    if category == "preflight_failed":
        return [
            {"name": "request", "status": "completed"},
            {"name": "preflight", "status": "failed"},
            {"name": "apply", "status": "pending"},
            {"name": "health_check", "status": "pending"},
        ]
    if category == "apply_failed":
        return [
            {"name": "request", "status": "completed"},
            {"name": "preflight", "status": "completed"},
            {"name": "apply", "status": "failed"},
            {"name": "health_check", "status": "pending"},
        ]
    if category == "health_check_failed":
        return [
            {"name": "request", "status": "completed"},
            {"name": "preflight", "status": "completed"},
            {"name": "apply", "status": "completed"},
            {"name": "health_check", "status": "failed"},
        ]
    if category in {"commit_mismatch", "commit_missing", "metadata_invalid"}:
        return [
            {"name": "request", "status": "completed"},
            {"name": "preflight", "status": "completed"},
            {"name": "apply", "status": "completed"},
            {"name": "health_check", "status": "completed"},
            {"name": "commit_verification", "status": "failed"},
        ]
    return [
        {"name": "request", "status": "failed"},
        {"name": "preflight", "status": "pending"},
        {"name": "apply", "status": "pending"},
        {"name": "health_check", "status": "pending"},
    ]


def validate_request(request: dict[str, Any]) -> None:
    if request.get("schema_version") != 1:
        raise HelperError("request_schema_unsupported", "Update request schema_version is unsupported.")
    if request.get("intent") != "apply_update" or request.get("confirmed") is not True:
        raise HelperError("request_not_confirmed", "Update request is not a confirmed apply_update intent.")
    for key in request:
        lower = str(key).lower()
        if key in FORBIDDEN_KEYS or any(token in lower for token in ("token", "secret", "command", "url")):
            raise HelperError("request_forbidden_field", f"Forbidden request field: {key}")
    source = request.get("source")
    if not isinstance(source, dict):
        raise HelperError("request_source_invalid", "Update request source must be trusted manifest metadata.")
    if source.get("kind") != "trusted_manifest" or source.get("source_type") != "github_tarball":
        raise HelperError("request_source_not_trusted", "Update request source must be trusted_manifest github_tarball.")
    for key in ("repo", "ref", "commit", "apply_ref"):
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise HelperError("request_source_incomplete", f"Trusted source field is missing: {key}")
    commit = str(source.get("commit") or "")
    apply_ref = str(source.get("apply_ref") or "")
    if not COMMIT_SHA_RE.fullmatch(commit):
        raise HelperError("request_commit_invalid", "Trusted source commit must be a full commit SHA.")
    if apply_ref != commit:
        raise HelperError("request_apply_ref_mismatch", "Trusted source apply_ref must equal commit.")


def compose_app_dir() -> Path:
    if HOST_APP_DIR is None:
        raise HelperError("helper_host_app_dir_missing", "KM_VMS_UPDATE_HOST_APP_DIR must be configured for Docker socket compose operations.")
    if not HOST_APP_DIR.is_absolute():
        raise HelperError("helper_host_app_dir_invalid", "KM_VMS_UPDATE_HOST_APP_DIR must be an absolute host path.")
    if not (HOST_APP_DIR / "docker-compose.yml").is_file() or not (HOST_APP_DIR / "scripts" / "update.sh").is_file():
        raise HelperError("helper_host_app_dir_unmounted", "KM_VMS_UPDATE_HOST_APP_DIR is not mounted inside update-helper.")
    return HOST_APP_DIR


def classify_apply_failure(update_dir: Path, stderr: str) -> HelperError:
    try:
        metadata = read_json(update_dir / ".km-vms-update.json")
    except HelperError:
        metadata = None
    failed_phase = metadata.get("failed_phase") if metadata else None
    if failed_phase == "health_check":
        return HelperError("health_check_failed", stderr or "Update health check failed.")
    return HelperError("apply_failed", stderr or "Update apply failed.")


def verify_installed_commit(update_dir: Path, expected_commit: str) -> tuple[str, str]:
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
    return installed_commit, expected_commit


def run_update(request: dict[str, Any]) -> int:
    source = request["source"]
    update_dir = compose_app_dir()
    expected_commit = str(source["commit"])
    common = ["sh", "scripts/update.sh", "--github-repo", source["repo"], "--branch", source["apply_ref"], "--yes"]
    if os.getenv("KM_VMS_GITHUB_PRIVATE", "0") == "1" or os.getenv("KMVMS_UPDATE_SOURCE_PRIVATE", "0") == "1":
        common.append("--github-private")
    env = os.environ.copy()
    env["KM_VMS_UPDATE_HELPER_MODE"] = "1"
    env["KM_VMS_UPDATE_CONTROL_REQUEST_ID"] = str(request["request_id"])
    steps = [
        {"name": "request", "status": "completed"},
        {"name": "preflight", "status": "running"},
        {"name": "apply", "status": "pending"},
        {"name": "health_check", "status": "pending"},
    ]
    write_json(STATUS_FILE, base_status(request, "preflight", "preflight", steps))
    dry = subprocess.run([*common, "--dry-run"], cwd=update_dir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=1800, check=False)
    if dry.returncode != 0:
        raise HelperError("preflight_failed", dry.stderr.strip() or "Update preflight failed.")
    steps = [
        {"name": "request", "status": "completed"},
        {"name": "preflight", "status": "completed"},
        {"name": "apply", "status": "running"},
        {"name": "health_check", "status": "pending"},
    ]
    write_json(STATUS_FILE, base_status(request, "applying", "applying", steps))
    apply = subprocess.run(common, cwd=update_dir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=7200, check=False)
    if apply.returncode != 0:
        raise classify_apply_failure(update_dir, apply.stderr.strip())
    installed_commit, expected_commit = verify_installed_commit(update_dir, expected_commit)
    steps = [
        {"name": "request", "status": "completed"},
        {"name": "preflight", "status": "completed"},
        {"name": "apply", "status": "completed"},
        {"name": "health_check", "status": "completed"},
        {"name": "commit_verification", "status": "completed"},
    ]
    completed = base_status(request, "completed", "completed", steps)
    completed["commit_verified"] = True
    completed["installed_commit"] = installed_commit
    completed["expected_commit"] = expected_commit
    write_json(STATUS_FILE, completed)
    return 0


def should_process(request: dict[str, Any], processed: set[str]) -> bool:
    request_id = str(request.get("request_id") or "")
    if not request_id or request_id in processed:
        return False
    status = read_json(STATUS_FILE)
    if status and status.get("request_id") == request_id and status.get("status") in TERMINAL:
        processed.add(request_id)
        save_history(processed)
        return False
    return True


def main() -> int:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            request = read_json(REQUEST_FILE)
            if not request:
                time.sleep(POLL_SECONDS)
                continue
            processed = load_history()
            if not should_process(request, processed):
                time.sleep(POLL_SECONDS)
                continue
            validate_request(request)
            run_update(request)
            processed.add(str(request["request_id"]))
            save_history(processed)
        except HelperError as exc:
            request = read_json(REQUEST_FILE) or {"request_id": None, "requested_at": utcnow(), "source": {}}
            failed = base_status(request, "failed", exc.phase, failed_steps(exc.category), error_payload(exc.category, str(exc)))
            if exc.diagnostics.get("installed_commit"):
                failed["installed_commit"] = safe_text(exc.diagnostics.get("installed_commit"), 40)
            write_json(STATUS_FILE, failed)
            if request.get("request_id"):
                processed = load_history()
                processed.add(str(request["request_id"]))
                save_history(processed)
        except Exception as exc:
            request = {"request_id": None, "requested_at": utcnow(), "source": {}}
            write_json(STATUS_FILE, base_status(request, "failed", "helper_exception", [], error_payload("helper_exception", type(exc).__name__)))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
