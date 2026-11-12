from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text

from .contracts import (
    METADATA_SOURCE_RECORDER, OWNERSHIP_KM_VMS, RECORDER_RECEIPT_CONTRACT_VERSION,
    RECEIPT_FINGERPRINT_CHUNK_BYTES, RETENTION_SIGNAL_SCOPE_JSON, RETENTION_SIGNAL_SCOPE_KEY,
    RETENTION_SIGNAL_TYPE, SEGMENT_STATUS_FAILED, SEGMENT_STATUS_FINALIZED,
    SEGMENT_STATUS_WRITING, STORAGE_NAMESPACE,
)
from .segment_recovery import same_file_version

def bounded_file_fingerprint(file_path: Path, stat_result: os.stat_result) -> str:
    digest = hashlib.sha256()
    digest.update(f"v1:{int(stat_result.st_size)}:{int(stat_result.st_mtime_ns)}".encode("ascii"))
    with file_path.open("rb") as handle:
        digest.update(handle.read(RECEIPT_FINGERPRINT_CHUNK_BYTES))
        if stat_result.st_size > RECEIPT_FINGERPRINT_CHUNK_BYTES:
            handle.seek(max(0, int(stat_result.st_size) - RECEIPT_FINGERPRINT_CHUNK_BYTES))
            digest.update(handle.read(RECEIPT_FINGERPRINT_CHUNK_BYTES))
    return digest.hexdigest()

def receipt_object_identity(root_id: str, relative_path: str, stat_result: os.stat_result) -> str:
    canonical = ":".join(
        (
            "v1",
            str(root_id),
            str(relative_path),
            str(int(stat_result.st_dev)),
            str(int(stat_result.st_ino)),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SegmentAccounting:
    """Own segment + receipt transactions. The orchestrator owns process state."""

    engine: Any
    default_record_stream: str
    media_stall_seconds: int
    storage_relative_path: Callable
    parse_segment_start_time: Callable
    path_format_metadata: Callable
    remember_processed_segment: Callable
    redact_text: Callable
    truncate_error: Callable
    classify_error: Callable
    log_event: Callable
    write_audit_event: Callable

    def create_segment_metadata_if_needed(self, job: Any, file_path: Path) -> None:
        if not job.db_job_id or not job.archive_root_id:
            return
        rel_path = self.storage_relative_path(file_path, root=job.archive_root_path)
        if not rel_path or rel_path in job.segment_baseline_paths:
            return
        if rel_path in job.known_segment_paths:
            return

        try:
            stat = file_path.stat()
        except Exception as exc:
            self.log_event("warning", "metadata_write_failed", camera_id=job.camera_id, camera_name=job.camera_name, path=str(file_path), error=str(exc))
            return

        started_at = self.parse_segment_start_time(file_path)
        media_metadata = self.path_format_metadata(file_path)
        with self.engine.begin() as conn:
            existing = conn.execute(
                text(
                    """
                    SELECT id, status
                    FROM recording_segments
                    WHERE (job_id = :job_id AND relative_path = :relative_path)
                       OR (
                            relative_path = :relative_path
                            AND archive_root_id = :archive_root_id
                            AND ownership = :ownership
                            AND source = :source
                       )
                    LIMIT 1
                    """
                ),
                {
                    "job_id": job.db_job_id,
                    "relative_path": rel_path,
                    "archive_root_id": job.archive_root_id,
                    "ownership": OWNERSHIP_KM_VMS,
                    "source": METADATA_SOURCE_RECORDER,
                },
            ).first()
            if existing:
                job.known_segment_paths.add(rel_path)
                if getattr(existing, "status", SEGMENT_STATUS_WRITING) != SEGMENT_STATUS_WRITING:
                    self.remember_processed_segment(job, file_path)
                return

            try:
                content_fingerprint = bounded_file_fingerprint(file_path, stat)
            except OSError:
                content_fingerprint = hashlib.sha256(
                    f"unavailable:{int(stat.st_size)}:{int(stat.st_mtime_ns)}".encode("ascii")
                ).hexdigest()
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO recording_segments (
                        job_id,
                        camera_id,
                        camera_name_snapshot,
                        camera_folder_snapshot,
                        file_path,
                        relative_path,
                        started_at,
                        ended_at,
                        duration_sec,
                        size_bytes,
                        stream_type,
                        status,
                        ownership,
                        source,
                        archive_root_id,
                        archive_root_resolution_status,
                        archive_root_resolution_detail,
                        archive_root_resolved_at,
                        storage_namespace,
                        container_format,
                        file_extension,
                        mime_type,
                        integrity_status,
                        file_size_verified_at,
                        media_progress_at,
                        file_mtime,
                        cleanup_candidate,
                        reconciliation_status,
                        created_at,
                        updated_at
                    ) VALUES (
                        :job_id,
                        :camera_id,
                        :camera_name,
                        :camera_folder,
                        :file_path,
                        :relative_path,
                        :started_at,
                        NULL,
                        0,
                        :size_bytes,
                        :stream_type,
                        :status,
                        :ownership,
                        :source,
                        :archive_root_id,
                        :archive_root_resolution_status,
                        NULL,
                        NOW(),
                        :storage_namespace,
                        :container_format,
                        :file_extension,
                        :mime_type,
                        :integrity_status,
                        NOW(),
                        :media_progress_at,
                        :file_mtime,
                        FALSE,
                        :reconciliation_status,
                        NOW(),
                        NOW()
                    )
                    RETURNING id
                    """
                ),
                {
                    "job_id": job.db_job_id,
                    "camera_id": job.camera_id,
                    "camera_name": self.redact_text(job.camera_name),
                    "camera_folder": job.folder_name,
                    "file_path": str(file_path),
                    "relative_path": rel_path,
                    "started_at": started_at,
                    "size_bytes": max(int(stat.st_size), 0),
                    "stream_type": job.source_stream or self.default_record_stream,
                    "status": SEGMENT_STATUS_WRITING,
                    "ownership": OWNERSHIP_KM_VMS,
                    "source": METADATA_SOURCE_RECORDER,
                    "archive_root_id": job.archive_root_id,
                    "archive_root_resolution_status": "resolved",
                    "storage_namespace": STORAGE_NAMESPACE,
                    "container_format": media_metadata["container_format"],
                    "file_extension": media_metadata["file_extension"],
                    "mime_type": media_metadata["mime_type"],
                    "integrity_status": SEGMENT_STATUS_WRITING,
                    "media_progress_at": datetime.utcnow() if stat.st_size > 0 else None,
                    "file_mtime": datetime.fromtimestamp(stat.st_mtime, UTC).replace(tzinfo=None),
                    "reconciliation_status": "pending",
                },
            ).first()
            if inserted is None:
                return
            physical_identity = conn.execute(
                text("SELECT physical_identity FROM archive_roots WHERE id = :root_id"),
                {"root_id": job.archive_root_id},
            ).scalar()
            conn.execute(
                text(
                    """
                    INSERT INTO recorder_file_receipts (
                        id,
                        contract_version,
                        segment_id,
                        job_id,
                        camera_id,
                        root_id,
                        physical_identity,
                        relative_path,
                        state,
                        object_identity,
                        device_id,
                        inode,
                        size_bytes,
                        mtime_ns,
                        content_fingerprint,
                        created_at,
                        updated_at
                    ) VALUES (
                        :id,
                        :contract_version,
                        :segment_id,
                        :job_id,
                        :camera_id,
                        :root_id,
                        :physical_identity,
                        :relative_path,
                        'writing',
                        :object_identity,
                        :device_id,
                        :inode,
                        :size_bytes,
                        :mtime_ns,
                        :content_fingerprint,
                        NOW(),
                        NOW()
                    )
                    ON CONFLICT (segment_id) DO NOTHING
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "contract_version": RECORDER_RECEIPT_CONTRACT_VERSION,
                    "segment_id": int(inserted.id),
                    "job_id": job.db_job_id,
                    "camera_id": job.camera_id,
                    "root_id": job.archive_root_id,
                    "physical_identity": physical_identity,
                    "relative_path": rel_path,
                    "object_identity": receipt_object_identity(job.archive_root_id, rel_path, stat),
                    "device_id": str(int(stat.st_dev)),
                    "inode": str(int(stat.st_ino)),
                    "size_bytes": max(int(stat.st_size), 0),
                    "mtime_ns": int(stat.st_mtime_ns),
                    "content_fingerprint": content_fingerprint,
                },
            )
        job.known_segment_paths.add(rel_path)
        if stat.st_size > 0:
            job.last_media_progress_at = time.time()
            if job.stable_progress_since is None:
                job.stable_progress_since = job.last_media_progress_at
            job.current_media_size = int(stat.st_size)
            job.current_media_relative_path = rel_path
        self.log_event("info", "segment_detected", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, relative_path=rel_path, size_bytes=stat.st_size)

    def update_writing_segment_progress(self, job: Any, file_path: Path) -> bool:
        if not job.db_job_id:
            return False
        rel_path = self.storage_relative_path(file_path, root=job.archive_root_path)
        if not rel_path or rel_path not in job.known_segment_paths:
            return False
        try:
            stat = file_path.stat()
        except OSError:
            return False
        if stat.st_size <= 0:
            return False
        file_mtime = datetime.fromtimestamp(stat.st_mtime, UTC).replace(tzinfo=None)
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE recording_segments
                    SET size_bytes = :size_bytes,
                        file_mtime = :file_mtime,
                        file_size_verified_at = timezone('UTC', NOW()),
                        media_progress_at = timezone('UTC', NOW()),
                        updated_at = timezone('UTC', NOW())
                    WHERE job_id = :job_id
                      AND relative_path = :relative_path
                      AND archive_root_id = :archive_root_id
                      AND ownership = :ownership
                      AND source = :source
                      AND status = :writing
                      AND (size_bytes <> :size_bytes OR file_mtime IS DISTINCT FROM :file_mtime)
                    """
                ),
                {
                    "job_id": job.db_job_id,
                    "relative_path": rel_path,
                    "archive_root_id": job.archive_root_id,
                    "ownership": OWNERSHIP_KM_VMS,
                    "source": METADATA_SOURCE_RECORDER,
                    "writing": SEGMENT_STATUS_WRITING,
                    "size_bytes": int(stat.st_size),
                    "file_mtime": file_mtime,
                },
            )
        if result.rowcount <= 0:
            return False
        now = time.time()
        if job.last_media_progress_at is None or now - job.last_media_progress_at > self.media_stall_seconds:
            job.stable_progress_since = now
        job.last_media_progress_at = now
        job.current_media_size = int(stat.st_size)
        job.current_media_relative_path = rel_path
        return True

    def finalize_segment_path(self, job: Any, file_path: Path, *, verified_stat: os.stat_result | None = None) -> bool:
        if not job.db_job_id:
            return False
        rel_path = self.storage_relative_path(file_path, root=job.archive_root_path)
        if not rel_path:
            return False
        if rel_path in job.segment_baseline_paths or rel_path not in job.known_segment_paths:
            return False
        try:
            stat = file_path.lstat() if verified_stat is not None else file_path.stat()
            if verified_stat is not None and not same_file_version(verified_stat, stat):
                return False
        except Exception as exc:
            self.mark_segment_failed(job, rel_path, f"segment stat failed: {exc}")
            self.remember_processed_segment(job, file_path)
            return False
        if stat.st_size <= 0:
            self.mark_segment_failed(job, rel_path, "segment file is empty")
            self.remember_processed_segment(job, file_path)
            return False

        started_at = self.parse_segment_start_time(file_path)
        ended_at = datetime.fromtimestamp(stat.st_mtime)
        duration_sec = max(int((ended_at - started_at).total_seconds()), 0)
        media_metadata = self.path_format_metadata(file_path)
        try:
            final_fingerprint = bounded_file_fingerprint(file_path, stat)
        except OSError:
            if verified_stat is not None:
                return False
            final_fingerprint = hashlib.sha256(
                f"unavailable:{int(stat.st_size)}:{int(stat.st_mtime_ns)}".encode("ascii")
            ).hexdigest()
        if verified_stat is not None and not same_file_version(verified_stat, file_path.lstat()):
            return False
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE recording_segments
                    SET status = :status,
                        ended_at = :ended_at,
                        finalized_at = NOW(),
                        duration_sec = :duration_sec,
                        size_bytes = :size_bytes,
                        error_message = NULL,
                        storage_namespace = COALESCE(storage_namespace, :storage_namespace),
                        integrity_status = :integrity_status,
                        integrity_error = NULL,
                        last_integrity_check_at = NOW(),
                        file_size_verified_at = NOW(),
                        file_mtime = :file_mtime,
                        content_probe_status = :content_probe_status,
                        container_format = :container_format,
                        file_extension = :file_extension,
                        mime_type = :mime_type,
                        cleanup_candidate = FALSE,
                        cleanup_reason = NULL,
                        reconciliation_status = :reconciliation_status,
                        reconciliation_checked_at = NOW(),
                        updated_at = NOW()
                    WHERE job_id = :job_id
                      AND relative_path = :relative_path
                      AND archive_root_id = :archive_root_id
                      AND ownership = :ownership
                      AND source = :source
                      AND status = :writing
                    RETURNING id
                    """
                ),
                {
                    "job_id": job.db_job_id,
                    "relative_path": rel_path,
                    "archive_root_id": job.archive_root_id,
                    "ownership": OWNERSHIP_KM_VMS,
                    "source": METADATA_SOURCE_RECORDER,
                    "writing": SEGMENT_STATUS_WRITING,
                    "status": SEGMENT_STATUS_FINALIZED,
                    "ended_at": ended_at,
                    "duration_sec": duration_sec,
                    "size_bytes": int(stat.st_size),
                    "storage_namespace": STORAGE_NAMESPACE,
                    "integrity_status": "ok_owned_finalized",
                    "file_mtime": ended_at,
                    "content_probe_status": "probe_ok" if verified_stat is not None else "stat_ok",
                    "container_format": media_metadata["container_format"],
                    "file_extension": media_metadata["file_extension"],
                    "mime_type": media_metadata["mime_type"],
                    "reconciliation_status": "ok_owned_finalized",
                },
            )
            finalized_row = result.first()
            if finalized_row is not None:
                physical_identity = conn.execute(
                    text("SELECT physical_identity FROM archive_roots WHERE id = :root_id"),
                    {"root_id": job.archive_root_id},
                ).scalar()
                receipt_result = conn.execute(
                    text(
                        """
                        UPDATE recorder_file_receipts
                        SET state = 'finalized',
                            physical_identity = :physical_identity,
                            object_identity = :object_identity,
                            device_id = :device_id,
                            inode = :inode,
                            size_bytes = :size_bytes,
                            mtime_ns = :mtime_ns,
                            content_fingerprint = :content_fingerprint,
                            finalized_at = NOW(),
                            updated_at = NOW()
                        WHERE segment_id = :segment_id
                          AND contract_version = :contract_version
                          AND root_id = :root_id
                          AND physical_identity IS NOT DISTINCT FROM :physical_identity
                          AND relative_path = :relative_path
                          AND object_identity = :object_identity
                          AND device_id IS NOT DISTINCT FROM :device_id
                          AND inode IS NOT DISTINCT FROM :inode
                          AND state = 'writing'
                        """
                    ),
                    {
                        "physical_identity": physical_identity,
                        "object_identity": receipt_object_identity(job.archive_root_id, rel_path, stat),
                        "device_id": str(int(stat.st_dev)),
                        "inode": str(int(stat.st_ino)),
                        "size_bytes": int(stat.st_size),
                        "mtime_ns": int(stat.st_mtime_ns),
                        "content_fingerprint": final_fingerprint,
                        "segment_id": int(finalized_row.id),
                        "contract_version": RECORDER_RECEIPT_CONTRACT_VERSION,
                        "root_id": job.archive_root_id,
                        "relative_path": rel_path,
                    },
                )
                if receipt_result.rowcount != 1:
                    raise RuntimeError("recorder_finalization_receipt_mismatch")
                conn.execute(
                    text(
                        """
                        INSERT INTO storage_work_signals (
                            signal_type,
                            scope_key,
                            scope,
                            status,
                            requested_watermark,
                            consumed_watermark,
                            claimed_watermark,
                            owner_token_hash,
                            owner_instance_id,
                            fencing_token,
                            revision,
                            lease_expires_at,
                            heartbeat_at,
                            created_at,
                            updated_at
                        ) VALUES (
                            :signal_type,
                            :scope_key,
                            CAST(:scope AS JSON),
                            'pending',
                            :watermark,
                            0,
                            NULL,
                            NULL,
                            NULL,
                            0,
                            1,
                            NULL,
                            NULL,
                            NOW(),
                            NOW()
                        )
                        ON CONFLICT (signal_type, scope_key) DO UPDATE SET
                            requested_watermark = GREATEST(
                                storage_work_signals.requested_watermark + 1,
                                :watermark
                            ),
                            status = CASE
                                WHEN storage_work_signals.status = 'running' THEN 'running'
                                ELSE 'pending'
                            END,
                            revision = storage_work_signals.revision + 1,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "signal_type": RETENTION_SIGNAL_TYPE,
                        "scope_key": RETENTION_SIGNAL_SCOPE_KEY,
                        "scope": RETENTION_SIGNAL_SCOPE_JSON,
                        "watermark": int(finalized_row.id),
                    },
                )
        self.remember_processed_segment(job, file_path)
        if finalized_row is None:
            return False
        self.log_event("info", "segment_finalized", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, relative_path=rel_path, size_bytes=stat.st_size, duration_sec=duration_sec)
        return True

    def mark_segment_failed(self, job: Any, rel_path: str, error: str) -> None:
        if not job.db_job_id:
            return
        if rel_path in job.segment_baseline_paths or rel_path not in job.known_segment_paths:
            return
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE recording_segments
                    SET status = :status,
                        ended_at = COALESCE(ended_at, NOW()),
                        error_message = :error_message,
                        integrity_status = :integrity_status,
                        integrity_error = :error_message,
                        last_integrity_check_at = NOW(),
                        cleanup_candidate = FALSE,
                        reconciliation_status = :reconciliation_status,
                        reconciliation_checked_at = NOW(),
                        updated_at = NOW()
                    WHERE job_id = :job_id
                      AND relative_path = :relative_path
                      AND archive_root_id = :archive_root_id
                      AND ownership = :ownership
                      AND source = :source
                      AND status = :writing
                    """
                ),
                {
                    "job_id": job.db_job_id,
                    "relative_path": rel_path,
                    "archive_root_id": job.archive_root_id,
                    "ownership": OWNERSHIP_KM_VMS,
                    "source": METADATA_SOURCE_RECORDER,
                    "writing": SEGMENT_STATUS_WRITING,
                    "status": SEGMENT_STATUS_FAILED,
                    "error_message": self.truncate_error(self.redact_text(error)),
                    "integrity_status": "partial_file",
                    "reconciliation_status": "partial_file",
                },
            )
        if result.rowcount <= 0:
            return
        self.log_event("warning", "segment_failed", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, relative_path=rel_path, error=error)
        self.write_audit_event(
            event_type="segment_failed",
            severity="warning",
            message=f"Recorder segment failed for camera {job.camera_name}",
            camera_id=job.camera_id,
            camera_name=job.camera_name,
            metadata={"job_id": job.db_job_id, "error_type": self.classify_error(error), "segment_name": Path(rel_path).name},
        )
