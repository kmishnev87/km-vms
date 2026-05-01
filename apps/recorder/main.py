from __future__ import annotations

import errno
import json
import os
import re
import signal
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

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class RecorderState:
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    STOPPED = "stopped"
    RESTARTING = "restarting"
    ERROR = "error"
    DISABLED = "disabled"


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

running = True
jobs: dict[int, "RecordingJob"] = {}


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
    config_signature: tuple[Any, ...] | None = None
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES))
    stderr_thread: threading.Thread | None = None

    def set_state(self, state: str, error: str | None = None, error_type: str | None = None) -> None:
        if self.state != state:
            self.last_state_change_at = time.time()
        self.state = state
        if error is not None:
            self.last_error = truncate_error(redact_text(error))
        if error_type is not None:
            self.last_error_type = error_type

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
    text_value = re.sub(r"([?&](?:token|access_token)=)[^&\s]+", r"\1***", text_value, flags=re.IGNORECASE)
    text_value = re.sub(r"(postgresql(?:\+\w+)?://[^:\s/@]+):([^@\s]+)@", r"\1:***@", text_value, flags=re.IGNORECASE)
    return text_value


def sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if re.search(r"(password|secret|token|authorization|jwt|encryption_key|key)", str(key), re.IGNORECASE):
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
                        'system',
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


def camera_archive_dir(folder_name: str) -> Path:
    path = STORAGE_ROOT / folder_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_segment_dir(folder_name: str) -> Path:
    now = datetime.now()
    path = camera_archive_dir(folder_name) / now.strftime("%Y-%m-%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_segment_pattern(camera_name: str, folder_name: str) -> str:
    dir_path = current_segment_dir(folder_name)
    safe_camera = safe_name(camera_name)
    return str(dir_path / f"{safe_camera}-%Y-%m-%d-%H-%M-%S.mp4")


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


def camera_signature(row) -> tuple[Any, ...]:
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
    )


def ffmpeg_cmd(camera) -> tuple[list[str], str] | tuple[None, None]:
    input_url = choose_input_url(camera)
    if not input_url:
        return None, None

    segment_minutes = int(camera.segment_minutes or 5)
    segment_seconds = max(segment_minutes, 1) * 60
    segment_pattern = build_segment_pattern(camera.name, camera.storage_folder_name)

    return [
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
        "-segment_format", "mp4",
        "-movflags", "+faststart",
        segment_pattern,
    ], segment_pattern


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
    if state == RecorderState.RECORDING:
        return "recording"
    if state == RecorderState.DISABLED:
        return "disabled"
    if state in {RecorderState.ERROR, RecorderState.RESTARTING}:
        return "error"
    if state == RecorderState.IDLE:
        return "idle"
    return "stopped"


def update_camera_status_from_job(job: RecordingJob) -> None:
    mark_camera_status(job.camera_id, external_camera_status(job.state), job.last_error)


def get_or_create_job(row) -> RecordingJob:
    job = jobs.get(row.id)
    if job is None:
        job = RecordingJob(
            camera_id=row.id,
            camera_name=row.name,
            folder_name=row.storage_folder_name,
            recording_mode=row.recording_mode,
            enabled=bool(row.enabled),
            config_signature=camera_signature(row),
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
        event_type="retry_scheduled",
        severity="warning",
        message=f"Recorder scheduled retry for camera {job.camera_name}: {error_type}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"error_type": error_type, "exit_code": exit_code, "retry_count": job.retry_count, "next_retry_at": iso_ts(job.next_retry_at)},
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
    log_event(
        "error",
        event,
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        error_type=error_type,
        error=error,
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
    schedule_retry(job, error, error_type)


def start_camera(row) -> None:
    job = get_or_create_job(row)
    proc = job.process
    if proc and proc.poll() is None:
        job.set_state(RecorderState.RECORDING)
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
        cmd, output_pattern = ffmpeg_cmd(row)
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
    job.config_signature = camera_signature(row)
    update_camera_status_from_job(job)

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
    log_event("info", "recording_started", camera_id=job.camera_id, camera_name=job.camera_name, pid=job.pid, output_pattern=job.current_output_path)
    write_audit_event(
        event_type="recording_started",
        message=f"Recorder started recording camera {job.camera_name}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"pid": job.pid, "state": job.state, "output_pattern": job.current_output_path},
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
    log_event("info", "recording_stopped", camera_id=job.camera_id, camera_name=job.camera_name, reason=reason, exit_code=job.last_exit_code)
    write_audit_event(
        event_type=audit_event,
        message=f"Recorder stopped recording camera {job.camera_name}: {reason}",
        camera_id=job.camera_id,
        camera_name=job.camera_name,
        metadata={"reason": reason, "exit_code": job.last_exit_code},
    )


def enforce_retention(camera_id: int, folder_name: str, retention_days: int, storage_quota_gb: int) -> None:
    root = camera_archive_dir(folder_name)
    if not root.exists():
        return

    files = sorted([p for p in root.rglob("*.mp4") if p.is_file()], key=lambda p: p.stat().st_mtime)

    if retention_days and retention_days > 0:
        cutoff = time.time() - (retention_days * 86400)
        for file_path in list(files):
            try:
                if file_path.stat().st_mtime < cutoff:
                    file_path.unlink(missing_ok=True)
            except Exception as exc:
                log_event("warning", "retention_delete_failed", camera_id=camera_id, path=str(file_path), error=str(exc))
        files = sorted([p for p in root.rglob("*.mp4") if p.is_file()], key=lambda p: p.stat().st_mtime)

    quota_bytes = max(int(storage_quota_gb), 50) * 1024 * 1024 * 1024
    total = 0
    for file_path in files:
        try:
            total += file_path.stat().st_size
        except Exception:
            pass

    while total > quota_bytes and files:
        oldest = files.pop(0)
        try:
            size = oldest.stat().st_size
            oldest.unlink(missing_ok=True)
            total -= size
        except Exception as exc:
            log_event("warning", "retention_delete_failed", camera_id=camera_id, path=str(oldest), error=str(exc))


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
            desired_signature = camera_signature(row)

            if not row.enabled:
                job.retry_count = 0
                job.next_retry_at = None
                if job.process and job.process.poll() is None:
                    stop_camera(row.id, RecorderState.DISABLED, audit_event="camera_disabled_recording_stopped")
                else:
                    job.set_state(RecorderState.DISABLED)
                    update_camera_status_from_job(job)
                continue

            if row.recording_mode != "always":
                job.retry_count = 0
                job.next_retry_at = None
                if job.process and job.process.poll() is None:
                    stop_camera(row.id, RecorderState.IDLE)
                else:
                    job.set_state(RecorderState.IDLE)
                    update_camera_status_from_job(job)
                continue

            if job.state == RecorderState.RESTARTING and not can_retry(job):
                continue

            if not retention_ready(row, job):
                continue

            if job.config_signature and job.config_signature != desired_signature and job.process and job.process.poll() is None:
                log_event("info", "recording_config_changed_restart", camera_id=job.camera_id, camera_name=job.camera_name)
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
        if not (job.retry_count or job.next_retry_at or job.last_error or job.last_error_type):
            continue

        job.retry_count = 0
        job.next_retry_at = None
        job.last_error = None
        job.last_error_type = None
        update_camera_status_from_job(job)
        log_event("info", "recording_start_confirmed", camera_id=job.camera_id, camera_name=job.camera_name, pid=job.pid)


def active_jobs_status() -> list[dict[str, Any]]:
    return [job.status_payload() for job in sorted(jobs.values(), key=lambda item: item.camera_id)]


def shutdown_handler(signum, frame) -> None:
    global running
    running = False
    log_event("info", "shutdown_signal_received", signal=signum)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

log_event("info", "service_started", storage_root=str(STORAGE_ROOT))

while running:
    try:
        check_children()
        confirm_recording_startups()
        sync_cameras()
    except Exception as exc:
        log_event("error", "loop_error", error=str(exc))
    time.sleep(LOOP_INTERVAL_SECONDS)

log_event("info", "shutdown_started", active_jobs=active_jobs_status())

for camera_id in list(jobs.keys()):
    stop_camera(camera_id, "shutdown")

log_event("info", "stopped")
