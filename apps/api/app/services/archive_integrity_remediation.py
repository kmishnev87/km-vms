from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.permissions import user_has_permission
from app.models.archive_integrity import (
    ArchiveIntegrityFinding,
    ArchiveIntegrityRemediationItem,
    ArchiveIntegrityRemediationPlan,
    ArchiveIntegrityScan,
    RecorderFileReceipt,
)
from app.models.audit_event import AuditEvent
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.storage_operation import StorageOperation
from app.models.user import User
from app.services.archive_integrity import (
    ORPHAN_MIN_AGE,
    ORPHAN_OBSERVATION_GRACE,
    RECENT_WRITE_WINDOW,
    _bounded_fingerprint,
    _file_within_recent_write_window,
    _metadata_version,
    _normalize_relative,
    _receipt_matches,
    _refresh_scan_summary,
    _root_access_identity,
    _root_snapshot_key,
    _safe_probe,
    acquire_remediation_plan_coordinator,
    remediation_apply_operation_candidates,
    remediation_apply_operation_identity,
)
from app.services.audit_log import create_event
from app.services.recording_operations import DestructiveScopeConflict, destructive_scope_guard
from app.services.recording_retention import (
    EXECUTION_POLICY_MANUAL_COMPLETE,
    execute_segments,
)
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    archive_root_runtime_access_state,
    archive_root_runtime_path,
    safe_resolve_relative_for_root,
)
from app.services.storage_operation_conflicts import (
    StorageOperationConflict,
    claim_operation_with_conflicts,
    claim_state_detail,
    operation_instance_id,
    reclaim_operation_with_conflicts,
    terminal_replay_result,
)
from app.services.storage_operations_foundation import (
    StorageOperationContractError,
    StorageOperationLeaseLost,
    TERMINAL_OPERATION_STATUSES,
    actor_identity,
    assert_operation_owned,
    canonical_operation_scope,
    claim_operation,
    database_now,
    ensure_operation_terminal_audit,
    finish_operation,
    normalize_operation_scope,
    operation_cancel_requested,
    reclaim_operation,
    request_fingerprint,
    safe_reason_code,
    stage_operation_terminal,
)


PLAN_TTL = timedelta(minutes=30)
PLAN_SCHEMA_VERSION = 1
TERMINAL_PLAN_STATES = frozenset({"completed", "partial", "blocked", "failed", "cancelled"})
TERMINAL_PENDING_PLAN_STATE = "terminal_pending"
PHYSICAL_PENDING_ITEM_STATES = frozenset(
    {
        "physical_mutation_prepared",
        "quarantine_prepared",
        "quarantined",
        "delete_committing",
        "physical_mutation_committed",
    }
)
MUTATING_ACTIONS = {
    "retire_missing_recording": {
        "categories": {"missing_file"},
        "permission": "delete_recordings",
        "operation_type": "integrity_catalog_retirement",
        "mutation": "retire_missing_metadata",
    },
    "mark_stale_recording": {
        "categories": {"stale_writing_segment"},
        "permission": "manage_settings",
        "operation_type": "integrity_metadata_repair",
        "mutation": "mark_stale_nonplayable",
    },
    "delete_unusable_recording": {
        "categories": {"zero_size_file", "corrupted_file", "partial_file"},
        "permission": "delete_recordings",
        "operation_type": "integrity_recording_delete",
        "mutation": "delete_trusted_unusable_recording",
    },
    "delete_proven_orphan": {
        "categories": {"orphan_file"},
        "permission": "delete_recordings",
        "operation_type": "orphan_file_cleanup",
        "mutation": "delete_proven_orphan_file",
    },
}

PRE_MUTATION_PERMANENT_CONTEXT_REASONS = frozenset(
    {
        "archive_integrity_finding_stale",
        "archive_integrity_scan_stale",
        "archive_integrity_root_unresolved",
        "archive_integrity_root_changed",
        "archive_integrity_root_access_changed",
        "archive_integrity_segment_missing",
        "archive_integrity_segment_changed",
    }
)
PRE_MUTATION_TRANSIENT_CONTEXT_REASONS = frozenset({"archive_integrity_root_unavailable"})
UNBOUND_RECOVERY_BATCH = 16
UNBOUND_RECOVERY_POLL_BUDGET = UNBOUND_RECOVERY_BATCH * 4

_unbound_recovery_cursor_lock = threading.Lock()
_unbound_recovery_cursor: tuple[datetime, str] | None = None


class IntegrityRemediationBlocked(RuntimeError):
    def __init__(self, reason_code: str, *, retry_mode: str | None = "new_scan"):
        self.reason_code = str(reason_code)[:96]
        self.retry_mode = retry_mode
        super().__init__(self.reason_code)


def _reset_unbound_recovery_scan_state() -> None:
    global _unbound_recovery_cursor
    with _unbound_recovery_cursor_lock:
        _unbound_recovery_cursor = None


def _unbound_recovery_cursor_snapshot() -> tuple[datetime, str] | None:
    with _unbound_recovery_cursor_lock:
        return _unbound_recovery_cursor


def _set_unbound_recovery_cursor(cursor: tuple[datetime, str] | None) -> None:
    global _unbound_recovery_cursor
    with _unbound_recovery_cursor_lock:
        _unbound_recovery_cursor = cursor


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _plan_public(plan: ArchiveIntegrityRemediationPlan, *, replayed: bool = False) -> dict[str, Any]:
    public_state = "running" if plan.state == TERMINAL_PENDING_PLAN_STATE else plan.state
    return {
        "plan_id": plan.id,
        "action_key": plan.action_kind,
        "state": public_state,
        "item_count": int(plan.item_count or 0),
        "total_bytes": int(plan.total_bytes or 0),
        "confirmation_level": plan.confirmation_level,
        "required_permission": plan.required_permission,
        "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
        "reason_code": plan.reason_code,
        "retry_mode": plan.retry_mode,
        "next_action": plan.next_action,
        "result": dict(plan.result_summary or {}),
        "replayed": bool(replayed),
    }


def get_remediation_plan(db: Session, plan_id: str, *, actor: Any) -> dict[str, Any]:
    plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
    if plan is None:
        raise StorageOperationContractError("archive_integrity_plan_not_found")
    _kind, actor_key, _user_id, _owner = actor_identity(actor)
    if plan.actor_key != actor_key:
        raise StorageOperationContractError("archive_integrity_plan_actor_mismatch")
    if plan.state in TERMINAL_PLAN_STATES:
        coordinator = acquire_remediation_plan_coordinator(db)
        try:
            coordinator.assert_owned()
            plan = (
                db.query(ArchiveIntegrityRemediationPlan)
                .filter(ArchiveIntegrityRemediationPlan.id == str(plan_id))
                .populate_existing()
                .with_for_update()
                .first()
            )
            if plan is None:
                raise StorageOperationContractError("archive_integrity_plan_not_found")
            if plan.actor_key != actor_key:
                raise StorageOperationContractError("archive_integrity_plan_actor_mismatch")
            if plan.state in TERMINAL_PLAN_STATES:
                _converge_terminal_plan(db, plan, actor=actor)
            coordinator.assert_owned()
        finally:
            coordinator.close()
    return _plan_public(plan)


def _finding_for_plan(db: Session, finding_id: str, action_key: str) -> tuple[ArchiveIntegrityFinding, ArchiveIntegrityScan, dict[str, Any]]:
    action = MUTATING_ACTIONS.get(str(action_key))
    if action is None:
        raise IntegrityRemediationBlocked("archive_integrity_action_unsupported", retry_mode=None)
    finding = db.get(ArchiveIntegrityFinding, str(finding_id))
    if finding is None or not finding.is_active or finding.state != "active":
        raise IntegrityRemediationBlocked("archive_integrity_finding_stale")
    if finding.category not in action["categories"] or finding.action_key != action_key:
        raise IntegrityRemediationBlocked("archive_integrity_action_mismatch", retry_mode=None)
    scan = db.get(ArchiveIntegrityScan, finding.scan_id)
    if scan is None or scan.status not in {"completed", "partial"} or scan.is_stale:
        raise IntegrityRemediationBlocked("archive_integrity_scan_stale")
    return finding, scan, action


def _root_for_finding(db: Session, finding: ArchiveIntegrityFinding) -> ArchiveRoot:
    if not finding.root_id:
        raise IntegrityRemediationBlocked("archive_integrity_root_unresolved")
    root = db.get(ArchiveRoot, str(finding.root_id))
    if root is None or root.retired_at is not None:
        raise IntegrityRemediationBlocked("archive_integrity_root_changed")
    facts = dict(finding.observed_facts or {})
    if str(facts.get("root_snapshot_key") or "") != _root_snapshot_key(root):
        raise IntegrityRemediationBlocked("archive_integrity_root_changed")
    access = archive_root_runtime_access_state(root)
    access_identity = _root_access_identity(root, access)
    if access.get("read_access_state") != "available" or not access_identity:
        raise IntegrityRemediationBlocked("archive_integrity_root_unavailable")
    if str(facts.get("root_access_identity") or "") != access_identity:
        raise IntegrityRemediationBlocked("archive_integrity_root_access_changed")
    return root


def _segment_for_finding(db: Session, finding: ArchiveIntegrityFinding) -> RecordingSegment:
    if finding.segment_id is None:
        raise IntegrityRemediationBlocked("archive_integrity_segment_missing")
    segment = db.get(RecordingSegment, int(finding.segment_id))
    if segment is None or segment.deleted_at is not None or str(segment.status) == "deleted":
        raise IntegrityRemediationBlocked("archive_integrity_segment_changed")
    if _metadata_version(segment) != str(finding.metadata_version or ""):
        raise IntegrityRemediationBlocked("archive_integrity_segment_changed")
    return segment


def _active_write_exists(db: Session, segment: RecordingSegment) -> bool:
    if str(segment.status or "") in {"starting", "stopping", "restarting"}:
        return True
    if segment.job_id:
        active = (
            db.query(RecordingJob.id)
            .filter(
                RecordingJob.id == str(segment.job_id),
                RecordingJob.state.in_(("starting", "recording", "stopping", "restarting")),
            )
            .first()
        )
        if active is not None:
            return True
    return False


def _relative_for_finding(finding: ArchiveIntegrityFinding) -> str:
    relative_ref = _normalize_relative(finding.relative_ref)
    if relative_ref is None or not (
        relative_ref == KMVMS_RECORDINGS_NAMESPACE
        or relative_ref.startswith(f"{KMVMS_RECORDINGS_NAMESPACE}/")
    ):
        raise IntegrityRemediationBlocked("archive_integrity_path_invalid", retry_mode=None)
    return relative_ref


def _stat_facts_match(observed: dict[str, Any], stat_result: os.stat_result) -> bool:
    return bool(
        str(observed.get("device_id") or "") == str(int(stat_result.st_dev))
        and str(observed.get("inode") or "") == str(int(stat_result.st_ino))
        and int(observed.get("size_bytes") or 0) == int(stat_result.st_size)
        and int(observed.get("mtime_ns") or 0) == int(stat_result.st_mtime_ns)
    )


def _revalidate_missing(db: Session, finding: ArchiveIntegrityFinding) -> tuple[RecordingSegment, ArchiveRoot, str, dict[str, Any]]:
    segment = _segment_for_finding(db, finding)
    root = _root_for_finding(db, finding)
    relative_ref = _relative_for_finding(finding)
    if (
        segment.ownership != "KM VMS"
        or segment.source != "recorder"
        or str(segment.status) not in {"finalized", "failed"}
    ):
        raise IntegrityRemediationBlocked("archive_integrity_missing_not_finalized")
    if _active_write_exists(db, segment):
        raise IntegrityRemediationBlocked("archive_integrity_active_write")
    age_anchor = segment.finalized_at or segment.updated_at or segment.started_at
    if not age_anchor or datetime.utcnow() - age_anchor < RECENT_WRITE_WINDOW:
        raise IntegrityRemediationBlocked("archive_integrity_recorder_window_active")
    target = safe_resolve_relative_for_root(relative_ref, root)
    try:
        target.lstat()
        raise IntegrityRemediationBlocked("archive_integrity_missing_file_reappeared")
    except FileNotFoundError:
        pass
    access = archive_root_runtime_access_state(root)
    access_identity = _root_access_identity(root, access)
    if access.get("read_access_state") != "available" or access_identity != dict(finding.observed_facts or {}).get("root_access_identity"):
        raise IntegrityRemediationBlocked("archive_integrity_root_access_changed")
    try:
        target.lstat()
        raise IntegrityRemediationBlocked("archive_integrity_missing_file_reappeared")
    except FileNotFoundError:
        pass
    return segment, root, relative_ref, {
        "segment_version": _metadata_version(segment),
        "root_snapshot_key": _root_snapshot_key(root),
        "root_access_identity": access_identity,
        "relative_ref": relative_ref,
        "absence_revalidated_at": datetime.utcnow().isoformat(),
    }


def _revalidate_stale(db: Session, finding: ArchiveIntegrityFinding) -> tuple[RecordingSegment, ArchiveRoot, str, dict[str, Any]]:
    segment = _segment_for_finding(db, finding)
    root = _root_for_finding(db, finding)
    relative_ref = _relative_for_finding(finding)
    if str(segment.status or "") not in {"writing", "stale_writing"}:
        raise IntegrityRemediationBlocked("archive_integrity_stale_state_changed")
    if _active_write_exists(db, segment):
        raise IntegrityRemediationBlocked("archive_integrity_active_write")
    anchor = segment.updated_at or segment.started_at
    if not anchor or datetime.utcnow() - anchor < RECENT_WRITE_WINDOW:
        raise IntegrityRemediationBlocked("archive_integrity_recorder_window_active")
    target = safe_resolve_relative_for_root(relative_ref, root)
    try:
        stat_result = target.lstat()
    except OSError as exc:
        raise IntegrityRemediationBlocked("archive_integrity_file_changed") from exc
    if not stat_module.S_ISREG(stat_result.st_mode) or not _stat_facts_match(dict(finding.observed_facts or {}), stat_result):
        raise IntegrityRemediationBlocked("archive_integrity_file_changed")
    if _file_within_recent_write_window(stat_result):
        raise IntegrityRemediationBlocked("archive_integrity_recorder_window_active", retry_mode="new_scan")
    return segment, root, relative_ref, {
        "segment_version": _metadata_version(segment),
        "root_snapshot_key": _root_snapshot_key(root),
        "root_access_identity": _root_access_identity(root, archive_root_runtime_access_state(root)),
        "relative_ref": relative_ref,
        "device_id": str(int(stat_result.st_dev)),
        "inode": str(int(stat_result.st_ino)),
        "size_bytes": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def _revalidate_unusable(db: Session, finding: ArchiveIntegrityFinding) -> tuple[RecordingSegment, ArchiveRoot, str, dict[str, Any]]:
    segment = _segment_for_finding(db, finding)
    root = _root_for_finding(db, finding)
    relative_ref = _relative_for_finding(finding)
    category = str(finding.category or "")
    expected_statuses = {"failed"} if category == "partial_file" else {"finalized"}
    if (
        segment.ownership != "KM VMS"
        or segment.source != "recorder"
        or str(segment.status or "") not in expected_statuses
    ):
        raise IntegrityRemediationBlocked("archive_integrity_unusable_state_changed")
    if _active_write_exists(db, segment):
        raise IntegrityRemediationBlocked("archive_integrity_active_write")
    anchor = segment.updated_at or segment.ended_at or segment.started_at
    if not anchor or datetime.utcnow() - anchor < RECENT_WRITE_WINDOW:
        raise IntegrityRemediationBlocked("archive_integrity_recorder_window_active")
    target = safe_resolve_relative_for_root(relative_ref, root)
    try:
        stat_result = target.lstat()
    except OSError as exc:
        raise IntegrityRemediationBlocked("archive_integrity_file_changed") from exc
    if not stat_module.S_ISREG(stat_result.st_mode) or not _stat_facts_match(dict(finding.observed_facts or {}), stat_result):
        raise IntegrityRemediationBlocked("archive_integrity_file_changed")
    if _file_within_recent_write_window(stat_result):
        raise IntegrityRemediationBlocked("archive_integrity_recorder_window_active", retry_mode="new_scan")
    if category == "zero_size_file":
        if int(stat_result.st_size) != 0:
            raise IntegrityRemediationBlocked("archive_integrity_file_changed")
    elif category == "corrupted_file":
        probe_ok, _probe_status = _safe_probe(target)
        if probe_ok is not False:
            raise IntegrityRemediationBlocked("archive_integrity_corruption_not_reproduced")
    elif category == "partial_file":
        if str((finding.observed_facts or {}).get("status") or "") != "failed":
            raise IntegrityRemediationBlocked("archive_integrity_incomplete_state_changed")
    else:
        raise IntegrityRemediationBlocked("archive_integrity_unusable_category_changed")
    return segment, root, relative_ref, {
        "segment_version": _metadata_version(segment),
        "root_snapshot_key": _root_snapshot_key(root),
        "root_access_identity": _root_access_identity(root, archive_root_runtime_access_state(root)),
        "relative_ref": relative_ref,
        "device_id": str(int(stat_result.st_dev)),
        "inode": str(int(stat_result.st_ino)),
        "size_bytes": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def _revalidate_orphan(db: Session, finding: ArchiveIntegrityFinding) -> tuple[None, ArchiveRoot, str, dict[str, Any]]:
    root = _root_for_finding(db, finding)
    relative_ref = _relative_for_finding(finding)
    facts = dict(finding.observed_facts or {})
    if (
        finding.category != "orphan_file"
        or not facts.get("receipt_verified")
        or int(finding.observation_count or 0) < 2
        or not finding.first_observed_at
        or datetime.utcnow() - finding.first_observed_at < ORPHAN_OBSERVATION_GRACE
    ):
        raise IntegrityRemediationBlocked("archive_integrity_orphan_evidence_insufficient")
    if (
        db.query(RecordingSegment.id)
        .filter(
            RecordingSegment.archive_root_id == str(root.id),
            RecordingSegment.relative_path == relative_ref,
            RecordingSegment.deleted_at.is_(None),
            RecordingSegment.status != "deleted",
        )
        .first()
        is not None
    ):
        raise IntegrityRemediationBlocked("archive_integrity_object_now_owned")
    target = safe_resolve_relative_for_root(relative_ref, root)
    try:
        stat_result = target.lstat()
    except OSError as exc:
        raise IntegrityRemediationBlocked("archive_integrity_file_changed") from exc
    if not stat_module.S_ISREG(stat_result.st_mode) or not _stat_facts_match(facts, stat_result):
        raise IntegrityRemediationBlocked("archive_integrity_file_changed")
    if datetime.utcnow() - datetime.fromtimestamp(stat_result.st_mtime) < ORPHAN_MIN_AGE:
        raise IntegrityRemediationBlocked("archive_integrity_orphan_too_new")
    receipt_id = str(facts.get("receipt_id") or "")
    receipt = db.get(RecorderFileReceipt, receipt_id) if receipt_id else None
    if receipt is None:
        raise IntegrityRemediationBlocked("archive_integrity_orphan_receipt_missing")
    try:
        fingerprint = _bounded_fingerprint(target, stat_result)
    except OSError as exc:
        raise IntegrityRemediationBlocked("archive_integrity_file_changed") from exc
    if not _receipt_matches(
        receipt,
        root=root,
        relative_ref=relative_ref,
        stat_result=stat_result,
        fingerprint=fingerprint,
    ):
        raise IntegrityRemediationBlocked("archive_integrity_orphan_receipt_mismatch")
    return None, root, relative_ref, {
        "root_snapshot_key": _root_snapshot_key(root),
        "root_access_identity": _root_access_identity(root, archive_root_runtime_access_state(root)),
        "relative_ref": relative_ref,
        "stable_object_key": finding.stable_object_key,
        "receipt_id": receipt.id,
        "device_id": str(int(stat_result.st_dev)),
        "inode": str(int(stat_result.st_ino)),
        "size_bytes": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "fingerprint": fingerprint,
        "first_observed_scan_id": finding.first_observed_scan_id,
        "first_observed_at": finding.first_observed_at.isoformat(),
        "second_observed_scan_id": finding.scan_id,
        "second_observed_at": finding.last_observed_at.isoformat() if finding.last_observed_at else None,
    }


def _plan_evidence(db: Session, finding: ArchiveIntegrityFinding, action_key: str):
    if action_key == "retire_missing_recording":
        return _revalidate_missing(db, finding)
    if action_key == "mark_stale_recording":
        return _revalidate_stale(db, finding)
    if action_key == "delete_unusable_recording":
        return _revalidate_unusable(db, finding)
    if action_key == "delete_proven_orphan":
        return _revalidate_orphan(db, finding)
    raise IntegrityRemediationBlocked("archive_integrity_action_unsupported", retry_mode=None)


def create_remediation_plan(
    db: Session,
    *,
    finding_id: str,
    action_key: str,
    actor: Any,
    idempotency_key: str,
) -> dict[str, Any]:
    finding, scan, action = _finding_for_plan(db, finding_id, action_key)
    if not user_has_permission(str(getattr(actor, "role", "")), str(action["permission"])):
        raise IntegrityRemediationBlocked("archive_integrity_permission_denied", retry_mode=None)
    _actor_kind, actor_key, actor_user_id, _owner = actor_identity(actor)
    idem = str(idempotency_key or "").strip().lower()
    if not idem or len(idem) > 64:
        raise StorageOperationContractError("archive_integrity_plan_idempotency_invalid")
    existing = (
        db.query(ArchiveIntegrityRemediationPlan)
        .filter(
            ArchiveIntegrityRemediationPlan.actor_key == actor_key,
            ArchiveIntegrityRemediationPlan.idempotency_key == idem,
        )
        .first()
    )
    if existing is not None:
        if existing.finding_id != finding.id or existing.action_kind != action_key:
            raise StorageOperationContractError("archive_integrity_plan_identity_mismatch")
        return _plan_public(existing, replayed=True)

    segment, root, relative_ref, evidence = _plan_evidence(db, finding, action_key)
    plan_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    request_identity = {
        "finding_id": finding.id,
        "action_key": action_key,
        "scan_id": scan.id,
        "idempotency_key": idem,
    }
    plan_operation_id = f"integrity-plan-{uuid.uuid4().hex}"
    claim = claim_operation(
        db,
        operation_type="integrity_plan_prepare",
        scope={
            "global": False,
            "root_ids": [str(root.id)],
            "camera_ids": [int(segment.camera_id)] if segment is not None and segment.camera_id is not None else [],
            "segment_ids": [int(segment.id)] if segment is not None else [],
        },
        request_identity=request_identity,
        actor=actor,
        operation_id=plan_operation_id,
        idempotency_key=idem,
        owner_instance_id=operation_instance_id("integrity-plan"),
        cancel_allowed=False,
    )
    if claim.get("state") != "claimed":
        raise IntegrityRemediationBlocked("archive_integrity_plan_operation_unavailable")
    handle = claim["handle"]
    now = database_now(db)
    canonical = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": plan_id,
        "scan_id": scan.id,
        "finding_id": finding.id,
        "action_kind": action_key,
        "required_permission": action["permission"],
        "item_id": item_id,
        "item_index": 0,
        "intended_mutation": action["mutation"],
        "evidence": evidence,
    }
    canonical_hash = _canonical_hash(canonical)
    plan = ArchiveIntegrityRemediationPlan(
        id=plan_id,
        scan_id=scan.id,
        finding_id=finding.id,
        operation_id=plan_operation_id,
        actor_user_id=actor_user_id,
        actor_key=actor_key,
        idempotency_key=idem,
        request_fingerprint=request_fingerprint(request_identity),
        action_kind=action_key,
        required_permission=str(action["permission"]),
        confirmation_level=str(finding.confirmation_level or "metadata"),
        schema_version=PLAN_SCHEMA_VERSION,
        item_count=1,
        total_bytes=int(evidence.get("size_bytes") or 0),
        canonical_hash=canonical_hash,
        state="prepared",
        created_at=now,
        expires_at=now + PLAN_TTL,
    )
    item = ArchiveIntegrityRemediationItem(
        id=item_id,
        plan_id=plan_id,
        finding_id=finding.id,
        item_index=0,
        segment_id=int(segment.id) if segment is not None else None,
        root_id=str(root.id),
        relative_ref=relative_ref,
        stable_object_key=finding.stable_object_key,
        receipt_id=str(evidence.get("receipt_id") or "") or None,
        intended_mutation=str(action["mutation"]),
        evidence=evidence,
        state="prepared",
    )
    try:
        db.add(plan)
        db.flush()
        db.add(item)
        db.commit()
    except Exception:
        db.rollback()
        finish_operation(
            db,
            handle,
            status="failed",
            result={"status": "failed"},
            reason_code="archive_integrity_plan_persistence_failed",
            retry_allowed=True,
            retry_mode="immediate",
        )
        raise
    finish_operation(
        db,
        handle,
        status="completed",
        result={"status": "completed", "plan_id": plan.id, "item_count": 1},
        progress={"planned_count": 1, "completed_count": 1, "failed_count": 0},
    )
    create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type="archive_integrity.remediation_plan_created",
        severity="warning" if str(plan.confirmation_level).startswith("destructive") else "info",
        message_ru="Archive integrity remediation plan created",
        message_en="Archive integrity remediation plan created",
        target_type="archive_integrity_plan",
        target_id=plan.id,
        metadata={"action_key": action_key, "item_count": 1, "scan_id": scan.id},
    )
    return _plan_public(plan)


def _plan_item(db: Session, plan: ArchiveIntegrityRemediationPlan) -> ArchiveIntegrityRemediationItem:
    item = (
        db.query(ArchiveIntegrityRemediationItem)
        .filter(ArchiveIntegrityRemediationItem.plan_id == plan.id)
        .order_by(ArchiveIntegrityRemediationItem.item_index.asc())
        .first()
    )
    if item is None or plan.item_count != 1:
        raise IntegrityRemediationBlocked("archive_integrity_plan_items_invalid", retry_mode=None)
    canonical = {
        "schema_version": int(plan.schema_version),
        "plan_id": plan.id,
        "scan_id": plan.scan_id,
        "finding_id": plan.finding_id,
        "action_kind": plan.action_kind,
        "required_permission": plan.required_permission,
        "item_id": item.id,
        "item_index": int(item.item_index),
        "intended_mutation": item.intended_mutation,
        "evidence": dict(item.evidence or {}),
    }
    if _canonical_hash(canonical) != plan.canonical_hash:
        raise IntegrityRemediationBlocked("archive_integrity_plan_hash_mismatch", retry_mode=None)
    return item


def _evidence_matches(item: ArchiveIntegrityRemediationItem, current: dict[str, Any]) -> bool:
    expected = dict(item.evidence or {})
    stable_keys = {
        "segment_version",
        "root_snapshot_key",
        "root_access_identity",
        "relative_ref",
        "stable_object_key",
        "receipt_id",
        "device_id",
        "inode",
        "size_bytes",
        "mtime_ns",
        "fingerprint",
    }
    return all(expected.get(key) == current.get(key) for key in stable_keys if key in expected)


def _apply_missing(db: Session, plan, item, finding, actor) -> dict[str, Any]:
    segment, _root, _relative, current = _revalidate_missing(db, finding)
    if not _evidence_matches(item, current):
        raise IntegrityRemediationBlocked("archive_integrity_plan_stale")
    now = database_now(db)
    segment.status = "deleted"
    segment.deleted_at = now
    segment.deletion_reason = "integrity_missing_file_retired"
    segment.deleted_by = str(getattr(actor, "username", None) or getattr(actor, "id", "system"))[:255]
    segment.deletion_source = "integrity_catalog_retirement"
    segment.integrity_status = "missing_file_retired"
    segment.integrity_error = None
    segment.cleanup_candidate = False
    segment.cleanup_reason = None
    segment.reconciliation_status = "missing_file_retired"
    segment.reconciliation_checked_at = now
    segment.updated_at = now
    db.add(segment)
    return {"status": "completed", "retired_count": 1, "deleted_file_count": 0}


def _apply_stale(db: Session, plan, item, finding, actor) -> dict[str, Any]:
    _revalidate_stale(db, finding)
    raise IntegrityRemediationBlocked(
        "archive_integrity_stale_requires_new_scan",
        retry_mode="new_scan",
    )


def _persist_item_state(
    db: Session,
    item: ArchiveIntegrityRemediationItem,
    state: str,
    *,
    result_code: str | None = None,
) -> None:
    item.state = state
    item.result_code = result_code
    item.updated_at = database_now(db)
    db.add(item)
    db.commit()


def _persist_physical_outcome(
    db: Session,
    *,
    plan: ArchiveIntegrityRemediationPlan,
    item: ArchiveIntegrityRemediationItem,
    result: dict[str, Any],
    reason_code: str | None = None,
    retry_mode: str | None = None,
    item_state: str = "physical_mutation_committed",
) -> None:
    now = database_now(db)
    exact_result = dict(result)
    next_action = "create_new_integrity_scan" if retry_mode == "new_scan" else "retry_remediation" if retry_mode else None
    updated = (
        db.query(ArchiveIntegrityRemediationPlan)
        .filter(
            ArchiveIntegrityRemediationPlan.id == str(plan.id),
            ArchiveIntegrityRemediationPlan.state == "running",
            ArchiveIntegrityRemediationPlan.apply_operation_id == str(plan.apply_operation_id),
        )
        .update(
            {
                ArchiveIntegrityRemediationPlan.state: TERMINAL_PENDING_PLAN_STATE,
                ArchiveIntegrityRemediationPlan.result_summary: exact_result,
                ArchiveIntegrityRemediationPlan.reason_code: reason_code,
                ArchiveIntegrityRemediationPlan.retry_mode: retry_mode,
                ArchiveIntegrityRemediationPlan.next_action: next_action,
                ArchiveIntegrityRemediationPlan.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        current = db.get(ArchiveIntegrityRemediationPlan, str(plan.id))
        if current is not None and current.state == TERMINAL_PENDING_PLAN_STATE and dict(current.result_summary or {}) == exact_result:
            return
        if current is not None and current.state in TERMINAL_PLAN_STATES and dict(current.result_summary or {}) == exact_result:
            return
        raise StorageOperationContractError("archive_integrity_physical_outcome_conflict")
    item_updated = db.query(ArchiveIntegrityRemediationItem).filter(
        ArchiveIntegrityRemediationItem.id == str(item.id),
        ArchiveIntegrityRemediationItem.plan_id == str(plan.id),
    ).update(
        {
            ArchiveIntegrityRemediationItem.state: item_state,
            ArchiveIntegrityRemediationItem.result_code: reason_code or str(exact_result.get("status") or "completed")[:96],
            ArchiveIntegrityRemediationItem.updated_at: now,
        },
        synchronize_session=False,
    )
    if item_updated != 1:
        db.rollback()
        raise StorageOperationContractError("archive_integrity_physical_item_missing")
    db.commit()
    db.expire(plan)
    db.expire(item)


def _physical_result(*, deleted_count: int, bytes_freed: int, status: str = "completed") -> dict[str, Any]:
    return {
        "status": status,
        "deleted_count": max(0, int(deleted_count)),
        "failed_count": 0,
        "skipped_count": 0,
        "bytes_freed": max(0, int(bytes_freed)),
    }


def _recover_unusable_outcome(
    db: Session,
    plan: ArchiveIntegrityRemediationPlan,
    item: ArchiveIntegrityRemediationItem,
    finding: ArchiveIntegrityFinding,
    actor: Any,
) -> dict[str, Any] | None:
    if item.state == "physical_mutation_committed":
        return dict(plan.result_summary or _physical_result(deleted_count=1, bytes_freed=int(item.evidence.get("size_bytes") or 0)))
    if item.state != "physical_mutation_prepared":
        return None
    segment = db.get(RecordingSegment, int(item.segment_id or 0))
    if segment is None:
        raise IntegrityRemediationBlocked("archive_integrity_segment_missing_after_mutation", retry_mode="new_scan")
    expected = dict(item.evidence or {})
    root = _root_for_finding(db, finding)
    relative_ref = _relative_for_finding(finding)
    opened: list[int] = []
    try:
        opened, parent_fd, filename = _open_verified_parent_handles(root, relative_ref, expected)
        try:
            stat_result = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            now = database_now(db)
            segment.status = "deleted"
            segment.deleted_at = now
            segment.deletion_reason = f"integrity_{finding.category}"
            segment.deleted_by = str(getattr(actor, "username", None) or getattr(actor, "id", "system"))[:255]
            segment.deletion_source = "integrity_recording_delete"
            segment.updated_at = now
            db.add(segment)
            return _physical_result(deleted_count=1, bytes_freed=int(expected.get("size_bytes") or 0))
        except OSError as exc:
            raise IntegrityRemediationBlocked("archive_integrity_file_state_unavailable", retry_mode="immediate") from exc
        if segment.deleted_at is not None or str(segment.status) == "deleted":
            raise IntegrityRemediationBlocked("archive_integrity_deleted_metadata_file_present", retry_mode="support")
        if _metadata_version(segment) != str(expected.get("segment_version") or ""):
            raise IntegrityRemediationBlocked("archive_integrity_segment_changed", retry_mode="new_scan")
        if not stat_module.S_ISREG(stat_result.st_mode) or not _stat_facts_match(expected, stat_result):
            raise IntegrityRemediationBlocked("archive_integrity_file_changed", retry_mode="new_scan")
        return None
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _apply_unusable(db: Session, plan, item, finding, actor, handle) -> dict[str, Any]:
    segment, _root, relative_ref, current = _revalidate_unusable(db, finding)
    if not _evidence_matches(item, current):
        raise IntegrityRemediationBlocked("archive_integrity_plan_stale")
    assert_operation_owned(db, handle)
    _persist_item_state(db, item, "physical_mutation_prepared")
    assert_operation_owned(db, handle)
    expected = {
        int(segment.id): {
            "segment_id": int(segment.id),
            "camera_id": int(segment.camera_id),
            "archive_root_id": str(segment.archive_root_id or ""),
            "relative_path": relative_ref,
            "size_bytes": int(segment.size_bytes or 0),
            "file_facts": {
                "device_id": str(current["device_id"]),
                "inode": str(current["inode"]),
                "size_bytes": int(current["size_bytes"]),
                "mtime_ns": int(current["mtime_ns"]),
                "minimum_age_seconds": int(RECENT_WRITE_WINDOW.total_seconds()),
            },
        }
    }
    result = execute_segments(
        db,
        [segment],
        actor=actor,
        operation="integrity_recording_delete",
        reason=f"integrity_{finding.category}",
        policy=EXECUTION_POLICY_MANUAL_COMPLETE,
        operation_id=str(plan.apply_operation_id),
        scope={
            "type": "segments",
            "segment_ids": [int(segment.id)],
            "camera_ids": [int(segment.camera_id)],
            "root_ids": [str(segment.archive_root_id)],
        },
        expected_identities=expected,
        outer_operation_handle=handle,
        manage_outer_operation=False,
        write_terminal_audit=False,
        allowed_integrity_statuses={str(segment.integrity_status or ""), finding.category},
        allowed_segment_statuses={str(segment.status or "")},
    )
    if result.get("status") not in {"completed", "partial"}:
        raise IntegrityRemediationBlocked("archive_integrity_recording_delete_blocked", retry_mode="new_scan")
    outcome = {
        "status": str(result.get("status")),
        "deleted_count": int(result.get("deleted_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "bytes_freed": int(result.get("bytes_freed") or 0),
    }
    _persist_physical_outcome(db, plan=plan, item=item, result=outcome)
    return outcome


def _open_parent_handles(root_path: Path, relative_ref: str):
    parts = PurePosixPath(relative_ref).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise IntegrityRemediationBlocked("archive_integrity_path_invalid", retry_mode=None)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root_path, flags)
    opened = [root_fd]
    try:
        parent_fd = root_fd
        for component in parts[:-1]:
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            opened.append(child_fd)
            parent_fd = child_fd
        return opened, parent_fd, parts[-1]
    except Exception:
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise


def _open_verified_parent_handles(
    root: ArchiveRoot,
    relative_ref: str,
    expected: dict[str, Any],
) -> tuple[list[int], int, str]:
    expected_snapshot = str(expected.get("root_snapshot_key") or "")
    expected_access = str(expected.get("root_access_identity") or "")
    if not expected_snapshot or expected_snapshot != _root_snapshot_key(root):
        raise IntegrityRemediationBlocked("archive_integrity_root_changed", retry_mode="new_scan")
    if not expected_access:
        raise IntegrityRemediationBlocked("archive_integrity_root_access_changed", retry_mode="immediate")

    relative_parts = PurePosixPath(relative_ref).parts
    namespace_parts = PurePosixPath(str(root.storage_namespace or KMVMS_RECORDINGS_NAMESPACE)).parts
    if (
        not namespace_parts
        or len(relative_parts) <= len(namespace_parts)
        or tuple(relative_parts[: len(namespace_parts)]) != tuple(namespace_parts)
    ):
        raise IntegrityRemediationBlocked("archive_integrity_path_invalid", retry_mode=None)

    opened: list[int] = []
    try:
        opened, parent_fd, filename = _open_parent_handles(archive_root_runtime_path(root), relative_ref)
        namespace_fd_index = len(namespace_parts)
        if len(opened) <= namespace_fd_index:
            raise IntegrityRemediationBlocked("archive_integrity_root_access_changed", retry_mode="immediate")
        root_stat = os.fstat(opened[0])
        namespace_stat = os.fstat(opened[namespace_fd_index])
        actual_access = _canonical_hash(
            {
                "root_key": _root_snapshot_key(root),
                "root_device": int(root_stat.st_dev),
                "root_inode": int(root_stat.st_ino),
                "namespace_device": int(namespace_stat.st_dev),
                "namespace_inode": int(namespace_stat.st_ino),
                "physical_identity": str(root.physical_identity or ""),
            }
        )
        if actual_access != expected_access:
            raise IntegrityRemediationBlocked("archive_integrity_root_access_changed", retry_mode="immediate")
        return opened, parent_fd, filename
    except IntegrityRemediationBlocked:
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise
    except OSError as exc:
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise IntegrityRemediationBlocked("archive_integrity_root_access_changed", retry_mode="immediate") from exc


def _fingerprint_fd(descriptor: int, stat_result: os.stat_result) -> str:
    digest = hashlib.sha256()
    digest.update(f"v1:{int(stat_result.st_size)}:{int(stat_result.st_mtime_ns)}".encode("ascii"))
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest.update(os.read(descriptor, 64 * 1024))
    if stat_result.st_size > 64 * 1024:
        os.lseek(descriptor, max(0, int(stat_result.st_size) - 64 * 1024), os.SEEK_SET)
        digest.update(os.read(descriptor, 64 * 1024))
    return digest.hexdigest()


def _orphan_quarantine_name(plan: ArchiveIntegrityRemediationPlan, item: ArchiveIntegrityRemediationItem) -> str:
    return f"orphan-{plan.id}-{item.id}"


def _orphan_quarantine_ref(name: str) -> str:
    return hashlib.sha256(name.encode("ascii")).hexdigest()[:32]


def _verify_quarantine_entry(
    db: Session,
    *,
    quarantine_fd: int,
    quarantine_name: str,
    item: ArchiveIntegrityRemediationItem,
    root: ArchiveRoot,
    relative_ref: str,
) -> None:
    descriptor = os.open(
        quarantine_name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=quarantine_fd,
    )
    try:
        stat_result = os.fstat(descriptor)
        expected = dict(item.evidence or {})
        if (
            not stat_module.S_ISREG(stat_result.st_mode)
            or not _stat_facts_match(expected, stat_result)
            or _fingerprint_fd(descriptor, stat_result) != str(expected.get("fingerprint") or "")
        ):
            raise IntegrityRemediationBlocked("archive_integrity_quarantine_identity_mismatch", retry_mode="support")
        receipt = db.get(RecorderFileReceipt, str(expected.get("receipt_id") or ""))
        if receipt is None or not _receipt_matches(
            receipt,
            root=root,
            relative_ref=relative_ref,
            stat_result=stat_result,
            fingerprint=str(expected.get("fingerprint") or ""),
        ):
            raise IntegrityRemediationBlocked("archive_integrity_orphan_receipt_mismatch", retry_mode="support")
    finally:
        os.close(descriptor)


def _apply_orphan(db: Session, plan, item, finding, actor, handle) -> dict[str, Any]:
    _segment, root, relative_ref, current = _revalidate_orphan(db, finding)
    if not _evidence_matches(item, current):
        raise IntegrityRemediationBlocked("archive_integrity_plan_stale")
    expected = dict(item.evidence or {})
    scope = {"type": "root", "segment_ids": [], "camera_ids": [], "root_ids": [str(root.id)]}
    quarantine_name = _orphan_quarantine_name(plan, item)
    quarantine_ref = _orphan_quarantine_ref(quarantine_name)
    quarantined = False
    restored = False
    opened: list[int] = []
    object_fd: int | None = None
    quarantine_fd: int | None = None
    try:
        with destructive_scope_guard(str(plan.apply_operation_id), scope, purpose="orphan_file_cleanup") as scope_lease:
            with scope_lease.mutation_guard():
                assert_operation_owned(db, handle)
                if (
                    db.query(RecordingSegment.id)
                    .filter(
                        RecordingSegment.archive_root_id == str(root.id),
                        RecordingSegment.relative_path == relative_ref,
                        RecordingSegment.deleted_at.is_(None),
                        RecordingSegment.status != "deleted",
                    )
                    .first()
                    is not None
                ):
                    raise IntegrityRemediationBlocked("archive_integrity_object_now_owned")
                opened, parent_fd, filename = _open_verified_parent_handles(root, relative_ref, expected)
                object_fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                object_stat = os.fstat(object_fd)
                link_stat = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat_module.S_ISREG(object_stat.st_mode)
                    or (int(object_stat.st_dev), int(object_stat.st_ino)) != (int(link_stat.st_dev), int(link_stat.st_ino))
                    or not _stat_facts_match(expected, object_stat)
                    or _fingerprint_fd(object_fd, object_stat) != str(expected.get("fingerprint") or "")
                ):
                    raise IntegrityRemediationBlocked("archive_integrity_object_identity_changed")
                receipt = db.get(RecorderFileReceipt, str(expected.get("receipt_id") or ""))
                if receipt is None or not _receipt_matches(
                    receipt,
                    root=root,
                    relative_ref=relative_ref,
                    stat_result=object_stat,
                    fingerprint=str(expected.get("fingerprint") or ""),
                ):
                    raise IntegrityRemediationBlocked("archive_integrity_orphan_receipt_mismatch")
                assert_operation_owned(db, handle)

                root_fd = opened[0]
                try:
                    os.mkdir(".km-vms-internal-quarantine", mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                quarantine_fd = os.open(
                    ".km-vms-internal-quarantine",
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_fd,
                )
                item.quarantine_ref = quarantine_ref
                _persist_item_state(db, item, "quarantine_prepared")
                assert_operation_owned(db, handle)
                os.rename(filename, quarantine_name, src_dir_fd=parent_fd, dst_dir_fd=quarantine_fd)
                quarantined = True
                quarantine_stat = os.stat(quarantine_name, dir_fd=quarantine_fd, follow_symlinks=False)
                if (int(quarantine_stat.st_dev), int(quarantine_stat.st_ino)) != (
                    int(object_stat.st_dev),
                    int(object_stat.st_ino),
                ):
                    try:
                        os.rename(quarantine_name, filename, src_dir_fd=quarantine_fd, dst_dir_fd=parent_fd)
                        restored = True
                        quarantined = False
                    except OSError:
                        pass
                    if not restored:
                        _persist_item_state(db, item, "quarantined", result_code="archive_integrity_quarantine_preserved")
                        raise IntegrityRemediationBlocked("archive_integrity_quarantine_preserved", retry_mode="support")
                    raise IntegrityRemediationBlocked("archive_integrity_quarantine_identity_mismatch", retry_mode=None)
                _persist_item_state(db, item, "quarantined")
                assert_operation_owned(db, handle)
                _persist_item_state(db, item, "delete_committing")
                assert_operation_owned(db, handle)
                os.unlink(quarantine_name, dir_fd=quarantine_fd)
                quarantined = False
                outcome = _physical_result(
                    deleted_count=1,
                    bytes_freed=int(current.get("size_bytes") or 0),
                )
                _persist_physical_outcome(db, plan=plan, item=item, result=outcome)
                try:
                    os.rmdir(".km-vms-internal-quarantine", dir_fd=root_fd)
                except OSError:
                    pass
    except (NotImplementedError, TypeError, AttributeError) as exc:
        raise IntegrityRemediationBlocked("orphan_identity_bound_delete_unsupported", retry_mode=None) from exc
    except DestructiveScopeConflict as exc:
        raise IntegrityRemediationBlocked(
            str(exc.detail.get("reason") or "archive_integrity_destructive_scope_conflict"),
            retry_mode="immediate",
        ) from exc
    except OSError as exc:
        if quarantined and not restored:
            item.quarantine_ref = quarantine_ref
            pending_state = "delete_committing" if item.state == "delete_committing" else "quarantined"
            _persist_item_state(db, item, pending_state, result_code="archive_integrity_quarantine_preserved")
            raise IntegrityRemediationBlocked("archive_integrity_quarantine_preserved", retry_mode="support") from exc
        raise IntegrityRemediationBlocked("archive_integrity_orphan_delete_failed", retry_mode="new_scan") from exc
    finally:
        if object_fd is not None:
            os.close(object_fd)
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        for descriptor in reversed(opened):
            os.close(descriptor)
    return dict(plan.result_summary or _physical_result(deleted_count=1, bytes_freed=int(current.get("size_bytes") or 0)))


def _recover_orphan_outcome(
    db: Session,
    plan: ArchiveIntegrityRemediationPlan,
    item: ArchiveIntegrityRemediationItem,
    finding: ArchiveIntegrityFinding,
    handle,
) -> dict[str, Any] | None:
    if item.state == "physical_mutation_committed":
        return dict(plan.result_summary or _physical_result(deleted_count=1, bytes_freed=int(item.evidence.get("size_bytes") or 0)))
    if item.state not in {"quarantine_prepared", "quarantined", "delete_committing"}:
        return None
    root = _root_for_finding(db, finding)
    relative_ref = _relative_for_finding(finding)
    expected = dict(item.evidence or {})
    quarantine_name = _orphan_quarantine_name(plan, item)
    quarantine_ref = _orphan_quarantine_ref(quarantine_name)
    if item.quarantine_ref not in {None, quarantine_ref}:
        raise IntegrityRemediationBlocked("archive_integrity_quarantine_identity_mismatch", retry_mode="support")
    scope = {"type": "root", "segment_ids": [], "camera_ids": [], "root_ids": [str(root.id)]}
    opened: list[int] = []
    quarantine_fd: int | None = None
    try:
        with destructive_scope_guard(str(plan.apply_operation_id), scope, purpose="orphan_file_cleanup") as scope_lease:
            with scope_lease.mutation_guard():
                assert_operation_owned(db, handle)
                opened, parent_fd, filename = _open_verified_parent_handles(root, relative_ref, expected)
                root_fd = opened[0]
                try:
                    original_stat = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    original_stat = None
                try:
                    quarantine_fd = os.open(
                        ".km-vms-internal-quarantine",
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=root_fd,
                    )
                except FileNotFoundError:
                    quarantine_fd = None
                try:
                    quarantine_stat = (
                        os.stat(quarantine_name, dir_fd=quarantine_fd, follow_symlinks=False)
                        if quarantine_fd is not None
                        else None
                    )
                except FileNotFoundError:
                    quarantine_stat = None

                if item.state == "quarantine_prepared" and original_stat is not None and quarantine_stat is None:
                    return None
                if original_stat is not None and quarantine_stat is not None:
                    raise IntegrityRemediationBlocked("archive_integrity_quarantine_state_ambiguous", retry_mode="support")
                if quarantine_stat is not None and quarantine_fd is not None:
                    _verify_quarantine_entry(
                        db,
                        quarantine_fd=quarantine_fd,
                        quarantine_name=quarantine_name,
                        item=item,
                        root=root,
                        relative_ref=relative_ref,
                    )
                    if original_stat is not None:
                        raise IntegrityRemediationBlocked("archive_integrity_quarantine_state_ambiguous", retry_mode="support")
                    item.quarantine_ref = quarantine_ref
                    if item.state != "delete_committing":
                        _persist_item_state(db, item, "quarantined")
                    assert_operation_owned(db, handle)
                    _persist_item_state(db, item, "delete_committing")
                    assert_operation_owned(db, handle)
                    os.unlink(quarantine_name, dir_fd=quarantine_fd)
                    outcome = _physical_result(
                        deleted_count=1,
                        bytes_freed=int(item.evidence.get("size_bytes") or 0),
                    )
                    _persist_physical_outcome(db, plan=plan, item=item, result=outcome)
                    return outcome
                if original_stat is None and quarantine_stat is None and item.state == "delete_committing":
                    outcome = _physical_result(
                        deleted_count=1,
                        bytes_freed=int(item.evidence.get("size_bytes") or 0),
                    )
                    _persist_physical_outcome(db, plan=plan, item=item, result=outcome)
                    return outcome
                raise IntegrityRemediationBlocked("archive_integrity_quarantine_state_ambiguous", retry_mode="support")
    except DestructiveScopeConflict as exc:
        raise IntegrityRemediationBlocked(
            str(exc.detail.get("reason") or "archive_integrity_destructive_scope_conflict"),
            retry_mode="immediate",
        ) from exc
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        for descriptor in reversed(opened):
            os.close(descriptor)


def _finish_plan(
    db: Session,
    *,
    plan: ArchiveIntegrityRemediationPlan,
    item: ArchiveIntegrityRemediationItem,
    handle=None,
    status: str,
    result: dict[str, Any],
    reason_code: str | None = None,
    retry_mode: str | None = None,
    actor: Any = None,
) -> dict[str, Any]:
    now = database_now(db)
    item.state = "completed" if status == "completed" else status
    item.result_code = reason_code
    item.updated_at = now
    plan.state = status
    plan.result_summary = dict(result)
    plan.reason_code = reason_code
    plan.retry_mode = retry_mode
    plan.next_action = "create_new_integrity_scan" if retry_mode == "new_scan" else "retry_remediation" if retry_mode else None
    plan.finished_at = now
    plan.updated_at = now
    db.add_all((plan, item))
    resolves_finding = status == "completed" or (
        status == "partial"
        and int(result.get("deleted_count") or 0) > 0
        and plan.action_kind in {"delete_unusable_recording", "delete_proven_orphan"}
    )
    if resolves_finding:
        finding = db.get(ArchiveIntegrityFinding, plan.finding_id)
        scan = db.get(ArchiveIntegrityScan, plan.scan_id)
        if finding is not None and finding.is_active:
            finding.is_active = False
            finding.state = "resolved"
            finding.resolved_at = now
            finding.updated_at = now
            db.add(finding)
        if scan is not None:
            db.flush()
            _refresh_scan_summary(db, scan)
            scan.updated_at = now
            db.add(scan)
    progress = _plan_terminal_progress(status)
    terminal_result = {"status": status, "plan_id": plan.id, **dict(result)}
    if handle is None:
        operation = db.get(StorageOperation, str(plan.apply_operation_id or ""))
        if operation is None or not _outer_terminal_matches(operation, status=status, plan=plan, result=result):
            raise StorageOperationContractError("archive_integrity_terminal_outcome_mismatch")
    else:
        stage_operation_terminal(
            db,
            handle,
            status=status,
            result=terminal_result,
            progress=progress,
            reason_code=reason_code,
            next_action=plan.next_action,
            retry_mode=retry_mode,
            retry_allowed=bool(retry_mode),
        )
    db.commit()
    operation = db.get(StorageOperation, str(plan.apply_operation_id))
    ensure_operation_terminal_audit(db, operation)
    _ensure_remediation_terminal_audit(db, plan, actor=actor)
    return _plan_public(plan)


def _plan_terminal_progress(status: str) -> dict[str, int]:
    return {
        "planned_count": 1,
        "completed_count": 1 if status == "completed" else 0,
        "failed_count": 1 if status in {"failed", "partial"} else 0,
        "skipped_count": 1 if status == "blocked" else 0,
    }


def _outer_terminal_matches(
    operation: StorageOperation,
    *,
    status: str,
    plan: ArchiveIntegrityRemediationPlan,
    result: dict[str, Any],
) -> bool:
    if str(operation.status) != str(status):
        return False
    actual = dict(operation.result or {})
    expected = {"status": str(status), "plan_id": str(plan.id), **dict(result)}
    return all(actual.get(key) == value for key, value in expected.items())


def _saved_terminal_outcome(
    plan: ArchiveIntegrityRemediationPlan,
) -> tuple[str, dict[str, Any], str | None, str | None]:
    result = dict(plan.result_summary or {})
    status = str(result.get("status") or "")
    if status not in {"completed", "partial"}:
        raise StorageOperationContractError("archive_integrity_terminal_outcome_missing")
    return status, result, plan.reason_code, plan.retry_mode


def _ensure_remediation_terminal_audit(
    db: Session,
    plan: ArchiveIntegrityRemediationPlan,
    *,
    actor: Any,
) -> None:
    if plan.state not in TERMINAL_PLAN_STATES:
        return
    event_type = f"archive_integrity.remediation_{plan.state}"
    exists = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_type == "archive_integrity_plan",
            AuditEvent.target_id == str(plan.id),
        )
        .first()
    )
    if exists is not None:
        return
    create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type=event_type,
        severity="info" if plan.state == "completed" else "warning",
        message_ru="Archive integrity remediation finished",
        message_en="Archive integrity remediation finished",
        target_type="archive_integrity_plan",
        target_id=plan.id,
        metadata={
            "action_key": plan.action_kind,
            "status": plan.state,
            "reason_code": plan.reason_code,
        },
    )


def _apply_operation_identity(plan: ArchiveIntegrityRemediationPlan) -> tuple[dict[str, str], str]:
    return remediation_apply_operation_identity(plan)


def _durable_apply_context(
    db: Session,
    plan: ArchiveIntegrityRemediationPlan,
) -> tuple[
    ArchiveIntegrityRemediationItem,
    ArchiveIntegrityFinding,
    dict[str, Any],
    dict[str, Any],
]:
    """Reconstruct immutable apply identity without consulting the filesystem."""
    item = _plan_item(db, plan)
    finding = db.get(ArchiveIntegrityFinding, str(plan.finding_id))
    action = MUTATING_ACTIONS.get(str(plan.action_kind))
    if finding is None or action is None:
        raise StorageOperationContractError("archive_integrity_apply_identity_missing")
    if not (
        str(plan.scan_id) == str(finding.scan_id)
        and str(item.finding_id) == str(plan.finding_id)
        and str(finding.action_key) == str(plan.action_kind)
        and str(finding.required_permission) == str(plan.required_permission)
        and str(action["permission"]) == str(plan.required_permission)
        and str(item.intended_mutation) == str(action["mutation"])
        and str(finding.category) in action["categories"]
    ):
        raise StorageOperationContractError("archive_integrity_apply_identity_mismatch")

    root_ids = {str(value) for value in (item.root_id, finding.root_id) if value is not None}
    segment_ids = {int(value) for value in (item.segment_id, finding.segment_id) if value is not None}
    if len(root_ids) != 1 or len(segment_ids) > 1:
        raise StorageOperationContractError("archive_integrity_apply_scope_identity_mismatch")
    root_id = next(iter(root_ids))
    scope = {
        "global": False,
        "physical_volume_ids": [str(finding.physical_identity)] if finding.physical_identity else [],
        "root_ids": [root_id],
        "camera_ids": [int(finding.camera_id)] if finding.camera_id is not None else [],
        "segment_ids": sorted(segment_ids),
    }
    return item, finding, action, scope


def _audit_count(db: Session, *, event_type: str, target_type: str, target_id: str) -> int:
    return int(
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == str(event_type),
            AuditEvent.target_type == str(target_type),
            AuditEvent.target_id == str(target_id),
        )
        .count()
    )


def _assert_exact_pre_mutation_evidence(
    db: Session,
    *,
    plan: ArchiveIntegrityRemediationPlan,
    candidate: StorageOperation,
) -> ArchiveIntegrityRemediationItem:
    items = (
        db.query(ArchiveIntegrityRemediationItem)
        .filter(ArchiveIntegrityRemediationItem.plan_id == str(plan.id))
        .order_by(ArchiveIntegrityRemediationItem.item_index.asc(), ArchiveIntegrityRemediationItem.id.asc())
        .with_for_update()
        .all()
    )
    if not (
        plan.apply_operation_id is None
        and str(plan.state) == "prepared"
        and plan.started_at is None
        and plan.finished_at is None
        and plan.result_summary is None
        and int(plan.item_count or 0) == 1
        and len(items) == 1
    ):
        raise StorageOperationContractError("archive_integrity_pre_mutation_plan_evidence_invalid")
    item = _plan_item(db, plan)
    if not (
        str(item.state) == "prepared"
        and item.result_code is None
        and item.quarantine_ref is None
    ):
        raise StorageOperationContractError("archive_integrity_pre_mutation_item_evidence_invalid")
    if not (
        str(candidate.status) not in TERMINAL_OPERATION_STATUSES
        and candidate.finished_at is None
        and candidate.result in (None, {})
        and _audit_count(
            db,
            event_type="storage_operation.finished",
            target_type="storage_operation",
            target_id=str(candidate.id),
        )
        == 0
    ):
        raise StorageOperationContractError("archive_integrity_pre_mutation_operation_evidence_invalid")

    prepare = db.get(StorageOperation, str(plan.operation_id or ""))
    prepare_result = dict(prepare.result or {}) if prepare is not None else {}
    if not (
        prepare is not None
        and str(prepare.operation_type) == "integrity_plan_prepare"
        and str(prepare.actor_key) == str(plan.actor_key)
        and str(prepare.idempotency_key) == str(plan.idempotency_key)
        and str(prepare.request_fingerprint) == str(plan.request_fingerprint)
        and str(prepare.status) == "completed"
        and prepare.finished_at is not None
        and str(prepare_result.get("status")) == "completed"
        and str(prepare_result.get("plan_id")) == str(plan.id)
        and int(prepare_result.get("item_count") or 0) == 1
        and _audit_count(
            db,
            event_type="storage_operation.finished",
            target_type="storage_operation",
            target_id=str(prepare.id),
        )
        == 1
        and _audit_count(
            db,
            event_type="archive_integrity.remediation_plan_created",
            target_type="archive_integrity_plan",
            target_id=str(plan.id),
        )
        == 1
    ):
        raise StorageOperationContractError("archive_integrity_pre_mutation_prepare_evidence_invalid")
    terminal_audit_count = sum(
        _audit_count(
            db,
            event_type=f"archive_integrity.remediation_{status}",
            target_type="archive_integrity_plan",
            target_id=str(plan.id),
        )
        for status in TERMINAL_PLAN_STATES
    )
    if terminal_audit_count:
        raise StorageOperationContractError("archive_integrity_pre_mutation_terminal_audit_present")
    return item


def _context_disposition(exc: IntegrityRemediationBlocked) -> str:
    if exc.reason_code in PRE_MUTATION_PERMANENT_CONTEXT_REASONS:
        return "permanent"
    if exc.reason_code in PRE_MUTATION_TRANSIENT_CONTEXT_REASONS:
        return "transient"
    return "fail_closed"


def _apply_context(
    db: Session,
    plan: ArchiveIntegrityRemediationPlan,
) -> tuple[
    ArchiveIntegrityRemediationItem,
    ArchiveIntegrityFinding,
    dict[str, Any],
    dict[str, Any],
]:
    item = _plan_item(db, plan)
    finding, scan, action = _finding_for_plan(db, plan.finding_id, plan.action_kind)
    if scan.id != plan.scan_id or action["permission"] != plan.required_permission:
        raise IntegrityRemediationBlocked("archive_integrity_plan_identity_mismatch", retry_mode=None)
    root = _root_for_finding(db, finding)
    if str(item.root_id or "") != str(root.id):
        raise IntegrityRemediationBlocked("archive_integrity_root_changed")
    segment = _segment_for_finding(db, finding) if item.segment_id is not None else None
    if segment is not None and int(item.segment_id) != int(segment.id):
        raise IntegrityRemediationBlocked("archive_integrity_segment_changed")
    scope = {
        "global": False,
        "physical_volume_ids": [str(root.physical_identity)] if root.physical_identity else [],
        "root_ids": [str(root.id)],
        "camera_ids": [int(segment.camera_id)] if segment is not None and segment.camera_id is not None else [],
        "segment_ids": [int(segment.id)] if segment is not None else [],
    }
    return item, finding, action, scope


def _claim_operation_id(claimed: dict[str, Any], *, fallback: str) -> str:
    operation = dict(claimed.get("operation") or {})
    return str(operation.get("operation_id") or fallback)


def _validate_apply_operation_candidate(
    plan: ArchiveIntegrityRemediationPlan,
    operation: StorageOperation,
    *,
    action: dict[str, Any],
    scope: dict[str, Any],
    allow_terminal: bool,
    allow_expired_unbound_global_scope: bool = False,
) -> None:
    request_identity, idempotency_key = _apply_operation_identity(plan)
    expected_fingerprint = request_fingerprint(request_identity)
    stored_scope = canonical_operation_scope(operation.scope)
    expected_scope = normalize_operation_scope(scope)
    conservative_global_scope = bool(
        allow_expired_unbound_global_scope
        and str(operation.domain_ref or "") == str(plan.id)
        and stored_scope.get("global") is True
        and not stored_scope.get("physical_volume_ids")
        and not stored_scope.get("root_ids")
        and not stored_scope.get("camera_ids")
        and not stored_scope.get("segment_ids")
    )
    if not (
        str(operation.actor_key) == str(plan.actor_key)
        and str(operation.operation_type) == str(action["operation_type"])
        and str(operation.idempotency_key) == str(idempotency_key)
        and str(operation.request_fingerprint) == str(expected_fingerprint)
        and operation.domain_ref in {None, str(plan.id)}
        and (stored_scope == expected_scope or conservative_global_scope)
    ):
        raise StorageOperationContractError("archive_integrity_unbound_apply_operation_mismatch")
    if not allow_terminal and str(operation.status) in TERMINAL_OPERATION_STATUSES:
        raise StorageOperationContractError("archive_integrity_unbound_apply_operation_terminal")


def _preserved_unbound_claim(claimed: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "preserved",
        "operation": dict(claimed.get("operation") or {}),
    }


def _converge_exact_unbound_pre_mutation(
    db: Session,
    *,
    plan_id: str,
    operation_id: str,
    actor: Any,
    disposition: str,
) -> dict[str, Any]:
    plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
    if plan is None:
        raise StorageOperationContractError("archive_integrity_plan_not_found")
    action = MUTATING_ACTIONS.get(str(plan.action_kind))
    if action is None:
        raise StorageOperationContractError("archive_integrity_apply_identity_missing")
    request_identity, idempotency_key = _apply_operation_identity(plan)
    claimed = reclaim_operation(
        db,
        operation_id=str(operation_id),
        operation_type=str(action["operation_type"]),
        request_identity=request_identity,
        idempotency_key=idempotency_key,
        owner_instance_id=operation_instance_id("integrity-remediation-pre-mutation-recovery"),
    )
    if claimed.get("state") != "claimed":
        return {
            "plan_id": str(plan.id),
            "claimed": _preserved_unbound_claim(claimed),
            "finding_id": str(plan.finding_id),
        }

    db.expire_all()
    plan = (
        db.query(ArchiveIntegrityRemediationPlan)
        .filter(ArchiveIntegrityRemediationPlan.id == str(plan_id))
        .populate_existing()
        .with_for_update()
        .first()
    )
    if plan is None:
        raise StorageOperationContractError("archive_integrity_plan_not_found")
    candidates = remediation_apply_operation_candidates(db, plan)
    if len(candidates) != 1 or str(candidates[0].id) != str(operation_id):
        raise StorageOperationContractError("archive_integrity_unbound_apply_operation_ambiguous")
    operation = (
        db.query(StorageOperation)
        .filter(StorageOperation.id == str(operation_id))
        .populate_existing()
        .with_for_update()
        .first()
    )
    if operation is None:
        raise StorageOperationContractError("archive_integrity_apply_operation_missing")
    item, finding, action, durable_scope = _durable_apply_context(db, plan)
    _validate_apply_operation_candidate(
        plan,
        operation,
        action=action,
        scope=durable_scope,
        allow_terminal=False,
        allow_expired_unbound_global_scope=(disposition == "expired"),
    )
    _assert_exact_pre_mutation_evidence(db, plan=plan, candidate=operation)

    if disposition == "expired":
        if plan.expires_at > database_now(db):
            return {
                "plan_id": str(plan.id),
                "claimed": _preserved_unbound_claim(claimed),
                "finding_id": str(finding.id),
            }
        binding = _bind_apply_operation(
            db,
            plan_id=str(plan.id),
            operation_id=str(operation.id),
            claimed=claimed,
            actor=actor,
            allow_expired_unbound_global_scope=True,
        )
        if binding.get("state") != "terminal":
            raise StorageOperationContractError("archive_integrity_expired_binding_not_terminal")
        terminal_response = dict(binding.get("terminal_response") or {})
        terminal_response["replayed"] = True
        return {"terminal_response": terminal_response}
    elif disposition == "permanent":
        try:
            live_item, live_finding, live_action, live_scope = _apply_context(db, plan)
        except IntegrityRemediationBlocked as exc:
            if _context_disposition(exc) == "transient":
                return {
                    "plan_id": str(plan.id),
                    "claimed": _preserved_unbound_claim(claimed),
                    "finding_id": str(finding.id),
                }
            if _context_disposition(exc) != "permanent":
                raise
        else:
            _validate_apply_operation_candidate(
                plan,
                operation,
                action=live_action,
                scope=live_scope,
                allow_terminal=False,
            )
            binding = _bind_apply_operation(
                db,
                plan_id=str(plan.id),
                operation_id=str(operation.id),
                claimed=claimed,
                actor=actor,
            )
            if binding.get("state") == "terminal":
                terminal_response = dict(binding.get("terminal_response") or {})
                terminal_response["replayed"] = True
                return {"terminal_response": terminal_response}
            if binding.get("state") != "bound":
                raise StorageOperationContractError("archive_integrity_apply_operation_binding_incomplete")
            return {
                "plan_id": str(plan.id),
                "claimed": claimed,
                "finding_id": str(live_finding.id),
            }
        reason_code = "archive_integrity_apply_context_stale_before_binding"
    else:
        raise StorageOperationContractError("archive_integrity_pre_mutation_disposition_invalid")

    now = database_now(db)
    result = {"status": "blocked", "mutated_count": 0}
    plan.apply_operation_id = str(operation.id)
    plan.state = "blocked"
    plan.result_summary = result
    plan.reason_code = reason_code
    plan.retry_mode = "new_scan"
    plan.next_action = "create_new_integrity_scan"
    plan.finished_at = now
    plan.updated_at = now
    item.state = "blocked"
    item.result_code = reason_code
    item.updated_at = now
    db.add_all((plan, item))
    stage_operation_terminal(
        db,
        claimed["handle"],
        status="blocked",
        result={"status": "blocked", "plan_id": str(plan.id), "mutated_count": 0},
        progress=_plan_terminal_progress("blocked"),
        reason_code=reason_code,
        next_action="create_new_integrity_scan",
        retry_mode="new_scan",
        retry_allowed=True,
    )
    db.commit()
    operation = db.get(StorageOperation, str(operation.id))
    ensure_operation_terminal_audit(db, operation)
    _ensure_remediation_terminal_audit(db, plan, actor=actor)
    return {"terminal_response": _plan_public(plan, replayed=True)}


def _bind_apply_operation(
    db: Session,
    *,
    plan_id: str,
    operation_id: str,
    claimed: dict[str, Any] | None,
    actor: Any,
    allow_expired_unbound_global_scope: bool = False,
) -> dict[str, Any]:
    plan = (
        db.query(ArchiveIntegrityRemediationPlan)
        .filter(ArchiveIntegrityRemediationPlan.id == str(plan_id))
        .populate_existing()
        .with_for_update()
        .first()
    )
    if plan is None:
        raise StorageOperationContractError("archive_integrity_plan_not_found")
    if plan.apply_operation_id is not None:
        raise StorageOperationContractError("archive_integrity_apply_operation_binding_conflict")
    operation = (
        db.query(StorageOperation)
        .filter(StorageOperation.id == str(operation_id))
        .populate_existing()
        .with_for_update()
        .first()
    )
    if operation is None:
        raise StorageOperationContractError("archive_integrity_apply_operation_missing")
    item, _finding, action, durable_scope = _durable_apply_context(db, plan)
    _validate_apply_operation_candidate(
        plan,
        operation,
        action=action,
        scope=durable_scope,
        allow_terminal=False,
        allow_expired_unbound_global_scope=allow_expired_unbound_global_scope,
    )
    item = _assert_exact_pre_mutation_evidence(db, plan=plan, candidate=operation)
    now = database_now(db)
    if plan.expires_at is None:
        raise StorageOperationContractError("archive_integrity_plan_expiry_missing")
    claimed_state = str((claimed or {}).get("state") or "")
    handle = (claimed or {}).get("handle") if claimed_state == "claimed" else None
    if handle is None:
        expired = plan.expires_at <= now
        db.rollback()
        return {
            "state": "expired_unowned" if expired else "unowned",
            "plan_id": str(plan.id),
            "operation_id": str(operation.id),
        }
    if str(getattr(handle, "operation_id", "")) != str(operation.id):
        raise StorageOperationContractError("archive_integrity_apply_operation_handle_mismatch")
    assert_operation_owned(db, handle)
    if plan.expires_at <= now:
        plan.apply_operation_id = str(operation.id)
        response = _finish_plan(
            db,
            plan=plan,
            item=item,
            handle=handle,
            status="blocked",
            result={"status": "blocked", "mutated_count": 0},
            reason_code="archive_integrity_apply_expired_before_binding",
            retry_mode="new_scan",
            actor=actor,
        )
        return {
            "state": "terminal",
            "plan_id": str(plan.id),
            "operation_id": str(operation.id),
            "terminal_response": response,
        }

    plan.apply_operation_id = str(operation_id)
    plan.state = "running"
    plan.started_at = plan.started_at or now
    plan.updated_at = now
    item.state = "running"
    item.updated_at = now
    db.add_all((plan, item))
    db.commit()
    return {"state": "bound", "plan": plan, "item": item}


def _reclaim_exact_unbound_apply_operation(
    db: Session,
    *,
    plan: ArchiveIntegrityRemediationPlan,
    action: dict[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    request_identity, idempotency_key = _apply_operation_identity(plan)
    return reclaim_operation_with_conflicts(
        db,
        operation_id=str(operation_id),
        operation_type=str(action["operation_type"]),
        request_identity=request_identity,
        idempotency_key=idempotency_key,
        owner_instance_id=operation_instance_id("integrity-remediation-unbound-recovery"),
    )


def _coordinated_apply_claim(
    db: Session,
    *,
    plan_id: str,
    actor: Any,
    requested_operation_id: str,
    expected_actor_key: str | None,
    allow_create: bool,
) -> dict[str, Any] | None:
    coordinator = acquire_remediation_plan_coordinator(db)
    try:
        coordinator.assert_owned()
        plan = (
            db.query(ArchiveIntegrityRemediationPlan)
            .filter(ArchiveIntegrityRemediationPlan.id == str(plan_id))
            .populate_existing()
            .with_for_update()
            .first()
        )
        if plan is None:
            raise StorageOperationContractError("archive_integrity_plan_not_found")
        if expected_actor_key is not None and str(plan.actor_key) != str(expected_actor_key):
            raise StorageOperationContractError("archive_integrity_plan_actor_mismatch")
        if plan.state in TERMINAL_PLAN_STATES:
            _converge_terminal_plan(db, plan, actor=actor)
            coordinator.assert_owned()
            return {"terminal_response": _plan_public(plan, replayed=True)}

        claimed: dict[str, Any] | None = None
        existing_candidate: StorageOperation | None = None
        existing_context_loaded = False
        if plan.apply_operation_id is None:
            candidates = remediation_apply_operation_candidates(db, plan)
            if len(candidates) > 1:
                raise StorageOperationContractError("archive_integrity_unbound_apply_operation_ambiguous")
            if candidates:
                existing_candidate = candidates[0]
            elif not allow_create:
                return None
            elif plan.expires_at <= database_now(db):
                plan.state = "blocked"
                plan.reason_code = "archive_integrity_plan_expired"
                plan.retry_mode = "new_scan"
                plan.next_action = None
                plan.updated_at = database_now(db)
                db.add(plan)
                db.commit()
                raise IntegrityRemediationBlocked("archive_integrity_plan_expired")

        if existing_candidate is not None:
            item, finding, action, durable_scope = _durable_apply_context(db, plan)
            candidate_expired = bool(
                plan.expires_at is not None
                and plan.expires_at <= database_now(db)
            )
            _validate_apply_operation_candidate(
                plan,
                existing_candidate,
                action=action,
                scope=durable_scope,
                allow_terminal=False,
                allow_expired_unbound_global_scope=candidate_expired,
            )
            _assert_exact_pre_mutation_evidence(db, plan=plan, candidate=existing_candidate)
            if candidate_expired:
                result = _converge_exact_unbound_pre_mutation(
                    db,
                    plan_id=str(plan.id),
                    operation_id=str(existing_candidate.id),
                    actor=actor,
                    disposition="expired",
                )
                coordinator.assert_owned()
                return result
            try:
                item, finding, action, scope = _apply_context(db, plan)
            except IntegrityRemediationBlocked as exc:
                disposition = _context_disposition(exc)
                if disposition == "permanent":
                    result = _converge_exact_unbound_pre_mutation(
                        db,
                        plan_id=str(plan.id),
                        operation_id=str(existing_candidate.id),
                        actor=actor,
                        disposition=disposition,
                    )
                    coordinator.assert_owned()
                    return result
                raise
            _validate_apply_operation_candidate(
                plan,
                existing_candidate,
                action=action,
                scope=scope,
                allow_terminal=False,
            )
            existing_context_loaded = True

        if plan.state == TERMINAL_PENDING_PLAN_STATE and plan.apply_operation_id is not None:
            item = _plan_item(db, plan)
            finding = db.get(ArchiveIntegrityFinding, str(plan.finding_id))
            action = MUTATING_ACTIONS.get(str(plan.action_kind))
            bound_operation = db.get(StorageOperation, str(plan.apply_operation_id))
            if finding is None or action is None or bound_operation is None:
                raise StorageOperationContractError("archive_integrity_terminal_pending_identity_missing")
            scope = dict(bound_operation.scope or {})
        elif plan.apply_operation_id is not None:
            durable_item, durable_finding, durable_action, durable_scope = _durable_apply_context(db, plan)
            if durable_item.state in PHYSICAL_PENDING_ITEM_STATES:
                item, finding, action, scope = (
                    durable_item,
                    durable_finding,
                    durable_action,
                    durable_scope,
                )
            elif not existing_context_loaded:
                item, finding, action, scope = _apply_context(db, plan)
        elif not existing_context_loaded:
            item, finding, action, scope = _apply_context(db, plan)

        if plan.apply_operation_id is not None:
            bound_operation = db.get(StorageOperation, str(plan.apply_operation_id))
            if bound_operation is None:
                raise StorageOperationContractError("archive_integrity_apply_operation_missing")
            _validate_apply_operation_candidate(
                plan,
                bound_operation,
                action=action,
                scope=scope,
                allow_terminal=True,
            )
            claimed = _claim_apply_operation(
                db,
                plan=plan,
                action=action,
                scope=scope,
                actor=actor,
                requested_operation_id=str(plan.apply_operation_id),
            )
        elif existing_candidate is not None:
            claimed = _reclaim_exact_unbound_apply_operation(
                db,
                plan=plan,
                action=action,
                operation_id=str(existing_candidate.id),
            )
            coordinator.assert_owned()
            binding = _bind_apply_operation(
                db,
                plan_id=str(plan.id),
                operation_id=str(existing_candidate.id),
                claimed=claimed,
                actor=actor,
            )
            coordinator.assert_owned()
            if binding.get("state") == "terminal":
                return {"terminal_response": dict(binding.get("terminal_response") or {})}
            if binding.get("state") in {"expired_unowned", "unowned"}:
                return {
                    "plan_id": str(plan.id),
                    "claimed": _preserved_unbound_claim(claimed),
                    "finding_id": str(finding.id),
                }
            if binding.get("state") != "bound":
                raise StorageOperationContractError("archive_integrity_apply_operation_binding_incomplete")
            plan = binding["plan"]
            item = binding["item"]
        else:
            claimed = _claim_apply_operation(
                db,
                plan=plan,
                action=action,
                scope=scope,
                actor=actor,
                requested_operation_id=str(requested_operation_id),
            )
            coordinator.assert_owned()
            operation_id = _claim_operation_id(claimed, fallback=str(requested_operation_id))
            operation = db.get(StorageOperation, operation_id)
            if operation is None:
                raise StorageOperationContractError("archive_integrity_apply_operation_missing")
            _validate_apply_operation_candidate(
                plan,
                operation,
                action=action,
                scope=scope,
                allow_terminal=False,
            )
            binding = _bind_apply_operation(
                db,
                plan_id=str(plan.id),
                operation_id=operation_id,
                claimed=claimed,
                actor=actor,
            )
            coordinator.assert_owned()
            if binding.get("state") == "terminal":
                return {"terminal_response": dict(binding.get("terminal_response") or {})}
            if binding.get("state") in {"expired_unowned", "unowned"}:
                return {
                    "plan_id": str(plan.id),
                    "claimed": _preserved_unbound_claim(claimed),
                    "finding_id": str(finding.id),
                }
            if binding.get("state") != "bound":
                raise StorageOperationContractError("archive_integrity_apply_operation_binding_incomplete")
            plan = binding["plan"]
            item = binding["item"]
            if claimed.get("state") != "claimed":
                claimed = _claim_apply_operation(
                    db,
                    plan=plan,
                    action=action,
                    scope=scope,
                    actor=actor,
                    requested_operation_id=operation_id,
                )

        coordinator.assert_owned()
        return {
            "plan_id": str(plan.id),
            "claimed": claimed,
            "finding_id": str(finding.id),
        }
    finally:
        coordinator.close()


def _converge_terminal_plan(
    db: Session,
    plan: ArchiveIntegrityRemediationPlan,
    *,
    actor: Any,
) -> bool:
    if plan.state not in TERMINAL_PLAN_STATES or not plan.apply_operation_id:
        return False
    operation = db.get(StorageOperation, str(plan.apply_operation_id))
    if operation is None:
        return False
    if operation.status in TERMINAL_PLAN_STATES:
        if not _outer_terminal_matches(
            operation,
            status=str(plan.state),
            plan=plan,
            result=dict(plan.result_summary or {}),
        ):
            raise StorageOperationContractError("archive_integrity_terminal_outcome_mismatch")
        ensure_operation_terminal_audit(db, operation)
        _ensure_remediation_terminal_audit(db, plan, actor=actor)
        return True
    action = MUTATING_ACTIONS.get(str(plan.action_kind))
    if action is None:
        return False
    request_identity, idempotency_key = _apply_operation_identity(plan)
    claimed = reclaim_operation(
        db,
        operation_id=str(operation.id),
        operation_type=str(action["operation_type"]),
        request_identity=request_identity,
        idempotency_key=idempotency_key,
        owner_instance_id=operation_instance_id("integrity-remediation-terminal-recovery"),
    )
    if claimed.get("state") == "terminal":
        operation = db.get(StorageOperation, str(plan.apply_operation_id))
        if operation is None or not _outer_terminal_matches(
            operation,
            status=str(plan.state),
            plan=plan,
            result=dict(plan.result_summary or {}),
        ):
            raise StorageOperationContractError("archive_integrity_terminal_outcome_mismatch")
        ensure_operation_terminal_audit(db, operation)
        _ensure_remediation_terminal_audit(db, plan, actor=actor)
        return True
    if claimed.get("state") != "claimed":
        return False
    result = dict(plan.result_summary or {})
    stage_operation_terminal(
        db,
        claimed["handle"],
        status=plan.state,
        result={"status": plan.state, "plan_id": plan.id, **result},
        progress=_plan_terminal_progress(plan.state),
        reason_code=plan.reason_code,
        next_action=plan.next_action,
        retry_mode=plan.retry_mode,
        retry_allowed=bool(plan.retry_mode),
    )
    db.commit()
    operation = db.get(StorageOperation, str(plan.apply_operation_id))
    ensure_operation_terminal_audit(db, operation)
    _ensure_remediation_terminal_audit(db, plan, actor=actor)
    return True


def _recover_physical_outcome(
    db: Session,
    *,
    plan: ArchiveIntegrityRemediationPlan,
    item: ArchiveIntegrityRemediationItem,
    finding: ArchiveIntegrityFinding,
    actor: Any,
    handle,
) -> dict[str, Any] | None:
    if plan.action_kind == "delete_unusable_recording":
        return _recover_unusable_outcome(db, plan, item, finding, actor)
    if plan.action_kind == "delete_proven_orphan":
        return _recover_orphan_outcome(db, plan, item, finding, handle)
    return None


def _claim_apply_operation(
    db: Session,
    *,
    plan: ArchiveIntegrityRemediationPlan,
    action: dict[str, Any],
    scope: dict[str, Any],
    actor: Any,
    requested_operation_id: str,
) -> dict[str, Any]:
    request_identity, idempotency_key = _apply_operation_identity(plan)
    if plan.apply_operation_id:
        if plan.state == TERMINAL_PENDING_PLAN_STATE or plan.state in TERMINAL_PLAN_STATES:
            return reclaim_operation(
                db,
                operation_id=str(plan.apply_operation_id),
                operation_type=str(action["operation_type"]),
                request_identity=request_identity,
                idempotency_key=idempotency_key,
                owner_instance_id=operation_instance_id("integrity-remediation-terminal-recovery"),
            )
        return reclaim_operation_with_conflicts(
            db,
            operation_id=str(plan.apply_operation_id),
            operation_type=str(action["operation_type"]),
            request_identity=request_identity,
            idempotency_key=idempotency_key,
            owner_instance_id=operation_instance_id("integrity-remediation-recovery"),
        )
    return claim_operation_with_conflicts(
        db,
        operation_type=str(action["operation_type"]),
        scope=scope,
        request_identity=request_identity,
        actor=actor,
        operation_id=str(requested_operation_id),
        idempotency_key=idempotency_key,
        owner_instance_id=operation_instance_id("integrity-remediation"),
        domain_ref=str(plan.id),
    )


def _finalize_saved_outcome(
    db: Session,
    *,
    plan: ArchiveIntegrityRemediationPlan,
    item: ArchiveIntegrityRemediationItem,
    handle,
    actor: Any,
) -> dict[str, Any]:
    status, result, reason_code, retry_mode = _saved_terminal_outcome(plan)
    return _finish_plan(
        db,
        plan=plan,
        item=item,
        handle=handle,
        status=status,
        result=result,
        reason_code=reason_code,
        retry_mode=retry_mode,
        actor=actor,
    )


def _preserve_pending_recovery(
    db: Session,
    *,
    plan: ArchiveIntegrityRemediationPlan,
    reason_code: str,
    retry_mode: str | None,
) -> None:
    db.query(ArchiveIntegrityRemediationPlan).filter(
        ArchiveIntegrityRemediationPlan.id == str(plan.id),
        ArchiveIntegrityRemediationPlan.state == "running",
    ).update(
        {
            ArchiveIntegrityRemediationPlan.reason_code: str(reason_code)[:96],
            ArchiveIntegrityRemediationPlan.retry_mode: retry_mode,
            ArchiveIntegrityRemediationPlan.next_action: "retry_remediation" if retry_mode else None,
            ArchiveIntegrityRemediationPlan.updated_at: database_now(db),
        },
        synchronize_session=False,
    )
    db.commit()
    db.expire(plan)


def apply_remediation_plan(
    db: Session,
    *,
    plan_id: str,
    actor: Any,
    confirm: bool,
    operation_id: str,
) -> dict[str, Any]:
    plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
    if plan is None:
        raise StorageOperationContractError("archive_integrity_plan_not_found")
    _kind, actor_key, _user_id, _owner = actor_identity(actor)
    if plan.actor_key != actor_key:
        raise StorageOperationContractError("archive_integrity_plan_actor_mismatch")
    if plan.state in TERMINAL_PLAN_STATES:
        try:
            terminal = _coordinated_apply_claim(
                db,
                plan_id=str(plan_id),
                actor=actor,
                requested_operation_id=str(operation_id),
                expected_actor_key=str(actor_key),
                allow_create=False,
            )
        except StorageOperationConflict as exc:
            raise IntegrityRemediationBlocked(
                str(exc.detail.get("reason_code") or "archive_integrity_operation_conflict"),
                retry_mode="immediate",
            ) from exc
        if terminal and terminal.get("terminal_response") is not None:
            return dict(terminal["terminal_response"])
        raise StorageOperationContractError("archive_integrity_terminal_replay_incomplete")
    if not confirm:
        raise IntegrityRemediationBlocked("archive_integrity_confirmation_required", retry_mode=None)
    if not user_has_permission(str(getattr(actor, "role", "")), plan.required_permission):
        raise IntegrityRemediationBlocked("archive_integrity_permission_denied", retry_mode=None)
    try:
        coordinated = _coordinated_apply_claim(
            db,
            plan_id=str(plan_id),
            actor=actor,
            requested_operation_id=str(operation_id),
            expected_actor_key=str(actor_key),
            allow_create=True,
        )
    except StorageOperationConflict as exc:
        raise IntegrityRemediationBlocked(
            str(exc.detail.get("reason_code") or "archive_integrity_operation_conflict"),
            retry_mode="immediate",
        ) from exc
    if coordinated is None:
        raise IntegrityRemediationBlocked("archive_integrity_operation_busy", retry_mode="refresh")
    terminal_response = coordinated.get("terminal_response")
    if terminal_response is not None:
        return dict(terminal_response)
    claimed = dict(coordinated.get("claimed") or {})
    plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
    if plan is None:
        raise StorageOperationContractError("archive_integrity_plan_not_found")
    item = _plan_item(db, plan)
    if claimed.get("state") == "terminal":
        plan = db.get(ArchiveIntegrityRemediationPlan, plan.id)
        item = _plan_item(db, plan) if plan is not None else None
        if plan and item and plan.state == TERMINAL_PENDING_PLAN_STATE:
            response = _finalize_saved_outcome(db, plan=plan, item=item, handle=None, actor=actor)
            response["replayed"] = True
            return response
        if plan and plan.state in TERMINAL_PLAN_STATES:
            coordinator = acquire_remediation_plan_coordinator(db)
            try:
                coordinator.assert_owned()
                plan = (
                    db.query(ArchiveIntegrityRemediationPlan)
                    .filter(ArchiveIntegrityRemediationPlan.id == str(plan.id))
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if plan is None:
                    raise StorageOperationContractError("archive_integrity_plan_not_found")
                _converge_terminal_plan(db, plan, actor=actor)
                coordinator.assert_owned()
                return _plan_public(plan, replayed=True)
            finally:
                coordinator.close()
        replay = terminal_replay_result(claimed)
        raise IntegrityRemediationBlocked(str(replay.get("reason_code") or "archive_integrity_terminal_replay_incomplete"))
    if claimed.get("state") != "claimed":
        detail = claim_state_detail(claimed)
        raise IntegrityRemediationBlocked(str(detail.get("reason_code") or "archive_integrity_operation_busy"), retry_mode="refresh")
    handle = claimed["handle"]
    finding = db.get(ArchiveIntegrityFinding, str(plan.finding_id))
    if finding is None:
        raise StorageOperationContractError("archive_integrity_finding_missing")

    try:
        if plan.state == TERMINAL_PENDING_PLAN_STATE:
            return _finalize_saved_outcome(db, plan=plan, item=item, handle=handle, actor=actor)

        finding, _scan, _action = _finding_for_plan(db, plan.finding_id, plan.action_kind)

        recovered = _recover_physical_outcome(
            db,
            plan=plan,
            item=item,
            finding=finding,
            actor=actor,
            handle=handle,
        )
        if recovered is not None:
            if plan.state != TERMINAL_PENDING_PLAN_STATE:
                _persist_physical_outcome(db, plan=plan, item=item, result=recovered)
            return _finalize_saved_outcome(db, plan=plan, item=item, handle=handle, actor=actor)

        if item.state not in PHYSICAL_PENDING_ITEM_STATES and operation_cancel_requested(db, handle):
            return _finish_plan(
                db,
                plan=plan,
                item=item,
                handle=handle,
                status="cancelled",
                result={"status": "cancelled", "mutated_count": 0},
                reason_code="archive_integrity_remediation_cancelled",
                actor=actor,
            )
        assert_operation_owned(db, handle)
        if plan.action_kind == "retire_missing_recording":
            result = _apply_missing(db, plan, item, finding, actor)
        elif plan.action_kind == "mark_stale_recording":
            result = _apply_stale(db, plan, item, finding, actor)
        elif plan.action_kind == "delete_unusable_recording":
            result = _apply_unusable(db, plan, item, finding, actor, handle)
        elif plan.action_kind == "delete_proven_orphan":
            result = _apply_orphan(db, plan, item, finding, actor, handle)
        else:
            raise IntegrityRemediationBlocked("archive_integrity_action_unsupported", retry_mode=None)
        terminal = "partial" if result.get("status") == "partial" else "completed"
        response = _finish_plan(
            db,
            plan=plan,
            item=item,
            handle=handle,
            status=terminal,
            result=result,
            actor=actor,
        )
    except IntegrityRemediationBlocked as exc:
        db.rollback()
        plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
        item = _plan_item(db, plan)
        if plan.state == TERMINAL_PENDING_PLAN_STATE:
            try:
                return _finalize_saved_outcome(db, plan=plan, item=item, handle=handle, actor=actor)
            except (StorageOperationLeaseLost, StorageOperationConflict):
                db.rollback()
                return _plan_public(db.get(ArchiveIntegrityRemediationPlan, str(plan_id)))
        if item.state in PHYSICAL_PENDING_ITEM_STATES:
            try:
                recovered = _recover_physical_outcome(
                    db,
                    plan=plan,
                    item=item,
                    finding=finding,
                    actor=actor,
                    handle=handle,
                )
                if recovered is not None:
                    if plan.state != TERMINAL_PENDING_PLAN_STATE:
                        _persist_physical_outcome(db, plan=plan, item=item, result=recovered)
                    return _finalize_saved_outcome(db, plan=plan, item=item, handle=handle, actor=actor)
            except (IntegrityRemediationBlocked, StorageOperationLeaseLost, StorageOperationConflict):
                db.rollback()
            plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
            _preserve_pending_recovery(
                db,
                plan=plan,
                reason_code=exc.reason_code,
                retry_mode=exc.retry_mode,
            )
            return _plan_public(plan)
        status = "partial" if str(exc.reason_code) == "archive_integrity_quarantine_preserved" else "blocked"
        response = _finish_plan(
            db,
            plan=plan,
            item=item,
            handle=handle,
            status=status,
            result={"status": status, "mutated_count": 0 if status == "blocked" else None},
            reason_code=exc.reason_code,
            retry_mode=exc.retry_mode,
            actor=actor,
        )
    except StorageOperationLeaseLost:
        db.rollback()
        plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
        if plan.state in TERMINAL_PLAN_STATES:
            return _plan_public(plan, replayed=True)
        return _plan_public(plan)
    except Exception:
        db.rollback()
        plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
        item = _plan_item(db, plan)
        if plan.state == TERMINAL_PENDING_PLAN_STATE:
            try:
                return _finalize_saved_outcome(db, plan=plan, item=item, handle=handle, actor=actor)
            except Exception:
                db.rollback()
                return _plan_public(db.get(ArchiveIntegrityRemediationPlan, str(plan_id)))
        if item.state in PHYSICAL_PENDING_ITEM_STATES:
            _preserve_pending_recovery(
                db,
                plan=plan,
                reason_code="archive_integrity_remediation_recovery_pending",
                retry_mode="immediate",
            )
            return _plan_public(plan)
        response = _finish_plan(
            db,
            plan=plan,
            item=item,
            handle=handle,
            status="failed",
            result={"status": "failed", "mutated_count": 0},
            reason_code="archive_integrity_remediation_failed",
            retry_mode="new_scan",
            actor=actor,
        )
    return response


def _recover_selected_remediation_once(db: Session, plan_id: str) -> bool:
    plan = db.get(ArchiveIntegrityRemediationPlan, str(plan_id))
    if plan is None:
        return False
    actor = db.get(User, int(plan.actor_user_id)) if plan.actor_user_id is not None else None
    if plan.state in TERMINAL_PLAN_STATES:
        coordinator = None
        try:
            coordinator = acquire_remediation_plan_coordinator(db)
            coordinator.assert_owned()
            plan = (
                db.query(ArchiveIntegrityRemediationPlan)
                .filter(ArchiveIntegrityRemediationPlan.id == str(plan.id))
                .populate_existing()
                .with_for_update()
                .first()
            )
            if plan is None or plan.state not in TERMINAL_PLAN_STATES:
                return False
            converged = _converge_terminal_plan(db, plan, actor=actor)
            coordinator.assert_owned()
            return converged
        except (StorageOperationConflict, StorageOperationLeaseLost, StorageOperationContractError):
            db.rollback()
            return False
        finally:
            if coordinator is not None:
                coordinator.close()

    try:
        coordinated = _coordinated_apply_claim(
            db,
            plan_id=str(plan.id),
            actor=actor,
            requested_operation_id=str(plan.apply_operation_id or uuid.uuid4()),
            expected_actor_key=None,
            allow_create=False,
        )
    except (StorageOperationConflict, StorageOperationLeaseLost, StorageOperationContractError, IntegrityRemediationBlocked):
        db.rollback()
        return False
    except Exception:
        db.rollback()
        raise
    if coordinated is None:
        return False
    if coordinated.get("terminal_response") is not None:
        return True
    claimed = dict(coordinated.get("claimed") or {})
    plan = db.get(ArchiveIntegrityRemediationPlan, str(plan.id))
    if plan is None or not plan.apply_operation_id:
        return False
    item = _plan_item(db, plan)
    finding = db.get(ArchiveIntegrityFinding, str(plan.finding_id))
    action = MUTATING_ACTIONS.get(str(plan.action_kind))
    if finding is None or action is None:
        return False
    if claimed.get("state") == "terminal":
        if plan.state != TERMINAL_PENDING_PLAN_STATE:
            return False
        _finalize_saved_outcome(db, plan=plan, item=item, handle=None, actor=actor)
        return True
    if claimed.get("state") != "claimed":
        return False
    handle = claimed["handle"]
    try:
        if plan.state == TERMINAL_PENDING_PLAN_STATE:
            _finalize_saved_outcome(db, plan=plan, item=item, handle=handle, actor=actor)
            return True
        result = _recover_physical_outcome(
            db,
            plan=plan,
            item=item,
            finding=finding,
            actor=actor,
            handle=handle,
        )
        if result is None:
            if plan.action_kind == "retire_missing_recording":
                result = _apply_missing(db, plan, item, finding, actor)
            elif plan.action_kind == "mark_stale_recording":
                result = _apply_stale(db, plan, item, finding, actor)
            elif plan.action_kind == "delete_unusable_recording":
                result = _apply_unusable(db, plan, item, finding, actor, handle)
            elif plan.action_kind == "delete_proven_orphan":
                result = _apply_orphan(db, plan, item, finding, actor, handle)
            else:
                return False
        if plan.state != TERMINAL_PENDING_PLAN_STATE:
            if plan.action_kind in {"retire_missing_recording", "mark_stale_recording"}:
                terminal = "partial" if result.get("status") == "partial" else "completed"
                _finish_plan(
                    db,
                    plan=plan,
                    item=item,
                    handle=handle,
                    status=terminal,
                    result=result,
                    actor=actor,
                )
                return True
            _persist_physical_outcome(db, plan=plan, item=item, result=result)
        _finalize_saved_outcome(db, plan=plan, item=item, handle=handle, actor=actor)
        return True
    except (IntegrityRemediationBlocked, StorageOperationLeaseLost, StorageOperationConflict):
        db.rollback()
        return False
    except Exception:
        db.rollback()
        raise


def _load_unbound_recovery_plan_batch(
    db: Session,
    cursor: tuple[datetime, str] | None,
) -> list[tuple[str, datetime]]:
    query = db.query(
        ArchiveIntegrityRemediationPlan.id,
        ArchiveIntegrityRemediationPlan.updated_at,
    ).filter(
        ArchiveIntegrityRemediationPlan.apply_operation_id.is_(None),
        ArchiveIntegrityRemediationPlan.state.notin_(tuple(TERMINAL_PLAN_STATES)),
    )
    if cursor is not None:
        updated_at, plan_id = cursor
        query = query.filter(
            or_(
                ArchiveIntegrityRemediationPlan.updated_at > updated_at,
                and_(
                    ArchiveIntegrityRemediationPlan.updated_at == updated_at,
                    ArchiveIntegrityRemediationPlan.id > str(plan_id),
                ),
            )
        )
    rows = (
        query.order_by(
            ArchiveIntegrityRemediationPlan.updated_at.asc(),
            ArchiveIntegrityRemediationPlan.id.asc(),
        )
        .limit(UNBOUND_RECOVERY_BATCH)
        .all()
    )
    return [(str(row.id), row.updated_at) for row in rows]


def recover_pending_remediation_once(db: Session) -> bool:
    """Recover one accepted remediation without repeating a committed mutation."""
    plan = (
        db.query(ArchiveIntegrityRemediationPlan)
        .filter(ArchiveIntegrityRemediationPlan.state == TERMINAL_PENDING_PLAN_STATE)
        .order_by(ArchiveIntegrityRemediationPlan.updated_at.asc(), ArchiveIntegrityRemediationPlan.id.asc())
        .first()
    )
    if plan is not None:
        return _recover_selected_remediation_once(db, str(plan.id))

    plan = (
            db.query(ArchiveIntegrityRemediationPlan)
            .join(
                ArchiveIntegrityRemediationItem,
                ArchiveIntegrityRemediationItem.plan_id == ArchiveIntegrityRemediationPlan.id,
            )
            .filter(
                ArchiveIntegrityRemediationPlan.state == "running",
                ArchiveIntegrityRemediationPlan.apply_operation_id.isnot(None),
                ArchiveIntegrityRemediationItem.state.in_(tuple(PHYSICAL_PENDING_ITEM_STATES | {"running"})),
            )
            .order_by(ArchiveIntegrityRemediationPlan.updated_at.asc(), ArchiveIntegrityRemediationPlan.id.asc())
            .first()
        )
    if plan is not None:
        return _recover_selected_remediation_once(db, str(plan.id))

    plan = (
            db.query(ArchiveIntegrityRemediationPlan)
            .join(StorageOperation, StorageOperation.id == ArchiveIntegrityRemediationPlan.apply_operation_id)
            .filter(
                ArchiveIntegrityRemediationPlan.state.in_(tuple(TERMINAL_PLAN_STATES)),
                StorageOperation.status.notin_(tuple(TERMINAL_PLAN_STATES)),
            )
            .order_by(ArchiveIntegrityRemediationPlan.updated_at.asc(), ArchiveIntegrityRemediationPlan.id.asc())
            .first()
        )
    if plan is not None:
        return _recover_selected_remediation_once(db, str(plan.id))

    scanned = 0
    while scanned < UNBOUND_RECOVERY_POLL_BUDGET:
        cursor = _unbound_recovery_cursor_snapshot()
        try:
            candidates = _load_unbound_recovery_plan_batch(db, cursor)
        except Exception:
            db.rollback()
            raise
        if not candidates:
            if cursor is not None:
                _set_unbound_recovery_cursor(None)
            return False
        for plan_id, updated_at in candidates:
            if scanned >= UNBOUND_RECOVERY_POLL_BUDGET:
                return False
            try:
                recovered = _recover_selected_remediation_once(db, plan_id)
            except Exception:
                db.rollback()
                db.expire_all()
                raise
            scanned += 1
            if recovered:
                _set_unbound_recovery_cursor(None)
                return True
            db.rollback()
            db.expire_all()
            _set_unbound_recovery_cursor((updated_at, plan_id))
    return False


def recover_terminal_remediation_audit_once(db: Session) -> bool:
    plans = (
        db.query(ArchiveIntegrityRemediationPlan)
        .filter(ArchiveIntegrityRemediationPlan.state.in_(tuple(TERMINAL_PLAN_STATES)))
        .order_by(ArchiveIntegrityRemediationPlan.finished_at.desc(), ArchiveIntegrityRemediationPlan.id.desc())
        .limit(16)
        .all()
    )
    for plan in plans:
        operation = db.get(StorageOperation, str(plan.apply_operation_id or ""))
        if operation is None or not _outer_terminal_matches(
            operation,
            status=str(plan.state),
            plan=plan,
            result=dict(plan.result_summary or {}),
        ):
            continue
        operation_audit = (
            db.query(AuditEvent.id)
            .filter(
                AuditEvent.event_type == "storage_operation.finished",
                AuditEvent.target_type == "storage_operation",
                AuditEvent.target_id == str(operation.id),
            )
            .first()
        )
        remediation_audit = (
            db.query(AuditEvent.id)
            .filter(
                AuditEvent.event_type == f"archive_integrity.remediation_{plan.state}",
                AuditEvent.target_type == "archive_integrity_plan",
                AuditEvent.target_id == str(plan.id),
            )
            .first()
        )
        if operation_audit is not None and remediation_audit is not None:
            continue
        coordinator = None
        try:
            coordinator = acquire_remediation_plan_coordinator(db)
            coordinator.assert_owned()
            plan = (
                db.query(ArchiveIntegrityRemediationPlan)
                .filter(ArchiveIntegrityRemediationPlan.id == str(plan.id))
                .populate_existing()
                .with_for_update()
                .first()
            )
            if plan is None or plan.state not in TERMINAL_PLAN_STATES:
                continue
            operation = db.get(StorageOperation, str(plan.apply_operation_id or ""))
            if operation is None or not _outer_terminal_matches(
                operation,
                status=str(plan.state),
                plan=plan,
                result=dict(plan.result_summary or {}),
            ):
                continue
            operation_audit = (
                db.query(AuditEvent.id)
                .filter(
                    AuditEvent.event_type == "storage_operation.finished",
                    AuditEvent.target_type == "storage_operation",
                    AuditEvent.target_id == str(operation.id),
                )
                .first()
            )
            remediation_audit = (
                db.query(AuditEvent.id)
                .filter(
                    AuditEvent.event_type == f"archive_integrity.remediation_{plan.state}",
                    AuditEvent.target_type == "archive_integrity_plan",
                    AuditEvent.target_id == str(plan.id),
                )
                .first()
            )
            if operation_audit is None:
                ensure_operation_terminal_audit(db, operation)
            if remediation_audit is None:
                actor = db.get(User, int(plan.actor_user_id)) if plan.actor_user_id is not None else None
                _ensure_remediation_terminal_audit(db, plan, actor=actor)
            coordinator.assert_owned()
            return True
        except (StorageOperationConflict, StorageOperationLeaseLost, StorageOperationContractError):
            db.rollback()
            return False
        finally:
            if coordinator is not None:
                coordinator.close()
    return False
