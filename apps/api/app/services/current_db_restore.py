from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.backup_before_upgrade import build_backup_plan, verify_backup_manifest
from app.services.backup_manager import (
    BackupManagerBlocked,
    actor_binding_key,
    current_restore_artifact_evidence,
    validate_artifact_id,
    validate_submission_id,
)
from app.services.maintenance_admission import (
    MaintenanceAdmissionBlocked,
    assert_no_maintenance_conflicts,
    maintenance_admission_guard,
    read_bounded_json,
    write_bounded_json_atomic,
)
from app.services.restore_maintenance import (
    RestoreMaintenanceBlocked,
    _manifest_for_artifact,
    run_backup_validation_operation,
)
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION, SAFE_STATUSES, schema_version_status


RESTORE_REQUEST_SCHEMA = "stage13.7.8.current-restore-request.v1"
RESTORE_PUBLIC_SCHEMA = "stage13.7.8.current-restore-public.v1"
RESTORE_RECEIPT_SCHEMA = "stage13.7.8.current-restore-receipt.v1"
RESTORE_CONFIRMATION_PHRASE = "RESTORE KM VMS"
ACTIVE_REQUEST_STATES = {"admitted", "claimed"}
TERMINAL_RESULTS = {
    "completed",
    "blocked",
    "failed_rolled_back",
    "failed_recovery_required",
}
PHASES = {
    "preflight",
    "pre_restore_backup",
    "writers_paused",
    "restore_running",
    "services_starting",
    "post_restore_check",
    *TERMINAL_RESULTS,
}
SAFE_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,100}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^restore-[0-9a-f]{32}$")
MACHINE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
MAX_RECEIPTS = 128


class CurrentRestoreBlocked(RuntimeError):
    def __init__(self, code: str, diagnostics: dict[str, Any] | None = None):
        self.code = str(code or "current_restore_blocked")[:80]
        self.diagnostics = diagnostics or {
            "status": "blocked",
            "reason_code": self.code,
        }
        super().__init__(self.code)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def restore_control_root() -> Path:
    return Path(settings.restore_control_root)


def restore_public_root() -> Path:
    return Path(settings.restore_public_root)


def restore_request_path() -> Path:
    return restore_control_root() / "restore-request.json"


def restore_journal_path() -> Path:
    return restore_control_root() / "restore-journal.json"


def restore_public_status_path() -> Path:
    return restore_public_root() / "restore-status.json"


def restore_receipt_dir() -> Path:
    return restore_control_root() / "receipts"


def restore_receipt_path(submission_id: str) -> Path:
    return restore_receipt_dir() / f"{validate_submission_id(submission_id)}.json"


def _actor_subject(actor: Any) -> str:
    subject = str(getattr(actor, "username", "") or "").strip()
    if not SAFE_SUBJECT_RE.fullmatch(subject):
        raise CurrentRestoreBlocked("actor_identity_invalid")
    return subject


def _actor_snapshot(actor: Any) -> dict[str, Any]:
    actor_id = getattr(actor, "id", None)
    role = str(getattr(actor, "role", "") or "").strip().lower()
    if (
        not isinstance(actor_id, int)
        or isinstance(actor_id, bool)
        or actor_id < 1
        or role not in {"owner", "admin"}
    ):
        raise CurrentRestoreBlocked("actor_access_not_eligible")
    return {
        "user_id": actor_id,
        "subject": _actor_subject(actor),
        "role": role,
        "binding": actor_binding_key(actor),
    }


def _parse_utc(value: Any) -> float | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _reader_healthy() -> bool:
    if str(settings.app_env or "").lower() == "test":
        return True
    url = str(
        os.getenv("KMVMS_RESTORE_STATUS_READER_HEALTH_URL")
        or "http://update-status-reader:8080/health"
    ).strip()
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read(4096).decode("utf-8"))
        capabilities = payload.get("capabilities")
        return bool(
            response.status == 200
            and payload.get("status") == "ok"
            and payload.get("role") == "reader"
            and isinstance(capabilities, list)
            and "current-db-restore-status-v1" in capabilities
        )
    except Exception:
        return False


def _helper_healthy() -> bool:
    if str(settings.app_env or "").lower() == "test":
        return True
    payload, state = read_bounded_json(restore_public_root() / "helper-health.json")
    if state != "valid" or not payload:
        return False
    updated = _parse_utc(payload.get("updated_at"))
    return bool(
        payload.get("schema_version") == 1
        and payload.get("role") == "update-helper-restore-dispatch"
        and updated is not None
        and time.time() - updated <= 30
    )


def _safe_artifact_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": evidence.get("artifact_id"),
        "artifact_created_at": evidence.get("artifact_created_at"),
        "artifact_schema_version": evidence.get("artifact_schema_version"),
        "db_backend": evidence.get("db_backend"),
        "file_size": int(evidence.get("file_size") or 0),
    }


def _validation_submission_id(
    _artifact_id: str,
    _actor: Any,
    _fingerprint: str,
) -> str:
    return str(uuid.uuid4())


def _artifact_evidence(
    artifact_id: str,
    *,
    actor: Any,
    db_backend: str,
) -> dict[str, Any]:
    try:
        return current_restore_artifact_evidence(
            artifact_id,
            actor=actor,
            db_backend=db_backend,
        )
    except BackupManagerBlocked as exc:
        raise CurrentRestoreBlocked(exc.code, exc.diagnostics) from exc


def current_restore_preflight(
    db: Session,
    *,
    artifact_id: str,
    actor: Any,
    perform_validation: bool = True,
) -> dict[str, Any]:
    try:
        safe_artifact = validate_artifact_id(artifact_id)
        actor_info = _actor_snapshot(actor)
    except BackupManagerBlocked as exc:
        raise CurrentRestoreBlocked(exc.code, exc.diagnostics) from exc
    backend = str(db.get_bind().url.get_backend_name()).lower()
    reason_codes: list[str] = []
    if not backend.startswith("postgresql"):
        reason_codes.append("postgresql_required")

    schema = schema_version_status(db)
    schema_exact = (
        schema.get("schema_version") == CURRENT_SCHEMA_VERSION
        and schema.get("status") in SAFE_STATUSES
    )
    if not schema_exact:
        reason_codes.append(
            "schema_newer_than_supported"
            if isinstance(schema.get("schema_version"), int)
            and schema["schema_version"] > CURRENT_SCHEMA_VERSION
            else "schema_migration_required"
        )

    try:
        manifest_path, manifest = _manifest_for_artifact(safe_artifact)
        verification = verify_backup_manifest(manifest_path)
    except RestoreMaintenanceBlocked as exc:
        raise CurrentRestoreBlocked(exc.status, exc.diagnostics) from exc
    artifact_backend = str(manifest.get("db_backend") or "").lower()
    manifest_schema = manifest.get("schema_version")
    if isinstance(manifest_schema, dict):
        manifest_schema = manifest_schema.get("schema_version")
    try:
        manifest_schema = int(manifest_schema)
    except (TypeError, ValueError):
        manifest_schema = None
    if artifact_backend != "postgresql":
        reason_codes.append("artifact_backend_unsupported")
    if manifest_schema != CURRENT_SCHEMA_VERSION:
        reason_codes.append(
            "artifact_schema_newer"
            if isinstance(manifest_schema, int) and manifest_schema > CURRENT_SCHEMA_VERSION
            else "artifact_schema_migration_required"
        )
    if not verification.get("valid") or verification.get("integrity_status") != "verified":
        reason_codes.append("artifact_integrity_not_verified")

    evidence = _artifact_evidence(
        safe_artifact,
        actor=actor,
        db_backend="postgresql",
    )
    needs_validation = not (
        evidence.get("integrity_verified")
        and evidence.get("temporary_restore_validated")
        and evidence.get("actor_access_verified")
    )
    if (
        perform_validation
        and not reason_codes
        and needs_validation
    ):
        validation = run_backup_validation_operation(
            db,
            submission_id=_validation_submission_id(
                safe_artifact,
                actor,
                str(evidence.get("fingerprint") or ""),
            ),
            artifact_id=safe_artifact,
            confirm=True,
            actor=actor,
        )
        if validation.get("state") != "completed":
            reason_codes.append(
                str(validation.get("reason_code") or "temporary_restore_validation_failed")
            )
        evidence = _artifact_evidence(
            safe_artifact,
            actor=actor,
            db_backend="postgresql",
        )

    if not evidence.get("integrity_verified"):
        reason_codes.append("artifact_integrity_evidence_stale")
    if not evidence.get("temporary_restore_validated"):
        reason_codes.append("temporary_restore_validation_required")
    if not evidence.get("actor_access_verified"):
        reason_codes.append("initiating_actor_missing_or_inactive_in_backup")

    backup_plan = build_backup_plan(db)
    root_ready = (
        backup_plan.get("backup_root_persistent") is True
        and backup_plan.get("backup_root_status") == "ready"
        and backup_plan.get("free_space", {}).get("passed") is True
    )
    if not root_ready:
        reason_codes.append(
            "insufficient_space_for_pre_restore_backup"
            if backup_plan.get("free_space", {}).get("passed") is not True
            else "backup_root_not_persistent"
        )

    try:
        with maintenance_admission_guard():
            states = assert_no_maintenance_conflicts("restore", db=db)
    except MaintenanceAdmissionBlocked as exc:
        states = {}
        reason_codes.append(exc.code)

    helper_healthy = _helper_healthy()
    reader_healthy = _reader_healthy()
    if not helper_healthy:
        reason_codes.append("restore_helper_unavailable")
    if not reader_healthy:
        reason_codes.append("restore_status_reader_unavailable")

    unique_reasons = list(dict.fromkeys(reason_codes))
    return {
        "status": "ready" if not unique_reasons else "blocked",
        "reason_codes": unique_reasons,
        "can_restore": not unique_reasons,
        "artifact": _safe_artifact_payload(evidence),
        "artifact_integrity_verified": bool(evidence.get("integrity_verified")),
        "temporary_restore_validated": bool(evidence.get("temporary_restore_validated")),
        "actor_access_verified": bool(evidence.get("actor_access_verified")),
        "current_schema_exact": bool(schema_exact),
        "backup_root_persistent": bool(backup_plan.get("backup_root_persistent")),
        "pre_restore_backup_required": True,
        "pre_restore_backup_space_ready": bool(
            backup_plan.get("free_space", {}).get("passed")
        ),
        "maintenance_conflicts_absent": bool(states) and all(
            value == "idle" for name, value in states.items() if name != "restore"
        ),
        "helper_healthy": helper_healthy,
        "status_reader_healthy": reader_healthy,
        "video_archive_included": False,
        "video_archive_will_be_modified": False,
        "actor": {
            "subject": actor_info["subject"],
            "role": actor_info["role"],
        },
    }


def _request_binding_matches(
    request: dict[str, Any],
    *,
    submission_id: str,
    artifact_id: str,
    actor: dict[str, Any],
    fingerprint: str,
) -> bool:
    return bool(
        request.get("submission_id") == submission_id
        and request.get("artifact", {}).get("artifact_id") == artifact_id
        and request.get("requested_by", {}).get("user_id") == actor["user_id"]
        and request.get("requested_by", {}).get("subject") == actor["subject"]
        and request.get("requested_by", {}).get("role") == actor["role"]
        and request.get("requested_by", {}).get("binding") == actor["binding"]
        and request.get("artifact", {}).get("fingerprint") == fingerprint
        and request.get("confirmation_phrase") == RESTORE_CONFIRMATION_PHRASE
    )


def _public_from_request(
    request: dict[str, Any],
    *,
    phase: str,
    terminal_result: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    now = utc_iso()
    artifact = request.get("artifact") if isinstance(request.get("artifact"), dict) else {}
    return {
        "schema": RESTORE_PUBLIC_SCHEMA,
        "operation_id": request.get("operation_id"),
        "submission_id": request.get("submission_id"),
        "actor_subject": request.get("requested_by", {}).get("subject"),
        "status": terminal_result or ("running" if request.get("state") == "claimed" else "queued"),
        "phase": phase if phase in PHASES else "preflight",
        "artifact": {
            "artifact_id": artifact.get("artifact_id"),
            "artifact_created_at": artifact.get("artifact_created_at"),
            "artifact_schema_version": artifact.get("artifact_schema_version"),
            "db_backend": artifact.get("db_backend"),
        },
        "pre_restore_backup_id": None,
        "accepted_at": request.get("requested_at"),
        "started_at": request.get("claimed_at"),
        "updated_at": now,
        "finished_at": now if terminal_result else None,
        "terminal_result": terminal_result,
        "reason_code": reason_code,
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


def _write_public_status(payload: dict[str, Any]) -> None:
    write_bounded_json_atomic(restore_public_status_path(), payload)


def _safe_receipt(request: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
    public = _public_from_request(
        request,
        phase=(
            request.get("terminal", {}).get("status")
            if isinstance(request.get("terminal"), dict)
            else "preflight"
        ),
        terminal_result=(
            request.get("terminal", {}).get("status")
            if isinstance(request.get("terminal"), dict)
            else None
        ),
        reason_code=(
            request.get("terminal", {}).get("reason_code")
            if isinstance(request.get("terminal"), dict)
            else None
        ),
    )
    return {
        "schema": RESTORE_RECEIPT_SCHEMA,
        "operation_id": request.get("operation_id"),
        "submission_id": request.get("submission_id"),
        "artifact_id": request.get("artifact", {}).get("artifact_id"),
        "state": request.get("state"),
        "accepted": True,
        "replayed": bool(replayed),
        "restore_status": {
            key: value
            for key, value in public.items()
            if key not in {"actor_subject", "schema"}
        },
    }


def _prune_receipts() -> None:
    directory = restore_receipt_dir()
    try:
        paths = sorted(
            directory.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for path in paths[MAX_RECEIPTS:]:
        try:
            payload, state = read_bounded_json(path)
            if state == "valid" and payload and payload.get("state") == "terminal":
                path.unlink()
        except OSError:
            continue


def request_current_restore(
    db: Session,
    *,
    artifact_id: str,
    submission_id: str,
    confirm: bool,
    confirmation_phrase: str,
    actor: Any,
    before_admit: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    if confirm is not True:
        raise CurrentRestoreBlocked("confirmation_required")
    if confirmation_phrase != RESTORE_CONFIRMATION_PHRASE:
        raise CurrentRestoreBlocked("confirmation_phrase_invalid")
    try:
        safe_artifact = validate_artifact_id(artifact_id)
        safe_submission = validate_submission_id(submission_id)
    except BackupManagerBlocked as exc:
        raise CurrentRestoreBlocked(exc.code, exc.diagnostics) from exc
    actor_info = _actor_snapshot(actor)
    try:
        with maintenance_admission_guard():
            existing_payload, existing_state = read_bounded_json(
                restore_receipt_path(safe_submission)
            )
            if existing_state == "invalid":
                raise MaintenanceAdmissionBlocked(
                    "restore_receipt_state_unavailable"
                )
            if existing_payload:
                existing = restore_request_contract(existing_payload)
                existing_fingerprint = str(
                    existing_payload.get("artifact", {}).get("fingerprint")
                    or ""
                )
                if (
                    existing is None
                    or not FINGERPRINT_RE.fullmatch(existing_fingerprint)
                    or not _request_binding_matches(
                        existing,
                        submission_id=safe_submission,
                        artifact_id=safe_artifact,
                        actor=actor_info,
                        fingerprint=existing_fingerprint,
                    )
                ):
                    raise MaintenanceAdmissionBlocked(
                        "submission_binding_conflict"
                    )
                return _safe_receipt(existing, replayed=True)
            current_payload, current_state = read_bounded_json(
                restore_request_path()
            )
            if current_state == "invalid":
                raise MaintenanceAdmissionBlocked(
                    "restore_state_unavailable"
                )
            if (
                current_payload
                and current_payload.get("state") in ACTIVE_REQUEST_STATES
            ):
                current = restore_request_contract(current_payload)
                current_fingerprint = str(
                    current_payload.get("artifact", {}).get("fingerprint")
                    or ""
                )
                if (
                    current is not None
                    and current.get("submission_id") == safe_submission
                    and FINGERPRINT_RE.fullmatch(current_fingerprint)
                    and _request_binding_matches(
                        current,
                        submission_id=safe_submission,
                        artifact_id=safe_artifact,
                        actor=actor_info,
                        fingerprint=current_fingerprint,
                    )
                ):
                    try:
                        write_bounded_json_atomic(
                            restore_receipt_path(safe_submission),
                            current,
                        )
                    except MaintenanceAdmissionBlocked:
                        pass
                    return _safe_receipt(current, replayed=True)
                raise MaintenanceAdmissionBlocked(
                    "restore_operation_active"
                    if current_payload.get("submission_id") != safe_submission
                    else "submission_binding_conflict"
                )
    except MaintenanceAdmissionBlocked as exc:
        raise CurrentRestoreBlocked(exc.code) from exc
    preflight = current_restore_preflight(
        db,
        artifact_id=safe_artifact,
        actor=actor,
        perform_validation=True,
    )
    if not preflight.get("can_restore"):
        raise CurrentRestoreBlocked(
            str((preflight.get("reason_codes") or ["restore_preflight_blocked"])[0]),
            {
                "status": "blocked",
                "reason_code": str(
                    (preflight.get("reason_codes") or ["restore_preflight_blocked"])[0]
                ),
                "preflight": preflight,
            },
        )
    evidence = _artifact_evidence(
        safe_artifact,
        actor=actor,
        db_backend="postgresql",
    )
    fingerprint = str(evidence.get("fingerprint") or "")
    if not FINGERPRINT_RE.fullmatch(fingerprint):
        raise CurrentRestoreBlocked("artifact_fingerprint_invalid")
    now = utc_iso()
    operation_id = f"restore-{uuid.uuid4().hex}"
    candidate = {
        "schema": RESTORE_REQUEST_SCHEMA,
        "operation_id": operation_id,
        "submission_id": safe_submission,
        "intent": "restore_current_database",
        "requested_at": now,
        "updated_at": now,
        "requested_by": actor_info,
        "artifact": {
            **_safe_artifact_payload(evidence),
            "fingerprint": fingerprint,
        },
        "confirmed": True,
        "confirmation_phrase": RESTORE_CONFIRMATION_PHRASE,
        "state": "admitted",
        "claimed_at": None,
        "terminal": None,
        "video_archive_scope": "excluded",
        "migration_auto_apply": False,
    }
    if restore_request_contract(candidate) is None:
        raise CurrentRestoreBlocked("restore_request_contract_invalid")
    try:
        with maintenance_admission_guard():
            receipt_payload, receipt_state = read_bounded_json(
                restore_receipt_path(safe_submission)
            )
            if receipt_state == "invalid":
                raise MaintenanceAdmissionBlocked("restore_receipt_state_unavailable")
            if receipt_payload:
                receipt = restore_request_contract(receipt_payload)
                if (
                    receipt is None
                    or not _request_binding_matches(
                        receipt,
                        submission_id=safe_submission,
                        artifact_id=safe_artifact,
                        actor=actor_info,
                        fingerprint=fingerprint,
                    )
                ):
                    raise MaintenanceAdmissionBlocked("submission_binding_conflict")
                return _safe_receipt(receipt, replayed=True)

            current_payload, current_state = read_bounded_json(
                restore_request_path()
            )
            if current_state == "invalid":
                raise MaintenanceAdmissionBlocked("restore_state_unavailable")
            if (
                current_payload
                and current_payload.get("state") in ACTIVE_REQUEST_STATES
            ):
                current = restore_request_contract(current_payload)
                if _request_binding_matches(
                    current or {},
                    submission_id=safe_submission,
                    artifact_id=safe_artifact,
                    actor=actor_info,
                    fingerprint=fingerprint,
                ):
                    try:
                        write_bounded_json_atomic(
                            restore_receipt_path(safe_submission),
                            current,
                        )
                    except MaintenanceAdmissionBlocked:
                        pass
                    return _safe_receipt(current, replayed=True)
                raise MaintenanceAdmissionBlocked("restore_operation_active")
            assert_no_maintenance_conflicts("restore", db=db)
            current_evidence = _artifact_evidence(
                safe_artifact,
                actor=actor,
                db_backend="postgresql",
            )
            if (
                current_evidence.get("fingerprint") != fingerprint
                or not current_evidence.get("integrity_verified")
                or not current_evidence.get("temporary_restore_validated")
                or not current_evidence.get("actor_access_verified")
            ):
                raise MaintenanceAdmissionBlocked("artifact_evidence_changed")
            if before_admit is None or before_admit(candidate) is not True:
                raise MaintenanceAdmissionBlocked("audit_unavailable")
            write_bounded_json_atomic(restore_request_path(), candidate)
            try:
                write_bounded_json_atomic(
                    restore_receipt_path(safe_submission),
                    candidate,
                )
                _write_public_status(
                    _public_from_request(candidate, phase="preflight")
                )
                _prune_receipts()
            except MaintenanceAdmissionBlocked:
                # The request file is the restore admission authority. Once it
                # is durable, a secondary receipt/projection failure must not
                # be reported as a rejected operation.
                pass
    except MaintenanceAdmissionBlocked as exc:
        raise CurrentRestoreBlocked(exc.code) from exc
    return _safe_receipt(candidate, replayed=False)


def read_current_restore_status(*, actor: Any) -> dict[str, Any]:
    subject = _actor_subject(actor)
    payload, state = read_bounded_json(restore_public_status_path())
    if state == "invalid":
        raise CurrentRestoreBlocked("restore_status_unavailable")
    if not payload:
        return {
            "status": "idle",
            "phase": None,
            "terminal_result": None,
            "video_archive_modified": False,
        }
    validated = restore_public_contract(payload)
    if validated is None:
        raise CurrentRestoreBlocked("restore_status_contract_invalid")
    if validated.get("actor_subject") != subject:
        raise CurrentRestoreBlocked(
            "restore_status_not_found",
            {"status": "not_found", "reason_code": "restore_status_not_found"},
        )
    return {
        key: value
        for key, value in validated.items()
        if key not in {"actor_subject", "schema"}
    }


def restore_public_contract(payload: Any) -> dict[str, Any] | None:
    required = {
        "schema",
        "operation_id",
        "submission_id",
        "actor_subject",
        "status",
        "phase",
        "artifact",
        "pre_restore_backup_id",
        "accepted_at",
        "started_at",
        "updated_at",
        "finished_at",
        "terminal_result",
        "reason_code",
        "next_action",
        "video_archive_modified",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        return None
    artifact = payload.get("artifact")
    terminal = payload.get("terminal_result")
    status_value = payload.get("status")
    phase = payload.get("phase")
    reason_code = payload.get("reason_code")
    next_action = payload.get("next_action")
    if (
        payload.get("schema") != RESTORE_PUBLIC_SCHEMA
        or not isinstance(payload.get("operation_id"), str)
        or not REQUEST_ID_RE.fullmatch(payload["operation_id"])
        or not isinstance(payload.get("submission_id"), str)
        or not isinstance(payload.get("actor_subject"), str)
        or not SAFE_SUBJECT_RE.fullmatch(payload["actor_subject"])
        or not isinstance(status_value, str)
        or status_value not in {"queued", "running", *TERMINAL_RESULTS}
        or not isinstance(phase, str)
        or phase not in PHASES
        or (
            terminal is not None
            and (
                not isinstance(terminal, str)
                or terminal not in TERMINAL_RESULTS
            )
        )
        or payload.get("video_archive_modified") is not False
        or not isinstance(artifact, dict)
        or set(artifact)
        != {
            "artifact_id",
            "artifact_created_at",
            "artifact_schema_version",
            "db_backend",
        }
        or artifact.get("artifact_schema_version") != CURRENT_SCHEMA_VERSION
        or artifact.get("db_backend") != "postgresql"
        or not isinstance(artifact.get("artifact_created_at"), str)
        or _parse_utc(artifact.get("artifact_created_at")) is None
        or _parse_utc(payload.get("accepted_at")) is None
        or _parse_utc(payload.get("updated_at")) is None
        or (
            payload.get("started_at") is not None
            and _parse_utc(payload.get("started_at")) is None
        )
        or (
            payload.get("finished_at") is not None
            and _parse_utc(payload.get("finished_at")) is None
        )
        or (
            reason_code is not None
            and (
                not isinstance(reason_code, str)
                or not MACHINE_CODE_RE.fullmatch(reason_code)
            )
        )
        or not isinstance(next_action, str)
        or next_action
        not in {
            "wait",
            "sign_in_again",
            "current_database_restored",
            "contact_support",
            "review_restore_status",
        }
    ):
        return None
    try:
        validate_submission_id(payload["submission_id"])
        validate_artifact_id(str(artifact.get("artifact_id") or ""))
        if payload.get("pre_restore_backup_id") is not None:
            validate_artifact_id(str(payload["pre_restore_backup_id"]))
    except BackupManagerBlocked:
        return None
    if terminal is None:
        if (
            status_value not in {"queued", "running"}
            or phase in TERMINAL_RESULTS
            or payload.get("finished_at") is not None
            or next_action != "wait"
            or (
                status_value == "queued"
                and payload.get("started_at") is not None
            )
            or (
                status_value == "running"
                and payload.get("started_at") is None
            )
        ):
            return None
    else:
        expected_action = {
            "completed": "sign_in_again",
            "blocked": "review_restore_status",
            "failed_rolled_back": "current_database_restored",
            "failed_recovery_required": "contact_support",
        }.get(terminal)
        if (
            terminal not in TERMINAL_RESULTS
            or status_value != terminal
            or phase != terminal
            or payload.get("finished_at") is None
            or payload.get("started_at") is None
            or next_action != expected_action
            or (terminal == "completed" and reason_code is not None)
            or (terminal != "completed" and reason_code is None)
        ):
            return None
    return json.loads(json.dumps(payload))


def restore_request_contract(payload: Any) -> dict[str, Any] | None:
    required = {
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
    if not isinstance(payload, dict) or set(payload) != required:
        return None
    actor = payload.get("requested_by")
    artifact = payload.get("artifact")
    if (
        payload.get("schema") != RESTORE_REQUEST_SCHEMA
        or payload.get("intent") != "restore_current_database"
        or payload.get("confirmed") is not True
        or payload.get("confirmation_phrase") != RESTORE_CONFIRMATION_PHRASE
        or payload.get("state") not in {"admitted", "claimed", "terminal"}
        or payload.get("video_archive_scope") != "excluded"
        or payload.get("migration_auto_apply") is not False
        or not isinstance(payload.get("operation_id"), str)
        or not REQUEST_ID_RE.fullmatch(payload["operation_id"])
        or not isinstance(payload.get("submission_id"), str)
        or not isinstance(actor, dict)
        or set(actor) != {"user_id", "subject", "role", "binding"}
        or not isinstance(actor.get("user_id"), int)
        or isinstance(actor.get("user_id"), bool)
        or actor["user_id"] < 1
        or not isinstance(actor.get("subject"), str)
        or not SAFE_SUBJECT_RE.fullmatch(actor["subject"])
        or actor.get("role") not in {"owner", "admin"}
        or not isinstance(actor.get("binding"), str)
        or not FINGERPRINT_RE.fullmatch(actor["binding"])
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
        or artifact.get("db_backend") != "postgresql"
        or artifact.get("artifact_schema_version") != CURRENT_SCHEMA_VERSION
        or not isinstance(artifact.get("artifact_created_at"), str)
        or _parse_utc(artifact.get("artifact_created_at")) is None
        or not isinstance(artifact.get("file_size"), int)
        or isinstance(artifact.get("file_size"), bool)
        or artifact["file_size"] < 1
        or not isinstance(artifact.get("fingerprint"), str)
        or not FINGERPRINT_RE.fullmatch(artifact["fingerprint"])
    ):
        return None
    try:
        validate_submission_id(str(payload.get("submission_id") or ""))
        validate_artifact_id(str(artifact.get("artifact_id") or ""))
    except BackupManagerBlocked:
        return None
    if _parse_utc(payload.get("requested_at")) is None or _parse_utc(payload.get("updated_at")) is None:
        return None
    if payload["state"] == "admitted" and (
        payload.get("claimed_at") is not None or payload.get("terminal") is not None
    ):
        return None
    if payload["state"] == "claimed" and (
        _parse_utc(payload.get("claimed_at")) is None or payload.get("terminal") is not None
    ):
        return None
    if payload["state"] == "terminal":
        terminal = payload.get("terminal")
        if (
            not isinstance(terminal, dict)
            or set(terminal) != {"status", "finished_at", "reason_code"}
            or terminal.get("status") not in TERMINAL_RESULTS
            or _parse_utc(payload.get("claimed_at")) is None
            or _parse_utc(terminal.get("finished_at")) is None
            or (
                terminal.get("reason_code") is not None
                and (
                    not isinstance(terminal.get("reason_code"), str)
                    or not MACHINE_CODE_RE.fullmatch(terminal["reason_code"])
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
        ):
            return None
    return json.loads(json.dumps(payload))
