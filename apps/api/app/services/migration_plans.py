from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models.archive_migration import ArchiveMigrationItem, ArchiveMigrationPlan
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.services.recording_storage import KMVMS_RECORDINGS_NAMESPACE
from app.services.storage_filesystem import StorageFilesystemError, assert_root_snapshot, is_migration_internal_relative, migration_internal_relative, normalize_relative_path, open_relative_read, stat_relative, verified_root_fd
from app.services.storage_operations_foundation import database_now


from app.services.migration_contracts import PLAN_READY_TTL, PLAN_BATCH_SIZE, PLAN_AUTHORITY_RECHECK_ITEMS, RECENT_WRITE_WINDOW, RESERVE_MIN_BYTES, RESERVE_PERCENT, ACTIVE_JOB_STATES, ELIGIBLE_SEGMENT_STATUSES, PLAN_READY_STATUSES
from app.services.migration_manifest import _sha256_json, _segment_metadata_fingerprint, _item_canonical_payload, _item_manifest_valid


@dataclass(frozen=True)
class MigrationPlanBuilder:
    """Prepare immutable plans; only the worker advances operation state."""

    _require_prepare_authority: Callable
    _root_by_id: Callable
    _hash_descriptor: Callable
    _increment_reason: Callable
    _snapshot_from_plan: Callable
    _plan_permission_contract: Callable
    _audit: Callable

    def _materialize_plan_batch(self, db: Session, plan: ArchiveMigrationPlan) -> bool:
        db.refresh(plan)
        if plan.status != "building" or plan.cancel_requested:
            return False
        self._require_prepare_authority(db, plan)
        source = self._root_by_id(db, plan.source_root_id)
        target = self._root_by_id(db, plan.target_root_id)
        source_expected = self._snapshot_from_plan(plan, source=True)
        target_expected = self._snapshot_from_plan(plan, source=False)
        assert_root_snapshot(source, source_expected, require_write=True)
        assert_root_snapshot(target, target_expected, require_write=True)
        rows = (
            db.query(RecordingSegment)
            .filter(
                RecordingSegment.archive_root_id == plan.source_root_id,
                RecordingSegment.id > int(plan.inventory_cursor or 0),
                RecordingSegment.id <= int(plan.segment_high_watermark or 0),
            )
            .order_by(RecordingSegment.id.asc())
            .limit(PLAN_BATCH_SIZE)
            .all()
        )
        if not rows:
            self._finalize_materialized_plan(db, plan, target_snapshot(target, target_expected))
            return False
        now = database_now(db)
        active_jobs = _active_job_ids(db, rows)
        with verified_root_fd(source, source_expected, require_write=True) as source_fd:
            with verified_root_fd(target, target_expected, require_write=True) as target_fd:
                for row_offset, segment in enumerate(rows):
                    if row_offset % PLAN_AUTHORITY_RECHECK_ITEMS == 0:
                        db.refresh(plan, attribute_names=["status", "cancel_requested"])
                        if plan.status != "building" or plan.cancel_requested:
                            db.rollback()
                            return False
                        self._require_prepare_authority(db, plan)
                    plan.inventory_cursor = int(segment.id)
                    reason = _segment_exclusion_reason(segment, active_job_ids=active_jobs, now=now)
                    if reason:
                        plan.excluded_count = int(plan.excluded_count or 0) + 1
                        plan.excluded_summary = self._increment_reason(plan.excluded_summary, reason)
                        continue
                    relative = normalize_relative_path(str(segment.relative_path))
                    try:
                        source_stat = stat_relative(source_fd, relative)
                    except (OSError, StorageFilesystemError):
                        plan.excluded_count = int(plan.excluded_count or 0) + 1
                        plan.excluded_summary = self._increment_reason(plan.excluded_summary, "source_file_unavailable")
                        continue
                    if not stat.S_ISREG(source_stat.st_mode):
                        plan.excluded_count = int(plan.excluded_count or 0) + 1
                        plan.excluded_summary = self._increment_reason(plan.excluded_summary, "source_file_not_regular")
                        continue
                    try:
                        stat_relative(target_fd, relative)
                    except FileNotFoundError:
                        pass
                    except (OSError, StorageFilesystemError):
                        plan.excluded_count = int(plan.excluded_count or 0) + 1
                        plan.excluded_summary = self._increment_reason(plan.excluded_summary, "target_collision")
                        continue
                    else:
                        plan.excluded_count = int(plan.excluded_count or 0) + 1
                        plan.excluded_summary = self._increment_reason(plan.excluded_summary, "target_collision")
                        continue
                    descriptor = open_relative_read(source_fd, relative)
                    try:
                        source_sha = self._hash_descriptor(descriptor)
                        stable_stat = os.fstat(descriptor)
                    finally:
                        os.close(descriptor)
                    if (
                        int(stable_stat.st_dev) != int(source_stat.st_dev)
                        or int(stable_stat.st_ino) != int(source_stat.st_ino)
                        or int(stable_stat.st_size) != int(source_stat.st_size)
                        or int(stable_stat.st_mtime_ns) != int(source_stat.st_mtime_ns)
                    ):
                        plan.excluded_count = int(plan.excluded_count or 0) + 1
                        plan.excluded_summary = self._increment_reason(plan.excluded_summary, "source_file_changed_during_plan")
                        continue
                    item_id = str(uuid.uuid4())
                    item_index = int(plan.item_count or 0) + 1
                    item = ArchiveMigrationItem(
                        id=item_id,
                        plan_id=str(plan.id),
                        item_index=item_index,
                        segment_id=int(segment.id),
                        camera_id=int(segment.camera_id),
                        camera_name_snapshot=str(segment.camera_name_snapshot or segment.camera_folder_snapshot or "Camera")[:255],
                        source_root_id=str(plan.source_root_id),
                        target_root_id=str(plan.target_root_id),
                        source_physical_identity=str(plan.source_physical_identity),
                        target_physical_identity=str(plan.target_physical_identity),
                        source_snapshot_key=str(plan.source_snapshot_key),
                        target_snapshot_key=str(plan.target_snapshot_key),
                        source_access_identity=str(plan.source_access_identity),
                        target_access_identity=str(plan.target_access_identity),
                        source_relative_path=relative,
                        target_final_relative_path=relative,
                        target_temp_relative_path=migration_internal_relative(str(plan.id), item_id, "target-temp"),
                        source_quarantine_relative_path=migration_internal_relative(str(plan.id), item_id, "source-quarantine"),
                        source_size_bytes=int(source_stat.st_size),
                        source_mtime_ns=int(source_stat.st_mtime_ns),
                        source_device=int(source_stat.st_dev),
                        source_inode=int(source_stat.st_ino),
                        source_mode=int(source_stat.st_mode),
                        source_uid=int(source_stat.st_uid) if hasattr(source_stat, "st_uid") else None,
                        source_gid=int(source_stat.st_gid) if hasattr(source_stat, "st_gid") else None,
                        source_metadata_fingerprint=_segment_metadata_fingerprint(segment),
                        source_sha256=source_sha,
                        intended_transition="source_to_target",
                        phase="planned",
                        cleanup_pending=False,
                        cleanup_status="not_started",
                    )
                    item.canonical_hash = _sha256_json(_item_canonical_payload(item))
                    db.add(item)
                    plan.item_count = item_index
                    plan.total_bytes = int(plan.total_bytes or 0) + int(source_stat.st_size)
                    plan.largest_item_bytes = max(int(plan.largest_item_bytes or 0), int(source_stat.st_size))
        plan.updated_at = now
        db.add(plan)
        db.commit()
        has_more = len(rows) == PLAN_BATCH_SIZE
        if not has_more:
            db.refresh(plan)
            if plan.status != "building" or plan.cancel_requested:
                return False
            self._finalize_materialized_plan(db, plan, target_snapshot(target, target_expected))
        return has_more

    def _canonical_manifest_hash(self, db: Session, plan: ArchiveMigrationPlan) -> str:
        digest = hashlib.sha256()
        digest.update(
            json.dumps(
                {
                    "plan_id": str(plan.id),
                    "schema_version": int(plan.schema_version),
                    "actor_key": str(plan.actor_key),
                    "source_root_id": str(plan.source_root_id),
                    "target_root_id": str(plan.target_root_id),
                    "source_physical_identity": str(plan.source_physical_identity),
                    "target_physical_identity": str(plan.target_physical_identity),
                    "source_snapshot_key": str(plan.source_snapshot_key),
                    "target_snapshot_key": str(plan.target_snapshot_key),
                    "source_access_identity": str(plan.source_access_identity),
                    "target_access_identity": str(plan.target_access_identity),
                    "same_physical_volume": bool(plan.same_physical_volume),
                    "segment_high_watermark": int(plan.segment_high_watermark),
                    "item_count": int(plan.item_count),
                    "total_bytes": int(plan.total_bytes),
                    "permission_contract": self._plan_permission_contract(plan),
                },
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        cursor = 0
        while True:
            rows = (
                db.query(ArchiveMigrationItem.item_index, ArchiveMigrationItem.canonical_hash)
                .filter(ArchiveMigrationItem.plan_id == plan.id, ArchiveMigrationItem.item_index > cursor)
                .order_by(ArchiveMigrationItem.item_index.asc())
                .limit(PLAN_BATCH_SIZE)
                .all()
            )
            if not rows:
                break
            for item_index, canonical_hash in rows:
                digest.update(f"\n{int(item_index)}:{str(canonical_hash)}".encode("ascii"))
                cursor = int(item_index)
        return digest.hexdigest()

    def _manifest_integrity_valid(self, db: Session, plan: ArchiveMigrationPlan) -> bool:
        cursor = 0
        while True:
            rows = (
                db.query(ArchiveMigrationItem)
                .filter(
                    ArchiveMigrationItem.plan_id == plan.id,
                    ArchiveMigrationItem.item_index > cursor,
                )
                .order_by(ArchiveMigrationItem.item_index.asc())
                .limit(PLAN_BATCH_SIZE)
                .all()
            )
            if not rows:
                break
            if any(not _item_manifest_valid(item) for item in rows):
                return False
            cursor = int(rows[-1].item_index)
        return bool(plan.canonical_hash and plan.canonical_hash == self._canonical_manifest_hash(db, plan))

    def _finalize_materialized_plan(self,
        db: Session,
        plan: ArchiveMigrationPlan,
        target_current: dict[str, Any],
    ) -> None:
        plan.capacity_total_bytes = int(target_current["total_bytes"])
        plan.capacity_free_bytes = int(target_current["free_bytes"])
        plan.reserve_bytes = migration_reserve_bytes(plan.capacity_total_bytes)
        plan.required_free_bytes = migration_required_free_bytes(
            same_physical_volume=bool(plan.same_physical_volume),
            remaining_not_target_finalized_bytes=int(plan.total_bytes or 0),
            largest_next_item_size_bytes=int(plan.largest_item_bytes or 0),
            reserve_bytes=int(plan.reserve_bytes),
        )
        plan.canonical_hash = self._canonical_manifest_hash(db, plan)
        now = database_now(db)
        plan.ready_at = now
        plan.expires_at = now + PLAN_READY_TTL
        plan.phase = "ready"
        if int(plan.item_count or 0) == 0:
            plan.status = "blocked"
            plan.reason_code = "migration_no_eligible_recordings"
            plan.next_action = "refresh_plan"
        elif int(plan.capacity_free_bytes) < int(plan.required_free_bytes):
            plan.status = "blocked"
            plan.reason_code = "migration_insufficient_target_space"
            plan.next_action = "free_target_space"
        else:
            plan.status = "ready_with_exclusions" if int(plan.excluded_count or 0) else "ready"
            plan.reason_code = None
            plan.next_action = "confirm_migration"
        plan.updated_at = now
        self._audit(
            db,
            event_type="archive_migration.plan_ready" if plan.status in PLAN_READY_STATUSES else "archive_migration.plan_blocked",
            plan=plan,
            severity="info" if plan.status in PLAN_READY_STATUSES else "warning",
            metadata={
                "item_count": int(plan.item_count),
                "total_bytes": int(plan.total_bytes),
                "excluded_count": int(plan.excluded_count),
                "reason_code": plan.reason_code,
            },
        )
        db.add(plan)
        db.commit()


def migration_reserve_bytes(total_bytes: int) -> int:
    return max(RESERVE_MIN_BYTES, int(math.ceil(max(0, int(total_bytes)) * RESERVE_PERCENT)))


def migration_required_free_bytes(
    *,
    same_physical_volume: bool,
    remaining_not_target_finalized_bytes: int,
    largest_next_item_size_bytes: int,
    reserve_bytes: int,
) -> int:
    growth = largest_next_item_size_bytes if same_physical_volume else remaining_not_target_finalized_bytes
    return max(0, int(growth)) + max(0, int(reserve_bytes))


def _active_job_ids(db: Session, rows: list[RecordingSegment]) -> set[str]:
    job_ids = sorted({str(item.job_id) for item in rows if item.job_id})
    if not job_ids:
        return set()
    return {
        str(value)
        for (value,) in db.query(RecordingJob.id)
        .filter(RecordingJob.id.in_(job_ids), RecordingJob.state.in_(tuple(ACTIVE_JOB_STATES)))
        .all()
    }


def _segment_exclusion_reason(
    segment: RecordingSegment,
    *,
    active_job_ids: set[str],
    now: datetime,
) -> str | None:
    if segment.deleted_at is not None or str(segment.status or "") == "deleted":
        return "recording_deleted"
    if segment.ownership != "KM VMS" or segment.source != "recorder":
        return "recording_ownership_untrusted"
    if str(segment.status or "") not in ELIGIBLE_SEGMENT_STATUSES:
        return "recording_not_finalized"
    anchor = segment.updated_at or segment.finalized_at or segment.created_at or segment.started_at
    if (segment.job_id and str(segment.job_id) in active_job_ids) or (anchor and now - anchor < RECENT_WRITE_WINDOW):
        return "recording_active_or_recent"
    try:
        relative = normalize_relative_path(str(segment.relative_path or ""))
    except StorageFilesystemError:
        return "recording_path_invalid"
    if is_migration_internal_relative(relative):
        return "migration_internal_object"
    if not (
        relative == KMVMS_RECORDINGS_NAMESPACE
        or relative.startswith(f"{KMVMS_RECORDINGS_NAMESPACE}/")
    ):
        return "recording_path_outside_archive"
    return None


def target_snapshot(target: ArchiveRoot, expected: dict[str, Any]) -> dict[str, Any]:
    return assert_root_snapshot(target, expected, require_write=True)
