"""Immutable migration rules and typed outcomes; no operation state."""
from __future__ import annotations
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable
from app.services.storage_operations_foundation import safe_reason_code

MIGRATION_OPERATION_TYPE = "archive_migration_apply"
MIGRATION_CLEANUP_CONTINUATION_MODE = "migration_cleanup_same_operation"
MIGRATION_PARENT_SNAPSHOT_MAX_BYTES = 1024
MIGRATION_PLAN_SCHEMA_VERSION = 1
PLAN_READY_TTL = timedelta(hours=2)
PLAN_HISTORY_TTL = timedelta(days=30)
PLAN_BATCH_SIZE = 256
PLAN_AUTHORITY_RECHECK_ITEMS = 16
ITEM_PAGE_MAX = 100
WORKER_POLL_SECONDS = 1.0
WORKER_LEASE_SECONDS = 30
WORKER_CANDIDATE_BATCH_SIZE = 32
WORKER_CANDIDATE_DIAGNOSTIC_INTERVAL_SECONDS = 60.0
WORKER_CANDIDATE_DIAGNOSTIC_MAX_KEYS = 128
COPY_CHUNK_BYTES = 1024 * 1024
TEMP_CREATE_MODE = 0o600
THROUGHPUT_MAX_SAMPLES = 8
THROUGHPUT_SAMPLE_INTERVAL_SECONDS = 0.5
THROUGHPUT_MIN_SAMPLES = 3
THROUGHPUT_MIN_WINDOW_SECONDS = 1.0
THROUGHPUT_MIN_BYTES = 1024 * 1024
THROUGHPUT_STALE_SECONDS = 5.0
THROUGHPUT_MAX_BYTES_PER_SECOND = 10 * 1024**4
THROUGHPUT_MAX_ETA_SECONDS = 365 * 24 * 60 * 60
RECENT_WRITE_WINDOW = timedelta(minutes=15)
RESERVE_MIN_BYTES = 1024 * 1024 * 1024
RESERVE_PERCENT = 0.01
ACTIVE_JOB_STATES = frozenset({"starting", "recording", "stopping", "restarting"})
ORPHAN_EXECUTION_AUDIT_EVENTS = frozenset(
    {
        "storage_operation.started",
        "storage_operation.taken_over",
        "storage_operation.cancel_requested",
        "storage_operation.cancelled",
        "storage_operation.finished",
    }
)
INITIAL_ORPHAN_EXECUTION_AUDIT_EVENTS = frozenset(
    {
        "archive_migration.apply_started",
        "archive_migration.cancel_requested",
        "archive_migration.operation_completed",
        "archive_migration.operation_partial",
        "archive_migration.operation_blocked",
        "archive_migration.operation_failed",
        "archive_migration.operation_cancelled",
    }
)
ELIGIBLE_SEGMENT_STATUSES = frozenset({"finalized"})
PLAN_READY_STATUSES = frozenset({"ready", "ready_with_exclusions"})
PLAN_TERMINAL_STATUSES = frozenset({"completed", "partial", "blocked", "failed", "cancelled", "expired"})
ITEM_TERMINAL_PHASES = frozenset({"completed", "cancelled", "blocked", "failed"})
CLEANUP_RECOVERY_ITEM_PHASES = frozenset(
    {
        "target_temp_create_pending",
        "copying",
        "target_temp_written",
        "target_verified",
        "target_finalized",
        "metadata_switched",
        "source_cleanup_pending",
        "source_quarantined",
        "source_delete_committing",
    }
)


@dataclass(frozen=True)
class MigrationRecoveryDecision:
    allowed: bool
    action: str | None
    reason_code: str | None = None
    existing_child_id: str | None = None
MIGRATION_FILESYSTEM_REASON_CODES = frozenset(
    {
        "archive_object_not_regular_file",
        "archive_path_component_not_directory",
        "archive_relative_path_invalid",
        "archive_root_identity_changed",
        "archive_root_not_readable",
        "archive_root_not_writable",
        "archive_root_open_failed",
        "archive_root_physical_identity_unknown",
        "archive_root_runtime_not_directory",
        "archive_root_runtime_unavailable",
        "archive_target_collision",
        "migration_internal_identity_invalid",
        "migration_internal_kind_invalid",
        "migration_internal_namespace_forbidden",
    }
)


class ArchiveMigrationBlocked(RuntimeError):
    def __init__(self, reason_code: str, *, retry_mode: str | None = "refresh"):
        self.reason_code = safe_reason_code(reason_code, fallback="migration_operation_failed")
        self.retry_mode = retry_mode
        super().__init__(self.reason_code)


class _MigrationAuditPersistenceFailed(ArchiveMigrationBlocked):
    def __init__(self):
        super().__init__("migration_audit_persistence_failed", retry_mode=None)


class ArchiveMigrationPartial(ArchiveMigrationBlocked):
    pass


class _WorkerCandidateRejected(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = safe_reason_code(
            reason_code,
            fallback="migration_worker_candidate_rejected",
        )
        super().__init__(self.reason_code)


class _CopyThroughputSampler:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque(maxlen=THROUGHPUT_MAX_SAMPLES)

    def observe(self, copied_bytes: int) -> None:
        now = float(self._clock())
        copied = max(0, int(copied_bytes))
        if self._samples:
            previous_time, previous_bytes = self._samples[-1]
            if copied < previous_bytes:
                self._samples.clear()
            elif now - previous_time < THROUGHPUT_SAMPLE_INTERVAL_SECONDS:
                return
        self._samples.append((now, copied))

    def values(self, *, remaining_bytes: int) -> tuple[int | None, int | None]:
        if len(self._samples) < THROUGHPUT_MIN_SAMPLES:
            return None, None
        now = float(self._clock())
        first_time, first_bytes = self._samples[0]
        last_time, last_bytes = self._samples[-1]
        elapsed = last_time - first_time
        copied = last_bytes - first_bytes
        if (
            now - last_time > THROUGHPUT_STALE_SECONDS
            or elapsed < THROUGHPUT_MIN_WINDOW_SECONDS
            or copied < THROUGHPUT_MIN_BYTES
        ):
            return None, None
        speed = int(copied / elapsed) if elapsed > 0 else 0
        if speed <= 0 or speed > THROUGHPUT_MAX_BYTES_PER_SECOND:
            return None, None
        remaining = max(0, int(remaining_bytes))
        eta = int(math.ceil(remaining / speed)) if remaining else 0
        if eta > THROUGHPUT_MAX_ETA_SECONDS:
            eta = None
        return speed, eta
