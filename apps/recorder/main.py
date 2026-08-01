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
MEDIA_PROGRESS_HEARTBEAT_GRACE_SECONDS = 45
OWNERSHIP_KM_VMS = "KM VMS"
METADATA_SOURCE_RECORDER = "recorder"
SEGMENT_STATUS_WRITING = "writing"
SEGMENT_STATUS_FINALIZED = "finalized"
SEGMENT_STATUS_FAILED = "failed"
SEGMENT_STATUS_STALE_WRITING = "stale_writing"
STORAGE_NAMESPACE = "kmvms/recordings"
DEFAULT_ARCHIVE_ROOT_ID = "default"
RECORDING_FORMATS = {"mkv", "mp4"}
DEFAULT_RECORDING_FORMAT = "mkv"
FORMAT_METADATA = {
    "mkv": {"container_format": "mkv", "file_extension": ".mkv", "mime_type": "video/x-matroska", "segment_format": "matroska"},
    "mp4": {"container_format": "mp4", "file_extension": ".mp4", "mime_type": "video/mp4", "segment_format": "mp4"},
}
RETENTION_SIGNAL_TYPE = "retention_evaluate"
RETENTION_SIGNAL_SCOPE = {
    "camera_ids": [],
    "global": True,
    "physical_volume_ids": [],
    "root_ids": [],
    "scope_escalated": False,
    "segment_ids": [],
}
RETENTION_SIGNAL_SCOPE_JSON = json.dumps(
    RETENTION_SIGNAL_SCOPE,
    sort_keys=True,
    ensure_ascii=True,
    separators=(",", ":"),
)
RETENTION_SIGNAL_SCOPE_KEY = (
    "scope:"
    + hashlib.sha256(RETENTION_SIGNAL_SCOPE_JSON.encode("utf-8")).hexdigest()[:32]
)
RECORDER_RECEIPT_CONTRACT_VERSION = 1
RECEIPT_FINGERPRINT_CHUNK_BYTES = 64 * 1024

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
RECORDER_INSTANCE_ID = os.getenv("RECORDER_INSTANCE_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
RECORDER_STARTED_AT = time.time()


class RecorderState:
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    STOPPED = "stopped"
    RESTARTING = "restarting"
    ERROR = "error"
    DISABLED = "disabled"


UNSET = object()
ERROR_CLEARING_STATES = {
    RecorderState.STARTING,
    RecorderState.RECORDING,
    RecorderState.STOPPING,
    RecorderState.STOPPED,
    RecorderState.IDLE,
    RecorderState.DISABLED,
}
EXIT_CODE_CLEARING_STATES = {
    RecorderState.STARTING,
    RecorderState.RECORDING,
    RecorderState.STOPPED,
    RecorderState.IDLE,
    RecorderState.DISABLED,
}


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
    source_stream: str | None = None
    recording_format: str = DEFAULT_RECORDING_FORMAT
    segment_baseline_paths: set[str] = field(default_factory=set)
    known_segment_paths: set[str] = field(default_factory=set)
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


def create_recording_job(row, job: RecordingJob) -> str:
    if job.db_job_id:
        return job.db_job_id

    job_id = str(uuid.uuid4())
    job.db_job_id = job_id
    job.source_stream = selected_source_stream(row)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO recording_jobs (
                    id,
                    camera_id,
                    camera_name_snapshot,
                    camera_folder_snapshot,
                    state,
                    source_stream,
                    input_fingerprint,
                    recorder_instance_id,
                    started_at,
                    created_by,
                    ownership,
                    source,
                    created_at,
                    updated_at
                )
                VALUES (
                    :id,
                    :camera_id,
                    :camera_name,
                    :camera_folder,
                    :state,
                    :source_stream,
                    :input_fingerprint,
                    :recorder_instance_id,
                    NOW(),
                    :created_by,
                    :ownership,
                    :source,
                    NOW(),
                    NOW()
                )
                """
            ),
            {
                "id": job_id,
                "camera_id": job.camera_id,
                "camera_name": redact_text(job.camera_name),
                "camera_folder": job.folder_name,
                "state": RecorderState.STARTING,
                "source_stream": job.source_stream,
                "input_fingerprint": input_fingerprint(row),
                "recorder_instance_id": RECORDER_INSTANCE_ID,
                "created_by": OWNERSHIP_KM_VMS,
                "ownership": OWNERSHIP_KM_VMS,
                "source": METADATA_SOURCE_RECORDER,
            },
        )
    log_event("info", "recording_job_created", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job_id)
    return job_id


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
    if not job.db_job_id:
        return
    sanitized_last_error = (
        truncate_error(redact_text(last_error))
        if last_error is not UNSET and last_error
        else None
    )

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE recording_jobs
                SET state = COALESCE(:state, state),
                    stopped_at = CASE WHEN :stopped THEN NOW() ELSE stopped_at END,
                    stop_reason = CASE WHEN :stop_reason_set THEN :stop_reason ELSE stop_reason END,
                    last_error = CASE WHEN :last_error_set THEN :last_error ELSE last_error END,
                    last_error_type = CASE WHEN :last_error_type_set THEN :last_error_type ELSE last_error_type END,
                    ffmpeg_pid = CASE WHEN :ffmpeg_pid_set THEN :ffmpeg_pid ELSE ffmpeg_pid END,
                    last_exit_code = CASE WHEN :last_exit_code_set THEN :last_exit_code ELSE last_exit_code END,
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {
                "id": job.db_job_id,
                "state": state,
                "stopped": stopped,
                "stop_reason_set": stop_reason is not UNSET,
                "stop_reason": None if stop_reason is UNSET else stop_reason,
                "last_error_set": last_error is not UNSET,
                "last_error": sanitized_last_error,
                "last_error_type_set": last_error_type is not UNSET,
                "last_error_type": None if last_error_type is UNSET else last_error_type,
                "ffmpeg_pid_set": ffmpeg_pid is not UNSET,
                "ffmpeg_pid": None if ffmpeg_pid is UNSET else ffmpeg_pid,
                "last_exit_code_set": last_exit_code is not UNSET,
                "last_exit_code": None if last_exit_code is UNSET else last_exit_code,
            },
        )
    log_event("info", "recording_job_updated", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, state=state)


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
    job.segment_baseline_paths.clear()
    job.known_segment_paths.clear()


def clear_recording_job_error(job: RecordingJob) -> None:
    if not job.db_job_id:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE recording_jobs
                SET last_error = NULL,
                    last_error_type = NULL,
                    last_exit_code = NULL,
                    updated_at = NOW()
                WHERE id = :id
                """
            ),
            {"id": job.db_job_id},
        )


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


def read_recording_format() -> str:
    try:
        with engine.begin() as conn:
            row = conn.execute(text("SELECT recording_format FROM system_settings ORDER BY id ASC LIMIT 1")).first()
    except Exception as exc:
        log_event("warning", "recording_format_read_failed", error=redact_text(str(exc)), fallback=DEFAULT_RECORDING_FORMAT)
        return DEFAULT_RECORDING_FORMAT

    raw_value = row.recording_format if row else None
    recording_format = normalize_recording_format(raw_value)
    if raw_value is not None and str(raw_value or "").strip().lower() not in RECORDING_FORMATS:
        log_event("warning", "recording_format_invalid_fallback", value=redact_text(str(raw_value)), fallback=recording_format)
    return recording_format


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
    prefix = segment_prefix(job.camera_id, job.camera_name)
    files = [path for path in dir_path.glob(f"{prefix}*.*") if path.is_file() and path.suffix.lower() in {".mp4", ".mkv"}]
    return sorted(files, key=lambda path: (parse_segment_start_time(path), path.name))


def create_segment_metadata_if_needed(job: RecordingJob, file_path: Path) -> None:
    if not job.db_job_id or not job.archive_root_id:
        return
    rel_path = storage_relative_path(file_path, root=job.archive_root_path)
    if not rel_path or rel_path in job.segment_baseline_paths:
        return
    if rel_path in job.known_segment_paths:
        return

    try:
        stat = file_path.stat()
    except Exception as exc:
        log_event("warning", "metadata_write_failed", camera_id=job.camera_id, camera_name=job.camera_name, path=str(file_path), error=str(exc))
        return

    started_at = parse_segment_start_time(file_path)
    media_metadata = path_format_metadata(file_path)
    try:
        content_fingerprint = bounded_file_fingerprint(file_path, stat)
    except OSError:
        content_fingerprint = hashlib.sha256(
            f"unavailable:{int(stat.st_size)}:{int(stat.st_mtime_ns)}".encode("ascii")
        ).hexdigest()
    with engine.begin() as conn:
        existing = conn.execute(
            text(
                """
                SELECT id
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
            return

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
                "camera_name": redact_text(job.camera_name),
                "camera_folder": job.folder_name,
                "file_path": str(file_path),
                "relative_path": rel_path,
                "started_at": started_at,
                "size_bytes": max(int(stat.st_size), 0),
                "stream_type": job.source_stream or DEFAULT_RECORD_STREAM,
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
                "file_mtime": datetime.fromtimestamp(stat.st_mtime),
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
        job.current_media_size = int(stat.st_size)
        job.current_media_relative_path = rel_path
    log_event("info", "segment_detected", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, relative_path=rel_path, size_bytes=stat.st_size)


def update_writing_segment_progress(job: RecordingJob, file_path: Path) -> bool:
    if not job.db_job_id:
        return False
    rel_path = storage_relative_path(file_path, root=job.archive_root_path)
    if not rel_path or rel_path not in job.known_segment_paths:
        return False
    try:
        stat = file_path.stat()
    except OSError:
        return False
    if stat.st_size <= 0:
        return False
    file_mtime = datetime.fromtimestamp(stat.st_mtime)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE recording_segments
                SET size_bytes = :size_bytes,
                    file_mtime = :file_mtime,
                    file_size_verified_at = NOW(),
                    media_progress_at = NOW(),
                    updated_at = NOW()
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
    job.last_media_progress_at = time.time()
    job.current_media_size = int(stat.st_size)
    job.current_media_relative_path = rel_path
    return True


def finalize_segment_path(job: RecordingJob, file_path: Path) -> bool:
    if not job.db_job_id:
        return False
    rel_path = storage_relative_path(file_path, root=job.archive_root_path)
    if not rel_path:
        return False
    if rel_path in job.segment_baseline_paths or rel_path not in job.known_segment_paths:
        return False
    try:
        stat = file_path.stat()
    except Exception as exc:
        mark_segment_failed(job, rel_path, f"segment stat failed: {exc}")
        return False
    if stat.st_size <= 0:
        mark_segment_failed(job, rel_path, "segment file is empty")
        return False

    started_at = parse_segment_start_time(file_path)
    ended_at = datetime.fromtimestamp(stat.st_mtime)
    duration_sec = max(int((ended_at - started_at).total_seconds()), 0)
    media_metadata = path_format_metadata(file_path)
    try:
        final_fingerprint = bounded_file_fingerprint(file_path, stat)
    except OSError:
        final_fingerprint = hashlib.sha256(
            f"unavailable:{int(stat.st_size)}:{int(stat.st_mtime_ns)}".encode("ascii")
        ).hexdigest()
    with engine.begin() as conn:
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
                "content_probe_status": "stat_ok",
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
    if finalized_row is None:
        return False
    log_event("info", "segment_finalized", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, relative_path=rel_path, size_bytes=stat.st_size, duration_sec=duration_sec)
    return True


def mark_segment_failed(job: RecordingJob, rel_path: str, error: str) -> None:
    if not job.db_job_id:
        return
    if rel_path in job.segment_baseline_paths or rel_path not in job.known_segment_paths:
        return
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
                "error_message": truncate_error(redact_text(error)),
                "integrity_status": "partial_file",
                "reconciliation_status": "partial_file",
            },
        )
    if result.rowcount <= 0:
        return
    log_event("warning", "segment_failed", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id, relative_path=rel_path, error=error)
    write_audit_event(
        event_type="segment_failed",
        severity="warning",
        message=f"Recorder segment failed for camera {job.camera_name}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"job_id": job.db_job_id, "error_type": classify_error(error), "segment_name": Path(rel_path).name},
    )


def sync_segment_metadata_for_job(job: RecordingJob) -> None:
    if not job.db_job_id:
        return
    try:
        files = discover_job_segments(job)
        for file_path in files:
            create_segment_metadata_if_needed(job, file_path)
        if files:
            update_writing_segment_progress(job, files[-1])
        for file_path in files[:-1]:
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


def current_segment_age_seconds(file_path: Path) -> int:
    started_at = parse_segment_start_time(file_path)
    return max(0, int((datetime.now() - started_at).total_seconds()))


def stale_recovery_allowed(job: RecordingJob, now_ts: float) -> bool:
    if job.last_stale_recovery_at and now_ts - job.last_stale_recovery_at < 15 * 60:
        return False
    return job.stale_recovery_count < 3


def handle_stale_current_segment(row, job: RecordingJob) -> bool:
    if not job.process or job.process.poll() is not None:
        return False
    sync_segment_metadata_for_job(job)
    file_path = current_writing_segment(job)
    if file_path is None:
        return False
    age_sec = current_segment_age_seconds(file_path)
    threshold_sec = stale_segment_after_seconds(row)
    if age_sec <= threshold_sec:
        return False

    rel_path = storage_relative_path(file_path, root=job.archive_root_path)
    now_ts = time.time()
    event_metadata = {
        "reason": ERROR_STALE_SEGMENT,
        "age_seconds": age_sec,
        "stale_after_seconds": threshold_sec,
        "segment_name": file_path.name,
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

    if not stale_recovery_allowed(job, now_ts):
        job.set_state(RecorderState.ERROR, "stale writing segment recovery limit reached", ERROR_STALE_SEGMENT)
        update_camera_status_from_job(job)
        update_recording_job(job, state=RecorderState.ERROR, last_error=job.last_error, last_error_type=ERROR_STALE_SEGMENT)
        log_event("error", "stale_current_segment_recovery_blocked", camera_id=job.camera_id, camera_name=job.camera_name, db_job_id=job.db_job_id)
        return True

    job.stale_recovery_count += 1
    job.last_stale_recovery_at = now_ts
    stop_camera(row.id, ERROR_STALE_SEGMENT, audit_event="camera_restarted")
    log_event("warning", "stale_current_segment_recovery_attempted", camera_id=job.camera_id, camera_name=job.camera_name, attempt=job.stale_recovery_count)
    return False


def finalize_segments_for_job(job: RecordingJob) -> None:
    if not job.db_job_id:
        return
    sync_segment_metadata_for_job(job)
    for file_path in discover_job_segments(job):
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
                      AND created_at < NOW() - INTERVAL '10 minutes'
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
    if "401" in text_value or "unauthorized" in text_value or "authentication" in text_value:
        return ERROR_AUTH_FAILED
    if "connection timed out" in text_value or "timed out" in text_value or "timeout" in text_value:
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
    job.next_retry_at = time.time() + backoff
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

    try:
        create_recording_job(row, job)
    except Exception as exc:
        log_event("warning", "metadata_write_failed", camera_id=job.camera_id, camera_name=job.camera_name, error=str(exc))

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
    job.set_state(RecorderState.RECORDING)
    start_stderr_reader(job)
    update_camera_status_from_job(job)
    update_recording_job(
        job,
        state=RecorderState.RECORDING,
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


def stop_camera(camera_id: int, reason: str = "stopped", audit_event: str = "recording_stopped") -> None:
    job = jobs.get(camera_id)
    if not job:
        return

    proc = job.process
    job.set_state(RecorderState.STOPPING)
    update_camera_status_from_job(job)

    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    if proc:
        job.last_exit_code = proc.poll()

    if job.stderr_thread and job.stderr_thread.is_alive():
        job.stderr_thread.join(timeout=1)

    finalize_segments_for_job(job)

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
    close_recording_job(job, state=job.state, reason=reason)
    log_event("info", "recording_stopped", camera_id=job.camera_id, camera_name=job.camera_name, reason=reason, exit_code=job.last_exit_code)
    write_audit_event(
        event_type="camera_stopped" if audit_event == "recording_stopped" else audit_event,
        message=f"Recorder stopped recording camera {job.camera_name}: {reason}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"reason": reason, "exit_code": job.last_exit_code},
    )


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
        effective_recording_format = read_recording_format()
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
            job.recording_format = effective_recording_format
            desired_signature = camera_signature(row, effective_recording_format)

            if not row.enabled:
                job.retry_count = 0
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
                log_event("info", "recording_config_changed_restart", camera_id=job.camera_id, camera_name=job.camera_name, recording_format=effective_recording_format)
                write_audit_event(
                    event_type="camera_restarted",
                    message=f"Recorder restarted camera {job.camera_name}: config_changed",
                    camera_id=job.camera_id,
                    camera_name=job.camera_name,
                    metadata={"reason": "config_changed", "recording_format": effective_recording_format},
                )
                stop_camera(row.id, "config_changed")
                job.config_signature = desired_signature

            if job.state == RecorderState.RESTARTING and can_retry(job):
                start_camera(row)
                continue

            if job.process and job.process.poll() is not None:
                continue

            if not job.process or job.process.poll() is not None:
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
        if job.state != RecorderState.RECORDING or not proc or not job.started_at:
            continue
        if proc.poll() is not None:
            continue
        if time.time() - job.started_at < STARTUP_CONFIRM_SECONDS:
            continue
        if not job.last_media_progress_at or time.time() - job.last_media_progress_at > MEDIA_PROGRESS_HEARTBEAT_GRACE_SECONDS:
            continue
        if not (job.retry_count or job.next_retry_at or job.last_error or job.last_error_type):
            continue

        job.retry_count = 0
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
                        to_timestamp(:started_at),
                        NOW(),
                        :active_jobs_count,
                        :recording_cameras_count,
                        :failed_cameras_count,
                        :last_error,
                        :last_exit_code,
                        NOW()
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
