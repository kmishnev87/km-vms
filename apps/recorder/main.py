from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from recorder_core.contracts import (
    OWNERSHIP_KM_VMS,
    METADATA_SOURCE_RECORDER,
    SEGMENT_STATUS_WRITING,
    SEGMENT_STATUS_FINALIZED,
    SEGMENT_STATUS_FAILED,
    SEGMENT_STATUS_STALE_WRITING,
    STORAGE_NAMESPACE,
    DEFAULT_ARCHIVE_ROOT_ID,
    RECORDING_FORMATS,
    DEFAULT_RECORDING_FORMAT,
    FORMAT_METADATA,
    RETENTION_SIGNAL_TYPE,
    RETENTION_SIGNAL_SCOPE,
    RETENTION_SIGNAL_SCOPE_JSON,
    RETENTION_SIGNAL_SCOPE_KEY,
    RECORDER_RECEIPT_CONTRACT_VERSION,
    RECEIPT_FINGERPRINT_CHUNK_BYTES,
    SEGMENT_METADATA_BATCH_SIZE,
    SEGMENT_DISCOVERY_REFRESH_SECONDS,
    RecorderState,
    UNSET,
    ERROR_CLEARING_STATES,
    EXIT_CODE_CLEARING_STATES,
)
from recorder_core.job_store import RecordingJobStore
from recorder_core.segment_accounting import SegmentAccounting, bounded_file_fingerprint, receipt_object_identity

DATABASE_URL = os.getenv("DATABASE_URL")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/storage/archive"))
FFMPEG_LOGLEVEL = os.getenv("FFMPEG_LOGLEVEL", "warning")
DEFAULT_RECORD_STREAM = os.getenv("DEFAULT_RECORD_STREAM", "main").lower()
LOOP_INTERVAL_SECONDS = 5
STOP_TIMEOUT_SECONDS = 15
STDERR_TAIL_LINES = 80
MAX_ERROR_LENGTH = 2000
BACKOFF_STEPS_SECONDS = (10, 20, 30, 60, 120)
STARTUP_CONFIRM_SECONDS = 5
MEDIA_STALL_SECONDS = 60
MEDIA_STALL_SAMPLES = 2
RECOVERY_STABLE_SECONDS = 60
BLOCKED_RETRY_SECONDS = 300
MEDIA_PROGRESS_HEARTBEAT_GRACE_SECONDS = MEDIA_STALL_SECONDS

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
RECORDER_INSTANCE_ID = os.getenv("RECORDER_INSTANCE_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
RECORDER_STARTED_AT = time.time()



ERROR_INVALID_RTSP = "invalid_rtsp"
ERROR_NETWORK_TIMEOUT = "network_timeout"
ERROR_AUTH_FAILED = "auth_failed"
ERROR_FFMPEG_NOT_FOUND = "ffmpeg_not_found"
ERROR_STORAGE_UNAVAILABLE = "storage_unavailable"
ERROR_PERMISSION_DENIED = "permission_denied"
ERROR_PROCESS_CRASHED = "process_crashed"
ERROR_PROCESS_START_FAILED = "process_start_failed"
ERROR_DUPLICATE_PROCESS = "duplicate_process_prevented"
ERROR_UNKNOWN_FFMPEG = "unknown_ffmpeg_error"
ERROR_STALE_SEGMENT = "recording_segment_not_rotating"
ERROR_MEDIA_STALLED = "recording_media_stalled"

running = True
jobs: dict[int, "RecordingJob"] = {}
last_runtime_mapping_log_key: tuple[str, str, str] | None = None


@dataclass
class RecordingJob:
    camera_id: int
    camera_name: str
    folder_name: str
    recording_mode: str
    enabled: bool
    state: str = RecorderState.IDLE
    process: subprocess.Popen | None = None
    pid: int | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    last_state_change_at: float = field(default_factory=time.time)
    last_error: str | None = None
    last_error_type: str | None = None
    last_exit_code: int | None = None
    restart_count: int = 0
    retry_count: int = 0
    last_retry_at: float | None = None
    next_retry_at: float | None = None
    current_output_path: str | None = None
    db_job_id: str | None = None
    pending_db_job_id: str | None = None
    source_stream: str | None = None
    recording_format: str = DEFAULT_RECORDING_FORMAT
    format_change_target: str | None = None
    format_change_anchor: str | None = None
    format_change_requested_epoch: float | None = None
    format_change_retry_at: float = 0
    segment_baseline_paths: set[str] = field(default_factory=set)
    known_segment_paths: set[str] = field(default_factory=set)
    segment_directory_signature: tuple | None = None
    segment_directory_checked_at: float = 0
    segment_discovery_cache: dict[str, Path] = field(default_factory=dict)
    processed_segment_names: set[str] = field(default_factory=set)
    archive_root_id: str | None = None
    archive_root_path: Path = STORAGE_ROOT
    config_signature: tuple[Any, ...] | None = None
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES))
    stderr_thread: threading.Thread | None = None
    stale_recovery_count: int = 0
    last_stale_recovery_at: float | None = None
    last_media_progress_at: float | None = None
    current_media_size: int = 0
    current_media_relative_path: str | None = None
    stalled_samples: int = 0
    stable_progress_since: float | None = None

    def set_state(self, state: str, error: str | None = None, error_type: str | None = None) -> None:
        if self.state != state:
            self.last_state_change_at = time.time()
        self.state = state
        if error is not None:
            self.last_error = truncate_error(redact_text(error))
        elif state in ERROR_CLEARING_STATES:
            self.last_error = None
        if error_type is not None:
            self.last_error_type = error_type
        elif state in ERROR_CLEARING_STATES:
            self.last_error_type = None
        if state in EXIT_CODE_CLEARING_STATES and error is None:
            self.last_exit_code = None

    def status_payload(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "state": self.state,
            "pid": self.pid,
            "started_at": iso_ts(self.started_at),
            "stopped_at": iso_ts(self.stopped_at),
            "last_state_change_at": iso_ts(self.last_state_change_at),
            "last_error": self.last_error,
            "last_error_type": self.last_error_type,
            "last_exit_code": self.last_exit_code,
            "restart_count": self.restart_count,
            "retry_count": self.retry_count,
            "last_retry_at": iso_ts(self.last_retry_at),
            "next_retry_at": iso_ts(self.next_retry_at),
            "current_output_path": self.current_output_path,
            "recording_mode": self.recording_mode,
            "enabled": self.enabled,
            "stale_recovery_count": self.stale_recovery_count,
            "last_stale_recovery_at": iso_ts(self.last_stale_recovery_at),
            "confirmed_recording": bool(
                self.state == RecorderState.RECORDING
                and self.last_media_progress_at
                and time.time() - self.last_media_progress_at <= MEDIA_PROGRESS_HEARTBEAT_GRACE_SECONDS
            ),
            "last_media_progress_at": iso_ts(self.last_media_progress_at),
        }


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def iso_ts(value: float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def safe_name(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r'[\\\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value[:120].strip("_") or "camera"


def truncate_error(value: str | None) -> str:
    text_value = str(value or "").strip()
    if len(text_value) <= MAX_ERROR_LENGTH:
        return text_value
    return text_value[-MAX_ERROR_LENGTH:]






def redact_text(value: str | None) -> str:
    if value is None:
        return ""
    text_value = str(value)
    text_value = re.sub(r"(rtsp://[^:\s/@]+):([^@\s]+)@", r"\1:***@", text_value, flags=re.IGNORECASE)
    text_value = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", text_value, flags=re.IGNORECASE)
    text_value = re.sub(r"([?&](?:token|access_token|refresh_token|media_token)=)[^&\s]+", r"\1***", text_value, flags=re.IGNORECASE)
    text_value = re.sub(r"(postgresql(?:\+\w+)?://[^:\s/@]+):([^@\s]+)@", r"\1:***@", text_value, flags=re.IGNORECASE)
    text_value = re.sub(r"((?:Cookie|Set-Cookie):\s*)[^\r\n;]+", r"\1***", text_value, flags=re.IGNORECASE)
    return text_value


def sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if re.search(r"(password|secret|token|authorization|jwt|encryption_key|key|credential|cookie)", str(key), re.IGNORECASE):
                result[str(key)] = "***"
            else:
                result[str(key)] = sanitize_metadata(item)
        return result
    if isinstance(value, list):
        return [sanitize_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_metadata(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


def log_event(level: str, event: str, **fields: Any) -> None:
    payload = {
        "ts": now_iso(),
        "level": level,
        "event": event,
        **sanitize_metadata(fields),
    }
    print("[RECORDER] " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def write_audit_event(
    *,
    event_type: str,
    severity: str = "info",
    message: str,
    camera_id: int | None = None,
    camera_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    sanitized_metadata = sanitize_metadata(metadata or {})
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        id,
                        created_at,
                        actor_user_id,
                        actor_username,
                        actor_role,
                        category,
                        event_type,
                        severity,
                        message_ru,
                        message_en,
                        target_type,
                        target_id,
                        target_name,
                        metadata,
                        ip_address,
                        user_agent
                    )
                    VALUES (
                        :id,
                        NOW(),
                        NULL,
                        'recorder',
                        'system',
                        'recorder',
                        :event_type,
                        :severity,
                        :message_ru,
                        :message_en,
                        'camera',
                        :target_id,
                        :target_name,
                        CAST(:metadata AS JSON),
                        NULL,
                        NULL
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "event_type": f"recorder.{event_type}",
                    "severity": severity if severity in {"info", "warning", "error", "security"} else "info",
                    "message_ru": redact_text(message),
                    "message_en": redact_text(message),
                    "target_id": str(camera_id) if camera_id is not None else None,
                    "target_name": redact_text(camera_name) if camera_name else None,
                    "metadata": json.dumps(sanitized_metadata, ensure_ascii=False),
                },
            )
    except Exception as exc:
        log_event("warning", "audit_write_failed", event_type=event_type, error=str(exc))


def ensure_recording_metadata_schema() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS recording_jobs (
                    id VARCHAR(36) PRIMARY KEY,
                    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                    camera_name_snapshot VARCHAR(255) NULL,
                    camera_folder_snapshot VARCHAR(255) NULL,
                    state VARCHAR(50) NOT NULL,
                    source_stream VARCHAR(20) NULL,
                    input_fingerprint VARCHAR(255) NULL,
                    recorder_instance_id VARCHAR(255) NULL,
                    started_at TIMESTAMP NOT NULL,
                    stopped_at TIMESTAMP NULL,
                    stop_reason TEXT NULL,
                    last_error TEXT NULL,
                    last_error_type VARCHAR(100) NULL,
                    ffmpeg_pid INTEGER NULL,
                    last_exit_code INTEGER NULL,
                    created_by VARCHAR(50) NOT NULL DEFAULT 'KM VMS',
                    ownership VARCHAR(50) NOT NULL DEFAULT 'KM VMS',
                    source VARCHAR(50) NOT NULL DEFAULT 'recorder',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS recording_segments (
                    id SERIAL PRIMARY KEY,
                    job_id VARCHAR(36) NULL REFERENCES recording_jobs(id) ON DELETE SET NULL,
                    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                    camera_name_snapshot VARCHAR(255) NULL,
                    camera_folder_snapshot VARCHAR(255) NULL,
                    file_path VARCHAR(1024) NOT NULL,
                    relative_path VARCHAR(1024) NULL,
                    started_at TIMESTAMP NOT NULL,
                    ended_at TIMESTAMP NULL,
                    duration_sec INTEGER DEFAULT 0 NOT NULL,
                    size_bytes BIGINT DEFAULT 0 NOT NULL,
                    stream_type VARCHAR(20) DEFAULT 'main' NOT NULL,
                    status VARCHAR(50) DEFAULT 'ready' NOT NULL,
                    error_message TEXT NULL,
                    ownership VARCHAR(50) DEFAULT 'KM VMS' NOT NULL,
                    source VARCHAR(50) DEFAULT 'recorder' NOT NULL,
                    archive_root_id VARCHAR(36) NULL,
                    archive_root_resolution_status VARCHAR(64) NULL,
                    archive_root_resolution_detail TEXT NULL,
                    archive_root_resolved_at TIMESTAMP NULL,
                    checksum VARCHAR(128) NULL,
                    storage_namespace VARCHAR(255) NULL,
                    container_format VARCHAR(32) NULL,
                    file_extension VARCHAR(16) NULL,
                    mime_type VARCHAR(100) NULL,
                    integrity_status VARCHAR(100) NULL,
                    integrity_error TEXT NULL,
                    last_integrity_check_at TIMESTAMP NULL,
                    file_size_verified_at TIMESTAMP NULL,
                    media_progress_at TIMESTAMP NULL,
                    file_mtime TIMESTAMP NULL,
                    content_probe_status VARCHAR(100) NULL,
                    cleanup_candidate BOOLEAN DEFAULT FALSE NULL,
                    cleanup_reason TEXT NULL,
                    reconciliation_status VARCHAR(100) NULL,
                    reconciliation_checked_at TIMESTAMP NULL,
                    finalized_at TIMESTAMP NULL,
                    deleted_at TIMESTAMP NULL,
                    deletion_reason TEXT NULL,
                    deleted_by VARCHAR(255) NULL,
                    deletion_source VARCHAR(100) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS job_id VARCHAR(36) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS camera_name_snapshot VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS camera_folder_snapshot VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS relative_path VARCHAR(1024) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS error_message TEXT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS ownership VARCHAR(50) DEFAULT 'KM VMS' NOT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'recorder' NOT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS archive_root_id VARCHAR(36) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS archive_root_resolution_status VARCHAR(64) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS archive_root_resolution_detail TEXT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS archive_root_resolved_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS checksum VARCHAR(128) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS storage_namespace VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS container_format VARCHAR(32) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS file_extension VARCHAR(16) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS mime_type VARCHAR(100) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS integrity_status VARCHAR(100) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS integrity_error TEXT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS last_integrity_check_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS file_size_verified_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS media_progress_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS file_mtime TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS content_probe_status VARCHAR(100) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS cleanup_candidate BOOLEAN DEFAULT FALSE NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS cleanup_reason TEXT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS reconciliation_status VARCHAR(100) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS reconciliation_checked_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS deletion_reason TEXT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS deletion_source VARCHAR(100) NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL"))
        conn.execute(text("ALTER TABLE recording_segments ALTER COLUMN ended_at DROP NOT NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_jobs_camera_id ON recording_jobs (camera_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_jobs_state ON recording_jobs (state)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_jobs_started_at ON recording_jobs (started_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_jobs_ownership ON recording_jobs (ownership)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_job_id ON recording_segments (job_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_status ON recording_segments (status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_ownership ON recording_segments (ownership)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_archive_root_id ON recording_segments (archive_root_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_archive_root_resolution_status ON recording_segments (archive_root_resolution_status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_relative_path ON recording_segments (relative_path)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_job_relative_path ON recording_segments (job_id, relative_path)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_integrity_status ON recording_segments (integrity_status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_reconciliation_status ON recording_segments (reconciliation_status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_deleted_at ON recording_segments (deleted_at)"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS recorder_file_receipts (
                    id VARCHAR(36) PRIMARY KEY,
                    contract_version INTEGER DEFAULT 1 NOT NULL,
                    segment_id BIGINT NOT NULL UNIQUE,
                    job_id VARCHAR(36) NULL,
                    camera_id INTEGER NULL,
                    root_id VARCHAR(36) NOT NULL,
                    physical_identity VARCHAR(128) NULL,
                    relative_path VARCHAR(1024) NOT NULL,
                    state VARCHAR(24) NOT NULL,
                    object_identity VARCHAR(128) NOT NULL,
                    device_id VARCHAR(64) NULL,
                    inode VARCHAR(64) NULL,
                    size_bytes BIGINT DEFAULT 0 NOT NULL,
                    mtime_ns BIGINT DEFAULT 0 NOT NULL,
                    content_fingerprint VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    finalized_at TIMESTAMP NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recorder_file_receipts_root_relative ON recorder_file_receipts (root_id, relative_path)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recorder_file_receipts_root_object ON recorder_file_receipts (root_id, object_identity)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recorder_file_receipts_state_finalized ON recorder_file_receipts (state, finalized_at)"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS recorder_runtime_status (
                    recorder_instance_id VARCHAR(255) PRIMARY KEY,
                    service_status VARCHAR(50) NOT NULL,
                    loop_state VARCHAR(100) NULL,
                    started_at TIMESTAMP NULL,
                    heartbeat_at TIMESTAMP NOT NULL,
                    active_jobs_count INTEGER DEFAULT 0 NOT NULL,
                    recording_cameras_count INTEGER DEFAULT 0 NOT NULL,
                    failed_cameras_count INTEGER DEFAULT 0 NOT NULL,
                    last_error TEXT NULL,
                    last_exit_code INTEGER NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        conn.execute(text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS auto_free_space_cleanup_enabled BOOLEAN DEFAULT FALSE NOT NULL"))
        conn.execute(text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS recording_suspended_by_low_disk BOOLEAN DEFAULT FALSE NOT NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recorder_runtime_status_heartbeat_at ON recorder_runtime_status (heartbeat_at)"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS archive_roots (
                    id VARCHAR(36) PRIMARY KEY,
                    label VARCHAR(255) NOT NULL,
                    root_path VARCHAR(1024) NOT NULL UNIQUE,
                    storage_namespace VARCHAR(255) NOT NULL DEFAULT 'kmvms/recordings',
                    is_active BOOLEAN DEFAULT FALSE NOT NULL,
                    is_readable BOOLEAN DEFAULT TRUE NOT NULL,
                    is_writable BOOLEAN DEFAULT TRUE NOT NULL,
                    is_available BOOLEAN DEFAULT TRUE NOT NULL,
                    problem TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    last_seen_at TIMESTAMP NULL,
                    retired_at TIMESTAMP NULL,
                    physical_identity VARCHAR(128) NULL,
                    retirement_status VARCHAR(50) NULL,
                    retirement_problem TEXT NULL
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_archive_roots_is_active ON archive_roots (is_active)"))
        conn.execute(text("ALTER TABLE archive_roots ADD COLUMN IF NOT EXISTS physical_identity VARCHAR(128) NULL"))
        conn.execute(text("ALTER TABLE archive_roots ADD COLUMN IF NOT EXISTS retirement_status VARCHAR(50) NULL"))
        conn.execute(text("ALTER TABLE archive_roots ADD COLUMN IF NOT EXISTS retirement_problem TEXT NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_archive_roots_physical_identity ON archive_roots (physical_identity)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_archive_roots_retirement_status ON archive_roots (retirement_status)"))
        conn.execute(
            text(
                """
                INSERT INTO archive_roots (
                    id, label, root_path, storage_namespace, is_active, is_readable, is_writable, is_available, created_at, updated_at, last_seen_at
                )
                VALUES (
                    :id, :label, :root_path, :namespace, TRUE, TRUE, TRUE, TRUE, NOW(), NOW(), NOW()
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": DEFAULT_ARCHIVE_ROOT_ID, "label": "Default archive", "root_path": str(STORAGE_ROOT), "namespace": STORAGE_NAMESPACE},
        )


def selected_source_stream(row) -> str:
    preferred = (getattr(row, "default_record_stream", None) or DEFAULT_RECORD_STREAM).lower()
    return "sub" if preferred == "sub" else "main"


def input_fingerprint(row) -> str:
    input_url = choose_input_url(row)
    if not input_url:
        return "missing_input"
    sanitized = redact_text(input_url)
    match = re.match(r"^([a-z][a-z0-9+.-]*://)?([^/?#]+)([^?#]*)", sanitized, flags=re.IGNORECASE)
    if not match:
        return "configured_input"
    scheme = (match.group(1) or "").rstrip(":/")
    host = match.group(2).split("@")[-1]
    path = match.group(3) or ""
    return truncate_error(f"{scheme}:{host}{path}" if scheme else f"{host}{path}")[:255]


def recording_job_store() -> RecordingJobStore:
    return RecordingJobStore(
        engine, RECORDER_INSTANCE_ID, selected_source_stream, input_fingerprint,
        redact_text, truncate_error, log_event,
    )


def segment_accounting() -> SegmentAccounting:
    return SegmentAccounting(
        engine, DEFAULT_RECORD_STREAM, MEDIA_STALL_SECONDS,
        storage_relative_path, parse_segment_start_time, path_format_metadata,
        remember_processed_segment, redact_text, truncate_error, classify_error,
        log_event, write_audit_event,
    )


def create_recording_job(row, job: RecordingJob) -> str:
    return recording_job_store().create_recording_job(row, job)


def update_recording_job(
    job: RecordingJob,
    *,
    state: str | None = None,
    stop_reason: Any = UNSET,
    stopped: bool = False,
    last_error: Any = UNSET,
    last_error_type: Any = UNSET,
    ffmpeg_pid: Any = UNSET,
    last_exit_code: Any = UNSET,
) -> None:
    recording_job_store().update_recording_job(
        job, state=state, stop_reason=stop_reason, stopped=stopped,
        last_error=last_error, last_error_type=last_error_type,
        ffmpeg_pid=ffmpeg_pid, last_exit_code=last_exit_code,
    )


def close_recording_job(job: RecordingJob, *, state: str, reason: str, error: str | None = None) -> None:
    has_current_error = bool(error)
    update_recording_job(
        job,
        state=state,
        stop_reason=reason,
        stopped=True,
        last_error=error if has_current_error else None,
        last_error_type=job.last_error_type if has_current_error else None,
        ffmpeg_pid=None,
        last_exit_code=job.last_exit_code if has_current_error else None,
    )
    job.db_job_id = None
    job.pending_db_job_id = None
    job.segment_baseline_paths.clear()
    job.known_segment_paths.clear()
    job.segment_directory_signature = None
    job.segment_discovery_cache.clear()
    job.processed_segment_names.clear()


def clear_recording_job_error(job: RecordingJob) -> None:
    recording_job_store().clear_recording_job_error(job)


def active_archive_root() -> tuple[str, Path]:
    global last_runtime_mapping_log_key
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, root_path
                    FROM archive_roots
                    WHERE is_active = TRUE AND retired_at IS NULL
                    ORDER BY updated_at DESC, id ASC
                    LIMIT 1
                    """
                )
            ).first()
    except Exception as exc:
        log_event("error", "archive_root_read_failed", error=redact_text(str(exc)))
        raise RuntimeError("active_archive_root_lookup_failed") from exc
    if not row or not row.id:
        raise RuntimeError("active_archive_root_missing")
    configured_path = Path(str(row.root_path or STORAGE_ROOT))
    if configured_path.as_posix() != STORAGE_ROOT.as_posix():
        key = (str(row.id), configured_path.as_posix(), STORAGE_ROOT.as_posix())
        if key != last_runtime_mapping_log_key:
            last_runtime_mapping_log_key = key
            log_event(
                "info",
                "archive_root_runtime_path_mapped",
                archive_root_id=key[0],
                configured_path=key[1],
                runtime_path=key[2],
            )
    return str(row.id), STORAGE_ROOT


def kmvms_recordings_root(root: Path | None = None) -> Path:
    path = (root or active_archive_root()[1]) / STORAGE_NAMESPACE
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_segment_dir(camera_id: int, job_id: str, *, root: Path | None = None) -> Path:
    now = datetime.now()
    path = kmvms_recordings_root(root) / f"camera_{int(camera_id)}" / f"job_{safe_name(job_id)}" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_recording_format(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in RECORDING_FORMATS else DEFAULT_RECORDING_FORMAT


def read_recording_format_choice() -> tuple[str, str, float | None] | None:
    try:
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT recording_format, recording_format_apply_mode, recording_format_requested_at "
                "FROM system_settings ORDER BY id ASC LIMIT 1"
            )).first()
    except Exception as exc:
        log_event("warning", "recording_format_read_failed", error=redact_text(str(exc)), action="keep_running_recordings")
        return None

    raw_value = row.recording_format if row else None
    recording_format = normalize_recording_format(raw_value)
    if raw_value is not None and str(raw_value or "").strip().lower() not in RECORDING_FORMATS:
        log_event("warning", "recording_format_invalid_fallback", value=redact_text(str(raw_value)), fallback=recording_format)
    mode = getattr(row, "recording_format_apply_mode", None) or "after_segment"
    requested = getattr(row, "recording_format_requested_at", None)
    if isinstance(requested, str):
        requested = datetime.fromisoformat(requested.replace("Z", "+00:00"))
    requested_epoch = requested.replace(tzinfo=UTC).timestamp() if requested is not None and requested.tzinfo is None else requested.timestamp() if requested is not None else None
    return recording_format, mode, requested_epoch


def read_recording_format() -> str:
    choice = read_recording_format_choice()
    return choice[0] if choice is not None else DEFAULT_RECORDING_FORMAT


def read_recording_suspended_by_low_disk() -> bool:
    try:
        with engine.begin() as conn:
            row = conn.execute(text("SELECT recording_suspended_by_low_disk FROM system_settings ORDER BY id ASC LIMIT 1")).first()
    except Exception as exc:
        log_event("warning", "low_disk_suspend_read_failed", error=redact_text(str(exc)))
        return False
    return bool(row and row.recording_suspended_by_low_disk)


def format_metadata(recording_format: str) -> dict[str, str]:
    return FORMAT_METADATA[normalize_recording_format(recording_format)]


def path_format_metadata(file_path: Path) -> dict[str, str]:
    suffix = file_path.suffix.lower()
    if suffix == ".mp4":
        return FORMAT_METADATA["mp4"]
    if suffix == ".mkv":
        return FORMAT_METADATA["mkv"]
    return {"container_format": suffix.lstrip(".") or "unknown", "file_extension": suffix, "mime_type": "application/octet-stream", "segment_format": "unknown"}


def build_segment_pattern(camera_id: int, camera_name: str, job_id: str, recording_format: str, *, root: Path | None = None) -> str:
    dir_path = current_segment_dir(camera_id, job_id, root=root)
    extension = format_metadata(recording_format)["file_extension"]
    return str(dir_path / f"{segment_prefix(camera_id, camera_name)}%Y-%m-%d-%H-%M-%S{extension}")


def segment_prefix(camera_id: int, camera_name: str) -> str:
    return f"{safe_name(camera_name)}-"


def expected_segment_dir(output_pattern: str | None) -> Path | None:
    if not output_pattern:
        return None
    return Path(output_pattern).parent


def storage_relative_path(path: Path, *, root: Path | None = None) -> str | None:
    try:
        return path.resolve().relative_to((root or STORAGE_ROOT).resolve()).as_posix()
    except Exception:
        return None


def capture_segment_baseline(output_pattern: str | None, camera_id: int, camera_name: str, *, root: Path | None = None) -> set[str]:
    dir_path = expected_segment_dir(output_pattern)
    if not dir_path or not dir_path.exists():
        return set()
    prefix = segment_prefix(camera_id, camera_name)
    baseline: set[str] = set()
    for file_path in dir_path.glob(f"{prefix}*.*"):
        rel_path = storage_relative_path(file_path, root=root)
        if rel_path:
            baseline.add(rel_path)
    return baseline


def parse_segment_start_time(file_path: Path) -> datetime:
    match = re.search(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})(?=\.[a-z0-9]+$)", file_path.name, flags=re.IGNORECASE)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d-%H-%M-%S")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(file_path.stat().st_mtime)
    except Exception:
        return datetime.utcnow()


def discover_job_segments(job: RecordingJob) -> list[Path]:
    dir_path = expected_segment_dir(job.current_output_path)
    if not dir_path or not dir_path.exists():
        return []
    stat = dir_path.stat()
    signature = (str(dir_path), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    now = time.monotonic()
    if signature != job.segment_directory_signature or now - job.segment_directory_checked_at >= SEGMENT_DISCOVERY_REFRESH_SECONDS:
        prefix = segment_prefix(job.camera_id, job.camera_name)
        names = set()
        pending = {}
        for path in dir_path.iterdir():
            if not path.name.startswith(prefix) or path.suffix.lower() not in {".mp4", ".mkv"}:
                continue
            names.add(path.name)
            if path.name not in job.processed_segment_names and path.is_file():
                pending[path.name] = path
        # Retention must also release our references to files no longer present.
        job.processed_segment_names.intersection_update(names)
        parent = storage_relative_path(dir_path, root=job.archive_root_path)
        if parent is not None:
            job.known_segment_paths.intersection_update({f"{parent}/{name}" for name in names})
        job.segment_discovery_cache = pending
        job.segment_directory_signature = signature
        job.segment_directory_checked_at = now
    return sorted(job.segment_discovery_cache.values(), key=lambda path: (parse_segment_start_time(path), path.name))


def remember_processed_segment(job: RecordingJob, file_path: Path) -> None:
    job.processed_segment_names.add(file_path.name)
    job.segment_discovery_cache.pop(file_path.name, None)
    rel_path = storage_relative_path(file_path, root=job.archive_root_path)
    if rel_path:
        job.known_segment_paths.discard(rel_path)


def create_segment_metadata_if_needed(job: RecordingJob, file_path: Path) -> None:
    return segment_accounting().create_segment_metadata_if_needed(job, file_path)


def update_writing_segment_progress(job: RecordingJob, file_path: Path) -> bool:
    return segment_accounting().update_writing_segment_progress(job, file_path)


def finalize_segment_path(job: RecordingJob, file_path: Path) -> bool:
    return segment_accounting().finalize_segment_path(job, file_path)


def mark_segment_failed(job: RecordingJob, rel_path: str, error: str) -> None:
    return segment_accounting().mark_segment_failed(job, rel_path, error)


def sync_segment_metadata_for_job(job: RecordingJob) -> None:
    if not job.db_job_id:
        return
    try:
        files = discover_job_segments(job)
        batch = files[:-1][:SEGMENT_METADATA_BATCH_SIZE - 1] + files[-1:]
        for file_path in batch:
            create_segment_metadata_if_needed(job, file_path)
        if files:
            update_writing_segment_progress(job, files[-1])
        for file_path in batch[:-1]:
            finalize_segment_path(job, file_path)
    except Exception as exc:
        log_event("warning", "metadata_update_failed", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, error=str(exc))


def segment_duration_seconds_for_row(row) -> int:
    return max(int(row.segment_minutes or 5), 1) * 60


def stale_segment_after_seconds(row) -> int:
    return segment_duration_seconds_for_row(row) + 300


def current_writing_segment(job: RecordingJob) -> Path | None:
    files = discover_job_segments(job)
    if not files:
        return None
    return files[-1]


def format_change_ready(job: RecordingJob, target: str, mode: str, requested_epoch: float | None) -> bool:
    if mode == "immediate":
        return True
    current = current_writing_segment(job)
    if job.format_change_target != target or job.format_change_requested_epoch != requested_epoch:
        job.format_change_target = target
        job.format_change_requested_epoch = requested_epoch
        job.format_change_anchor = None
    if current is None:
        return False
    # FFmpeg and this process share TZ. Convert its filename-local timestamp to
    # an epoch, not to the operator's display timezone. A boundary that happened
    # between saving settings and this loop must not incur another full segment.
    if requested_epoch is not None and parse_segment_start_time(current).timestamp() >= requested_epoch:
        return True
    current_name = str(current)
    if job.format_change_anchor is None:
        job.format_change_anchor = current_name
        return False
    return current_name != job.format_change_anchor


def current_segment_age_seconds(file_path: Path) -> int:
    started_at = parse_segment_start_time(file_path)
    return max(0, int((datetime.now() - started_at).total_seconds()))


def handle_stale_current_segment(row, job: RecordingJob) -> bool:
    if not job.process or job.process.poll() is not None:
        return False
    file_path = (job.archive_root_path / job.current_media_relative_path) if job.current_media_relative_path else current_writing_segment(job)
    age_sec = current_segment_age_seconds(file_path) if file_path is not None else 0
    threshold_sec = stale_segment_after_seconds(row)
    now_ts = time.time()
    progress_age = now_ts - (job.last_media_progress_at or job.started_at or now_ts)
    stalled = progress_age > MEDIA_STALL_SECONDS
    job.stalled_samples = job.stalled_samples + 1 if stalled else 0
    rotation_overdue = file_path is not None and age_sec > threshold_sec
    if not rotation_overdue and job.stalled_samples < MEDIA_STALL_SAMPLES:
        return False

    reason = ERROR_MEDIA_STALLED if stalled else ERROR_STALE_SEGMENT
    rel_path = storage_relative_path(file_path, root=job.archive_root_path) if file_path is not None else None
    event_metadata = {
        "reason": reason,
        "age_seconds": age_sec,
        "stale_after_seconds": threshold_sec,
        "segment_name": file_path.name if file_path is not None else None,
        "progress_age_seconds": round(progress_age),
    }
    log_event(
        "warning",
        "stale_current_segment_detected",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        db_job_id=job.db_job_id,
        relative_path=rel_path,
        age_seconds=age_sec,
        stale_after_seconds=threshold_sec,
    )
    write_audit_event(
        event_type="stale_writing_detected",
        severity="warning",
        message=f"Recorder detected stale writing segment for camera {job.camera_name}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata=event_metadata,
    )

    job.stale_recovery_count += 1
    job.last_stale_recovery_at = now_ts
    stop_camera(row.id, reason, audit_event="camera_restarted", for_retry=True)
    schedule_retry(job, "Recording media stopped progressing" if stalled else "Recording segment rotation overdue", reason)
    log_event("warning", "stale_current_segment_recovery_attempted", camera_id=job.camera_id, camera_name=job.camera_name, attempt=job.stale_recovery_count)
    return True


def finalize_segments_for_job(job: RecordingJob) -> None:
    if not job.db_job_id:
        return
    sync_segment_metadata_for_job(job)
    for file_path in discover_job_segments(job):
        create_segment_metadata_if_needed(job, file_path)
        finalize_segment_path(job, file_path)


def mark_segments_failed_for_job(job: RecordingJob, error: str) -> None:
    if not job.db_job_id:
        return
    sync_segment_metadata_for_job(job)
    with engine.begin() as conn:
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
                    reconciliation_status = :reconciliation_status,
                    reconciliation_checked_at = NOW(),
                    updated_at = NOW()
                WHERE job_id = :job_id
                  AND ownership = :ownership
                  AND source = :source
                  AND status = :writing
                """
            ),
            {
                "job_id": job.db_job_id,
                "ownership": OWNERSHIP_KM_VMS,
                "source": METADATA_SOURCE_RECORDER,
                "writing": SEGMENT_STATUS_WRITING,
                "status": SEGMENT_STATUS_FAILED,
                "error_message": truncate_error(redact_text(error)),
                "integrity_status": "partial_file",
                "reconciliation_status": "partial_file",
            },
        )
    if result.rowcount <= 0:
        return
    log_event("warning", "segment_failed", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, error=error)
    write_audit_event(
        event_type="segment_summary",
        severity="warning",
        message=f"Recorder marked failed segments for camera {job.camera_name}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"job_id": job.db_job_id, "failed_count": int(result.rowcount), "error_type": classify_error(error)},
    )


def mark_stale_writing_segments_on_startup() -> None:
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE recording_segments
                    SET status = :stale_status,
                        error_message = COALESCE(error_message, :error_message),
                        integrity_status = :integrity_status,
                        integrity_error = COALESCE(integrity_error, :error_message),
                        last_integrity_check_at = NOW(),
                        cleanup_candidate = FALSE,
                        reconciliation_status = :reconciliation_status,
                        reconciliation_checked_at = NOW(),
                        updated_at = NOW()
                    WHERE ownership = :ownership
                      AND source = :source
                      AND status = :writing
                      AND job_id IN (
                          SELECT id FROM recording_jobs
                          WHERE recorder_instance_id IS DISTINCT FROM :instance_id
                      )
                    """
                ),
                {
                    "stale_status": SEGMENT_STATUS_STALE_WRITING,
                    "error_message": "recorder restarted with stale writing segment",
                    "integrity_status": "stale_writing_segment",
                    "reconciliation_status": "stale_writing_segment",
                    "ownership": OWNERSHIP_KM_VMS,
                    "source": METADATA_SOURCE_RECORDER,
                    "writing": SEGMENT_STATUS_WRITING,
                    "instance_id": RECORDER_INSTANCE_ID,
                },
            )
        if result.rowcount:
            log_event("warning", "stale_writing_segments_marked", count=result.rowcount)
            write_audit_event(
                event_type="stale_writing_detected",
                severity="warning",
                message="Recorder detected stale writing segments on startup",
                metadata={"stale_writing_count": int(result.rowcount)},
            )
    except Exception as exc:
        log_event("warning", "stale_writing_reconciliation_failed", error=str(exc))


def reconcile_previous_recorder_instances() -> None:
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE recording_jobs
            SET state = 'stopped',
                stop_reason = 'previous_recorder_instance_interrupted',
                ffmpeg_pid = NULL,
                updated_at = timezone('UTC', NOW())
            WHERE recorder_instance_id IS DISTINCT FROM :instance_id
              AND ownership = :ownership AND source = :source
              AND state IN ('starting', 'recording', 'stopping', 'restarting')
        """), {"instance_id": RECORDER_INSTANCE_ID, "ownership": OWNERSHIP_KM_VMS, "source": METADATA_SOURCE_RECORDER})
        conn.execute(text("""
            UPDATE recorder_runtime_status SET loop_state = 'replaced'
            WHERE recorder_instance_id IS DISTINCT FROM :instance_id
              AND loop_state NOT IN ('shutdown', 'replaced')
        """), {"instance_id": RECORDER_INSTANCE_ID})
    if result.rowcount:
        log_event("info", "previous_recorder_jobs_interrupted", count=result.rowcount)


def choose_input_url(row) -> str | None:
    preferred = (getattr(row, "default_record_stream", None) or DEFAULT_RECORD_STREAM).lower()
    if preferred == "sub":
        primary = row.rtsp_sub_url or row.rtsp_main_url
    else:
        primary = row.rtsp_main_url or row.rtsp_sub_url

    protocol = (row.protocol or "").lower()
    if protocol in {"rtsp", "onvif"}:
        return primary
    return None


def camera_signature(row, recording_format: str) -> tuple[Any, ...]:
    return (
        row.name,
        row.storage_folder_name,
        bool(row.enabled),
        row.protocol,
        row.rtsp_main_url,
        row.rtsp_sub_url,
        row.rtsp_transport,
        row.recording_mode,
        row.default_record_stream,
        int(row.segment_minutes or 5),
        normalize_recording_format(recording_format),
    )


def ffmpeg_cmd(camera, job: RecordingJob) -> tuple[list[str], str] | tuple[None, None]:
    input_url = choose_input_url(camera)
    if not input_url:
        return None, None

    segment_minutes = int(camera.segment_minutes or 5)
    segment_seconds = max(segment_minutes, 1) * 60
    if not job.db_job_id:
        raise RuntimeError("recording job metadata is required before building segment path")
    recording_format = normalize_recording_format(job.recording_format)
    media_metadata = format_metadata(recording_format)
    root_id, root_path = active_archive_root()
    if not root_path.exists() or not root_path.is_dir():
        raise RuntimeError("active_archive_root_unavailable")
    namespace_root = root_path / STORAGE_NAMESPACE
    namespace_root.mkdir(parents=True, exist_ok=True)
    if not os.access(namespace_root, os.W_OK):
        raise PermissionError("active_archive_root_unwritable")
    job.archive_root_id = root_id
    job.archive_root_path = root_path
    segment_pattern = build_segment_pattern(camera.id, camera.name, job.db_job_id, recording_format, root=root_path)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel", FFMPEG_LOGLEVEL,
        "-rtsp_transport", (camera.rtsp_transport or "tcp"),
        "-i", input_url,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "copy",
        "-c:a", "aac",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-segment_format", media_metadata["segment_format"],
    ]
    if recording_format == "mp4":
        cmd.extend(["-movflags", "+faststart"])
    cmd.append(segment_pattern)
    return cmd, segment_pattern


def mark_camera_status(camera_id: int, status: str, last_error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE cameras
                SET status = :status,
                    last_error = :last_error,
                    updated_at = NOW()
                WHERE id = :camera_id
                  AND (
                      status IS DISTINCT FROM :status
                      OR last_error IS DISTINCT FROM :last_error
                  )
                """
            ),
            {
                "camera_id": camera_id,
                "status": status,
                "last_error": truncate_error(redact_text(last_error)) if last_error else None,
            },
        )


def external_camera_status(state: str) -> str:
    if state == RecorderState.DISABLED:
        return "disabled"
    if state in {RecorderState.ERROR, RecorderState.RESTARTING}:
        return "error"
    if state == RecorderState.RECORDING:
        return "starting"
    if state == RecorderState.IDLE:
        return "idle"
    return "stopped"


def update_camera_status_from_job(job: RecordingJob) -> None:
    status = "recording" if job.state == RecorderState.RECORDING and job.known_segment_paths else external_camera_status(job.state)
    mark_camera_status(job.camera_id, status, job.last_error)


def get_or_create_job(row) -> RecordingJob:
    job = jobs.get(row.id)
    if job is None:
        job = RecordingJob(
            camera_id=row.id,
            camera_name=row.name,
            folder_name=row.storage_folder_name,
            recording_mode=row.recording_mode,
            enabled=bool(row.enabled),
            config_signature=camera_signature(row, DEFAULT_RECORDING_FORMAT),
        )
        jobs[row.id] = job
    else:
        job.camera_name = row.name
        job.folder_name = row.storage_folder_name
        job.recording_mode = row.recording_mode
        job.enabled = bool(row.enabled)
    return job


def read_stream_tail(job: RecordingJob) -> None:
    stream = job.process.stderr if job.process else None
    if stream is None:
        return
    try:
        for line in stream:
            line = redact_text(line.rstrip())
            if line:
                job.stderr_tail.append(line)
    except Exception as exc:
        job.stderr_tail.append(f"stderr reader failed: {redact_text(str(exc))}")


def start_stderr_reader(job: RecordingJob) -> None:
    thread = threading.Thread(target=read_stream_tail, args=(job,), name=f"recorder-stderr-{job.camera_id}", daemon=True)
    job.stderr_thread = thread
    thread.start()


def stderr_tail_text(job: RecordingJob) -> str:
    return truncate_error("\n".join(job.stderr_tail))


def classify_error(message: str | None, exit_code: int | None = None) -> str:
    text_value = (message or "").lower()
    if "no such file or directory" in text_value and "ffmpeg" in text_value:
        return ERROR_FFMPEG_NOT_FOUND
    if any(value in text_value for value in ("unauthorized", "forbidden", "authentication failed")) or re.search(
        r"(?:server returned|(?:http|rtsp)(?:/\d(?:\.\d)?)?|failed:?)\s+(?:401|403)\b", text_value
    ):
        return ERROR_AUTH_FAILED
    if any(value in text_value for value in ("connection", "timed out", "timeout", "no route to host", "network is unreachable", "end of file")):
        return ERROR_NETWORK_TIMEOUT
    if "permission denied" in text_value:
        return ERROR_PERMISSION_DENIED
    if "storage" in text_value or "read-only file system" in text_value or "no space left" in text_value:
        return ERROR_STORAGE_UNAVAILABLE
    if "mount" in text_value or "not a directory" in text_value or "directory nonexistent" in text_value:
        return ERROR_STORAGE_UNAVAILABLE
    if "no such file" in text_value or "invalid data" in text_value or "invalid argument" in text_value:
        return ERROR_INVALID_RTSP
    if exit_code is not None:
        return ERROR_PROCESS_CRASHED
    return ERROR_UNKNOWN_FFMPEG


def classify_preflight_error(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return ERROR_PERMISSION_DENIED
    if isinstance(exc, OSError) and exc.errno in {errno.EROFS, errno.ENOSPC, errno.ENOENT, errno.ENOTDIR}:
        return ERROR_STORAGE_UNAVAILABLE
    return classify_error(str(exc))


def schedule_retry(job: RecordingJob, error: str, error_type: str, exit_code: int | None = None) -> None:
    job.last_exit_code = exit_code
    job.restart_count += 1
    job.retry_count += 1
    job.last_retry_at = time.time()
    backoff = BACKOFF_STEPS_SECONDS[min(job.retry_count - 1, len(BACKOFF_STEPS_SECONDS) - 1)]
    if error_type in {ERROR_AUTH_FAILED, ERROR_INVALID_RTSP, ERROR_FFMPEG_NOT_FOUND, ERROR_PERMISSION_DENIED, ERROR_STORAGE_UNAVAILABLE}:
        backoff = BLOCKED_RETRY_SECONDS
    job.next_retry_at = time.time() + backoff
    job.stable_progress_since = None
    job.set_state(RecorderState.RESTARTING, error=error, error_type=error_type)
    update_camera_status_from_job(job)
    update_recording_job(job, state=RecorderState.RESTARTING, last_error=error, last_error_type=error_type, last_exit_code=exit_code)
    log_event(
        "warning",
        "retry_scheduled",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        error_type=error_type,
        exit_code=exit_code,
        retry_count=job.retry_count,
        next_retry_at=iso_ts(job.next_retry_at),
    )
    write_audit_event(
        event_type="backoff_entered",
        severity="warning",
        message=f"Recorder scheduled retry for camera {job.camera_name}: {error_type}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={
            "error_type": error_type,
            "exit_code": exit_code,
            "retry_count": job.retry_count,
            "backoff_seconds": backoff,
            "next_retry_at": iso_ts(job.next_retry_at),
        },
    )


def can_retry(job: RecordingJob) -> bool:
    return bool(job.next_retry_at and job.next_retry_at <= time.time())


def handle_start_failure(
    job: RecordingJob,
    *,
    event: str,
    error: str,
    error_type: str,
    exit_code: int | None = None,
) -> None:
    job.process = None
    job.pid = None
    update_recording_job(job, state=RecorderState.RESTARTING, last_error=error, last_error_type=error_type, last_exit_code=exit_code)
    log_event(
        "error",
        event,
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        error_type=error_type,
        error=error,
    )
    write_audit_event(
        event_type="ffmpeg_start_failed",
        severity="error",
        message=f"Recorder failed to start FFmpeg for camera {job.camera_name}: {error_type}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"error_type": error_type, "exit_code": exit_code},
    )
    schedule_retry(job, error, error_type, exit_code=exit_code)


def handle_retention_failure(job: RecordingJob, exc: Exception) -> None:
    error = redact_text(str(exc))
    error_type = classify_preflight_error(exc)
    log_event(
        "error",
        "retention_storage_precheck_failed",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        error_type=error_type,
        error=error,
    )
    update_recording_job(job, state=RecorderState.RESTARTING, last_error=error, last_error_type=error_type)
    schedule_retry(job, error, error_type)


def start_camera(row) -> None:
    job = get_or_create_job(row)
    proc = job.process
    if proc and proc.poll() is None:
        job.set_state(RecorderState.RECORDING)
        update_camera_status_from_job(job)
        update_recording_job(
            job,
            state=RecorderState.RECORDING,
            ffmpeg_pid=job.pid,
            last_error=None,
            last_error_type=None,
            last_exit_code=None,
        )
        log_event("warning", "duplicate_process_prevented", camera_id=job.camera_id, camera_name=job.camera_name, pid=job.pid)
        write_audit_event(
            event_type="duplicate_process_prevented",
            severity="warning",
            message=f"Recorder prevented duplicate FFmpeg process for camera {job.camera_name}",
            camera_id=job.camera_id,
            camera_name=job.camera_name,
            metadata={"pid": job.pid},
        )
        return

    if job.db_job_id and job.stopped_at is None:
        close_recording_job(job, state=RecorderState.STOPPED, reason="retry_replaced", error=job.last_error)
    job.stopped_at = None
    job.last_media_progress_at = None
    job.stable_progress_since = None
    job.current_media_relative_path = None
    job.current_media_size = 0
    job.stalled_samples = 0
    try:
        create_recording_job(row, job)
    except Exception as exc:
        log_event("warning", "metadata_write_failed", camera_id=job.camera_id, camera_name=job.camera_name, error=str(exc))
        try:
            schedule_retry(job, "Recording job metadata could not be committed", ERROR_PROCESS_START_FAILED)
        except Exception as retry_exc:
            # Backoff/state were set before persistence. The same pending id is
            # reconciled on the next attempt even while the DB remains offline.
            log_event("warning", "metadata_retry_persistence_failed", camera_id=job.camera_id, error=str(retry_exc))
        return

    try:
        cmd, output_pattern = ffmpeg_cmd(row, job)
    except Exception as exc:
        error = redact_text(str(exc))
        handle_start_failure(
            job,
            event="recording_preflight_failed",
            error=error,
            error_type=classify_preflight_error(exc),
        )
        return

    if not cmd:
        error = "recording input URL is not configured"
        handle_start_failure(
            job,
            event="invalid_input_or_rtsp_error",
            error=error,
            error_type=ERROR_INVALID_RTSP,
        )
        return

    job.set_state(RecorderState.STARTING)
    job.current_output_path = output_pattern
    job.segment_baseline_paths = capture_segment_baseline(output_pattern, job.camera_id, job.camera_name, root=job.archive_root_path)
    job.known_segment_paths.clear()
    job.segment_directory_signature = None
    job.segment_discovery_cache.clear()
    job.processed_segment_names.clear()
    job.config_signature = camera_signature(row, job.recording_format)
    update_camera_status_from_job(job)
    update_recording_job(
        job,
        state=RecorderState.STARTING,
        last_error=None,
        last_error_type=None,
        last_exit_code=None,
    )

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        error = redact_text(str(exc))
        handle_start_failure(
            job,
            event="ffmpeg_start_failed",
            error=error,
            error_type=ERROR_FFMPEG_NOT_FOUND,
        )
        return
    except Exception as exc:
        error = redact_text(str(exc))
        error_type = classify_error(error)
        if error_type == ERROR_UNKNOWN_FFMPEG:
            error_type = ERROR_PROCESS_START_FAILED
        handle_start_failure(
            job,
            event="ffmpeg_start_failed",
            error=error,
            error_type=error_type,
        )
        return

    job.process = proc
    job.pid = proc.pid
    job.started_at = time.time()
    job.stopped_at = None
    job.last_exit_code = None
    job.stderr_tail.clear()
    job.set_state(RecorderState.STARTING)
    start_stderr_reader(job)
    update_camera_status_from_job(job)
    update_recording_job(
        job,
        state=RecorderState.STARTING,
        ffmpeg_pid=job.pid,
        last_error=None,
        last_error_type=None,
        last_exit_code=None,
    )
    log_event("info", "recording_started", camera_id=job.camera_id, camera_name=job.camera_name, pid=job.pid, output_pattern=job.current_output_path, recording_format=job.recording_format)
    write_audit_event(
        event_type="camera_started",
        message=f"Recorder started recording camera {job.camera_name}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"pid": job.pid, "state": job.state, "recording_format": job.recording_format},
    )


def stop_camera(camera_id: int, reason: str = "stopped", audit_event: str = "recording_stopped", *, for_retry: bool = False, graceful_format_change: bool = False) -> bool:
    job = jobs.get(camera_id)
    if not job:
        return True

    proc = job.process
    previous_state = job.state
    job.set_state(RecorderState.STOPPING)
    update_camera_status_from_job(job)

    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            if graceful_format_change:
                # Never force-kill a writer merely to apply a format preference.
                job.set_state(previous_state)
                update_camera_status_from_job(job)
                update_recording_job(job, state=previous_state, stop_reason="recording_format_finish_timeout")
                job.format_change_retry_at = time.monotonic() + 30
                log_event("warning", "recording_format_finish_timeout", camera_id=job.camera_id)
                return False
            proc.kill()
            proc.wait(timeout=5)

    if proc:
        job.last_exit_code = proc.poll()

    if job.stderr_thread and job.stderr_thread.is_alive():
        job.stderr_thread.join(timeout=1)

    finalized = True
    try:
        finalize_segments_for_job(job)
        if graceful_format_change and job.db_job_id:
            with engine.begin() as conn:
                unfinished = conn.execute(text(
                    "SELECT COUNT(*) FROM recording_segments WHERE job_id = :job_id "
                    "AND status IN ('writing', 'failed', 'stale_writing')"
                ), {"job_id": job.db_job_id}).scalar_one()
            finalized = unfinished == 0
    except Exception:
        if not graceful_format_change:
            raise
        finalized = False
    if graceful_format_change and not finalized:
        reason = "recording_format_finalization_failed"

    job.process = None
    job.pid = None
    job.stopped_at = time.time()
    job.next_retry_at = None
    if reason == RecorderState.DISABLED:
        job.set_state(RecorderState.DISABLED)
    elif reason == RecorderState.IDLE:
        job.set_state(RecorderState.IDLE)
    else:
        job.set_state(RecorderState.STOPPED)
    update_camera_status_from_job(job)
    if for_retry:
        update_recording_job(job, state=RecorderState.RESTARTING, ffmpeg_pid=None, stop_reason=reason)
    else:
        close_recording_job(job, state=job.state, reason=reason)
    job.last_media_progress_at = None
    job.stable_progress_since = None
    job.stalled_samples = 0
    job.format_change_target = None
    job.format_change_anchor = None
    job.format_change_requested_epoch = None
    job.format_change_retry_at = 0
    log_event("info", "recording_stopped", camera_id=job.camera_id, camera_name=job.camera_name, reason=reason, exit_code=job.last_exit_code)
    write_audit_event(
        event_type="camera_stopped" if audit_event == "recording_stopped" else audit_event,
        message=f"Recorder stopped recording camera {job.camera_name}: {reason}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"reason": reason, "exit_code": job.last_exit_code},
    )
    return finalized


def enforce_retention(camera_id: int, folder_name: str, retention_days: int, storage_quota_gb: int) -> None:
    root = kmvms_recordings_root() / f"camera_{int(camera_id)}"
    root.parent.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        return
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    root.stat()


def retention_ready(row, job: RecordingJob) -> bool:
    try:
        enforce_retention(
            row.id,
            row.storage_folder_name,
            int(row.retention_days or 30),
            int(row.storage_quota_gb or 50),
        )
    except Exception as exc:
        handle_retention_failure(job, exc)
        return False
    return True


def sync_cameras() -> None:
    db = SessionLocal()
    try:
        format_choice = read_recording_format_choice()
        if format_choice is None:
            return
        effective_recording_format, format_apply_mode, format_requested_epoch = format_choice
        low_disk_recording_suspended = read_recording_suspended_by_low_disk()
        rows = db.execute(
            text(
                """
                SELECT
                    id,
                    name,
                    storage_folder_name,
                    enabled,
                    protocol,
                    host,
                    port,
                    rtsp_main_url,
                    rtsp_sub_url,
                    rtsp_transport,
                    recording_mode,
                    default_record_stream,
                    segment_minutes,
                    retention_days,
                    storage_quota_gb
                FROM cameras
                ORDER BY id ASC
                """
            )
        ).fetchall()

        seen_ids = set()

        for row in rows:
            seen_ids.add(row.id)
            job = get_or_create_job(row)
            desired_signature = camera_signature(row, effective_recording_format)

            if not row.enabled:
                job.retry_count = 0
                job.stale_recovery_count = 0
                job.next_retry_at = None
                if job.process and job.process.poll() is None:
                    stop_camera(row.id, RecorderState.DISABLED, audit_event="camera_disabled_recording_stopped")
                else:
                    job.set_state(RecorderState.DISABLED)
                    update_camera_status_from_job(job)
                    close_recording_job(job, state=RecorderState.DISABLED, reason="camera_disabled")
                continue

            if low_disk_recording_suspended and row.recording_mode == "always":
                job.retry_count = 0
                job.next_retry_at = None
                if job.process and job.process.poll() is None:
                    stop_camera(row.id, "critical_low_disk_recording_suspended", audit_event="critical_low_disk_recording_suspended")
                else:
                    job.set_state(RecorderState.IDLE)
                    update_camera_status_from_job(job)
                    close_recording_job(job, state=RecorderState.IDLE, reason="critical_low_disk_recording_suspended")
                continue

            if row.recording_mode != "always":
                job.retry_count = 0
                job.stale_recovery_count = 0
                job.next_retry_at = None
                if job.process and job.process.poll() is None:
                    stop_camera(row.id, RecorderState.IDLE)
                else:
                    job.set_state(RecorderState.IDLE)
                    update_camera_status_from_job(job)
                    close_recording_job(job, state=RecorderState.IDLE, reason="recording_mode_idle")
                continue

            if job.state == RecorderState.RESTARTING and not can_retry(job):
                continue

            if not retention_ready(row, job):
                continue

            if job.process and job.process.poll() is None:
                sync_segment_metadata_for_job(job)
                if handle_stale_current_segment(row, job):
                    continue

            if job.config_signature and job.config_signature != desired_signature and job.process and job.process.poll() is None:
                format_changed = job.recording_format != effective_recording_format
                format_only = format_changed and job.config_signature[:-1] == desired_signature[:-1]
                if format_only:
                    if time.monotonic() < job.format_change_retry_at:
                        continue
                    if not format_change_ready(job, effective_recording_format, format_apply_mode, format_requested_epoch):
                        continue
                log_event("info", "recording_config_changed_restart", camera_id=job.camera_id, camera_name=job.camera_name, recording_format=effective_recording_format)
                write_audit_event(
                    event_type="camera_restarted",
                    message=f"Recorder restarted camera {job.camera_name}: config_changed",
                    camera_id=job.camera_id,
                    camera_name=job.camera_name,
                    metadata={"reason": "config_changed", "recording_format": effective_recording_format},
                )
                finished = stop_camera(
                    row.id,
                    "recording_format_change" if format_changed else "config_changed",
                    graceful_format_change=format_changed,
                )
                if not finished and job.process and job.process.poll() is None:
                    continue
                job.config_signature = desired_signature

            if job.state == RecorderState.RESTARTING and can_retry(job):
                job.recording_format = effective_recording_format
                start_camera(row)
                continue

            if job.process and job.process.poll() is not None:
                continue

            if not job.process or job.process.poll() is not None:
                job.recording_format = effective_recording_format
                start_camera(row)

        for camera_id in list(jobs.keys()):
            if camera_id not in seen_ids:
                stop_camera(camera_id, "removed_or_disabled")
                jobs.pop(camera_id, None)

    finally:
        db.close()


def check_children() -> None:
    for camera_id, job in list(jobs.items()):
        proc = job.process
        if not proc:
            continue
        exit_code = proc.poll()
        if exit_code is None:
            continue

        if job.stderr_thread and job.stderr_thread.is_alive():
            job.stderr_thread.join(timeout=1)

        error_text = stderr_tail_text(job) or "ffmpeg stopped unexpectedly"
        error_type = classify_error(error_text, exit_code)
        mark_segments_failed_for_job(job, error_text)
        job.process = None
        job.pid = None
        job.stopped_at = time.time()
        log_event(
            "error",
            "ffmpeg_crashed",
            camera_id=job.camera_id,
            camera_name=job.camera_name,
            exit_code=exit_code,
            error_type=error_type,
            error=error_text,
        )
        write_audit_event(
            event_type="ffmpeg_crashed",
            severity="error",
            message=f"Recorder FFmpeg crashed for camera {job.camera_name}: {error_type}",
            camera_id=job.camera_id,
            camera_name=job.camera_name,
            metadata={"error_type": error_type, "exit_code": exit_code, "error": error_text},
        )
        schedule_retry(job, error_text, error_type, exit_code=exit_code)


def confirm_recording_startups() -> None:
    for job in list(jobs.values()):
        proc = job.process
        if job.state not in {RecorderState.STARTING, RecorderState.RECORDING} or not proc or not job.started_at:
            continue
        if proc.poll() is not None:
            continue
        if time.time() - job.started_at < STARTUP_CONFIRM_SECONDS:
            continue
        if not job.last_media_progress_at or time.time() - job.last_media_progress_at > MEDIA_PROGRESS_HEARTBEAT_GRACE_SECONDS:
            continue
        if job.state == RecorderState.STARTING:
            job.set_state(RecorderState.RECORDING)
            update_camera_status_from_job(job)
            update_recording_job(job, state=RecorderState.RECORDING, last_error=None, last_error_type=None, last_exit_code=None)
            log_event("info", "recording_start_confirmed", camera_id=job.camera_id, camera_name=job.camera_name, pid=job.pid)
        if not job.stable_progress_since or time.time() - job.stable_progress_since < RECOVERY_STABLE_SECONDS:
            continue
        if not (job.retry_count or job.stale_recovery_count or job.next_retry_at or job.last_error or job.last_error_type):
            continue

        job.retry_count = 0
        job.stale_recovery_count = 0
        job.last_stale_recovery_at = None
        job.next_retry_at = None
        job.last_error = None
        job.last_error_type = None
        job.last_exit_code = None
        update_camera_status_from_job(job)
        clear_recording_job_error(job)
        log_event("info", "recording_start_confirmed", camera_id=job.camera_id, camera_name=job.camera_name, pid=job.pid)
        write_audit_event(
            event_type="recovery_succeeded",
            message=f"Recorder recovery succeeded for camera {job.camera_name}",
            camera_id=job.camera_id,
            camera_name=job.camera_name,
            metadata={"pid": job.pid, "state": job.state},
        )


def active_jobs_status() -> list[dict[str, Any]]:
    return [job.status_payload() for job in sorted(jobs.values(), key=lambda item: item.camera_id)]


def write_recorder_heartbeat(loop_state: str = "loop") -> None:
    try:
        rows = list(jobs.values())
        active_count = sum(1 for job in rows if job.state in {RecorderState.STARTING, RecorderState.RECORDING, RecorderState.STOPPING, RecorderState.RESTARTING})
        recording_count = sum(
            1
            for job in rows
            if job.state == RecorderState.RECORDING
            and job.last_media_progress_at
            and time.time() - job.last_media_progress_at <= MEDIA_PROGRESS_HEARTBEAT_GRACE_SECONDS
        )
        current_failure_jobs = [
            job
            for job in rows
            if job.state in {RecorderState.ERROR, RecorderState.RESTARTING}
            or (bool(job.last_error) and job.state not in ERROR_CLEARING_STATES)
        ]
        failed_count = len(current_failure_jobs)
        last_error_job = next((job for job in sorted(current_failure_jobs, key=lambda item: item.last_state_change_at, reverse=True) if job.last_error), None)
        last_exit_code = next((job.last_exit_code for job in sorted(current_failure_jobs, key=lambda item: item.last_state_change_at, reverse=True) if job.last_exit_code is not None), None)
        service_status = "error" if failed_count and recording_count == 0 else ("degraded" if failed_count else "healthy")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO recorder_runtime_status (
                        recorder_instance_id,
                        service_status,
                        loop_state,
                        started_at,
                        heartbeat_at,
                        active_jobs_count,
                        recording_cameras_count,
                        failed_cameras_count,
                        last_error,
                        last_exit_code,
                        updated_at
                    )
                    VALUES (
                        :recorder_instance_id,
                        :service_status,
                        :loop_state,
                        timezone('UTC', to_timestamp(:started_at)),
                        timezone('UTC', NOW()),
                        :active_jobs_count,
                        :recording_cameras_count,
                        :failed_cameras_count,
                        :last_error,
                        :last_exit_code,
                        timezone('UTC', NOW())
                    )
                    ON CONFLICT (recorder_instance_id) DO UPDATE
                    SET service_status = EXCLUDED.service_status,
                        loop_state = EXCLUDED.loop_state,
                        started_at = EXCLUDED.started_at,
                        heartbeat_at = EXCLUDED.heartbeat_at,
                        active_jobs_count = EXCLUDED.active_jobs_count,
                        recording_cameras_count = EXCLUDED.recording_cameras_count,
                        failed_cameras_count = EXCLUDED.failed_cameras_count,
                        last_error = EXCLUDED.last_error,
                        last_exit_code = EXCLUDED.last_exit_code,
                        updated_at = NOW()
                    """
                ),
                {
                    "recorder_instance_id": RECORDER_INSTANCE_ID,
                    "service_status": service_status,
                    "loop_state": loop_state,
                    "started_at": RECORDER_STARTED_AT,
                    "active_jobs_count": active_count,
                    "recording_cameras_count": recording_count,
                    "failed_cameras_count": failed_count,
                    "last_error": truncate_error(redact_text(last_error_job.last_error)) if last_error_job else None,
                    "last_exit_code": last_exit_code,
                },
            )
    except Exception as exc:
        log_event("warning", "recorder_heartbeat_write_failed", error=redact_text(str(exc)))


def shutdown_handler(signum, frame) -> None:
    global running
    running = False
    log_event("info", "shutdown_signal_received", signal=signum)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

try:
    ensure_recording_metadata_schema()
    mark_stale_writing_segments_on_startup()
    reconcile_previous_recorder_instances()
    log_event("info", "metadata_schema_ready")
except Exception as exc:
    log_event("error", "metadata_schema_failed", error=str(exc))

log_event("info", "service_started", storage_root=str(STORAGE_ROOT))
write_audit_event(
    event_type="service_started",
    message="Recorder service started",
    metadata={"recorder_instance_id": RECORDER_INSTANCE_ID},
)

while running:
    try:
        check_children()
        confirm_recording_startups()
        sync_cameras()
        write_recorder_heartbeat("loop")
    except Exception as exc:
        log_event("error", "loop_error", error=str(exc))
        write_recorder_heartbeat("loop_error")
    time.sleep(LOOP_INTERVAL_SECONDS)

log_event("info", "shutdown_started", active_jobs=active_jobs_status())
write_recorder_heartbeat("shutdown")

for camera_id in list(jobs.keys()):
    stop_camera(camera_id, "shutdown")

log_event("info", "stopped")
write_audit_event(
    event_type="service_stopped",
    message="Recorder service stopped",
    metadata={"recorder_instance_id": RECORDER_INSTANCE_ID},
)
