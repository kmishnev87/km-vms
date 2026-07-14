from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat as stat_module
import subprocess
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.permissions import user_has_permission
from app.core.sanitization import redact_text
from app.db.session import SessionLocal
from app.models.archive_integrity import (
    ArchiveIntegrityDirectoryWork,
    ArchiveIntegrityFinding,
    ArchiveIntegrityScan,
    RecorderFileReceipt,
)
from app.models.audit_event import AuditEvent
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.storage_operation import StorageOperation
from app.services.audit_log import create_event
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    VIDEO_EXTENSIONS,
    archive_root_runtime_access_state,
    archive_root_runtime_path,
    safe_resolve_relative_for_root,
    segment_has_resolved_archive_root,
)
from app.services.storage_operation_conflicts import (
    StorageOperationConflict,
    operation_instance_id,
    reclaim_operation_with_conflicts,
)
from app.services.storage_operations_foundation import (
    OperationHeartbeatController,
    StorageOperationContractError,
    StorageOperationLeaseLost,
    WorkerLeaseHandle,
    acquire_worker_lease,
    actor_identity,
    create_operation,
    database_now,
    ensure_operation_terminal_audit,
    finish_operation,
    heartbeat_operation,
    public_operation_summary,
    release_worker_lease,
    renew_worker_lease,
    request_operation_cancel,
    stage_operation_terminal,
)


SCAN_OPERATION_TYPE = "integrity_scan"
SCAN_ACTIVE_SLOT = "global"
SCAN_WORKER_KEY = "archive-integrity-scan"
SCAN_WORKER_LEASE_SECONDS = 180
SCAN_OPERATION_LEASE_SECONDS = 180
SCAN_POLL_SECONDS = 1.0
METADATA_PAGE_SIZE = 20
DIRECTORY_COMMIT_SLICE = 64
FINDING_PAGE_MAX = 100
FINDING_PAGE_DEFAULT = 50
RECENT_WRITE_WINDOW = timedelta(minutes=15)
ORPHAN_MIN_AGE = timedelta(minutes=15)
ORPHAN_OBSERVATION_GRACE = timedelta(minutes=15)
SCAN_HISTORY_DAYS = 30
SCAN_HISTORY_MAX_ROWS = 100
SCAN_CLEANUP_BATCH = 20
MEDIA_PROBE_TIMEOUT_SECONDS = 5
FINGERPRINT_CHUNK_BYTES = 64 * 1024
ACTIVE_JOB_STATES = frozenset({"starting", "recording", "stopping", "restarting"})
ACTIVE_SEGMENT_STATES = frozenset({"starting", "writing", "stopping", "restarting"})
TERMINAL_SCAN_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})
ACTIVE_SCAN_STATUSES = frozenset({"queued", "running", "cancel_requested", "interrupted"})


CATEGORY_CONTRACT: dict[str, dict[str, Any]] = {
    "missing_file": {
        "severity": "error",
        "impact": "recording_unavailable",
        "action": "retire_missing_recording",
        "permission": "delete_recordings",
        "confirmation": "destructive_catalog",
    },
    "zero_size_file": {
        "severity": "error",
        "impact": "recording_unplayable",
        "action": "delete_unusable_recording",
        "permission": "delete_recordings",
        "confirmation": "destructive_media",
    },
    "corrupted_file": {
        "severity": "error",
        "impact": "recording_unplayable",
        "action": "delete_unusable_recording",
        "permission": "delete_recordings",
        "confirmation": "destructive_media",
    },
    "stale_writing_segment": {
        "severity": "warning",
        "impact": "recording_unplayable",
        "action": "mark_stale_recording",
        "permission": "manage_settings",
        "confirmation": "metadata",
    },
    "partial_file": {
        "severity": "info",
        "impact": "recording_incomplete",
        "no_action": "incomplete_recording_review_required",
    },
    "orphan_file": {
        "severity": "warning",
        "impact": "unindexed_storage_usage",
        "action": "delete_proven_orphan",
        "permission": "delete_recordings",
        "confirmation": "destructive_media",
    },
    "pre_metadata_km_vms_file": {
        "severity": "warning",
        "impact": "unindexed_storage_usage",
        "no_action": "legacy_file_review_required",
    },
    "legacy_archive_file": {
        "severity": "warning",
        "impact": "unindexed_storage_usage",
        "no_action": "legacy_file_review_required",
    },
    "foreign_file": {
        "severity": "info",
        "impact": "outside_product_ownership",
        "no_action": "foreign_file_not_managed",
    },
    "unknown_file": {
        "severity": "warning",
        "impact": "ownership_unknown",
        "no_action": "unknown_file_review_required",
    },
    "invalid_path": {
        "severity": "error",
        "impact": "recording_unavailable",
        "no_action": "contact_support_with_diagnostics",
    },
    "path_outside_storage": {
        "severity": "error",
        "impact": "recording_unavailable",
        "no_action": "contact_support_with_diagnostics",
    },
    "unreadable_file": {
        "severity": "error",
        "impact": "recording_unavailable",
        "no_action": "restore_archive_access",
    },
    "storage_unavailable": {
        "severity": "error",
        "impact": "archive_root_unavailable",
        "no_action": "restore_archive_access",
    },
    "root_unresolved": {
        "severity": "error",
        "impact": "recording_location_unknown",
        "no_action": "restore_archive_root_mapping",
    },
    "probe_unavailable": {
        "severity": "warning",
        "impact": "integrity_not_fully_checked",
        "no_action": "retry_integrity_check",
    },
    "ownership_untrusted": {
        "severity": "warning",
        "impact": "ownership_unknown",
        "no_action": "contact_support_with_diagnostics",
    },
}


_worker_stop = threading.Event()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.utcnow()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_relative(value: str | None) -> str | None:
    raw = str(value or "").replace("\\", "/").lstrip("/")
    if not raw or len(raw) > 1024:
        return None
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _root_snapshot_key(root: ArchiveRoot) -> str:
    return _sha256_json(
        {
            "id": str(root.id),
            "root_path": str(root.root_path or ""),
            "namespace": str(root.storage_namespace or KMVMS_RECORDINGS_NAMESPACE),
            "physical_identity": str(root.physical_identity or ""),
            "retired_at": root.retired_at.isoformat() if root.retired_at else None,
        }
    )


def _root_access_identity(root: ArchiveRoot, access: dict[str, Any]) -> str | None:
    if access.get("read_access_state") != "available":
        return None
    try:
        root_stat = archive_root_runtime_path(root).stat()
        namespace = archive_root_runtime_path(root) / str(root.storage_namespace or KMVMS_RECORDINGS_NAMESPACE)
        namespace_stat = namespace.stat()
    except OSError:
        return None
    return _sha256_json(
        {
            "root_key": _root_snapshot_key(root),
            "root_device": int(root_stat.st_dev),
            "root_inode": int(root_stat.st_ino),
            "namespace_device": int(namespace_stat.st_dev),
            "namespace_inode": int(namespace_stat.st_ino),
            "physical_identity": str(root.physical_identity or ""),
        }
    )


def _root_public_snapshot(root: ArchiveRoot, access: dict[str, Any]) -> dict[str, Any]:
    return {
        "root_id": str(root.id),
        "label": str(root.label or "Archive"),
        "is_active": bool(root.is_active),
        "physical_identity": str(root.physical_identity or ""),
        "snapshot_key": _root_snapshot_key(root),
        "access_identity": _root_access_identity(root, access),
        "readable": access.get("read_access_state") == "available",
        "problem": str(access.get("problem") or "")[:96] or None,
    }


def _scan_roots(db: Session) -> tuple[list[ArchiveRoot], list[dict[str, Any]]]:
    roots = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.retired_at.is_(None))
        .order_by(ArchiveRoot.is_active.desc(), ArchiveRoot.created_at.asc(), ArchiveRoot.id.asc())
        .all()
    )
    snapshots = [_root_public_snapshot(root, archive_root_runtime_access_state(root)) for root in roots]
    return roots, snapshots


def _scan_identity(scan_id: str) -> dict[str, str]:
    return {"scan_id": str(scan_id), "scope": "all_configured_archive_roots"}


def _scan_id_for_operation(db: Session, operation_id: str) -> ArchiveIntegrityScan | None:
    return db.query(ArchiveIntegrityScan).filter(ArchiveIntegrityScan.operation_id == str(operation_id)).first()


def _active_scan(db: Session) -> ArchiveIntegrityScan | None:
    return (
        db.query(ArchiveIntegrityScan)
        .filter(ArchiveIntegrityScan.active_slot == SCAN_ACTIVE_SLOT)
        .order_by(ArchiveIntegrityScan.created_at.asc())
        .first()
    )


def start_integrity_scan(db: Session, *, actor: Any, idempotency_key: str | None = None) -> dict[str, Any]:
    actor_kind, actor_key, actor_user_id, _system_owner = actor_identity(actor)
    requested_key = str(idempotency_key or "").strip().lower()
    if requested_key:
        existing_operation = (
            db.query(StorageOperation)
            .filter(
                StorageOperation.actor_key == actor_key,
                StorageOperation.operation_type == SCAN_OPERATION_TYPE,
                StorageOperation.idempotency_key == requested_key,
            )
            .first()
        )
        if existing_operation is not None:
            existing_scan = _scan_id_for_operation(db, str(existing_operation.id))
            if existing_scan is not None:
                return public_scan(db, existing_scan, replayed=True)

    current = _active_scan(db)
    if current is not None:
        return public_scan(db, current, replayed=True, coalesced=True)

    scan_id = str(uuid.uuid4())
    operation_id = f"integrity-scan-{uuid.uuid4().hex}"
    idempotency = requested_key or hashlib.sha256(scan_id.encode("ascii")).hexdigest()
    operation = create_operation(
        db,
        operation_type=SCAN_OPERATION_TYPE,
        scope={"global": True},
        request_identity=_scan_identity(scan_id),
        actor=actor,
        operation_id=operation_id,
        idempotency_key=idempotency,
        cancel_allowed=True,
        initial_progress={
            "scan_id": scan_id,
            "phase": "queued",
            "checked_count": 0,
            "found_count": 0,
            "failed_count": 0,
        },
    )
    if operation.get("state") == "terminal":
        replay = _scan_id_for_operation(db, operation_id)
        if replay is None:
            raise StorageOperationContractError("integrity_scan_replay_missing")
        return public_scan(db, replay, replayed=True)

    roots, snapshots = _scan_roots(db)
    high_watermark = int(db.query(func.coalesce(func.max(RecordingSegment.id), 0)).scalar() or 0)
    now = database_now(db)
    scan = ArchiveIntegrityScan(
        id=scan_id,
        operation_id=operation_id,
        actor_user_id=actor_user_id,
        actor_key=actor_key,
        active_slot=SCAN_ACTIVE_SLOT,
        status="queued",
        phase="metadata",
        root_snapshot=snapshots,
        root_snapshot_hash=_sha256_json(snapshots),
        segment_high_watermark=high_watermark,
        scan_cutoff_at=now,
        planned_count=int(
            db.query(RecordingSegment)
            .filter(
                RecordingSegment.id <= high_watermark,
                RecordingSegment.deleted_at.is_(None),
                RecordingSegment.status != "deleted",
            )
            .count()
        ),
        expires_at=now + timedelta(days=SCAN_HISTORY_DAYS),
    )
    db.add(scan)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        try:
            request_operation_cancel(db, operation_id, actor=actor)
        except Exception:
            db.rollback()
        current = _active_scan(db)
        if current is None:
            raise
        return public_scan(db, current, replayed=True, coalesced=True)

    for root, snapshot in zip(roots, snapshots):
        if snapshot["readable"]:
            namespace = _normalize_relative(str(root.storage_namespace or KMVMS_RECORDINGS_NAMESPACE))
            if namespace:
                db.add(
                    ArchiveIntegrityDirectoryWork(
                        id=str(uuid.uuid4()),
                        scan_id=scan.id,
                        root_id=str(root.id),
                        root_snapshot_key=str(snapshot["snapshot_key"]),
                        physical_identity=str(root.physical_identity or "") or None,
                        relative_directory=namespace,
                        status="queued",
                    )
                )
        else:
            _upsert_file_finding(
                db,
                scan=scan,
                root=root,
                category="storage_unavailable",
                stable_object_key=f"root:{snapshot['snapshot_key']}",
                relative_ref=None,
                display_name=str(root.label or "Archive"),
                observed_facts={
                    "root_snapshot_key": snapshot["snapshot_key"],
                    "access_identity": None,
                    "problem_key": snapshot.get("problem") or "archive_root_unavailable",
                },
                finding_scope="root",
            )
            scan.failed_count += 1
    db.add(scan)
    db.commit()
    create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type="archive_integrity.scan_queued",
        severity="info",
        message_ru="Archive integrity scan queued",
        message_en="Archive integrity scan queued",
        target_type="archive_integrity_scan",
        target_id=scan.id,
        metadata={"root_count": len(snapshots), "segment_high_watermark": high_watermark},
    )
    return public_scan(db, scan)


def _finding_contract(category: str) -> dict[str, Any]:
    return dict(
        CATEGORY_CONTRACT.get(
            category,
            {
                "severity": "warning",
                "impact": "integrity_review_required",
                "no_action": "contact_support_with_diagnostics",
            },
        )
    )


def _apply_contract(finding: ArchiveIntegrityFinding, category: str, *, orphan_actionable: bool = True) -> None:
    contract = _finding_contract(category)
    finding.severity = str(contract["severity"])
    finding.impact_key = str(contract["impact"])
    action = contract.get("action")
    if category == "orphan_file" and not orphan_actionable:
        action = None
        finding.no_action_reason = "orphan_observation_grace_required"
    else:
        finding.no_action_reason = str(contract.get("no_action") or "") or None
    finding.action_key = str(action) if action else None
    finding.required_permission = str(contract.get("permission") or "") or None
    finding.confirmation_level = str(contract.get("confirmation") or "") or None
    finding.retry_mode = "new_scan" if category in {"storage_unavailable", "root_unresolved", "probe_unavailable"} else None
    finding.next_action = finding.action_key or finding.no_action_reason


def _metadata_version(segment: RecordingSegment) -> str:
    return _sha256_json(
        {
            "id": int(segment.id),
            "status": str(segment.status or ""),
            "root": str(segment.archive_root_id or ""),
            "relative": str(segment.relative_path or ""),
            "updated_at": segment.updated_at.isoformat() if segment.updated_at else None,
            "deleted_at": segment.deleted_at.isoformat() if segment.deleted_at else None,
            "size": int(segment.size_bytes or 0),
        }
    )


def _upsert_metadata_finding(
    db: Session,
    *,
    scan: ArchiveIntegrityScan,
    segment: RecordingSegment,
    root: ArchiveRoot | None,
    category: str,
    relative_ref: str | None,
    observed_facts: dict[str, Any],
) -> ArchiveIntegrityFinding:
    finding = (
        db.query(ArchiveIntegrityFinding)
        .filter(
            ArchiveIntegrityFinding.scan_id == scan.id,
            ArchiveIntegrityFinding.segment_id == int(segment.id),
            ArchiveIntegrityFinding.is_active.is_(True),
        )
        .first()
    )
    if finding is None:
        finding = ArchiveIntegrityFinding(
            id=str(uuid.uuid4()),
            scan_id=scan.id,
            finding_scope="metadata",
            category=category,
            severity="warning",
            impact_key="integrity_review_required",
            segment_id=int(segment.id),
            root_id=str(root.id) if root else None,
            root_label_snapshot=str(root.label or "Archive") if root else None,
            physical_identity=str(root.physical_identity or "") or None if root else None,
            camera_id=int(segment.camera_id) if segment.camera_id is not None else None,
            camera_name_snapshot=str(segment.camera_name_snapshot or segment.camera_folder_snapshot or "Camera"),
            relative_ref=relative_ref,
            display_name=Path(relative_ref).name if relative_ref else None,
            observed_facts=observed_facts,
            metadata_version=_metadata_version(segment),
            first_observed_scan_id=scan.id,
            first_observed_at=scan.scan_cutoff_at,
            last_observed_at=scan.scan_cutoff_at,
            observation_count=1,
        )
    else:
        finding.category = category
        finding.root_id = str(root.id) if root else None
        finding.root_label_snapshot = str(root.label or "Archive") if root else None
        finding.physical_identity = str(root.physical_identity or "") or None if root else None
        finding.relative_ref = relative_ref
        finding.display_name = Path(relative_ref).name if relative_ref else None
        finding.observed_facts = observed_facts
        finding.metadata_version = _metadata_version(segment)
        finding.last_observed_at = scan.scan_cutoff_at
    _apply_contract(finding, category)
    db.add(finding)
    return finding


def _upsert_file_finding(
    db: Session,
    *,
    scan: ArchiveIntegrityScan,
    root: ArchiveRoot,
    category: str,
    stable_object_key: str,
    relative_ref: str | None,
    display_name: str | None,
    observed_facts: dict[str, Any],
    finding_scope: str = "file",
    observation_count: int = 1,
    first_observed_scan_id: str | None = None,
    first_observed_at: datetime | None = None,
) -> ArchiveIntegrityFinding:
    finding = (
        db.query(ArchiveIntegrityFinding)
        .filter(
            ArchiveIntegrityFinding.scan_id == scan.id,
            ArchiveIntegrityFinding.root_id == str(root.id),
            ArchiveIntegrityFinding.stable_object_key == stable_object_key,
            ArchiveIntegrityFinding.segment_id.is_(None),
            ArchiveIntegrityFinding.is_active.is_(True),
        )
        .first()
    )
    if finding is None:
        finding = ArchiveIntegrityFinding(
            id=str(uuid.uuid4()),
            scan_id=scan.id,
            finding_scope=finding_scope,
            category=category,
            severity="warning",
            impact_key="integrity_review_required",
            root_id=str(root.id),
            root_label_snapshot=str(root.label or "Archive"),
            physical_identity=str(root.physical_identity or "") or None,
            stable_object_key=stable_object_key,
            relative_ref=relative_ref,
            display_name=display_name,
            observed_facts=observed_facts,
            first_observed_scan_id=first_observed_scan_id or scan.id,
            first_observed_at=first_observed_at or scan.scan_cutoff_at,
            last_observed_at=scan.scan_cutoff_at,
            observation_count=max(1, int(observation_count)),
        )
    else:
        finding.category = category
        finding.finding_scope = finding_scope
        finding.relative_ref = relative_ref
        finding.display_name = display_name
        finding.observed_facts = observed_facts
        finding.last_observed_at = scan.scan_cutoff_at
        finding.observation_count = max(int(finding.observation_count or 1), int(observation_count))
    orphan_actionable = bool(
        category == "orphan_file"
        and finding.observation_count >= 2
        and finding.first_observed_at
        and scan.scan_cutoff_at - finding.first_observed_at >= ORPHAN_OBSERVATION_GRACE
        and bool(observed_facts.get("receipt_verified"))
    )
    _apply_contract(finding, category, orphan_actionable=orphan_actionable)
    db.add(finding)
    return finding


def _bounded_fingerprint(path: Path, stat_result: os.stat_result) -> str:
    digest = hashlib.sha256()
    digest.update(f"v1:{int(stat_result.st_size)}:{int(stat_result.st_mtime_ns)}".encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(FINGERPRINT_CHUNK_BYTES))
        if stat_result.st_size > FINGERPRINT_CHUNK_BYTES:
            handle.seek(max(0, int(stat_result.st_size) - FINGERPRINT_CHUNK_BYTES))
            digest.update(handle.read(FINGERPRINT_CHUNK_BYTES))
    return digest.hexdigest()


def _stable_object_key(root_id: str, relative_ref: str, stat_result: os.stat_result) -> str:
    if int(stat_result.st_ino or 0) > 0:
        identity = f"v1:{root_id}:{int(stat_result.st_dev)}:{int(stat_result.st_ino)}"
    else:
        identity = f"v1:{root_id}:{relative_ref}:{int(stat_result.st_size)}:{int(stat_result.st_mtime_ns)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _safe_probe(path: Path) -> tuple[bool | None, str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, "probe_unavailable"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=MEDIA_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, "probe_timeout"
    except OSError:
        return None, "probe_unavailable"
    if result.returncode != 0:
        return False, "probe_failed"
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False, "probe_failed"
    if not isinstance(payload, dict) or not isinstance(payload.get("format"), dict):
        return False, "probe_failed"
    return True, "probe_ok"


def _root_by_snapshot(db: Session, scan: ArchiveIntegrityScan) -> dict[str, tuple[ArchiveRoot, dict[str, Any], dict[str, Any]]]:
    result: dict[str, tuple[ArchiveRoot, dict[str, Any], dict[str, Any]]] = {}
    for snapshot in list(scan.root_snapshot or []):
        root_id = str(snapshot.get("root_id") or "")
        root = db.get(ArchiveRoot, root_id)
        if root is None:
            continue
        access = archive_root_runtime_access_state(root)
        result[root_id] = (root, dict(snapshot), access)
    return result


def _segment_recent(segment: RecordingSegment, scan: ArchiveIntegrityScan) -> bool:
    anchor = segment.updated_at or segment.finalized_at or segment.created_at or segment.started_at
    return bool(anchor and scan.scan_cutoff_at - anchor < RECENT_WRITE_WINDOW)


def _active_jobs_for_page(db: Session, rows: list[RecordingSegment]) -> set[str]:
    ids = sorted({str(row.job_id) for row in rows if row.job_id})
    if not ids:
        return set()
    return {
        str(value)
        for (value,) in db.query(RecordingJob.id)
        .filter(RecordingJob.id.in_(ids), RecordingJob.state.in_(tuple(ACTIVE_JOB_STATES)))
        .all()
    }


def _metadata_observed_facts(
    segment: RecordingSegment,
    *,
    root_snapshot: dict[str, Any] | None,
    access_identity: str | None,
    stat_result: os.stat_result | None = None,
    probe_status: str | None = None,
) -> dict[str, Any]:
    return {
        "segment_version": _metadata_version(segment),
        "root_snapshot_key": (root_snapshot or {}).get("snapshot_key"),
        "root_access_identity": access_identity,
        "status": str(segment.status or ""),
        "finalized_at": segment.finalized_at.isoformat() if segment.finalized_at else None,
        "updated_at": segment.updated_at.isoformat() if segment.updated_at else None,
        "size_bytes": int(stat_result.st_size) if stat_result else None,
        "device_id": str(int(stat_result.st_dev)) if stat_result else None,
        "inode": str(int(stat_result.st_ino)) if stat_result else None,
        "mtime_ns": int(stat_result.st_mtime_ns) if stat_result else None,
        "probe_status": probe_status,
    }


def _classify_metadata_page(
    db: Session,
    scan: ArchiveIntegrityScan,
    rows: list[RecordingSegment],
    heartbeat: OperationHeartbeatController,
) -> None:
    roots = _root_by_snapshot(db, scan)
    active_jobs = _active_jobs_for_page(db, rows)
    for index, segment in enumerate(rows, start=1):
        if index % 5 == 0:
            heartbeat.touch(force=True)
        scan.checked_count += 1
        if segment.ownership != "KM VMS" or segment.source != "recorder":
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=None,
                category="ownership_untrusted",
                relative_ref=_normalize_relative(segment.relative_path),
                observed_facts=_metadata_observed_facts(segment, root_snapshot=None, access_identity=None),
            )
            continue

        # A stale catalog row saying "writing" is not proof of a live Recorder
        # writer. Only an authoritative active job or the recent-write window
        # suppresses integrity classification.
        is_active = bool(segment.job_id and str(segment.job_id) in active_jobs)
        if is_active or _segment_recent(segment, scan):
            continue

        if not segment_has_resolved_archive_root(segment) or not segment.archive_root_id:
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=None,
                category="root_unresolved",
                relative_ref=_normalize_relative(segment.relative_path),
                observed_facts=_metadata_observed_facts(segment, root_snapshot=None, access_identity=None),
            )
            continue

        root_entry = roots.get(str(segment.archive_root_id))
        if root_entry is None:
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=None,
                category="root_unresolved",
                relative_ref=_normalize_relative(segment.relative_path),
                observed_facts=_metadata_observed_facts(segment, root_snapshot=None, access_identity=None),
            )
            continue
        root, snapshot, access = root_entry
        if access.get("read_access_state") != "available":
            continue

        relative_ref = _normalize_relative(segment.relative_path)
        if relative_ref is None:
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=root,
                category="invalid_path",
                relative_ref=None,
                observed_facts=_metadata_observed_facts(
                    segment,
                    root_snapshot=snapshot,
                    access_identity=_root_access_identity(root, access),
                ),
            )
            continue
        if not (relative_ref == KMVMS_RECORDINGS_NAMESPACE or relative_ref.startswith(f"{KMVMS_RECORDINGS_NAMESPACE}/")):
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=root,
                category="path_outside_storage",
                relative_ref=relative_ref,
                observed_facts=_metadata_observed_facts(
                    segment,
                    root_snapshot=snapshot,
                    access_identity=_root_access_identity(root, access),
                ),
            )
            continue
        try:
            target = safe_resolve_relative_for_root(relative_ref, root)
            target_stat = target.lstat()
        except FileNotFoundError:
            current_access = archive_root_runtime_access_state(root)
            current_identity = _root_access_identity(root, current_access)
            if (
                current_access.get("read_access_state") != "available"
                or current_identity != snapshot.get("access_identity")
                or _root_snapshot_key(root) != snapshot.get("snapshot_key")
            ):
                scan.is_stale = True
                scan.failed_count += 1
                continue
            try:
                target.lstat()
                continue
            except FileNotFoundError:
                pass
            except OSError:
                scan.failed_count += 1
                continue
            if _root_access_identity(root, archive_root_runtime_access_state(root)) != current_identity:
                scan.is_stale = True
                scan.failed_count += 1
                continue
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=root,
                category="missing_file",
                relative_ref=relative_ref,
                observed_facts=_metadata_observed_facts(
                    segment,
                    root_snapshot=snapshot,
                    access_identity=_root_access_identity(root, access),
                ),
            )
            continue
        except ValueError:
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=root,
                category="path_outside_storage",
                relative_ref=relative_ref,
                observed_facts=_metadata_observed_facts(
                    segment,
                    root_snapshot=snapshot,
                    access_identity=_root_access_identity(root, access),
                ),
            )
            continue
        except OSError:
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=root,
                category="unreadable_file",
                relative_ref=relative_ref,
                observed_facts=_metadata_observed_facts(
                    segment,
                    root_snapshot=snapshot,
                    access_identity=_root_access_identity(root, access),
                ),
            )
            continue

        if stat_module.S_ISLNK(target_stat.st_mode) or not stat_module.S_ISREG(target_stat.st_mode):
            category = "invalid_path"
        elif not os.access(target, os.R_OK):
            category = "unreadable_file"
        elif int(target_stat.st_size) == 0:
            category = "zero_size_file"
        elif str(segment.status or "") in {"writing", "stale_writing"}:
            category = "stale_writing_segment"
        elif str(segment.status or "") == "finalized":
            heartbeat.touch(force=True)
            probe_ok, probe_status = _safe_probe(target)
            heartbeat.touch(force=True)
            if probe_ok is True:
                scan.checked_bytes += int(target_stat.st_size)
                continue
            category = "corrupted_file" if probe_ok is False else "probe_unavailable"
            if probe_ok is None:
                scan.failed_count += 1
            _upsert_metadata_finding(
                db,
                scan=scan,
                segment=segment,
                root=root,
                category=category,
                relative_ref=relative_ref,
                observed_facts=_metadata_observed_facts(
                    segment,
                    root_snapshot=snapshot,
                    access_identity=_root_access_identity(root, access),
                    stat_result=target_stat,
                    probe_status=probe_status,
                ),
            )
            scan.checked_bytes += int(target_stat.st_size)
            continue
        else:
            category = "partial_file"

        _upsert_metadata_finding(
            db,
            scan=scan,
            segment=segment,
            root=root,
            category=category,
            relative_ref=relative_ref,
            observed_facts=_metadata_observed_facts(
                segment,
                root_snapshot=snapshot,
                access_identity=_root_access_identity(root, access),
                stat_result=target_stat,
            ),
        )
        scan.checked_bytes += int(target_stat.st_size)


def _previous_file_observation(
    db: Session,
    *,
    scan: ArchiveIntegrityScan,
    root_id: str,
    stable_object_key: str,
    category: str,
) -> ArchiveIntegrityFinding | None:
    return (
        db.query(ArchiveIntegrityFinding)
        .join(ArchiveIntegrityScan, ArchiveIntegrityScan.id == ArchiveIntegrityFinding.scan_id)
        .filter(
            ArchiveIntegrityFinding.scan_id != scan.id,
            ArchiveIntegrityFinding.root_id == root_id,
            ArchiveIntegrityFinding.stable_object_key == stable_object_key,
            ArchiveIntegrityFinding.category == category,
            ArchiveIntegrityFinding.is_active.is_(True),
            ArchiveIntegrityScan.status.in_(("completed", "partial")),
            ArchiveIntegrityScan.finished_at.isnot(None),
        )
        .order_by(
            ArchiveIntegrityScan.finished_at.desc(),
            ArchiveIntegrityScan.scan_cutoff_at.desc(),
            ArchiveIntegrityFinding.id.desc(),
        )
        .first()
    )


def _receipt_matches(
    receipt: RecorderFileReceipt,
    *,
    root: ArchiveRoot,
    relative_ref: str,
    stat_result: os.stat_result,
    fingerprint: str,
) -> bool:
    return bool(
        receipt.contract_version == 1
        and receipt.state == "finalized"
        and str(receipt.root_id) == str(root.id)
        and str(receipt.physical_identity or "") == str(root.physical_identity or "")
        and str(receipt.relative_path) == relative_ref
        and str(receipt.device_id or "") == str(int(stat_result.st_dev))
        and str(receipt.inode or "") == str(int(stat_result.st_ino))
        and int(receipt.size_bytes or 0) == int(stat_result.st_size)
        and int(receipt.mtime_ns or 0) == int(stat_result.st_mtime_ns)
        and str(receipt.content_fingerprint or "") == fingerprint
    )


def _classify_filesystem_entry(
    db: Session,
    *,
    scan: ArchiveIntegrityScan,
    root: ArchiveRoot,
    snapshot: dict[str, Any],
    path: Path,
    relative_ref: str,
    entry_stat: os.stat_result,
) -> None:
    stable_key = _stable_object_key(str(root.id), relative_ref, entry_stat)
    if stat_module.S_ISLNK(entry_stat.st_mode):
        _upsert_file_finding(
            db,
            scan=scan,
            root=root,
            category="invalid_path",
            stable_object_key=stable_key,
            relative_ref=relative_ref,
            display_name=Path(relative_ref).name,
            observed_facts={
                "root_snapshot_key": snapshot.get("snapshot_key"),
                "object_type": "symlink",
            },
        )
        return
    if not stat_module.S_ISREG(entry_stat.st_mode):
        return

    catalog_owner = (
        db.query(RecordingSegment.id, RecordingSegment.ownership, RecordingSegment.source)
        .filter(
            RecordingSegment.archive_root_id == str(root.id),
            RecordingSegment.relative_path == relative_ref,
            RecordingSegment.deleted_at.is_(None),
            RecordingSegment.status != "deleted",
            RecordingSegment.id <= scan.segment_high_watermark,
        )
        .order_by(RecordingSegment.id.asc())
        .first()
    )
    if catalog_owner is not None:
        # Metadata classification owns this physical reference, including the
        # review-only untrusted-ownership case. Filesystem traversal must not
        # publish a second active finding for the same catalog object.
        return

    age = scan.scan_cutoff_at - datetime.fromtimestamp(entry_stat.st_mtime)
    if age < RECENT_WRITE_WINDOW:
        return

    receipt = (
        db.query(RecorderFileReceipt)
        .filter(
            RecorderFileReceipt.root_id == str(root.id),
            RecorderFileReceipt.relative_path == relative_ref,
            RecorderFileReceipt.state == "finalized",
        )
        .order_by(RecorderFileReceipt.finalized_at.desc(), RecorderFileReceipt.id.desc())
        .first()
    )
    fingerprint: str | None = None
    receipt_verified = False
    if receipt is not None:
        try:
            fingerprint = _bounded_fingerprint(path, entry_stat)
            receipt_verified = _receipt_matches(
                receipt,
                root=root,
                relative_ref=relative_ref,
                stat_result=entry_stat,
                fingerprint=fingerprint,
            )
        except OSError:
            receipt_verified = False

    if receipt_verified:
        category = "orphan_file"
    elif Path(relative_ref).suffix.lower() in VIDEO_EXTENSIONS:
        category = "pre_metadata_km_vms_file"
    else:
        category = "unknown_file"

    previous = _previous_file_observation(
        db,
        scan=scan,
        root_id=str(root.id),
        stable_object_key=stable_key,
        category=category,
    )
    observation_count = int(previous.observation_count or 1) + 1 if previous else 1
    first_scan_id = previous.first_observed_scan_id if previous else scan.id
    first_at = previous.first_observed_at if previous else scan.scan_cutoff_at
    _upsert_file_finding(
        db,
        scan=scan,
        root=root,
        category=category,
        stable_object_key=stable_key,
        relative_ref=relative_ref,
        display_name=Path(relative_ref).name,
        observed_facts={
            "root_snapshot_key": snapshot.get("snapshot_key"),
            "root_access_identity": snapshot.get("access_identity"),
            "device_id": str(int(entry_stat.st_dev)),
            "inode": str(int(entry_stat.st_ino)),
            "size_bytes": int(entry_stat.st_size),
            "mtime_ns": int(entry_stat.st_mtime_ns),
            "fingerprint": fingerprint,
            "receipt_id": str(receipt.id) if receipt_verified and receipt else None,
            "receipt_verified": receipt_verified,
            "minimum_age_met": age >= ORPHAN_MIN_AGE,
        },
        observation_count=observation_count,
        first_observed_scan_id=first_scan_id,
        first_observed_at=first_at,
    )


def _process_metadata_unit(
    db: Session,
    scan: ArchiveIntegrityScan,
    heartbeat: OperationHeartbeatController,
) -> bool:
    rows = (
        db.query(RecordingSegment)
        .filter(
            RecordingSegment.id > int(scan.metadata_cursor or 0),
            RecordingSegment.id <= int(scan.segment_high_watermark or 0),
            RecordingSegment.deleted_at.is_(None),
            RecordingSegment.status != "deleted",
        )
        .order_by(RecordingSegment.id.asc())
        .limit(METADATA_PAGE_SIZE)
        .all()
    )
    if not rows:
        scan.phase = "filesystem"
        scan.updated_at = _utcnow()
        db.add(scan)
        db.commit()
        return False
    _classify_metadata_page(db, scan, rows, heartbeat)
    scan.metadata_cursor = int(rows[-1].id)
    scan.heartbeat_at = _utcnow()
    scan.updated_at = _utcnow()
    db.add(scan)
    db.commit()
    heartbeat_operation(
        db,
        heartbeat.handle,
        progress=_operation_progress(scan),
        lease_seconds=SCAN_OPERATION_LEASE_SECONDS,
    )
    return True


def _claim_directory(db: Session, scan: ArchiveIntegrityScan, owner_instance_id: str) -> ArchiveIntegrityDirectoryWork | None:
    now = database_now(db)
    row = (
        db.query(ArchiveIntegrityDirectoryWork)
        .filter(
            ArchiveIntegrityDirectoryWork.scan_id == scan.id,
            ArchiveIntegrityDirectoryWork.status.in_(("queued", "interrupted")),
        )
        .order_by(ArchiveIntegrityDirectoryWork.root_id.asc(), ArchiveIntegrityDirectoryWork.relative_directory.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if row is None:
        db.commit()
        return None
    row.status = "claimed"
    row.owner_instance_id = owner_instance_id[:128]
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=SCAN_WORKER_LEASE_SECONDS)
    row.reason_code = None
    row.updated_at = now
    db.add(row)
    db.commit()
    return row


def _process_directory_unit(
    db: Session,
    scan: ArchiveIntegrityScan,
    work: ArchiveIntegrityDirectoryWork,
    heartbeat: OperationHeartbeatController,
    worker_lease: WorkerLeaseHandle,
) -> None:
    root_entry = _root_by_snapshot(db, scan).get(str(work.root_id))
    if root_entry is None:
        work.status = "failed"
        work.reason_code = "archive_root_snapshot_missing"
        work.completed_at = _utcnow()
        scan.failed_count += 1
        db.add_all((work, scan))
        db.commit()
        return
    root, snapshot, access = root_entry
    if (
        str(snapshot.get("snapshot_key")) != _root_snapshot_key(root)
        or access.get("read_access_state") != "available"
        or snapshot.get("access_identity") != _root_access_identity(root, access)
    ):
        work.status = "failed"
        work.reason_code = "archive_root_changed_or_unavailable"
        work.completed_at = _utcnow()
        scan.failed_count += 1
        scan.is_stale = True
        db.add_all((work, scan))
        db.commit()
        return

    relative_directory = _normalize_relative(work.relative_directory)
    if relative_directory is None:
        work.status = "failed"
        work.reason_code = "integrity_directory_identity_invalid"
        work.completed_at = _utcnow()
        scan.failed_count += 1
        db.add_all((work, scan))
        db.commit()
        return
    try:
        directory_path = safe_resolve_relative_for_root(relative_directory, root)
        directory_stat = directory_path.lstat()
        if stat_module.S_ISLNK(directory_stat.st_mode) or not stat_module.S_ISDIR(directory_stat.st_mode):
            raise OSError("integrity_directory_not_safe")
        entries_seen = 0
        child_count = 0
        file_count = 0
        checked_bytes = 0
        commit_slice = max(1, int(DIRECTORY_COMMIT_SLICE))
        directory_identity = (
            int(directory_stat.st_dev),
            int(directory_stat.st_ino),
            int(directory_stat.st_mtime_ns),
        )

        def checkpoint() -> None:
            now = database_now(db)
            work.heartbeat_at = now
            work.lease_expires_at = now + timedelta(seconds=SCAN_WORKER_LEASE_SECONDS)
            work.updated_at = now
            db.add(work)
            db.commit()
            heartbeat.touch(force=True)
            with Session(bind=db.get_bind()) as lease_db:
                renew_worker_lease(lease_db, worker_lease, lease_seconds=SCAN_WORKER_LEASE_SECONDS)

        with os.scandir(directory_path) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen % 50 == 0:
                    heartbeat.touch(force=True)
                child_relative = _normalize_relative(f"{relative_directory}/{entry.name}")
                if child_relative is None:
                    if entries_seen % commit_slice == 0:
                        checkpoint()
                    continue
                entry_stat = entry.stat(follow_symlinks=False)
                if stat_module.S_ISDIR(entry_stat.st_mode) and not stat_module.S_ISLNK(entry_stat.st_mode):
                    existing = (
                        db.query(ArchiveIntegrityDirectoryWork.id)
                        .filter(
                            ArchiveIntegrityDirectoryWork.scan_id == scan.id,
                            ArchiveIntegrityDirectoryWork.root_id == str(root.id),
                            ArchiveIntegrityDirectoryWork.relative_directory == child_relative,
                        )
                        .first()
                    )
                    if existing is None:
                        db.add(
                            ArchiveIntegrityDirectoryWork(
                                id=str(uuid.uuid4()),
                                scan_id=scan.id,
                                root_id=str(root.id),
                                root_snapshot_key=str(snapshot["snapshot_key"]),
                                physical_identity=str(root.physical_identity or "") or None,
                                relative_directory=child_relative,
                                status="queued",
                            )
                        )
                    child_count += 1
                else:
                    _classify_filesystem_entry(
                        db,
                        scan=scan,
                        root=root,
                        snapshot=snapshot,
                        path=Path(entry.path),
                        relative_ref=child_relative,
                        entry_stat=entry_stat,
                    )
                    file_count += 1
                    checked_bytes += int(entry_stat.st_size or 0)
                if entries_seen % commit_slice == 0:
                    checkpoint()
        final_directory_stat = directory_path.lstat()
        if (
            stat_module.S_ISLNK(final_directory_stat.st_mode)
            or not stat_module.S_ISDIR(final_directory_stat.st_mode)
            or directory_identity
            != (
                int(final_directory_stat.st_dev),
                int(final_directory_stat.st_ino),
                int(final_directory_stat.st_mtime_ns),
            )
        ):
            raise RuntimeError("integrity_directory_changed_during_scan")
        work.status = "completed"
        work.discovered_directory_count = child_count
        work.discovered_file_count = file_count
        work.reason_code = None
        scan.checked_count += file_count
        scan.checked_bytes += checked_bytes
    except StorageOperationLeaseLost:
        db.rollback()
        raise
    except RuntimeError as exc:
        work.status = "failed"
        work.reason_code = str(exc)[:96]
        scan.failed_count += 1
    except (FileNotFoundError, PermissionError, OSError):
        work.status = "failed"
        work.reason_code = "integrity_directory_unavailable"
        scan.failed_count += 1
    work.owner_instance_id = None
    work.lease_expires_at = None
    work.heartbeat_at = _utcnow()
    work.completed_at = _utcnow()
    work.updated_at = _utcnow()
    scan.heartbeat_at = _utcnow()
    scan.updated_at = _utcnow()
    db.add_all((work, scan))
    db.commit()
    heartbeat_operation(
        db,
        heartbeat.handle,
        progress=_operation_progress(scan),
        lease_seconds=SCAN_OPERATION_LEASE_SECONDS,
    )


def _operation_progress(scan: ArchiveIntegrityScan) -> dict[str, Any]:
    return {
        "scan_id": scan.id,
        "phase": scan.phase,
        "planned_count": int(scan.planned_count or 0),
        "checked_count": int(scan.checked_count or 0),
        "found_count": int(scan.found_count or 0),
        "failed_count": int(scan.failed_count or 0),
        "checked_bytes": int(scan.checked_bytes or 0),
    }


def _refresh_scan_summary(db: Session, scan: ArchiveIntegrityScan) -> None:
    rows = (
        db.query(ArchiveIntegrityFinding.category, func.count(ArchiveIntegrityFinding.id))
        .filter(ArchiveIntegrityFinding.scan_id == scan.id, ArchiveIntegrityFinding.is_active.is_(True))
        .group_by(ArchiveIntegrityFinding.category)
        .order_by(ArchiveIntegrityFinding.category.asc())
        .all()
    )
    counts = {str(category): int(count) for category, count in rows[:64]}
    scan.category_summary = counts
    scan.found_count = int(sum(counts.values()))
    impact_rows = (
        db.query(ArchiveIntegrityFinding.impact_key, func.count(ArchiveIntegrityFinding.id))
        .filter(ArchiveIntegrityFinding.scan_id == scan.id, ArchiveIntegrityFinding.is_active.is_(True))
        .group_by(ArchiveIntegrityFinding.impact_key)
        .order_by(ArchiveIntegrityFinding.impact_key.asc())
        .all()
    )
    scan.impact_summary = {str(key): int(count) for key, count in impact_rows[:32]}
    root_rows = (
        db.query(ArchiveIntegrityFinding.root_label_snapshot, func.count(ArchiveIntegrityFinding.id))
        .filter(ArchiveIntegrityFinding.scan_id == scan.id, ArchiveIntegrityFinding.is_active.is_(True))
        .group_by(ArchiveIntegrityFinding.root_label_snapshot)
        .order_by(ArchiveIntegrityFinding.root_label_snapshot.asc())
        .limit(32)
        .all()
    )
    scan.root_summary = {str(label or "Archive"): int(count) for label, count in root_rows}


def _root_snapshot_stale(db: Session, scan: ArchiveIntegrityScan) -> bool:
    _roots, current = _scan_roots(db)
    current_by_id = {str(item["root_id"]): item for item in current}
    expected = list(scan.root_snapshot or [])
    if set(current_by_id) != {str(item.get("root_id")) for item in expected}:
        return True
    return any(
        current_by_id[str(item.get("root_id"))].get("snapshot_key") != item.get("snapshot_key")
        or current_by_id[str(item.get("root_id"))].get("access_identity") != item.get("access_identity")
        for item in expected
    )


def _terminalize_scan(
    db: Session,
    scan: ArchiveIntegrityScan,
    handle,
    *,
    status: str | None = None,
    reason_code: str | None = None,
) -> None:
    _refresh_scan_summary(db, scan)
    scan.is_stale = bool(scan.is_stale or _root_snapshot_stale(db, scan))
    terminal = status or ("partial" if scan.failed_count or scan.is_stale else "completed")
    now = database_now(db)
    scan.status = terminal
    scan.phase = "completed" if terminal in {"completed", "partial"} else terminal
    scan.reason_code = reason_code or (
        "archive_integrity_scan_stale" if scan.is_stale else "archive_integrity_scan_partial" if terminal == "partial" else None
    )
    scan.retry_mode = "new_scan" if terminal in {"partial", "failed"} else None
    scan.next_action = "retry_integrity_scan" if terminal in {"partial", "failed"} else None
    scan.finished_at = now
    scan.heartbeat_at = now
    scan.updated_at = now
    scan.active_slot = None
    db.add(scan)
    result = _scan_terminal_result(scan, terminal)
    progress = _operation_progress(scan)
    stage_operation_terminal(
        db,
        handle,
        status=terminal,
        result=result,
        progress=progress,
        reason_code=scan.reason_code,
        next_action=scan.next_action,
        retry_mode=scan.retry_mode,
        retry_allowed=terminal in {"partial", "failed"},
    )
    db.commit()
    operation = db.get(StorageOperation, scan.operation_id)
    ensure_operation_terminal_audit(db, operation)
    _ensure_scan_terminal_audit(db, scan)


def _scan_terminal_result(scan: ArchiveIntegrityScan, status: str | None = None) -> dict[str, Any]:
    terminal = str(status or scan.status)
    return {
        "status": terminal,
        "scan_id": scan.id,
        "found_count": int(scan.found_count or 0),
        "failed_count": int(scan.failed_count or 0),
        "stale": bool(scan.is_stale),
        "category_count": len(scan.category_summary or {}),
    }


def _scan_outer_terminal_matches(scan: ArchiveIntegrityScan, operation: StorageOperation) -> bool:
    if str(operation.status) != str(scan.status):
        return False
    actual = dict(operation.result or {})
    return all(actual.get(key) == value for key, value in _scan_terminal_result(scan).items())


def _ensure_scan_terminal_audit(db: Session, scan: ArchiveIntegrityScan) -> None:
    if scan.status not in TERMINAL_SCAN_STATUSES:
        return
    event_type = f"archive_integrity.scan_{scan.status}"
    exists = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_type == "archive_integrity_scan",
            AuditEvent.target_id == str(scan.id),
        )
        .first()
    )
    if exists is not None:
        return
    create_event(
        db=db,
        actor=None,
        category="storage",
        event_type=event_type,
        severity="info" if scan.status == "completed" else "warning",
        message_ru="Archive integrity scan finished",
        message_en="Archive integrity scan finished",
        target_type="archive_integrity_scan",
        target_id=scan.id,
        metadata={
            "status": scan.status,
            "finding_count": int(scan.found_count or 0),
            "failed_count": int(scan.failed_count or 0),
            "stale": bool(scan.is_stale),
            "category_counts": dict(scan.category_summary or {}),
        },
    )


def _recover_terminal_scan(db: Session, scan: ArchiveIntegrityScan, operation: StorageOperation) -> bool:
    if scan.status not in TERMINAL_SCAN_STATUSES or not scan.active_slot:
        return False
    if operation.status in {"completed", "partial", "failed", "cancelled"}:
        if _scan_outer_terminal_matches(scan, operation):
            scan.active_slot = None
            scan.updated_at = database_now(db)
            db.add(scan)
            db.commit()
            ensure_operation_terminal_audit(db, operation)
            _ensure_scan_terminal_audit(db, scan)
        return True
    try:
        claimed = reclaim_operation_with_conflicts(
            db,
            operation_id=str(operation.id),
            operation_type=SCAN_OPERATION_TYPE,
            request_identity=_scan_identity(scan.id),
            idempotency_key=str(operation.idempotency_key),
            owner_instance_id=operation_instance_id("integrity-scan-terminal-recovery"),
        )
    except (StorageOperationConflict, StorageOperationLeaseLost):
        db.rollback()
        return True
    if claimed.get("state") == "claimed":
        stage_operation_terminal(
            db,
            claimed["handle"],
            status=scan.status,
            result=_scan_terminal_result(scan),
            progress=_operation_progress(scan),
            reason_code=scan.reason_code,
            next_action=scan.next_action,
            retry_mode=scan.retry_mode,
            retry_allowed=scan.status in {"partial", "failed"},
        )
        scan.active_slot = None
        scan.updated_at = database_now(db)
        db.add(scan)
        db.commit()
        operation = db.get(StorageOperation, scan.operation_id)
        ensure_operation_terminal_audit(db, operation)
        _ensure_scan_terminal_audit(db, scan)
    return True


def _recover_terminal_scan_audit_once(db: Session) -> bool:
    scans = (
        db.query(ArchiveIntegrityScan)
        .filter(
            ArchiveIntegrityScan.status.in_(tuple(TERMINAL_SCAN_STATUSES)),
            ArchiveIntegrityScan.active_slot.is_(None),
        )
        .order_by(ArchiveIntegrityScan.finished_at.desc(), ArchiveIntegrityScan.id.desc())
        .limit(16)
        .all()
    )
    for scan in scans:
        operation = db.get(StorageOperation, str(scan.operation_id))
        if operation is None or not _scan_outer_terminal_matches(scan, operation):
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
        scan_audit = (
            db.query(AuditEvent.id)
            .filter(
                AuditEvent.event_type == f"archive_integrity.scan_{scan.status}",
                AuditEvent.target_type == "archive_integrity_scan",
                AuditEvent.target_id == str(scan.id),
            )
            .first()
        )
        if operation_audit is None:
            ensure_operation_terminal_audit(db, operation)
        if scan_audit is None:
            _ensure_scan_terminal_audit(db, scan)
        if operation_audit is None or scan_audit is None:
            return True
    return False


def _reset_interrupted_directory_work(db: Session, scan: ArchiveIntegrityScan) -> None:
    now = database_now(db)
    rows = (
        db.query(ArchiveIntegrityDirectoryWork)
        .filter(
            ArchiveIntegrityDirectoryWork.scan_id == scan.id,
            ArchiveIntegrityDirectoryWork.status == "claimed",
            ArchiveIntegrityDirectoryWork.lease_expires_at.isnot(None),
            ArchiveIntegrityDirectoryWork.lease_expires_at <= now,
        )
        .limit(128)
        .all()
    )
    for row in rows:
        row.status = "interrupted"
        row.owner_instance_id = None
        row.lease_expires_at = None
        row.reason_code = "integrity_directory_worker_interrupted"
        row.updated_at = now
        db.add(row)
    if rows:
        db.commit()


def _run_scan(db: Session, scan: ArchiveIntegrityScan, worker_lease: WorkerLeaseHandle) -> None:
    operation = db.get(StorageOperation, scan.operation_id)
    if operation is None:
        scan.status = "failed"
        scan.reason_code = "integrity_scan_operation_missing"
        scan.active_slot = None
        scan.finished_at = database_now(db)
        db.add(scan)
        db.commit()
        return
    if _recover_terminal_scan(db, scan, operation):
        return
    if operation.status == "cancelled":
        scan.status = "cancelled"
        scan.phase = "cancelled"
        scan.finished_at = database_now(db)
        scan.active_slot = None
        db.add(scan)
        db.commit()
        return

    claimed = reclaim_operation_with_conflicts(
        db,
        operation_id=str(operation.id),
        operation_type=SCAN_OPERATION_TYPE,
        request_identity=_scan_identity(scan.id),
        idempotency_key=str(operation.idempotency_key),
        owner_instance_id=operation_instance_id("integrity-scan-worker"),
    )
    if claimed.get("state") == "terminal":
        scan.active_slot = None
        db.add(scan)
        db.commit()
        return
    if claimed.get("state") != "claimed":
        return
    handle = claimed["handle"]
    heartbeat = OperationHeartbeatController(db.get_bind(), handle, interval_seconds=30)
    scan.status = "running"
    scan.started_at = scan.started_at or database_now(db)
    scan.heartbeat_at = database_now(db)
    db.add(scan)
    db.commit()
    _reset_interrupted_directory_work(db, scan)

    try:
        while not _worker_stop.is_set():
            scan = db.get(ArchiveIntegrityScan, scan.id)
            if scan is None:
                raise RuntimeError("integrity_scan_disappeared")
            operation = db.get(StorageOperation, scan.operation_id)
            if scan.cancel_requested or (operation and operation.status == "cancel_requested"):
                _terminalize_scan(db, scan, handle, status="cancelled", reason_code="archive_integrity_scan_cancelled")
                return
            with Session(bind=db.get_bind()) as lease_db:
                renew_worker_lease(lease_db, worker_lease, lease_seconds=SCAN_WORKER_LEASE_SECONDS)
            if scan.phase == "metadata":
                _process_metadata_unit(db, scan, heartbeat)
                continue
            work = _claim_directory(db, scan, str(worker_lease.owner_instance_id))
            if work is not None:
                _process_directory_unit(db, scan, work, heartbeat, worker_lease)
                continue
            incomplete = (
                db.query(ArchiveIntegrityDirectoryWork.id)
                .filter(
                    ArchiveIntegrityDirectoryWork.scan_id == scan.id,
                    ArchiveIntegrityDirectoryWork.status.in_(("queued", "claimed", "interrupted")),
                )
                .first()
            )
            if incomplete is not None:
                time.sleep(0.1)
                continue
            _terminalize_scan(db, scan, handle)
            return
    except (StorageOperationLeaseLost, StorageOperationConflict):
        db.rollback()
        scan = db.get(ArchiveIntegrityScan, scan.id)
        if scan and scan.status not in TERMINAL_SCAN_STATUSES:
            scan.status = "interrupted"
            scan.reason_code = "archive_integrity_scan_authority_lost"
            scan.retry_mode = "resume"
            db.add(scan)
            db.commit()
    except Exception:
        db.rollback()
        scan = db.get(ArchiveIntegrityScan, scan.id)
        if scan and scan.status not in TERMINAL_SCAN_STATUSES:
            try:
                _terminalize_scan(
                    db,
                    scan,
                    handle,
                    status="failed",
                    reason_code="archive_integrity_scan_failed",
                )
            except Exception:
                db.rollback()
                scan = db.get(ArchiveIntegrityScan, scan.id)
                if scan:
                    scan.status = "interrupted"
                    scan.reason_code = "archive_integrity_scan_terminal_persistence_failed"
                    db.add(scan)
                    db.commit()


def run_integrity_worker_once() -> bool:
    owner_instance_id = operation_instance_id("integrity-worker")
    with SessionLocal() as db:
        worker_lease = acquire_worker_lease(
            db,
            worker_key=SCAN_WORKER_KEY,
            owner_instance_id=owner_instance_id,
            lease_seconds=SCAN_WORKER_LEASE_SECONDS,
        )
        if worker_lease is None:
            return False
        try:
            from app.services.archive_integrity_remediation import (
                recover_pending_remediation_once,
                recover_terminal_remediation_audit_once,
            )

            if recover_pending_remediation_once(db):
                return True
            if recover_terminal_remediation_audit_once(db):
                return True
            if _recover_terminal_scan_audit_once(db):
                return True
            scan = _active_scan(db)
            if scan is None:
                return False
            _run_scan(db, scan, worker_lease)
            return True
        finally:
            try:
                release_worker_lease(db, worker_lease)
            except Exception:
                db.rollback()


def _worker_loop() -> None:
    while not _worker_stop.wait(SCAN_POLL_SECONDS):
        try:
            run_integrity_worker_once()
        except Exception:
            time.sleep(SCAN_POLL_SECONDS)


def start_archive_integrity_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_stop.clear()
        _worker_thread = threading.Thread(target=_worker_loop, name="archive-integrity-worker", daemon=True)
        _worker_thread.start()


def stop_archive_integrity_worker() -> None:
    global _worker_thread
    with _worker_lock:
        _worker_stop.set()
        thread = _worker_thread
        _worker_thread = None
    if thread is not None:
        thread.join(timeout=5)


def _public_operation(db: Session, scan: ArchiveIntegrityScan) -> dict[str, Any] | None:
    operation = db.get(StorageOperation, scan.operation_id)
    return public_operation_summary(operation, now=database_now(db)) if operation else None


def public_scan(
    db: Session,
    scan: ArchiveIntegrityScan,
    *,
    replayed: bool = False,
    coalesced: bool = False,
) -> dict[str, Any]:
    return {
        "scan_id": scan.id,
        "operation_id": scan.operation_id,
        "status": scan.status,
        "phase": scan.phase,
        "stale": bool(scan.is_stale),
        "progress": {
            "planned_count": int(scan.planned_count or 0),
            "checked_count": int(scan.checked_count or 0),
            "found_count": int(scan.found_count or 0),
            "failed_count": int(scan.failed_count or 0),
            "checked_bytes": int(scan.checked_bytes or 0),
        },
        "category_counts": dict(scan.category_summary or {}),
        "impact_counts": dict(scan.impact_summary or {}),
        "root_counts": dict(scan.root_summary or {}),
        "reason_code": scan.reason_code,
        "next_action": scan.next_action,
        "retry_mode": scan.retry_mode,
        "created_at": scan.created_at.isoformat() if scan.created_at else None,
        "started_at": scan.started_at.isoformat() if scan.started_at else None,
        "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
        "operation": _public_operation(db, scan),
        "replayed": bool(replayed),
        "coalesced": bool(coalesced),
    }


def latest_integrity_scan(db: Session) -> dict[str, Any]:
    scan = (
        db.query(ArchiveIntegrityScan)
        .order_by(ArchiveIntegrityScan.created_at.desc(), ArchiveIntegrityScan.id.desc())
        .first()
    )
    if scan is None:
        return {
            "status": "not_run",
            "scan_id": None,
            "operation_id": None,
            "stale": False,
            "progress": {"planned_count": 0, "checked_count": 0, "found_count": 0, "failed_count": 0, "checked_bytes": 0},
            "category_counts": {},
            "impact_counts": {},
            "root_counts": {},
            "operation": None,
        }
    return public_scan(db, scan)


def get_integrity_scan(db: Session, scan_id: str) -> dict[str, Any] | None:
    scan = db.get(ArchiveIntegrityScan, str(scan_id))
    return public_scan(db, scan) if scan else None


def cancel_integrity_scan(db: Session, scan_id: str, *, actor: Any) -> dict[str, Any]:
    scan = db.get(ArchiveIntegrityScan, str(scan_id))
    if scan is None:
        raise StorageOperationContractError("archive_integrity_scan_not_found")
    if scan.actor_user_id != getattr(actor, "id", None):
        raise StorageOperationContractError("archive_integrity_scan_actor_mismatch")
    operation = request_operation_cancel(db, scan.operation_id, actor=actor)
    if operation.get("status") == "cancelled":
        scan.status = "cancelled"
        scan.phase = "cancelled"
        scan.finished_at = database_now(db)
        scan.active_slot = None
    else:
        scan.status = "cancel_requested"
        scan.cancel_requested = True
    scan.updated_at = database_now(db)
    db.add(scan)
    db.commit()
    return public_scan(db, scan)


def _public_finding(finding: ArchiveIntegrityFinding, *, role: str | None) -> dict[str, Any]:
    action_allowed = bool(
        finding.action_key
        and finding.required_permission
        and user_has_permission(str(role or ""), str(finding.required_permission))
    )
    return {
        "finding_id": finding.id,
        "category": finding.category,
        "severity": finding.severity,
        "impact_key": finding.impact_key,
        "root_label": finding.root_label_snapshot,
        "camera_name": finding.camera_name_snapshot,
        "display_name": finding.display_name,
        "action_key": finding.action_key if action_allowed else None,
        "action_allowed": action_allowed,
        "required_permission": finding.required_permission,
        "confirmation_level": finding.confirmation_level if action_allowed else None,
        "no_action_reason": finding.no_action_reason if not action_allowed else None,
        "next_action": finding.next_action,
        "retry_mode": finding.retry_mode,
        "observation_count": int(finding.observation_count or 1),
        "state": finding.state,
        "stale": finding.state == "stale" or not bool(finding.is_active),
    }


def list_integrity_findings(
    db: Session,
    scan_id: str,
    *,
    role: str | None,
    cursor: str | None = None,
    limit: int = FINDING_PAGE_DEFAULT,
    category: str | None = None,
    impact: str | None = None,
) -> dict[str, Any]:
    scan = db.get(ArchiveIntegrityScan, str(scan_id))
    if scan is None:
        raise StorageOperationContractError("archive_integrity_scan_not_found")
    bounded_limit = max(1, min(int(limit or FINDING_PAGE_DEFAULT), FINDING_PAGE_MAX))
    query = db.query(ArchiveIntegrityFinding).filter(
        ArchiveIntegrityFinding.scan_id == scan.id,
        ArchiveIntegrityFinding.is_active.is_(True),
    )
    if cursor:
        query = query.filter(ArchiveIntegrityFinding.id > str(cursor))
    if category:
        query = query.filter(ArchiveIntegrityFinding.category == str(category)[:64])
    if impact:
        query = query.filter(ArchiveIntegrityFinding.impact_key == str(impact)[:64])
    rows = query.order_by(ArchiveIntegrityFinding.id.asc()).limit(bounded_limit + 1).all()
    has_more = len(rows) > bounded_limit
    page = rows[:bounded_limit]
    return {
        "scan_id": scan.id,
        "status": scan.status,
        "items": [_public_finding(item, role=role) for item in page],
        "next_cursor": page[-1].id if has_more and page else None,
        "has_more": has_more,
        "limit": bounded_limit,
    }


def latest_integrity_summary_for_status(db: Session) -> dict[str, Any]:
    scan = (
        db.query(ArchiveIntegrityScan)
        .order_by(ArchiveIntegrityScan.created_at.desc(), ArchiveIntegrityScan.id.desc())
        .first()
    )
    if scan is None:
        return {
            "evidence_status": "not_checked",
            "status": "not_run",
            "problem_count": 0,
            "problem_file_count": 0,
            "category_counts": {},
            "last_checked_at": None,
            "active": False,
            "scan_id": None,
        }
    active = scan.status in ACTIVE_SCAN_STATUSES
    return {
        "evidence_status": "running" if active else "stale" if scan.is_stale else scan.status,
        "status": scan.status,
        "problem_count": int(scan.found_count or 0),
        "problem_file_count": int(scan.found_count or 0),
        "category_counts": dict(scan.category_summary or {}),
        "last_checked_at": scan.finished_at.isoformat() if scan.finished_at else scan.heartbeat_at.isoformat() if scan.heartbeat_at else None,
        "active": active,
        "scan_id": scan.id,
        "phase": scan.phase,
        "checked_count": int(scan.checked_count or 0),
        "failed_count": int(scan.failed_count or 0),
    }


def cleanup_old_integrity_generations(db: Session, *, now: datetime | None = None) -> int:
    current = now or database_now(db)
    active_ids = {
        str(value)
        for (value,) in db.query(ArchiveIntegrityScan.id)
        .filter(ArchiveIntegrityScan.active_slot.isnot(None))
        .limit(8)
        .all()
    }
    protected_plan_scan_ids = {
        str(value)
        for (value,) in db.execute(
            # Kept as SQL to avoid importing remediation models into the scan worker hot path.
            text(
                "SELECT DISTINCT scan_id FROM archive_integrity_remediation_plans "
                "WHERE state IN ('prepared', 'running', 'partial', 'blocked') LIMIT 256"
            )
        ).all()
    }
    keep_ids = {
        str(value)
        for (value,) in db.query(ArchiveIntegrityScan.id)
        .order_by(ArchiveIntegrityScan.created_at.desc())
        .limit(SCAN_HISTORY_MAX_ROWS)
        .all()
    }
    candidates = (
        db.query(ArchiveIntegrityScan)
        .filter(
            ArchiveIntegrityScan.finished_at.isnot(None),
            ArchiveIntegrityScan.finished_at < current - timedelta(days=SCAN_HISTORY_DAYS),
        )
        .order_by(ArchiveIntegrityScan.finished_at.asc())
        .limit(SCAN_CLEANUP_BATCH)
        .all()
    )
    deleted = 0
    for row in candidates:
        if row.id in active_ids or row.id in protected_plan_scan_ids or row.id in keep_ids:
            continue
        db.delete(row)
        deleted += 1
    if deleted:
        db.commit()
    return deleted


def legacy_reconciliation_summary(db: Session) -> dict[str, Any]:
    latest = latest_integrity_summary_for_status(db)
    return {
        "mode": "durable_latest_scan",
        "status": latest["status"],
        "scan_id": latest["scan_id"],
        "last_checked_at": latest["last_checked_at"],
        "counts": dict(latest["category_counts"]),
        "problem_count": int(latest["problem_count"]),
        "problem_file_count": int(latest["problem_file_count"]),
        "cleanup_candidate_count": 0,
        "evidence_status": latest["evidence_status"],
        "active": bool(latest["active"]),
        "compatibility": "durable_scan_required",
        "mutated": False,
    }
