from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from sqlalchemy.orm import Session, object_session

from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.core.sanitization import redact_text
from app.services.audit_log import create_event
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    VIDEO_EXTENSIONS,
    is_kmvms_namespace_relative,
    is_video_file,
    list_archive_roots,
    relative_to_archive_root,
    resolve_segment_file_path,
    safe_resolve_relative_for_root,
    storage_root,
)

OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
STATUS_FINALIZED = "finalized"
STATUS_WRITING = "writing"
STATUS_FAILED = "failed"
STATUS_MISSING = "missing"
STATUS_CORRUPTED = "corrupted"
STATUS_STALE_WRITING = "stale_writing"
APPLY_SAFE_MODE = "apply_safe"
DRY_RUN_MODE = "dry_run"
STALE_WRITING_AFTER = timedelta(minutes=10)
MAX_SAMPLE_ITEMS = 20
MEDIA_PROBE_TIMEOUT_SEC = 5
_LAST_CLEANUP_CANDIDATES_COUNT: int | None = None
REVIEW_ONLY_CLEANUP_CLASSES = {
    "orphan_file",
    "pre_metadata_km_vms_file",
    "legacy_archive_file",
}
PROBLEM_CLASSES = {
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
    "skipped",
}
CLASSIFICATION_LABELS_RU = {
    "ok_owned_finalized": "owned запись в порядке",
    "missing_file": "файл отсутствует",
    "orphan_metadata": "запись без файла / осиротевшая запись",
    "orphan_file": "файл без записи в базе",
    "pre_metadata_km_vms_file": "старый файл KM VMS без новых метаданных",
    "legacy_archive_file": "старый архивный файл",
    "foreign_file": "чужой файл",
    "unknown_file": "неизвестный файл",
    "zero_size_file": "нулевой размер",
    "partial_file": "частичный файл / запись ещё не завершена",
    "corrupted_file": "повреждённый файл",
    "stale_writing_segment": "зависшая запись",
    "invalid_path": "некорректный путь",
    "path_outside_storage": "путь вне хранилища",
    "unreadable_file": "файл недоступен для чтения",
    "storage_unavailable": "хранилище недоступно",
    "skipped": "пропущено",
}


@dataclass(frozen=True)
class Classification:
    name: str
    cleanup_candidate: bool = False
    cleanup_reason: str | None = None
    error: str | None = None
    file_size: int | None = None
    file_mtime: datetime | None = None
    content_probe_status: str | None = None


def _empty_counts() -> Counter:
    keys = [
        "ok_owned_finalized",
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
        "skipped",
    ]
    counts = Counter()
    for key in keys:
        counts[key] = 0
    return counts


def _safe_rel_for_segment(segment: RecordingSegment) -> tuple[str | None, str | None, Path | None]:
    raw_path = segment.relative_path or segment.file_path
    if not raw_path:
        return None, "invalid_path", None

    try:
        db = object_session(segment)
        if db is None:
            return None, "db_session_missing", None
        target = resolve_segment_file_path(db, segment)
        from app.services.recording_storage import archive_root_for_segment

        root = archive_root_for_segment(db, segment)
        return relative_to_archive_root(target, root), None, target
    except ValueError as exc:
        error = str(exc) or "path_outside_storage"
        if error == "path_outside_archive_root":
            error = "path_outside_storage"
        return None, error, None
    except Exception as exc:
        return None, "invalid_path", None


def _probe_media_file(path: Path) -> tuple[bool | None, str, str | None]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, "probe_unavailable", "ffprobe is not available"

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=MEDIA_PROBE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return False, "probe_timeout", "ffprobe timed out"
    except OSError as exc:
        return None, "probe_error", redact_text(str(exc))

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "ffprobe failed").strip()
        return False, "probe_failed", redact_text(error[:500])

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return False, "probe_failed", redact_text(str(exc))

    if not isinstance(payload, dict) or not isinstance(payload.get("format"), dict):
        return False, "probe_failed", "ffprobe returned no format data"

    return True, "probe_ok", None


def _classify_segment(segment: RecordingSegment, active_job_ids: set[str] | None = None) -> tuple[str | None, Classification]:
    rel_path, path_error, target = _safe_rel_for_segment(segment)
    if path_error:
        name = "path_outside_storage" if path_error == "path_outside_storage" else "invalid_path"
        return rel_path, Classification(name=name, error=path_error)
    if target is None:
        return rel_path, Classification(name="invalid_path", error="invalid_path")

    if not target.exists():
        return rel_path, Classification(name="missing_file", error="recording file not found")
    if not target.is_file():
        return rel_path, Classification(name="unreadable_file", error="recording path is not a file")

    try:
        stat = target.stat()
    except OSError as exc:
        return rel_path, Classification(name="unreadable_file", error=redact_text(str(exc)))

    file_mtime = datetime.fromtimestamp(stat.st_mtime)
    if stat.st_size <= 0:
        return rel_path, Classification(
            name="zero_size_file",
            error="recording file is empty",
            file_size=0,
            file_mtime=file_mtime,
            content_probe_status="stat_zero_size",
        )

    active_job_ids = active_job_ids or set()
    if segment.status == STATUS_WRITING:
        age_anchor = segment.updated_at or segment.created_at or segment.started_at
        if (segment.job_id not in active_job_ids) and age_anchor and datetime.utcnow() - age_anchor > STALE_WRITING_AFTER:
            return rel_path, Classification(
                name="stale_writing_segment",
                error="writing segment has no active recorder job",
                file_size=int(stat.st_size),
                file_mtime=file_mtime,
                content_probe_status="stat_ok",
            )
        return rel_path, Classification(
            name="partial_file",
            error="segment is still writing",
            file_size=int(stat.st_size),
            file_mtime=file_mtime,
            content_probe_status="stat_ok",
        )

    if segment.status == STATUS_STALE_WRITING:
        return rel_path, Classification(
            name="stale_writing_segment",
            error="segment was previously marked as stale writing",
            file_size=int(stat.st_size),
            file_mtime=file_mtime,
            content_probe_status="stat_ok",
        )

    if segment.status == STATUS_FINALIZED:
        probe_ok, probe_status, probe_error = _probe_media_file(target)
        if probe_ok is False:
            return rel_path, Classification(
                name="corrupted_file",
                error=probe_error or "recording media probe failed",
                file_size=int(stat.st_size),
                file_mtime=file_mtime,
                content_probe_status=probe_status,
            )
        return rel_path, Classification(
            name="ok_owned_finalized",
            error=probe_error,
            file_size=int(stat.st_size),
            file_mtime=file_mtime,
            content_probe_status=probe_status,
        )

    return rel_path, Classification(
        name="partial_file",
        error=f"segment status is not playable: {segment.status}",
        file_size=int(stat.st_size),
        file_mtime=file_mtime,
        content_probe_status="stat_ok",
    )


def _iter_storage_video_files(db: Session | None = None) -> Iterable[tuple[object | None, Path]]:
    if db is not None:
        roots = list_archive_roots(db)
        result = []
        for root_row in roots:
            root = Path(root_row.root_path)
            if not root.exists():
                continue
            result.extend((root_row, path) for path in root.rglob("*") if is_video_file(path))
        return result

    root = storage_root()
    if not root.exists():
        return []
    return ((None, path) for path in root.rglob("*") if is_video_file(path))


def _looks_like_legacy_kmvms_file(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    filename = normalized.rsplit("/", 1)[-1]
    return bool(
        filename.lower().endswith(tuple(VIDEO_EXTENSIONS))
        and (
            "KMVMS" in filename.upper()
            or "camera_" in normalized
            or filename.count("-") >= 5
        )
    )


def _classify_orphan_file(rel_path: str) -> Classification:
    normalized = rel_path.replace("\\", "/").lower()
    if normalized.startswith("legacy/") or "/legacy/" in normalized or normalized.startswith("archive/"):
        return Classification("legacy_archive_file", cleanup_candidate=True, cleanup_reason="legacy archive file has no Recorder PRO metadata")
    if is_kmvms_namespace_relative(rel_path):
        return Classification("orphan_file", cleanup_candidate=True, cleanup_reason="file in KM VMS namespace has no recording metadata")
    if _looks_like_legacy_kmvms_file(rel_path):
        return Classification("pre_metadata_km_vms_file", cleanup_candidate=True, cleanup_reason="KM VMS-looking file has no Recorder PRO metadata")
    if normalized.startswith("surveillance/") or "/surveillance/" in normalized:
        return Classification("foreign_file")
    return Classification("unknown_file")


def _apply_segment_classification(db: Session, segment: RecordingSegment, classification: Classification) -> bool:
    status = segment.status
    if classification.name == "missing_file":
        status = STATUS_MISSING
    elif classification.name in {"zero_size_file", "corrupted_file"}:
        status = STATUS_CORRUPTED
    elif classification.name == "stale_writing_segment":
        status = STATUS_STALE_WRITING

    changed = False
    updates = {
        "status": status,
        "integrity_status": classification.name,
        "integrity_error": classification.error,
        "last_integrity_check_at": datetime.utcnow(),
        "file_size_verified_at": datetime.utcnow() if classification.file_size is not None else segment.file_size_verified_at,
        "file_mtime": classification.file_mtime,
        "content_probe_status": classification.content_probe_status,
        "cleanup_candidate": False,
        "cleanup_reason": None,
        "reconciliation_status": classification.name,
        "reconciliation_checked_at": datetime.utcnow(),
    }
    for field, value in updates.items():
        if getattr(segment, field, None) != value:
            setattr(segment, field, value)
            changed = True
    if classification.file_size is not None and int(segment.size_bytes or 0) != int(classification.file_size):
        segment.size_bytes = int(classification.file_size)
        changed = True
    if changed:
        db.add(segment)
    return changed


def _reconciliation_audit_metadata(summary: dict) -> dict:
    counts = dict(summary.get("counts") or {})
    cleanup_candidates_count = int(
        counts.get("orphan_file", 0)
        + counts.get("pre_metadata_km_vms_file", 0)
        + counts.get("legacy_archive_file", 0)
    )
    return {
        "mode": summary.get("mode"),
        "storage_namespace": summary.get("storage_namespace"),
        "total_metadata_rows_checked": int(summary.get("total_metadata_rows_checked") or 0),
        "updated_metadata_count": int(summary.get("updated_metadata_count") or 0),
        "deleted_files_count": int(summary.get("deleted_files_count") or 0),
        "deleted_product_metadata_count": int(summary.get("deleted_product_metadata_count") or 0),
        "cleanup_candidates_count": cleanup_candidates_count,
        "counts": counts,
        "scan_limited": bool(summary.get("scan_limited")),
        "partial": bool(summary.get("partial")),
    }


def _audit_reconciliation(db: Session, *, actor, event_type: str, severity: str, message: str, metadata: dict) -> None:
    create_event(
        db=db,
        actor=actor,
        category="reconciliation",
        event_type=event_type,
        severity=severity,
        message_ru=message,
        message_en=message,
        target_type="recording_reconciliation",
        metadata=metadata,
    )


def _safe_failure_error(exc: Exception) -> str:
    text = redact_text(str(exc))
    if "/" in text or "\\" in text:
        return "storage scan failed"
    return text or "storage scan failed"


def _empty_camera_summary(camera: Camera | None, camera_id: int | None) -> dict:
    return {
        "camera_id": camera_id,
        "camera_name": camera.name if camera else None,
        "counts": _empty_counts(),
        "problem_count": 0,
    }


def _status_from_counts(counts: Counter, failed: bool, partial: bool) -> str:
    if failed:
        return "failed" if not counts.get("storage_unavailable") else "storage_unavailable"
    if partial:
        return "partial"
    if any(counts.get(key, 0) for key in PROBLEM_CLASSES if key != "skipped"):
        return "problems_found"
    if counts.get("skipped"):
        return "warnings"
    return "ok"


def _cleanup_candidates_summary(counts: Counter, samples: dict[str, list[dict]]) -> dict:
    classification_counts = {key: int(counts.get(key) or 0) for key in REVIEW_ONLY_CLEANUP_CLASSES}
    total = int(sum(classification_counts.values()))
    safe_samples = []
    for key in REVIEW_ONLY_CLEANUP_CLASSES:
        for item in samples.get(key, [])[:5]:
            safe_samples.append(
                {
                    "classification": key,
                    "relative_path": item.get("relative_path"),
                    "cleanup_reason": item.get("cleanup_reason"),
                }
            )
            if len(safe_samples) >= 10:
                break
        if len(safe_samples) >= 10:
            break
    return {
        "count": total,
        "classification_counts": classification_counts,
        "review_only": True,
        "deleted_files_count": 0,
        "explanation": "Stage 2 marks cleanup candidates for review only; reconciliation does not delete, import, adopt, or auto-own files.",
        "samples": safe_samples,
    }


def reconcile_recordings(db: Session, *, mode: str = DRY_RUN_MODE, actor=None, write_audit: bool = True) -> dict:
    global _LAST_CLEANUP_CANDIDATES_COUNT
    mode = APPLY_SAFE_MODE if mode == APPLY_SAFE_MODE else DRY_RUN_MODE
    apply_safe = mode == APPLY_SAFE_MODE
    if write_audit:
        _audit_reconciliation(
            db,
            actor=actor,
            event_type="reconciliation.apply_started" if apply_safe else "reconciliation.scan_started",
            severity="info",
            message=f"Recorder PRO reconciliation {mode} started",
            metadata={"mode": mode, "apply_safe": apply_safe},
        )
    counts = _empty_counts()
    samples: dict[str, list[dict]] = defaultdict(list)
    updated_metadata = 0
    checked_paths: set[tuple[str, str]] = set()
    segments: list[RecordingSegment] = []
    total_storage_files_scanned = 0
    failed = False
    failure_error = None
    per_camera: dict[int | None, dict] = {}

    try:
        root_rows = list_archive_roots(db)
        cameras = {camera.id: camera for camera in db.query(Camera).order_by(Camera.id.asc()).all()}
        segments = db.query(RecordingSegment).order_by(RecordingSegment.id.asc()).all()
        active_job_ids = {
            str(job_id)
            for (job_id,) in db.query(RecordingJob.id)
            .filter(RecordingJob.state.in_(("starting", "recording", "stopping", "restarting")))
            .distinct()
            .all()
        }

        for segment in segments:
            if segment.ownership != OWNERSHIP_KM_VMS or segment.source != RECORDER_SOURCE:
                counts["skipped"] += 1
                continue
            rel_path, classification = _classify_segment(segment, active_job_ids)
            counts[classification.name] += 1
            camera_summary = per_camera.setdefault(segment.camera_id, _empty_camera_summary(cameras.get(segment.camera_id), segment.camera_id))
            camera_summary["counts"][classification.name] += 1
            if classification.name in PROBLEM_CLASSES:
                camera_summary["problem_count"] += 1
            if rel_path:
                checked_paths.add((segment.archive_root_id or "default", rel_path))
            if len(samples[classification.name]) < MAX_SAMPLE_ITEMS:
                samples[classification.name].append(
                    {
                        "segment_id": segment.id,
                        "camera_id": segment.camera_id,
                        "relative_path": rel_path,
                        "status": segment.status,
                        "error": classification.error,
                    }
                )
        per_root_counts: dict[str, Counter] = defaultdict(Counter)
        for root_row, path in _iter_storage_video_files(db):
            total_storage_files_scanned += 1
            try:
                if root_row is None:
                    rel_path = path.resolve().relative_to(storage_root().resolve()).as_posix()
                    root_id = "default"
                else:
                    rel_path = relative_to_archive_root(path, root_row)
                    root_id = root_row.id
            except Exception:
                counts["path_outside_storage"] += 1
                continue
            if (root_id, rel_path) in checked_paths:
                continue
            classification = _classify_orphan_file(rel_path)
            counts[classification.name] += 1
            per_root_counts[root_id][classification.name] += 1
            if len(samples[classification.name]) < MAX_SAMPLE_ITEMS:
                samples[classification.name].append(
                    {
                        "archive_root_id": root_id,
                        "relative_path": rel_path,
                        "cleanup_candidate": classification.cleanup_candidate,
                        "cleanup_reason": classification.cleanup_reason,
                    }
                )
        if apply_safe:
            for segment in segments:
                if segment.ownership != OWNERSHIP_KM_VMS or segment.source != RECORDER_SOURCE:
                    continue
                _rel_path, classification = _classify_segment(segment, active_job_ids)
                if _apply_segment_classification(db, segment, classification):
                    updated_metadata += 1
    except Exception as exc:
        failed = True
        failure_error = _safe_failure_error(exc)
        if apply_safe:
            db.rollback()
            updated_metadata = 0
        counts["storage_unavailable"] += 1
        samples["storage_unavailable"].append({"error": failure_error})
        if write_audit:
            _audit_reconciliation(
                db,
                actor=actor,
                event_type="reconciliation.apply_failed" if apply_safe else "reconciliation.scan_failed",
                severity="error",
                message=f"Recorder PRO reconciliation {mode} failed",
                metadata={"mode": mode, "error": failure_error, "deleted_files_count": 0},
            )

    if apply_safe and not failed:
        db.commit()

    cleanup_summary = _cleanup_candidates_summary(counts, samples)
    apply_summary = {
        "updated_metadata_count": int(updated_metadata),
        "deleted_files_count": 0,
        "deleted_product_metadata_count": 0,
        "skipped_count": int(counts.get("skipped") or 0),
        "reason_counts": {key: int(value) for key, value in counts.items() if value and key != "ok_owned_finalized"},
        "warnings": [],
        "errors": [failure_error] if failure_error else [],
        "safe_metadata_fields_only": True,
        "ownership_source_unchanged": True,
        "orphan_foreign_unknown_pre_metadata_not_adopted": True,
    }
    status = _status_from_counts(counts, failed, partial=False)
    summary = {
        "ok": status == "ok",
        "mode": mode,
        "storage_namespace": KMVMS_RECORDINGS_NAMESPACE,
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "status": status,
        "evidence_status": "failed" if failed else "fresh",
        "total_metadata_rows_checked": len(segments),
        "total_storage_files_scanned": int(total_storage_files_scanned),
        "scan_limited": False,
        "partial": False,
        "partial_reason": None,
        "updated_metadata_count": updated_metadata,
        "deleted_files_count": 0,
        "deleted_product_metadata_count": 0,
        "skipped_count": int(counts.get("skipped") or 0),
        "counts": dict(counts),
        "classification_counts": dict(counts),
        "classification_labels_ru": CLASSIFICATION_LABELS_RU,
        "cleanup_candidates": cleanup_summary,
        "cleanup_candidates_summary": cleanup_summary,
        "apply_safe_summary": apply_summary,
        "per_camera": [
            {
                "camera_id": item["camera_id"],
                "camera_name": item["camera_name"],
                "counts": dict(item["counts"]),
                "problem_count": int(item["problem_count"]),
            }
            for item in sorted(per_camera.values(), key=lambda row: row.get("camera_id") or 0)
        ],
        "samples": dict(samples),
        "archive_roots": root_statuses if "root_statuses" in locals() else [],
        "per_root_counts": {root_id: dict(root_counts) for root_id, root_counts in (per_root_counts.items() if "per_root_counts" in locals() else [])},
    }

    if write_audit and not failed:
        severity = "warning" if any(counts[key] for key in counts if key not in {"ok_owned_finalized"}) else "info"
        metadata = _reconciliation_audit_metadata(summary)
        _audit_reconciliation(
            db=db,
            actor=actor,
            event_type="reconciliation.apply_completed" if apply_safe else "reconciliation.scan_completed",
            severity=severity,
            message=f"Recorder PRO reconciliation {mode} completed",
            metadata=metadata,
        )
        current_cleanup = int(metadata["cleanup_candidates_count"])
        if _LAST_CLEANUP_CANDIDATES_COUNT is not None and _LAST_CLEANUP_CANDIDATES_COUNT != current_cleanup:
            _audit_reconciliation(
                db=db,
                actor=actor,
                event_type="reconciliation.cleanup_candidates_changed",
                severity="warning" if current_cleanup else "info",
                message="Recorder PRO reconciliation cleanup candidates changed",
                metadata={
                    "previous_cleanup_candidates_count": _LAST_CLEANUP_CANDIDATES_COUNT,
                    "current_cleanup_candidates_count": current_cleanup,
                    "mode": mode,
                },
            )
        _LAST_CLEANUP_CANDIDATES_COUNT = current_cleanup
        db.commit()

    return summary


def reconciliation_diagnostics(db: Session) -> dict:
    summary = reconcile_recordings(db, mode=DRY_RUN_MODE, write_audit=False)
    diagnostics = {
        "checked_at": summary.get("checked_at"),
        "mode": summary.get("mode"),
        "status": summary.get("status"),
        "evidence_status": summary.get("evidence_status"),
        "storage_namespace": summary.get("storage_namespace"),
        "total_metadata_rows_checked": summary.get("total_metadata_rows_checked"),
        "total_storage_files_scanned": summary.get("total_storage_files_scanned"),
        "scan_limited": bool(summary.get("scan_limited")),
        "partial": bool(summary.get("partial")),
        "partial_reason": summary.get("partial_reason"),
        "classification_counts": dict(summary.get("classification_counts") or summary.get("counts") or {}),
        "cleanup_candidates": {
            key: value
            for key, value in dict(summary.get("cleanup_candidates_summary") or {}).items()
            if key != "samples"
        },
        "updated_metadata_count": int(summary.get("updated_metadata_count") or 0),
        "deleted_files_count": 0,
        "deleted_product_metadata_count": 0,
    }
    return diagnostics
