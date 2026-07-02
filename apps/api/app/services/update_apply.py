from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.update_check import run_update_check

REQUEST_SCHEMA_VERSION = 1
STATUS_SCHEMA_VERSION = 1
MAX_CONTROL_BYTES = 64 * 1024
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
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SENSITIVE_VALUE_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9_]+|Bearer\s+[A-Za-z0-9._~+/=-]+|rtsp://[^@\s]+@|postgresql://[^:\s]+:[^@\s]+@|-----BEGIN [^-]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


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


def _safe_string(value: Any, *, max_length: int = 300) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = SENSITIVE_VALUE_RE.sub("***", str(value).strip())
    return text[:max_length] or None


def _safe_error(code: str, message: str, action: str = "Review the update status and use terminal recovery if needed.") -> dict[str, str]:
    return {
        "category": _safe_string(code, max_length=80) or "update_apply_error",
        "message": _safe_string(message, max_length=300) or "Update apply is unavailable.",
        "operator_action": _safe_string(action, max_length=300) or "Review update status.",
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if SENSITIVE_VALUE_RE.search(rendered):
        raise UpdateApplyBlocked("control_payload_sensitive", "Update control payload contains sensitive content.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        if not path.exists():
            return None, "missing"
        if path.stat().st_size > MAX_CONTROL_BYTES:
            return None, "too_large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "invalid_shape"
    return payload, "valid"


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
    if expected_version and expected_version != latest.get("version"):
        raise UpdateApplyBlocked("manifest_version_changed", "Trusted manifest version changed. Refresh update status and retry.")
    if expected_commit and expected_commit != latest.get("commit"):
        raise UpdateApplyBlocked("manifest_commit_changed", "Trusted manifest commit changed. Refresh update status and retry.")


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
                "name": _safe_string(item.get("name"), max_length=80) or "unknown",
                "status": _safe_string(item.get("status"), max_length=40) or "pending",
            }
        )
    return steps


def _sanitize_apply_history_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    error = item.get("error") if isinstance(item.get("error"), dict) else None
    sanitized = {
        "request_id": _safe_string(item.get("request_id"), max_length=80),
        "status": _safe_string(item.get("status"), max_length=40),
        "phase": _safe_string(item.get("phase"), max_length=80),
        "started_at": _safe_string(item.get("started_at"), max_length=80),
        "finished_at": _safe_string(item.get("finished_at") or item.get("updated_at"), max_length=80),
        "updated_at": _safe_string(item.get("updated_at"), max_length=80),
        "expected_commit": _safe_string(item.get("expected_commit"), max_length=40),
        "installed_commit": _safe_string(item.get("installed_commit"), max_length=40),
        "commit_verified": bool(item.get("commit_verified")),
        "source": {
            "kind": _safe_string(source.get("kind"), max_length=80),
            "repo": _safe_string(source.get("repo"), max_length=160),
            "ref": _safe_string(source.get("ref"), max_length=120),
            "commit": _safe_string(source.get("commit"), max_length=40),
            "apply_ref": _safe_string(source.get("apply_ref"), max_length=40),
        }
        if source
        else None,
        "steps": _safe_steps(item.get("steps")),
        "error": {
            "category": _safe_string(error.get("category"), max_length=80),
            "message": _safe_string(error.get("message"), max_length=300),
            "operator_action": _safe_string(error.get("operator_action"), max_length=300),
        }
        if error
        else None,
        "history_detail_status": _safe_string(item.get("history_detail_status"), max_length=80) or "step_timestamps_unavailable",
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


def _base_status(status_value: str = "idle", phase: str = "idle", *, request_id: str | None = None) -> dict[str, Any]:
    now = _iso()
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "request_id": request_id,
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
        "last_apply_summary": None,
        "apply_history": {"available": False, "state": "missing", "items": [], "last": None, "max_items": MAX_APPLY_HISTORY_ITEMS},
    }


def read_update_apply_status() -> dict[str, Any]:
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
    sanitized = _base_status(str(payload.get("status") or "unknown"), str(payload.get("phase") or payload.get("current_step") or "unknown"), request_id=_safe_string(payload.get("request_id"), max_length=80))
    sanitized.update(
        {
            "schema_version": 1,
            "started_at": _safe_string(payload.get("started_at"), max_length=80),
            "updated_at": _safe_string(payload.get("updated_at"), max_length=80) or _iso(),
            "source": payload.get("source") if isinstance(payload.get("source"), dict) else None,
            "steps": _safe_steps(payload.get("steps")),
            "can_cancel": bool(payload.get("can_cancel")) and str(payload.get("status")) == "queued",
            "rollback_supported": False,
            "error": payload.get("error") if isinstance(payload.get("error"), dict) else None,
            "expected_commit": _safe_string(payload.get("expected_commit"), max_length=40),
            "installed_commit": _safe_string(payload.get("installed_commit"), max_length=40),
            "commit_verified": bool(payload.get("commit_verified")),
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
        sanitized["release_identity"] = {
            "metadata_status": _safe_string(release_payload.get("metadata_status"), max_length=40),
            "metadata_source": _safe_string(release_payload.get("metadata_source"), max_length=80),
            "commit_sha": _safe_string(release_payload.get("commit_sha"), max_length=40),
        }
    elif release_state != "missing":
        sanitized["release_identity"] = {"metadata_status": release_state}
    rendered = json.dumps(sanitized, ensure_ascii=False)
    if SENSITIVE_VALUE_RE.search(rendered):
        blocked = _base_status("blocked", "status_redaction", request_id=sanitized.get("request_id"))
        blocked["error"] = _safe_error("status_sensitive_content", "Update status contained sensitive content and was suppressed.")
        return blocked
    return sanitized


def request_update_apply(db: Session, *, confirm: bool, expected_manifest_version: str | None, expected_manifest_commit: str | None, actor: Any) -> dict[str, Any]:
    if not confirm:
        raise UpdateApplyBlocked("confirmation_required", "Explicit confirmation is required before update apply.")
    if not settings.kmvms_update_helper_enabled:
        raise UpdateApplyBlocked("helper_not_configured", "Update helper service is not enabled for this installation.")
    current_status = read_update_apply_status()
    if _running_status(current_status) or _lock_path().exists():
        raise UpdateApplyBlocked("update_already_running", "Another update apply is already running.")
    update = run_update_check(db, manual=False)
    latest = _validate_latest_for_apply(update)
    _validate_expected(latest, expected_version=expected_manifest_version, expected_commit=expected_manifest_commit)
    _check_token_precondition()
    source = _sanitized_source(latest)
    if not source["repo"] or not source["ref"] or not source["source_type"] or not source["commit"] or not source["apply_ref"]:
        raise UpdateApplyBlocked("trusted_source_incomplete", "Trusted release source is incomplete.")
    now = _iso()
    request_id = "update-" + uuid.uuid4().hex
    request = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "requested_at": now,
        "requested_by": {
            "user_id": _safe_string(getattr(actor, "id", None), max_length=80),
            "role": _safe_string(getattr(actor, "role", None), max_length=40),
        },
        "intent": "apply_update",
        "source": source,
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
    }
    status_payload = _base_status("queued", "queued", request_id=request_id)
    status_payload.update({"started_at": now, "updated_at": now, "source": {"kind": "github-tarball", "repo": source["repo"], "ref": source["ref"], "commit": source["commit"], "apply_ref": source["apply_ref"]}, "expected_commit": source["commit"], "commit_verified": False})
    status_payload["steps"] = [
        {"name": "request", "status": "completed"},
        {"name": "preflight", "status": "pending"},
        {"name": "apply", "status": "pending"},
        {"name": "health_check", "status": "pending"},
    ]
    _atomic_write_json(_status_path(), status_payload)
    _atomic_write_json(_request_path(), request)
    return {"accepted": True, "status": "queued", "request_id": request_id, "apply_status": status_payload, "can_cancel": True}


def cancel_update_apply() -> dict[str, Any]:
    status_payload = read_update_apply_status()
    if status_payload.get("status") != "queued":
        return {"status": "not_cancelable", "request_id": status_payload.get("request_id"), "can_cancel": False, "reason": "Update apply can only be cancelled before helper starts."}
    status_payload["status"] = "cancelled"
    status_payload["phase"] = "cancelled"
    status_payload["updated_at"] = _iso()
    status_payload["can_cancel"] = False
    status_payload["error"] = _safe_error("cancelled_before_start", "Queued update apply was cancelled before helper started.", "No update was applied.")
    _atomic_write_json(_status_path(), status_payload)
    return {"status": "cancelled", "request_id": status_payload.get("request_id"), "can_cancel": False}


def reject_forbidden_apply_fields(payload: dict[str, Any]) -> None:
    for key in payload:
        if key in FORBIDDEN_REQUEST_FIELDS or any(token in key.lower() for token in ("token", "secret", "command", "url", "path")):
            raise UpdateApplyBlocked("forbidden_request_field", f"Forbidden update apply request field: {key}")
        value = payload[key]
        if isinstance(value, str) and (SENSITIVE_VALUE_RE.search(value) or not SAFE_TEXT_RE.fullmatch(value)):
            raise UpdateApplyBlocked("unsafe_request_value", f"Unsafe update apply request value for field: {key}")
