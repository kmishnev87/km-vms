from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveExportJob, RecordingSegment
from app.models.user import User
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.recording_storage import resolve_segment_file_path

OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
SEGMENT_STATUS_FINALIZED = "finalized"
EXPORT_STATUS_QUEUED = "queued"
EXPORT_STATUS_RUNNING = "running"
EXPORT_STATUS_DONE = "done"
EXPORT_STATUS_FAILED = "failed"
EXPORT_STATUS_EXPIRED = "expired"
EXPORT_STATUSES = frozenset(
    {
        EXPORT_STATUS_QUEUED,
        EXPORT_STATUS_RUNNING,
        EXPORT_STATUS_DONE,
        EXPORT_STATUS_FAILED,
        EXPORT_STATUS_EXPIRED,
    }
)

MAX_EXPORT_DURATION_SECONDS = 30 * 60
MAX_SOURCE_SEGMENTS = 120
MAX_ESTIMATED_SOURCE_BYTES = 4 * 1024 * 1024 * 1024
MAX_ACTIVE_JOBS_PER_USER = 3
MAX_NOTE_LENGTH = 500
MAX_TITLE_LENGTH = 200
EXPORT_JOB_EXPIRES_AFTER = timedelta(hours=24)
FUTURE_RANGE_TOLERANCE = timedelta(hours=24)
GAP_TOLERANCE_SECONDS = 2
ALLOWED_FORMAT_HINTS = frozenset({"mkv", "mp4"})
GENERATION_TIMEOUT_SECONDS = 120
OUTPUT_DURATION_TOLERANCE_SECONDS = 5
EXPORT_OUTPUT_DIR = "stage11_stage2_clips"
EXPORT_TEMP_DIR = "stage11_stage2_tmp"
EXPORT_MANIFEST_DIR = "stage11_stage3_manifests"
MANIFEST_SCHEMA_VERSION = "1.0"
MANIFEST_TYPE = "evidence_export_manifest"
MANIFEST_FORBIDDEN_STRINGS = (
    "rtsp://",
    "password",
    "secret",
    "token",
    "jwt",
    "authorization",
    "cookie",
    ".env",
    "internal_output_path",
    "internal_manifest_path",
    "file_path",
    "relative_path",
    "ffmpeg stderr",
    "traceback",
    "/volume",
)
EXPORT_CLEANUP_MAX_JOBS = 100
EXPORT_OWNED_DIRS = frozenset({EXPORT_OUTPUT_DIR, EXPORT_MANIFEST_DIR})
PROBLEM_INTEGRITY_STATUSES = {
    "missing_file",
    "orphan_metadata",
    "orphan_file",
    "pre_metadata_km_vms_file",
    "legacy_archive_file",
    "foreign_file",
    "unknown_file",
    "zero_size_file",
    "partial_file",
    "corrupted_file",
    "stale_writing_segment",
    "invalid_path",
    "path_outside_storage",
    "unreadable_file",
    "storage_unavailable",
}
TECHNICAL_DELETED_CAMERA_RE = re.compile(r"__deleted_\d+_\d+$")
_GENERATION_LOCKS: dict[str, threading.Lock] = {}
_GENERATION_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ExportPreflight:
    segments: list[RecordingSegment]
    estimated_source_bytes: int
    gap_warnings: list[dict[str, Any]]


@dataclass(frozen=True)
class ResolvedExportSegment:
    segment: RecordingSegment
    path: Path
    start_ts: datetime
    end_ts: datetime
    source_size: int
    source_mtime_ns: int


def _safe_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _generation_lock(job_id: str) -> threading.Lock:
    with _GENERATION_LOCKS_GUARD:
        lock = _GENERATION_LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _GENERATION_LOCKS[job_id] = lock
        return lock


def _safe_public_message(error_code: str) -> str:
    messages = {
        "source_missing": "Source recording is unavailable",
        "source_gap_detected": "Requested range is not fully covered by source recordings",
        "incompatible_segments": "Source segments are not compatible for safe generation",
        "generation_timeout": "Clip generation timed out",
        "generation_failed": "Clip generation failed",
        "output_validation_failed": "Generated clip validation failed",
        "expired_job": "Export job is expired",
        "invalid_job_status": "Export job status does not allow generation",
    }
    return messages.get(error_code, "Clip generation failed")


def _iso(value: datetime | None) -> str | None:
    return value.replace(microsecond=0).isoformat() if value else None


def _safe_text(value: str | None, *, max_length: int = 255) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[\x00-\x1f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length] if text else None


def _manifest_text_has_forbidden(text: str) -> bool:
    lowered = text.lower()
    dynamic_forbidden = {
        str(_export_root()).lower(),
        str(settings.storage_root).lower(),
        str(settings.storage_exports).lower(),
        str(settings.storage_previews).lower(),
    }
    return any(pattern in lowered for pattern in MANIFEST_FORBIDDEN_STRINGS) or any(
        pattern and pattern in lowered for pattern in dynamic_forbidden
    )


def _safe_download_filename(job: ArchiveExportJob, suffix: str) -> str:
    start = job.start_ts.strftime("%Y%m%dT%H%M%S") if job.start_ts else "unknown-start"
    end = job.end_ts.strftime("%Y%m%dT%H%M%S") if job.end_ts else "unknown-end"
    camera = re.sub(r"[^A-Za-z0-9_-]+", "_", str(job.camera_label_snapshot or "camera")).strip("_")[:40]
    camera = camera or f"camera_{job.camera_id or 'unknown'}"
    return f"km-vms-evidence-{camera}-{start}-{end}-{job.id[:8]}.{suffix}"


def _mark_failed(db: Session, job: ArchiveExportJob, error_code: str) -> ArchiveExportJob:
    job.status = EXPORT_STATUS_FAILED
    job.progress_percent = 0
    job.error_code = error_code
    job.sanitized_error_message = _safe_public_message(error_code)
    job.updated_at = _utcnow_naive()
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _export_root() -> Path:
    root = Path(settings.storage_exports).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_child(root: Path, *parts: str) -> Path:
    target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("export_path_outside_root") from exc
    return target


def _relative_to_export_root(path: Path) -> str:
    root = _export_root()
    return path.resolve().relative_to(root).as_posix()


def _path_from_internal(internal_path: str | None) -> Path | None:
    if not internal_path:
        return None
    root = _export_root()
    target = _safe_child(root, str(internal_path).replace("\\", "/").lstrip("/"))
    return target


def _run_media_tool(args: list[str], *, timeout: int = GENERATION_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )


def _ffprobe_duration(path: Path) -> float | None:
    result = _run_media_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        return float(str(result.stdout or "").strip())
    except ValueError:
        return None


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_export_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise _safe_error(422, f"Invalid {field_name}")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _segment_end(segment: RecordingSegment) -> datetime | None:
    if segment.ended_at and segment.ended_at > segment.started_at:
        return segment.ended_at
    if segment.finalized_at and segment.finalized_at > segment.started_at:
        return segment.finalized_at
    if segment.duration_sec and segment.duration_sec > 0:
        return segment.started_at + timedelta(seconds=int(segment.duration_sec))
    return None


def _segment_interval(segment: RecordingSegment) -> tuple[datetime, datetime] | None:
    if not segment.started_at:
        return None
    ended = _segment_end(segment)
    if not ended or ended <= segment.started_at:
        return None
    return segment.started_at, ended


def safe_camera_label(camera: Camera) -> str:
    text = str(getattr(camera, "name", "") or "").strip()
    if not text or TECHNICAL_DELETED_CAMERA_RE.search(text):
        return f"Camera {camera.id}"
    return text[:255]


def clean_optional_text(value: str | None, *, max_length: int, field_name: str) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_length:
        raise _safe_error(422, f"{field_name} is too long")
    return text or None


def finalized_export_segments_query(db: Session):
    return db.query(RecordingSegment).filter(
        RecordingSegment.ownership == OWNERSHIP_KM_VMS,
        RecordingSegment.source == RECORDER_SOURCE,
        RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
        RecordingSegment.relative_path.isnot(None),
        RecordingSegment.deleted_at.is_(None),
        or_(
            RecordingSegment.integrity_status.is_(None),
            ~RecordingSegment.integrity_status.in_(PROBLEM_INTEGRITY_STATUSES),
        ),
    )


def _gap_warnings(intervals: list[tuple[datetime, datetime]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    previous_end: datetime | None = None
    for start_dt, end_dt in sorted(intervals, key=lambda item: item[0]):
        if previous_end is not None:
            gap = int((start_dt - previous_end).total_seconds())
            if gap > GAP_TOLERANCE_SECONDS:
                warnings.append({"type": "source_gap", "gap_seconds": gap})
        previous_end = max(previous_end or end_dt, end_dt)
    return warnings[:20]


def _validate_full_coverage(job: ArchiveExportJob, intervals: list[tuple[datetime, datetime]]) -> list[dict[str, Any]]:
    warnings = _gap_warnings(intervals)
    if not intervals:
        raise ValueError("source_gap_detected")
    ordered = sorted(intervals, key=lambda item: item[0])
    tolerance = timedelta(seconds=GAP_TOLERANCE_SECONDS)
    if ordered[0][0] > job.start_ts + tolerance:
        raise ValueError("source_gap_detected")
    if ordered[-1][1] < job.end_ts - tolerance:
        raise ValueError("source_gap_detected")
    if warnings:
        raise ValueError("source_gap_detected")
    return warnings


def preflight_source_segments(
    db: Session,
    *,
    camera_id: int,
    start_ts: datetime,
    end_ts: datetime,
) -> ExportPreflight:
    candidates = (
        finalized_export_segments_query(db)
        .filter(RecordingSegment.camera_id == camera_id, RecordingSegment.started_at < end_ts)
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
        .all()
    )

    selected: list[RecordingSegment] = []
    intervals: list[tuple[datetime, datetime]] = []
    for segment in candidates:
        interval = _segment_interval(segment)
        if not interval:
            continue
        segment_start, segment_end = interval
        if segment_end <= start_ts or segment_start >= end_ts:
            continue
        selected.append(segment)
        intervals.append((max(segment_start, start_ts), min(segment_end, end_ts)))

    if not selected:
        raise _safe_error(404, "No exportable source segments found")
    if len(selected) > MAX_SOURCE_SEGMENTS:
        raise _safe_error(413, "Too many source segments for export")

    estimated_bytes = 0
    for segment in selected:
        try:
            file_path = resolve_segment_file_path(db, segment, require_exists=True)
        except (FileNotFoundError, ValueError):
            raise _safe_error(409, "Source recording file unavailable")
        try:
            file_size = int(Path(file_path).stat().st_size)
        except OSError:
            raise _safe_error(409, "Source recording file unavailable")
        estimated_bytes += max(file_size, int(segment.size_bytes or 0))
        if estimated_bytes > MAX_ESTIMATED_SOURCE_BYTES:
            raise _safe_error(413, "Estimated export source size is too large")

    return ExportPreflight(
        segments=selected,
        estimated_source_bytes=estimated_bytes,
        gap_warnings=_gap_warnings(intervals),
    )


def revalidate_job_sources(db: Session, job: ArchiveExportJob) -> list[ResolvedExportSegment]:
    ids = [int(value) for value in (job.source_segment_ids or []) if str(value).isdigit()]
    if not ids:
        raise ValueError("source_missing")
    rows = (
        finalized_export_segments_query(db)
        .filter(RecordingSegment.id.in_(ids), RecordingSegment.camera_id == job.camera_id)
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
        .all()
    )
    if len(rows) != len(set(ids)):
        raise ValueError("source_missing")

    resolved: list[ResolvedExportSegment] = []
    intervals: list[tuple[datetime, datetime]] = []
    estimated_bytes = 0
    for segment in rows:
        interval = _segment_interval(segment)
        if not interval:
            raise ValueError("source_missing")
        segment_start, segment_end = interval
        if segment_end <= job.start_ts or segment_start >= job.end_ts:
            continue
        try:
            source_path = resolve_segment_file_path(db, segment, require_exists=True)
            stat = source_path.stat()
        except (FileNotFoundError, ValueError, OSError) as exc:
            raise ValueError("source_missing") from exc
        estimated_bytes += max(int(stat.st_size), int(segment.size_bytes or 0))
        intervals.append((max(segment_start, job.start_ts), min(segment_end, job.end_ts)))
        resolved.append(
            ResolvedExportSegment(
                segment=segment,
                path=source_path,
                start_ts=segment_start,
                end_ts=segment_end,
                source_size=int(stat.st_size),
                source_mtime_ns=int(stat.st_mtime_ns),
            )
        )
    if not resolved or estimated_bytes > MAX_ESTIMATED_SOURCE_BYTES or len(resolved) > MAX_SOURCE_SEGMENTS:
        raise ValueError("source_missing")
    _validate_full_coverage(job, intervals)
    return resolved


def validate_export_range(start_ts: datetime, end_ts: datetime) -> tuple[datetime, datetime, int]:
    start_dt = normalize_export_datetime(start_ts, "start_ts")
    end_dt = normalize_export_datetime(end_ts, "end_ts")
    if end_dt <= start_dt:
        raise _safe_error(422, "end_ts must be greater than start_ts")
    duration = int((end_dt - start_dt).total_seconds())
    if duration > MAX_EXPORT_DURATION_SECONDS:
        raise _safe_error(413, "Export range is too long")
    if start_dt > _utcnow_naive() + FUTURE_RANGE_TOLERANCE:
        raise _safe_error(422, "Export range is too far in the future")
    return start_dt, end_dt, duration


def assert_active_job_limit(db: Session, actor: User) -> None:
    active_count = (
        db.query(ArchiveExportJob)
        .filter(
            ArchiveExportJob.actor_user_id == getattr(actor, "id", None),
            ArchiveExportJob.status.in_((EXPORT_STATUS_QUEUED, EXPORT_STATUS_RUNNING)),
        )
        .count()
    )
    if active_count >= MAX_ACTIVE_JOBS_PER_USER:
        raise _safe_error(429, "Too many active export jobs")


def create_archive_export_job(
    db: Session,
    *,
    actor: User,
    camera_id: int,
    start_ts: datetime,
    end_ts: datetime,
    title: str | None = None,
    reason: str | None = None,
    note: str | None = None,
    format_hint: str | None = None,
    request=None,
) -> ArchiveExportJob:
    start_dt, end_dt, duration = validate_export_range(start_ts, end_ts)
    title_text = clean_optional_text(title, max_length=MAX_TITLE_LENGTH, field_name="title")
    reason_text = clean_optional_text(reason or note, max_length=MAX_NOTE_LENGTH, field_name="reason")
    hint = str(format_hint or "").strip().lower() or None
    if hint and hint not in ALLOWED_FORMAT_HINTS:
        raise _safe_error(422, "Unsupported format_hint")

    camera = db.query(Camera).filter(Camera.id == int(camera_id), Camera.deleted_at.is_(None)).first()
    if not camera:
        raise _safe_error(404, "Camera not found")

    assert_active_job_limit(db, actor)
    preflight = preflight_source_segments(db, camera_id=camera.id, start_ts=start_dt, end_ts=end_dt)
    now = _utcnow_naive()
    job = ArchiveExportJob(
        id=str(uuid.uuid4()),
        actor_user_id=getattr(actor, "id", None),
        camera_id=camera.id,
        camera_label_snapshot=safe_camera_label(camera),
        start_ts=start_dt,
        end_ts=end_dt,
        duration_seconds=duration,
        status=EXPORT_STATUS_QUEUED,
        progress_percent=0,
        title=title_text,
        reason=reason_text,
        format_hint=hint,
        source_segment_ids=[segment.id for segment in preflight.segments],
        source_segment_count=len(preflight.segments),
        estimated_source_bytes=preflight.estimated_source_bytes,
        gap_warnings=preflight.gap_warnings,
        created_at=now,
        updated_at=now,
        expires_at=now + EXPORT_JOB_EXPIRES_AFTER,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    create_event(
        db=db,
        actor=actor,
        category="archive",
        event_type="archive_export_requested",
        severity="info",
        message_ru="Archive export requested",
        message_en="Archive export requested",
        target_type="archive_export_job",
        target_id=job.id,
        target_name=job.camera_label_snapshot,
        metadata={
            "export_job_id": job.id,
            "camera_id": job.camera_id,
            "camera_label": job.camera_label_snapshot,
            "start_ts": job.start_ts,
            "end_ts": job.end_ts,
            "duration_seconds": job.duration_seconds,
            "source_segment_count": job.source_segment_count,
            "estimated_source_bytes": job.estimated_source_bytes,
            "status": job.status,
            "gap_warning_count": len(job.gap_warnings or []),
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return job


def _trim_segment(
    *,
    source: ResolvedExportSegment,
    job: ArchiveExportJob,
    target: Path,
) -> None:
    clip_start = max(job.start_ts, source.start_ts)
    clip_end = min(job.end_ts, source.end_ts)
    if clip_end <= clip_start:
        raise RuntimeError("source_gap_detected")
    offset = max(0.0, (clip_start - source.start_ts).total_seconds())
    duration = max(0.1, (clip_end - clip_start).total_seconds())
    result = _run_media_tool(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-ss",
            f"{offset:.3f}",
            "-i",
            str(source.path),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(target),
        ]
    )
    if result.returncode != 0 or not target.exists() or target.stat().st_size <= 0:
        raise RuntimeError("incompatible_segments")


def _concat_segments(parts: list[Path], output: Path, concat_file: Path) -> None:
    lines = []
    for part in parts:
        escaped = str(part).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run_media_tool(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output),
        ]
    )
    if result.returncode != 0 or not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError("incompatible_segments")


def _validate_output(job: ArchiveExportJob, output: Path) -> tuple[int, float | None]:
    root = _export_root()
    try:
        output.resolve().relative_to(root)
    except ValueError as exc:
        raise RuntimeError("output_validation_failed") from exc
    if not output.exists() or not output.is_file():
        raise RuntimeError("output_validation_failed")
    size = int(output.stat().st_size)
    if size <= 0:
        raise RuntimeError("output_validation_failed")
    duration = _ffprobe_duration(output)
    if duration is None or duration <= 0:
        raise RuntimeError("output_validation_failed")
    requested = max(0, int((job.end_ts - job.start_ts).total_seconds()))
    if requested and abs(duration - requested) > max(OUTPUT_DURATION_TOLERANCE_SECONDS, requested * 0.5):
        raise RuntimeError("output_validation_failed")
    return size, duration


def _assert_sources_unchanged(sources: list[ResolvedExportSegment]) -> None:
    for source in sources:
        stat = source.path.stat()
        if int(stat.st_size) != source.source_size or int(stat.st_mtime_ns) != source.source_mtime_ns:
            raise RuntimeError("source_mutated")


def generate_archive_export_job(
    db: Session,
    *,
    export_id: str,
    actor: User,
    request=None,
) -> ArchiveExportJob:
    lock = _generation_lock(export_id)
    if not lock.acquire(blocking=False):
        raise _safe_error(409, "Export job is already generating")
    try:
        job = db.get(ArchiveExportJob, export_id)
        if not job:
            raise _safe_error(404, "Export job not found")

        existing_output = _path_from_internal(job.internal_output_path)
        if job.status == EXPORT_STATUS_DONE and existing_output and existing_output.exists() and existing_output.is_file():
            return job
        if job.status == EXPORT_STATUS_EXPIRED:
            raise _safe_error(409, "Export job is expired")
        if job.status == EXPORT_STATUS_RUNNING:
            raise _safe_error(409, "Export job is already generating")
        if job.status != EXPORT_STATUS_QUEUED:
            raise _safe_error(409, "Export job status does not allow generation")

        output_path: Path | None = None
        temp_dir: Path | None = None
        try:
            sources = revalidate_job_sources(db, job)
            now = _utcnow_naive()
            job.status = EXPORT_STATUS_RUNNING
            job.progress_percent = 10
            job.error_code = None
            job.sanitized_error_message = None
            job.updated_at = now
            db.add(job)
            db.commit()
            db.refresh(job)

            create_event(
                db=db,
                actor=actor,
                category="archive",
                event_type="archive_export_generation_started",
                severity="info",
                message_ru="Archive export generation started",
                message_en="Archive export generation started",
                target_type="archive_export_job",
                target_id=job.id,
                target_name=job.camera_label_snapshot,
                metadata={"export_job_id": job.id, "camera_id": job.camera_id, "source_segment_count": len(sources), "status": job.status},
                ip_address=request_ip(request),
                user_agent=request_user_agent(request),
            )

            root = _export_root()
            output_dir = _safe_child(root, EXPORT_OUTPUT_DIR)
            output_dir.mkdir(parents=True, exist_ok=True)
            temp_dir = _safe_child(root, EXPORT_TEMP_DIR, f"stage11_stage2_{uuid.uuid4().hex}")
            temp_dir.mkdir(parents=True, exist_ok=False)
            output_path = _safe_child(output_dir, f"stage11_clip_{job.id}.mkv")
            part_paths = [_safe_child(temp_dir, f"part_{index:03d}.mkv") for index, _source in enumerate(sources)]
            for source, part_path in zip(sources, part_paths):
                _trim_segment(source=source, job=job, target=part_path)
            if len(part_paths) == 1:
                shutil.move(str(part_paths[0]), str(output_path))
            else:
                _concat_segments(part_paths, output_path, _safe_child(temp_dir, "concat.txt"))

            size, _duration = _validate_output(job, output_path)
            _assert_sources_unchanged(sources)
            job.status = EXPORT_STATUS_DONE
            job.progress_percent = 100
            job.internal_output_path = _relative_to_export_root(output_path)
            job.internal_checksum = _sha256(output_path)
            job.internal_manifest_path = None
            job.error_code = None
            job.sanitized_error_message = None
            job.updated_at = _utcnow_naive()
            db.add(job)
            db.commit()
            db.refresh(job)

            create_event(
                db=db,
                actor=actor,
                category="archive",
                event_type="archive_export_generation_completed",
                severity="info",
                message_ru="Archive export generation completed",
                message_en="Archive export generation completed",
                target_type="archive_export_job",
                target_id=job.id,
                target_name=job.camera_label_snapshot,
                metadata={
                    "export_job_id": job.id,
                    "camera_id": job.camera_id,
                    "source_segment_count": len(sources),
                    "output_size_bytes": size,
                    "status": job.status,
                },
                ip_address=request_ip(request),
                user_agent=request_user_agent(request),
            )
            return job
        except subprocess.TimeoutExpired:
            if output_path and output_path.exists():
                output_path.unlink(missing_ok=True)
            job = _mark_failed(db, job, "generation_timeout")
            create_event(
                db=db,
                actor=actor,
                category="archive",
                event_type="archive_export_generation_failed",
                severity="warning",
                message_ru="Archive export generation failed",
                message_en="Archive export generation failed",
                target_type="archive_export_job",
                target_id=job.id,
                target_name=job.camera_label_snapshot,
                metadata={"export_job_id": job.id, "camera_id": job.camera_id, "error_code": job.error_code, "status": job.status},
                ip_address=request_ip(request),
                user_agent=request_user_agent(request),
            )
            return job
        except (RuntimeError, ValueError) as exc:
            error_code = str(exc) if str(exc) else "generation_failed"
            if error_code not in {
                "source_missing",
                "source_gap_detected",
                "incompatible_segments",
                "generation_timeout",
                "generation_failed",
                "output_validation_failed",
                "source_mutated",
            }:
                error_code = "generation_failed"
            if output_path and output_path.exists():
                output_path.unlink(missing_ok=True)
            job = _mark_failed(db, job, "output_validation_failed" if error_code == "source_mutated" else error_code)
            create_event(
                db=db,
                actor=actor,
                category="archive",
                event_type="archive_export_generation_failed",
                severity="warning",
                message_ru="Archive export generation failed",
                message_en="Archive export generation failed",
                target_type="archive_export_job",
                target_id=job.id,
                target_name=job.camera_label_snapshot,
                metadata={"export_job_id": job.id, "camera_id": job.camera_id, "error_code": job.error_code, "status": job.status},
                ip_address=request_ip(request),
                user_agent=request_user_agent(request),
            )
            return job
        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
    finally:
        lock.release()


def _manifest_output_metadata(db: Session, job: ArchiveExportJob) -> tuple[Path, dict[str, Any]]:
    if job.status == EXPORT_STATUS_EXPIRED:
        raise _safe_error(409, "expired_job")
    if job.status != EXPORT_STATUS_DONE:
        raise _safe_error(409, "invalid_job_status")
    try:
        output_path = _path_from_internal(job.internal_output_path)
    except RuntimeError as exc:
        raise _safe_error(409, "output_invalid") from exc
    if not output_path:
        raise _safe_error(409, "output_missing")
    try:
        size, duration = _validate_output(job, output_path)
    except RuntimeError as exc:
        raise _safe_error(409, "output_invalid") from exc
    checksum = _sha256(output_path)
    if job.internal_checksum and checksum != job.internal_checksum:
        raise _safe_error(409, "checksum_mismatch")
    if not job.internal_checksum:
        job.internal_checksum = checksum
        job.updated_at = _utcnow_naive()
        db.add(job)
        db.commit()
        db.refresh(job)
    return output_path, {
        "artifact_name": output_path.name,
        "container": output_path.suffix.lstrip(".").lower() or None,
        "size_bytes": size,
        "duration_seconds": duration,
        "sha256": checksum,
    }


def _source_manifest_rows(db: Session, job: ArchiveExportJob) -> list[dict[str, Any]]:
    ids = [int(value) for value in (job.source_segment_ids or []) if str(value).isdigit()]
    rows = (
        finalized_export_segments_query(db)
        .filter(RecordingSegment.id.in_(ids), RecordingSegment.camera_id == job.camera_id)
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
        .all()
    )
    if len(rows) != len(set(ids)):
        raise _safe_error(409, "source_metadata_unavailable")
    result: list[dict[str, Any]] = []
    for segment in rows:
        overlap_start = max(job.start_ts, segment.started_at) if segment.started_at else None
        ended = _segment_end(segment)
        overlap_end = min(job.end_ts, ended) if ended else None
        overlap_duration = None
        if overlap_start and overlap_end and overlap_end > overlap_start:
            overlap_duration = int((overlap_end - overlap_start).total_seconds())
        result.append(
            {
                "id": segment.id,
                "camera_id": segment.camera_id,
                "started_at": _iso(segment.started_at),
                "ended_at": _iso(ended),
                "duration_sec": segment.duration_sec,
                "size_bytes": segment.size_bytes,
                "stream_type": _safe_text(segment.stream_type, max_length=50),
                "container": _safe_text(segment.container_format, max_length=32),
                "mime_type": _safe_text(segment.mime_type, max_length=100),
                "extension": _safe_text(segment.file_extension, max_length=16),
                "checksum": _safe_text(segment.checksum, max_length=128),
                "overlap_start": _iso(overlap_start),
                "overlap_end": _iso(overlap_end),
                "overlap_duration_seconds": overlap_duration,
            }
        )
    return result


def _audit_manifest_summary(db: Session, job: ArchiveExportJob) -> dict[str, Any]:
    event_types = (
        "archive_export_requested",
        "archive_export_generation_started",
        "archive_export_generation_completed",
        "archive_export_generation_failed",
        "archive_export_manifest_created",
        "archive_export_manifest_failed",
    )
    rows = (
        db.query(AuditEvent)
        .filter(AuditEvent.category == "archive", AuditEvent.target_id == job.id, AuditEvent.event_type.in_(event_types))
        .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        .all()
    )
    counts = {event_type: 0 for event_type in event_types}
    references: list[dict[str, Any]] = []
    for row in rows:
        counts[row.event_type] = counts.get(row.event_type, 0) + 1
        references.append({"id": row.id, "event_type": row.event_type, "created_at": _iso(row.created_at)})
    return {"event_counts": counts, "references": references[:50]}


def _build_manifest(db: Session, job: ArchiveExportJob, actor: User | None) -> tuple[dict[str, Any], Path]:
    output_path, output = _manifest_output_metadata(db, job)
    source_segments = _source_manifest_rows(db, job)
    actor_row = db.get(User, job.actor_user_id) if job.actor_user_id else None
    now = _utcnow_naive()
    manifest_path = _safe_child(_export_root(), EXPORT_MANIFEST_DIR, f"stage11_manifest_{job.id}.json")
    manifest = {
        "actor": {
            "user_id": getattr(actor_row, "id", None),
            "username": _safe_text(getattr(actor_row, "username", None), max_length=100),
            "role": _safe_text(getattr(actor_row, "role", None), max_length=50),
        },
        "audit": _audit_manifest_summary(db, job),
        "camera": {"id": job.camera_id, "label": _safe_text(job.camera_label_snapshot, max_length=255)},
        "diagnostics": {
            "generated_output_validation": "passed",
            "manifest_path_policy": "export_root_relative",
            "read_contract": "protected_manifest_endpoint",
        },
        "export_job": {
            "id": job.id,
            "status": job.status,
            "title": _safe_text(job.title, max_length=MAX_TITLE_LENGTH),
            "reason": _safe_text(job.reason, max_length=MAX_NOTE_LENGTH),
            "requested_start_ts": _iso(job.start_ts),
            "requested_end_ts": _iso(job.end_ts),
            "duration_seconds": job.duration_seconds,
            "created_at": _iso(job.created_at),
            "generated_at": _iso(job.updated_at),
            "manifest_created_at": _iso(now),
        },
        "integrity": {
            "generated_clip_sha256": output["sha256"],
            "checksum_algorithm": "sha256",
            "checksum_status": "verified",
            "source_metadata_status": "verified",
        },
        "manifest_type": MANIFEST_TYPE,
        "output": output,
        "product": "KM VMS",
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": {
            "source_segment_count": job.source_segment_count,
            "source_segment_ids": [row["id"] for row in source_segments],
            "segments": source_segments,
            "gap_warnings": job.gap_warnings or [],
        },
        "watermark": {"status": "not_embedded_stage3"},
    }
    return manifest, manifest_path


def _manifest_json_bytes(manifest: dict[str, Any]) -> bytes:
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if _manifest_text_has_forbidden(text):
        raise _safe_error(409, "manifest_forbidden_content")
    return text.encode("utf-8")


def _read_manifest_file(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _safe_error(409, "manifest_missing") from exc
    if data.startswith(b"\xef\xbb\xbf"):
        raise _safe_error(409, "manifest_invalid")
    try:
        text = data.decode("utf-8")
        manifest = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _safe_error(409, "manifest_invalid") from exc
    if not isinstance(manifest, dict) or _manifest_text_has_forbidden(text):
        raise _safe_error(409, "manifest_invalid")
    return manifest


def create_archive_export_manifest(db: Session, *, export_id: str, actor: User, request=None) -> dict[str, Any]:
    job = db.get(ArchiveExportJob, export_id)
    if not job:
        raise _safe_error(404, "Export job not found")
    manifest: dict[str, Any] | None = None
    try:
        manifest, manifest_path = _build_manifest(db, job, actor)
        payload = _manifest_json_bytes(manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = _safe_child(manifest_path.parent, f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(payload)
            verified = _read_manifest_file(temp_path)
            if verified != manifest:
                raise _safe_error(409, "manifest_write_failed")
            temp_path.replace(manifest_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        job.internal_manifest_path = _relative_to_export_root(manifest_path)
        job.updated_at = _utcnow_naive()
        db.add(job)
        db.commit()
        db.refresh(job)
        create_event(
            db=db,
            actor=actor,
            category="archive",
            event_type="archive_export_manifest_created",
            severity="info",
            message_ru="Archive export evidence manifest created",
            message_en="Archive export evidence manifest created",
            target_type="archive_export_job",
            target_id=job.id,
            target_name=job.camera_label_snapshot,
            metadata={
                "export_job_id": job.id,
                "camera_id": job.camera_id,
                "source_segment_count": job.source_segment_count,
                "output_size_bytes": manifest["output"]["size_bytes"],
                "clip_checksum": manifest["integrity"]["generated_clip_sha256"],
                "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
                "status": "created",
            },
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        return manifest
    except HTTPException as exc:
        create_event(
            db=db,
            actor=actor,
            category="archive",
            event_type="archive_export_manifest_failed",
            severity="warning",
            message_ru="Archive export evidence manifest failed",
            message_en="Archive export evidence manifest failed",
            target_type="archive_export_job",
            target_id=job.id,
            target_name=job.camera_label_snapshot,
            metadata={"export_job_id": job.id, "camera_id": job.camera_id, "error_code": str(exc.detail), "status": "failed"},
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise


def read_archive_export_manifest(db: Session, *, export_id: str) -> dict[str, Any]:
    job = db.get(ArchiveExportJob, export_id)
    if not job:
        raise _safe_error(404, "Export job not found")
    try:
        manifest_path = _path_from_internal(job.internal_manifest_path)
    except RuntimeError as exc:
        raise _safe_error(409, "manifest_invalid") from exc
    if not manifest_path or not manifest_path.exists() or not manifest_path.is_file():
        raise _safe_error(409, "manifest_missing")
    manifest = _read_manifest_file(manifest_path)
    _output_path, output = _manifest_output_metadata(db, job)
    manifest_checksum = str((manifest.get("integrity") or {}).get("generated_clip_sha256") or "")
    if manifest_checksum != output["sha256"] or str((manifest.get("output") or {}).get("sha256") or "") != output["sha256"]:
        raise _safe_error(409, "checksum_mismatch")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION or manifest.get("manifest_type") != MANIFEST_TYPE:
        raise _safe_error(409, "manifest_invalid")
    return manifest


def prepare_archive_export_download(db: Session, *, export_id: str, actor: User, request=None) -> tuple[Path, str, str, int]:
    job = db.get(ArchiveExportJob, export_id)
    if not job:
        raise _safe_error(404, "Export job not found")
    output_path, output = _manifest_output_metadata(db, job)
    try:
        read_archive_export_manifest(db, export_id=export_id)
    except HTTPException as exc:
        if exc.status_code == 409 and exc.detail == "manifest_missing":
            raise _safe_error(409, "manifest_not_ready") from exc
        raise

    filename = _safe_download_filename(job, output["container"] or "mkv")
    create_event(
        db=db,
        actor=actor,
        category="archive",
        event_type="archive_export_downloaded",
        severity="info",
        message_ru="Archive export downloaded",
        message_en="Archive export downloaded",
        target_type="archive_export_job",
        target_id=job.id,
        target_name=job.camera_label_snapshot,
        metadata={
            "export_job_id": job.id,
            "camera_id": job.camera_id,
            "source_segment_count": job.source_segment_count,
            "output_size_bytes": output["size_bytes"],
            "output_container": output["container"],
            "status": job.status,
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return output_path, filename, "video/x-matroska", int(output["size_bytes"] or 0)


def prepare_archive_manifest_download(db: Session, *, export_id: str, actor: User, request=None) -> tuple[bytes, str]:
    job = db.get(ArchiveExportJob, export_id)
    if not job:
        raise _safe_error(404, "Export job not found")
    manifest = read_archive_export_manifest(db, export_id=export_id)
    filename = _safe_download_filename(job, "json")
    payload = _manifest_json_bytes(manifest)
    return payload, filename


def _cleanup_path_allowed(path: Path) -> bool:
    root = _export_root()
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0] in EXPORT_OWNED_DIRS)


def cleanup_archive_export_artifacts(
    db: Session,
    *,
    actor: User,
    dry_run: bool = False,
    limit: int = EXPORT_CLEANUP_MAX_JOBS,
    request=None,
) -> dict[str, Any]:
    now = _utcnow_naive()
    bounded_limit = max(1, min(int(limit or EXPORT_CLEANUP_MAX_JOBS), EXPORT_CLEANUP_MAX_JOBS))
    jobs = (
        db.query(ArchiveExportJob)
        .filter(
            or_(
                ArchiveExportJob.status == EXPORT_STATUS_EXPIRED,
                ArchiveExportJob.expires_at <= now,
            )
        )
        .order_by(ArchiveExportJob.expires_at.asc(), ArchiveExportJob.created_at.asc())
        .limit(bounded_limit)
        .all()
    )

    result = {
        "dry_run": bool(dry_run),
        "jobs_considered": len(jobs),
        "jobs_marked_expired": 0,
        "artifacts_removed": 0,
        "bytes_removed": 0,
        "skipped_unsafe_paths": 0,
        "missing_artifacts": 0,
        "errors": 0,
    }

    for job in jobs:
        if job.status != EXPORT_STATUS_EXPIRED and job.expires_at and job.expires_at <= now:
            result["jobs_marked_expired"] += 1
            if not dry_run:
                job.status = EXPORT_STATUS_EXPIRED
                job.progress_percent = 100
                job.updated_at = now
        for attr in ("internal_output_path", "internal_manifest_path"):
            internal = getattr(job, attr)
            if not internal:
                continue
            try:
                path = _path_from_internal(internal)
            except RuntimeError:
                result["skipped_unsafe_paths"] += 1
                continue
            if not path or not _cleanup_path_allowed(path):
                result["skipped_unsafe_paths"] += 1
                continue
            if not path.exists():
                result["missing_artifacts"] += 1
                if not dry_run:
                    setattr(job, attr, None)
                continue
            if not path.is_file():
                result["skipped_unsafe_paths"] += 1
                continue
            try:
                size = int(path.stat().st_size)
                if not dry_run:
                    path.unlink()
                    setattr(job, attr, None)
                    if attr == "internal_output_path":
                        job.internal_checksum = None
                result["artifacts_removed"] += 1
                result["bytes_removed"] += max(0, size)
            except OSError:
                result["errors"] += 1
        if not dry_run:
            db.add(job)

    if not dry_run:
        db.commit()

    create_event(
        db=db,
        actor=actor,
        category="archive",
        event_type="archive_export_cleanup_completed" if result["errors"] == 0 else "archive_export_cleanup_failed",
        severity="info" if result["errors"] == 0 else "warning",
        message_ru="Archive export cleanup completed" if result["errors"] == 0 else "Archive export cleanup failed",
        message_en="Archive export cleanup completed" if result["errors"] == 0 else "Archive export cleanup failed",
        target_type="archive_export_cleanup",
        metadata=result,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return result


def serialize_archive_export_job(job: ArchiveExportJob) -> dict[str, Any]:
    try:
        output_path = _path_from_internal(job.internal_output_path)
    except RuntimeError:
        output_path = None
    try:
        manifest_path = _path_from_internal(job.internal_manifest_path)
    except RuntimeError:
        manifest_path = None
    output_exists = bool(output_path and output_path.exists() and output_path.is_file())
    manifest_exists = bool(manifest_path and manifest_path.exists() and manifest_path.is_file())
    output_size = int(output_path.stat().st_size) if output_exists and output_path else None
    output_container = output_path.suffix.lstrip(".").lower() if output_exists and output_path else None
    ready = bool(job.status == EXPORT_STATUS_DONE and output_exists)
    return {
        "id": job.id,
        "status": job.status if job.status in EXPORT_STATUSES else EXPORT_STATUS_FAILED,
        "camera": {
            "id": job.camera_id,
            "label": job.camera_label_snapshot,
        },
        "start_ts": job.start_ts.isoformat() if job.start_ts else None,
        "end_ts": job.end_ts.isoformat() if job.end_ts else None,
        "duration_seconds": job.duration_seconds,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "progress_percent": job.progress_percent,
        "title": job.title,
        "reason": job.reason,
        "format_hint": job.format_hint,
        "source_segment_count": job.source_segment_count,
        "estimated_source_bytes": job.estimated_source_bytes,
        "gap_warnings": job.gap_warnings or [],
        "error_code": job.error_code,
        "error_message": job.sanitized_error_message,
        "has_generated_clip": bool(job.status == EXPORT_STATUS_DONE and output_exists),
        "clip_ready": ready,
        "manifest_ready": bool(ready and manifest_exists),
        "download_ready": bool(ready and manifest_exists),
        "output_container": output_container,
        "output_size_bytes": output_size,
    }
