from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.services.backup_before_upgrade import (
    BackupExecutionConfig,
    BackupSafetyBlocked,
    create_backup_before_upgrade,
    verify_backup_manifest,
)
from app.services.archive_integrity import (
    invalidate_integrity_truth_after_restore,
    start_integrity_scan,
)
from app.services.backup_manager import (
    artifact_version_evidence,
    configured_backup_root,
    current_restore_artifact_evidence,
    current_validation_context,
    write_artifact_state,
)
from app.services.current_db_restore import (
    CurrentRestoreBlocked,
    restore_control_root,
    restore_request_contract,
    restore_request_path,
    utc_iso,
)
from app.services.maintenance_admission import (
    MaintenanceAdmissionBlocked,
    assert_no_maintenance_conflicts,
    read_bounded_json,
    write_bounded_json_atomic,
)
from app.services.restore_maintenance import _manifest_for_artifact
from app.services.restore_validation import (
    is_benign_transaction_timeout_restore_warning,
)
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION, SAFE_STATUSES, schema_version_status


RESULT_FILE = "restore-executor-result.json"
DESTRUCTIVE_MARKER_FILE = "restore-destructive-started.json"
SAFE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
RECORDER_PROOF_TIMEOUT_SECONDS = 60
RECORDER_PROOF_POLL_SECONDS = 2.0
RECORDER_HEARTBEAT_MAX_AGE_SECONDS = 30.0
RECORDER_CLOCK_TOLERANCE_SECONDS = 2.0


class RestoreExecutorBlocked(RuntimeError):
    def __init__(self, code: str, *, mutation_started: bool = False):
        self.code = str(code or "restore_executor_failed")[:80]
        self.mutation_started = bool(mutation_started)
        super().__init__(self.code)


def _result_path() -> Path:
    return restore_control_root() / RESULT_FILE


def _marker_path() -> Path:
    return restore_control_root() / DESTRUCTIVE_MARKER_FILE


def _destructive_started_for(operation_id: str) -> bool:
    marker, state = read_bounded_json(_marker_path())
    return bool(
        state == "valid"
        and marker
        and marker.get("schema_version") == 1
        and marker.get("operation_id") == operation_id
        and marker.get("mutation_started") is True
    )


def _write_result(
    *,
    operation_id: str,
    action: str,
    status: str,
    reason_code: str | None = None,
    mutation_started: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    write_bounded_json_atomic(
        _result_path(),
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "action": action,
            "status": status,
            "reason_code": reason_code,
            "mutation_started": bool(mutation_started),
            "updated_at": utc_iso(),
            "details": details or {},
            "video_archive_modified": False,
        },
    )


def _load_request(
    operation_id: str,
    *,
    allow_terminal: bool = False,
) -> dict[str, Any]:
    payload, state = read_bounded_json(restore_request_path())
    request = restore_request_contract(payload)
    if (
        state != "valid"
        or request is None
        or request.get("operation_id") != operation_id
        or request.get("state")
        not in (
            {"admitted", "claimed", "terminal"}
            if allow_terminal
            else {"admitted", "claimed"}
        )
    ):
        raise RestoreExecutorBlocked("restore_request_invalid")
    return request


def _actor_from_request(request: dict[str, Any]) -> Any:
    actor = request["requested_by"]
    return SimpleNamespace(
        id=actor["user_id"],
        username=actor["subject"],
        role=actor["role"],
    )


def _session() -> tuple[Session, Any]:
    engine = create_engine(settings.database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal(), engine


def _integrity_convergence_identity(
    request: dict[str, Any],
    final_db_outcome: str | None,
) -> tuple[str, str]:
    outcome = str(final_db_outcome or "").strip().lower()
    if outcome not in {"source", "rollback"}:
        raise RestoreExecutorBlocked(
            "restore_integrity_outcome_invalid",
            mutation_started=True,
        )
    if request.get("state") == "terminal":
        terminal = request.get("terminal") or {}
        expected = {
            "completed": "source",
            "failed_rolled_back": "rollback",
        }.get(str(terminal.get("status") or ""))
        if expected != outcome:
            raise RestoreExecutorBlocked(
                "restore_integrity_terminal_outcome_mismatch",
                mutation_started=True,
            )
    identity = (
        "archive-integrity-post-restore:v1:"
        f"{request['operation_id']}:{outcome}"
    )
    return outcome, hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _invalidate_integrity_truth(
    request: dict[str, Any],
    *,
    final_db_outcome: str | None,
) -> dict[str, Any]:
    outcome, _idempotency_key = _integrity_convergence_identity(
        request,
        final_db_outcome,
    )
    db, engine = _session()
    try:
        return invalidate_integrity_truth_after_restore(
            db,
            restore_operation_id=request["operation_id"],
            final_db_outcome=outcome,
        )
    finally:
        db.close()
        engine.dispose()


def _enqueue_integrity_scan(
    request: dict[str, Any],
    *,
    final_db_outcome: str | None,
) -> dict[str, Any]:
    outcome, idempotency_key = _integrity_convergence_identity(
        request,
        final_db_outcome,
    )
    db, engine = _session()
    try:
        result = start_integrity_scan(
            db,
            actor=_actor_from_request(request),
            idempotency_key=idempotency_key,
        )
        return {
            "final_db_outcome": outcome,
            "scan_id": str(result.get("scan_id") or ""),
            "scan_status": str(result.get("status") or "queued"),
            "replayed": bool(result.get("replayed")),
            "coalesced": bool(result.get("coalesced")),
            "idempotency_key": idempotency_key,
        }
    finally:
        db.close()
        engine.dispose()


def _exact_artifact(
    request: dict[str, Any],
    *,
    artifact_id: str | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    selected = artifact_id or request["artifact"]["artifact_id"]
    manifest_path, manifest = _manifest_for_artifact(selected)
    verification = verify_backup_manifest(manifest_path)
    if not verification.get("valid") or verification.get("integrity_status") != "verified":
        raise RestoreExecutorBlocked("artifact_integrity_not_verified")
    actor = _actor_from_request(request)
    evidence = current_restore_artifact_evidence(
        selected,
        actor=actor,
        db_backend="postgresql",
    )
    if selected == request["artifact"]["artifact_id"] and (
        evidence.get("fingerprint") != request["artifact"]["fingerprint"]
        or not evidence.get("temporary_restore_validated")
        or not evidence.get("actor_access_verified")
    ):
        raise RestoreExecutorBlocked("artifact_fingerprint_changed")
    return manifest_path, manifest, evidence


def _preflight(request: dict[str, Any]) -> dict[str, Any]:
    db, engine = _session()
    try:
        backend = str(db.get_bind().url.get_backend_name()).lower()
        schema = schema_version_status(db)
        if (
            not backend.startswith("postgresql")
            or schema.get("schema_version") != CURRENT_SCHEMA_VERSION
            or schema.get("status") not in SAFE_STATUSES
        ):
            raise RestoreExecutorBlocked("current_schema_not_exact")
        assert_no_maintenance_conflicts("restore", db=db)
        _exact_artifact(request)
        actor = request["requested_by"]
        row = db.execute(
            text(
                "SELECT id, username, role, is_active FROM users "
                "WHERE username=:username LIMIT 1"
            ),
            {"username": actor["subject"]},
        ).mappings().first()
        if (
            row is None
            or row.get("id") != actor["user_id"]
            or row.get("role") != actor["role"]
            or not bool(row.get("is_active"))
        ):
            raise RestoreExecutorBlocked("current_actor_access_changed")
        return {
            "schema_exact": True,
            "artifact_exact": True,
            "current_actor_active": True,
        }
    except MaintenanceAdmissionBlocked as exc:
        raise RestoreExecutorBlocked(exc.code) from exc
    finally:
        db.close()
        engine.dispose()


def _pre_restore_backup(request: dict[str, Any]) -> dict[str, Any]:
    db, engine = _session()
    try:
        backup = create_backup_before_upgrade(
            db,
            config=BackupExecutionConfig(source="pre_restore"),
            migration_plan_summary={
                "operation": "current_db_restore",
                "operation_id": request["operation_id"],
                "migration_auto_apply": False,
            },
        )
        manifest_path = Path(backup["manifest_path"])
        verification = verify_backup_manifest(manifest_path)
        if not verification.get("valid") or verification.get("integrity_status") != "verified":
            raise RestoreExecutorBlocked("pre_restore_backup_verification_failed")
        root = configured_backup_root()
        artifact_id = str(backup["backup_id"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        evidence = artifact_version_evidence(
            root,
            artifact_id,
            manifest,
            checksum_sha256=str(verification.get("observed_checksum_sha256") or ""),
            context=current_validation_context("postgresql"),
        )
        write_artifact_state(
            root,
            artifact_id,
            {
                "integrity": {
                    "status": "verified",
                    "checked_at": utc_iso(),
                    "operation_id": request["operation_id"],
                    "reason_code": None,
                    "evidence": evidence,
                },
                "restore_validation": {
                    "status": "not_performed",
                    "validated_at": None,
                    "operation_id": None,
                    "reason_code": "not_performed",
                    "evidence": None,
                    "actor_key": None,
                },
                "delete_status": "allowed",
            },
        )
        return {
            "pre_restore_backup_id": artifact_id,
            "verified": True,
        }
    except BackupSafetyBlocked as exc:
        raise RestoreExecutorBlocked(exc.status) from exc
    finally:
        db.close()
        engine.dispose()


def _admin_url(url: URL) -> URL:
    return url.set(database="postgres" if url.database != "postgres" else "template1")


def _quote_identifier(value: str) -> str:
    if not SAFE_ID_RE.fullmatch(value):
        raise RestoreExecutorBlocked("database_identity_invalid")
    return '"' + value.replace('"', '""') + '"'


def _reset_postgres_database(url: URL) -> None:
    database = str(url.database or "")
    owner = str(url.username or "")
    quoted_database = _quote_identifier(database)
    quoted_owner = _quote_identifier(owner)
    engine = create_engine(_admin_url(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:database AND pid <> pg_backend_pid()"
                ),
                {"database": database},
            )
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted_database}"))
            conn.execute(text(f"CREATE DATABASE {quoted_database} OWNER {quoted_owner}"))
    finally:
        engine.dispose()


def _pg_restore(url: URL, dump_path: Path) -> None:
    command = [
        "pg_restore",
        "--no-owner",
        "--no-privileges",
    ]
    if url.host:
        command.extend(["--host", str(url.host)])
    if url.port:
        command.extend(["--port", str(url.port)])
    if url.username:
        command.extend(["--username", str(url.username)])
    command.extend(["--dbname", str(url.database), str(dump_path)])
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    result = subprocess.run(
        command,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode != 0:
        if is_benign_transaction_timeout_restore_warning(
            result.stderr or ""
        ):
            return
        raise RestoreExecutorBlocked("pg_restore_failed", mutation_started=True)


def _restore_fault_injection_enabled() -> bool:
    return bool(
        str(settings.app_env or "").strip().lower() == "test"
        and str(
            os.getenv("KMVMS_TEST_FAULT_INJECTION") or ""
        ).strip()
        == "1"
    )


def _restore(
    request: dict[str, Any],
    *,
    artifact_id: str,
    mode: str,
) -> dict[str, Any]:
    if mode not in {"source", "rollback"}:
        raise RestoreExecutorBlocked("restore_mode_invalid")
    manifest_path, manifest, _evidence = _exact_artifact(
        request,
        artifact_id=artifact_id,
    )
    dump_name = Path(str(manifest.get("backup_file_label") or "")).name
    dump_path = manifest_path.with_name(dump_name)
    write_bounded_json_atomic(
        _marker_path(),
        {
            "schema_version": 1,
            "operation_id": request["operation_id"],
            "mode": mode,
            "artifact_id": artifact_id,
            "started_at": utc_iso(),
            "mutation_started": True,
        },
    )
    url = make_url(settings.database_url)
    _reset_postgres_database(url)
    injected_phase = str(
        os.getenv("KMVMS_RESTORE_TEST_FAILURE_PHASE") or ""
    )
    if (
        _restore_fault_injection_enabled()
        and (
            (injected_phase == "after_database_reset" and mode == "source")
            or (
                injected_phase == "after_rollback_database_reset"
                and mode == "rollback"
            )
        )
    ):
        raise RestoreExecutorBlocked(
            (
                "test_injected_rollback_failure_after_database_reset"
                if mode == "rollback"
                else "test_injected_failure_after_database_reset"
            ),
            mutation_started=True,
        )
    _pg_restore(url, dump_path)
    return {
        "artifact_id": artifact_id,
        "mode": mode,
        "mutation_started": True,
        "restore_completed": True,
    }


def _latest_recorder_runtime(engine: Any) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT recorder_instance_id,
                       loop_state,
                       EXTRACT(
                           EPOCH FROM (
                               started_at AT TIME ZONE
                               current_setting('TimeZone')
                           )
                       ) AS started_at_epoch,
                       EXTRACT(
                           EPOCH FROM (
                               heartbeat_at AT TIME ZONE
                               current_setting('TimeZone')
                           )
                       ) AS heartbeat_at_epoch,
                       EXTRACT(
                           EPOCH FROM (
                               CURRENT_TIMESTAMP
                               - (
                                   heartbeat_at AT TIME ZONE
                                   current_setting('TimeZone')
                               )
                           )
                       ) AS heartbeat_age_seconds
                FROM recorder_runtime_status
                ORDER BY heartbeat_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
    return dict(row) if row is not None else None


def _recorder_runtime_proof(
    *,
    not_before_epoch: float | None,
    timeout_seconds: float = RECORDER_PROOF_TIMEOUT_SECONDS,
    poll_seconds: float = RECORDER_PROOF_POLL_SECONDS,
) -> dict[str, Any]:
    boundary: float | None = None
    if not_before_epoch is not None:
        try:
            boundary = float(not_before_epoch)
        except (TypeError, ValueError) as exc:
            raise RestoreExecutorBlocked(
                "restore_recorder_proof_boundary_invalid"
            ) from exc
        if (
            not math.isfinite(boundary)
            or boundary <= 0
            or boundary > time.time() + 300
        ):
            raise RestoreExecutorBlocked(
                "restore_recorder_proof_boundary_invalid"
            )

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    try:
        while True:
            runtime: dict[str, Any] | None
            try:
                runtime = _latest_recorder_runtime(engine)
            except Exception:
                runtime = None
            if runtime is not None:
                instance_id = str(
                    runtime.get("recorder_instance_id") or ""
                ).strip()
                loop_state = str(runtime.get("loop_state") or "").strip()
                try:
                    started_at_epoch = float(
                        runtime.get("started_at_epoch") or 0
                    )
                    heartbeat_at_epoch = float(
                        runtime.get("heartbeat_at_epoch") or 0
                    )
                    heartbeat_age_seconds = float(
                        runtime.get("heartbeat_age_seconds") or 0
                    )
                except (TypeError, ValueError):
                    started_at_epoch = 0
                    heartbeat_at_epoch = 0
                    heartbeat_age_seconds = float("inf")
                after_boundary = bool(
                    boundary is None
                    or (
                        started_at_epoch
                        >= boundary - RECORDER_CLOCK_TOLERANCE_SECONDS
                        and heartbeat_at_epoch
                        >= boundary - RECORDER_CLOCK_TOLERANCE_SECONDS
                    )
                )
                heartbeat_fresh = bool(
                    -RECORDER_CLOCK_TOLERANCE_SECONDS
                    <= heartbeat_age_seconds
                    <= RECORDER_HEARTBEAT_MAX_AGE_SECONDS
                )
                if (
                    1 <= len(instance_id) <= 255
                    and loop_state == "loop"
                    and heartbeat_fresh
                    and after_boundary
                ):
                    return {
                        "recorder_container_check_required": True,
                        "recorder_instance_current": True,
                        "recorder_heartbeat_fresh": True,
                        "recorder_loop_operational": True,
                    }
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.0, float(poll_seconds)))
    finally:
        engine.dispose()
    raise RestoreExecutorBlocked("restore_recorder_heartbeat_timeout")


def _table_count(db: Session, table: str) -> int:
    if not SAFE_ID_RE.fullmatch(table):
        raise RestoreExecutorBlocked("post_restore_table_invalid")
    return int(db.execute(text(f"SELECT COUNT(*) FROM {_quote_identifier(table)}")).scalar() or 0)


def _post_check(request: dict[str, Any]) -> dict[str, Any]:
    db, engine = _session()
    try:
        tables = set(inspect(db.get_bind()).get_table_names())
        required = {
            "users",
            "cameras",
            "system_settings",
            "archive_roots",
            "recording_jobs",
            "recording_segments",
            "schema_version_state",
            "schema_migration_history",
            "audit_events",
        }
        if not required.issubset(tables):
            raise RestoreExecutorBlocked("post_restore_tables_missing")
        schema = schema_version_status(db)
        if (
            schema.get("schema_version") != CURRENT_SCHEMA_VERSION
            or schema.get("status") not in SAFE_STATUSES
        ):
            raise RestoreExecutorBlocked("post_restore_schema_invalid")
        actor = request["requested_by"]
        initiating = db.execute(
            text(
                "SELECT id, role, is_active FROM users "
                "WHERE username=:username LIMIT 1"
            ),
            {"username": actor["subject"]},
        ).mappings().first()
        active_admins = int(
            db.execute(
                text(
                    "SELECT COUNT(*) FROM users "
                    "WHERE is_active=true AND role IN ('owner','admin')"
                )
            ).scalar()
            or 0
        )
        if (
            initiating is None
            or initiating.get("role") not in {"owner", "admin"}
            or not bool(initiating.get("is_active"))
            or active_admins < 1
        ):
            raise RestoreExecutorBlocked("post_restore_actor_access_invalid")
        counts = {
            table: _table_count(db, table)
            for table in (
                "users",
                "cameras",
                "system_settings",
                "archive_roots",
                "recording_jobs",
                "recording_segments",
            )
        }
        if counts["users"] < 1 or counts["system_settings"] < 1:
            raise RestoreExecutorBlocked("post_restore_metadata_invalid")
        return {
            "schema_exact": True,
            "initiating_actor_active": True,
            "active_owner_or_admin_present": True,
            "users_readable": True,
            "cameras_readable": True,
            "settings_readable": True,
            "recording_metadata_readable": True,
            "counts": counts,
            "video_archive_checked_by_metadata_only": True,
        }
    finally:
        db.close()
        engine.dispose()


def execute(
    action: str,
    operation_id: str,
    artifact_id: str | None,
    mode: str,
    recorder_not_before_epoch: float | None = None,
    final_db_outcome: str | None = None,
) -> dict[str, Any]:
    request = _load_request(
        operation_id,
        allow_terminal=action == "enqueue-integrity",
    )
    if action == "preflight":
        return _preflight(request)
    if action == "pre-restore-backup":
        return _pre_restore_backup(request)
    if action == "restore":
        if not artifact_id:
            raise RestoreExecutorBlocked("restore_artifact_missing")
        return _restore(request, artifact_id=artifact_id, mode=mode)
    if action == "invalidate-integrity":
        return _invalidate_integrity_truth(
            request,
            final_db_outcome=final_db_outcome,
        )
    if action == "post-check":
        return _post_check(request)
    if action == "recorder-proof":
        if recorder_not_before_epoch is None:
            raise RestoreExecutorBlocked(
                "restore_recorder_proof_boundary_missing"
            )
        return _recorder_runtime_proof(
            not_before_epoch=recorder_not_before_epoch
        )
    if action == "recorder-live-proof":
        return _recorder_runtime_proof(not_before_epoch=None)
    if action == "enqueue-integrity":
        return _enqueue_integrity_scan(
            request,
            final_db_outcome=final_db_outcome,
        )
    raise RestoreExecutorBlocked("restore_action_invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "preflight",
            "pre-restore-backup",
            "restore",
            "invalidate-integrity",
            "post-check",
            "recorder-proof",
            "recorder-live-proof",
            "enqueue-integrity",
        ),
    )
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--artifact-id")
    parser.add_argument("--mode", choices=("source", "rollback"), default="source")
    parser.add_argument("--recorder-not-before-epoch", type=float)
    parser.add_argument(
        "--final-db-outcome",
        choices=("source", "rollback"),
    )
    args = parser.parse_args()
    operation_id = str(args.operation_id or "")
    try:
        details = execute(
            args.action,
            operation_id,
            args.artifact_id,
            args.mode,
            args.recorder_not_before_epoch,
            args.final_db_outcome,
        )
        _write_result(
            operation_id=operation_id,
            action=args.action,
            status="completed",
            mutation_started=bool(details.get("mutation_started")),
            details=details,
        )
        return 0
    except (
        CurrentRestoreBlocked,
        RestoreExecutorBlocked,
        MaintenanceAdmissionBlocked,
    ) as exc:
        reason = getattr(exc, "code", "restore_executor_failed")
        mutation_started = bool(getattr(exc, "mutation_started", False))
    except Exception:
        reason = "restore_executor_failed"
        mutation_started = _destructive_started_for(operation_id)
    _write_result(
        operation_id=operation_id,
        action=args.action,
        status="failed",
        reason_code=reason,
        mutation_started=mutation_started,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
