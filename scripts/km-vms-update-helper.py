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
PROGRESS_FILE = CONTROL_DIR / "update-progress.json"
APPLY_HISTORY_FILE = CONTROL_DIR / "update-apply-history.json"
POLL_SECONDS = int(os.getenv("KM_VMS_UPDATE_HELPER_POLL_SECONDS") or "2")
MAX_CONTROL_BYTES = 64 * 1024
MAX_APPLY_HISTORY_ITEMS = 10
TERMINAL = {"completed", "failed", "cancelled", "blocked"}
STEP_ORDER = ["queued", "preflight", "acquire_source", "extracting", "validating_source", "overlay", "compose_config", "rebuilding", "restarting", "health_check", "commit_verification"]
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


def append_apply_history(status_payload: dict[str, Any]) -> None:
    try:
        existing = read_json(APPLY_HISTORY_FILE) or {"items": []}
        items = existing.get("items") if isinstance(existing.get("items"), list) else []
        entry = {
            "request_id": safe_text(status_payload.get("request_id"), 80),
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
    action = "Review sanitized update status and use terminal recovery if needed."
    if category in {"build_network_dependency_failed", "jellyfin_ffmpeg_repo_unavailable"}:
        action = "External FFmpeg repository or network dependency failed during API image build. Retry after repository connectivity is restored or use the documented terminal recovery path."
    elif category == "docker_build_failed":
        action = "Docker image rebuild failed. Review sanitized update status and retry after the build cause is fixed."
    elif category == "compose_config_failed":
        action = "Compose configuration failed. Review server-side compose configuration before retrying."
    elif category == "health_check_failed":
        action = "Containers were recreated but API health did not recover. Review service status before retrying."
    elif category == "commit_mismatch":
        action = "Installed commit did not match trusted release evidence. Treat the update as failed and retry only after checking the release source."
    return {
        "category": safe_text(category, 80) or "helper_error",
        "message": safe_text(message, 1000) or "Update helper failed.",
        "operator_action": action,
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


def failed_steps(category: str, phase: str | None = None) -> list[dict[str, str]]:
    if category == "preflight_failed":
        return steps_for("preflight", failed=True)
    if category == "compose_config_failed":
        return steps_for("compose_config", failed=True)
    if category in {"jellyfin_ffmpeg_repo_unavailable", "build_network_dependency_failed", "docker_build_failed"}:
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
    if request_id and payload.get("request_id") not in {None, request_id}:
        return None
    return payload


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
        return HelperError("health_check_failed", stderr or "Update health check failed.")
    if failed_phase == "compose_config":
        return HelperError("compose_config_failed", "Docker Compose configuration validation failed.")
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
    return HelperError("apply_failed", stderr or "Update apply failed.")


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
        raise HelperError("preflight_failed", dry.stderr.strip() or "Update preflight failed.")
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
    write_json(STATUS_FILE, completed)
    append_apply_history(completed)
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
                    tail = read_stderr_tail(stderr_path) if stderr_path else ""
                    message = "Update helper child process exceeded the bounded timeout."
                    if tail:
                        message = f"{message} Last sanitized stderr tail: {tail}"
                    raise HelperError("apply_timeout", message, phase=last_step)
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
            failed = base_status(request, "failed", exc.phase, failed_steps(exc.category, exc.phase), error_payload(exc.category, str(exc)))
            if exc.diagnostics.get("installed_commit"):
                failed["installed_commit"] = safe_text(exc.diagnostics.get("installed_commit"), 40)
            write_json(STATUS_FILE, failed)
            append_apply_history(failed)
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
