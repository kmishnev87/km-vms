from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from app.core.config import settings
from sqlalchemy.orm import Session

KMVMS_RECORDINGS_NAMESPACE = "kmvms/recordings"
VIDEO_EXTENSIONS = {".mp4", ".mkv"}
DEFAULT_ARCHIVE_ROOT_ID = "default"


def storage_root() -> Path:
    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def approved_archive_base() -> Path:
    return storage_root().resolve().parent


def safe_name(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r'[\\\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text[:120].strip("_") or "camera"


def safe_resolve_relative(relative_path: str) -> Path:
    if not relative_path:
        raise ValueError("empty_relative_path")

    root = storage_root().resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path_outside_storage") from exc
    return target


def _normalize_relative(relative_path: str) -> str:
    normalized = (relative_path or "").replace("\\", "/").lstrip("/")
    if not normalized:
        raise ValueError("empty_relative_path")
    return normalized


def _root_path(root_row) -> Path:
    return Path(str(getattr(root_row, "root_path", "") or settings.storage_root))


def root_status(root_path: Path) -> dict:
    exists = root_path.exists()
    is_dir = root_path.is_dir() if exists else False
    namespace_root = root_path / KMVMS_RECORDINGS_NAMESPACE
    namespace_exists = namespace_root.exists() and namespace_root.is_dir()
    readable = False
    writable = False
    write_probe_error = None
    if exists and is_dir:
        try:
            next(root_path.iterdir(), None)
            readable = True
        except OSError:
            readable = False
        if namespace_exists:
            probe = namespace_root / f".kmvms_write_probe_{uuid.uuid4().hex}.tmp"
            try:
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                writable = True
            except OSError:
                write_probe_error = "archive_root_not_writable"
                try:
                    if probe.exists():
                        probe.unlink()
                except OSError:
                    pass
    problem = None
    if not exists:
        problem = "root_missing"
    elif not is_dir:
        problem = "root_not_directory"
    elif not namespace_exists:
        problem = "namespace_missing"
    elif not readable:
        problem = "root_not_readable"
    elif not writable:
        problem = write_probe_error or "archive_root_not_writable"
    return {
        "exists": exists,
        "is_dir": is_dir,
        "readable": readable,
        "writable": writable,
        "available": bool(exists and is_dir and readable and namespace_exists),
        "namespace_exists": namespace_exists,
        "problem": problem,
    }


def sanitize_archive_root_path(path_value: str, *, allow_create: bool = False) -> Path:
    raw = str(path_value or "").strip()
    if not raw:
        raise ValueError("archive_root_path_required")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("archive_root_path_must_be_absolute")
    if any(part == ".." for part in candidate.parts):
        raise ValueError("archive_root_path_traversal")

    resolved = candidate.resolve(strict=False)
    base = approved_archive_base().resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("archive_root_outside_approved_storage_base") from exc

    normalized = resolved.as_posix().lower()
    if normalized.endswith("/surveillance") or "/surveillance/" in normalized:
        raise ValueError("foreign_surveillance_root_rejected")

    if resolved.exists() and not resolved.is_dir():
        raise ValueError("archive_root_path_not_directory")

    if allow_create:
        (resolved / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    return resolved


def ensure_archive_roots(db: Session) -> list:
    from app.models.recording import ArchiveRoot, RecordingSegment

    default_path = Path(settings.storage_root).resolve(strict=False)
    rows = db.query(ArchiveRoot).order_by(ArchiveRoot.created_at.asc(), ArchiveRoot.id.asc()).all()
    default = db.get(ArchiveRoot, DEFAULT_ARCHIVE_ROOT_ID)
    if default is None:
        default = ArchiveRoot(
            id=DEFAULT_ARCHIVE_ROOT_ID,
            label="Default archive",
            root_path=str(default_path),
            storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
            is_active=not any(row.is_active for row in rows),
            is_readable=True,
            is_writable=True,
            is_available=True,
            last_seen_at=datetime.utcnow(),
        )
        db.add(default)
        db.commit()
        rows.append(default)
    if not db.query(ArchiveRoot).filter(ArchiveRoot.is_active == True, ArchiveRoot.retired_at.is_(None)).first():  # noqa: E712
        default.is_active = True
        default.updated_at = datetime.utcnow()
        db.add(default)
        db.commit()

    db.query(RecordingSegment).filter(RecordingSegment.archive_root_id.is_(None)).update(
        {
            RecordingSegment.archive_root_id: DEFAULT_ARCHIVE_ROOT_ID,
            RecordingSegment.updated_at: RecordingSegment.updated_at,
        },
        synchronize_session=False,
    )
    db.commit()
    return db.query(ArchiveRoot).order_by(ArchiveRoot.is_active.desc(), ArchiveRoot.created_at.asc(), ArchiveRoot.id.asc()).all()


def list_archive_roots(db: Session) -> list:
    return ensure_archive_roots(db)


def active_archive_root(db: Session):
    from app.models.recording import ArchiveRoot

    ensure_archive_roots(db)
    row = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.is_active == True, ArchiveRoot.retired_at.is_(None))  # noqa: E712
        .order_by(ArchiveRoot.updated_at.desc(), ArchiveRoot.id.asc())
        .first()
    )
    if row:
        return row
    return db.get(ArchiveRoot, DEFAULT_ARCHIVE_ROOT_ID)


def archive_root_for_segment(db: Session, segment):
    from app.models.recording import ArchiveRoot

    root_id = getattr(segment, "archive_root_id", None) or DEFAULT_ARCHIVE_ROOT_ID
    row = db.get(ArchiveRoot, root_id)
    if row is None:
        row = db.get(ArchiveRoot, DEFAULT_ARCHIVE_ROOT_ID)
    if row is None and root_id == DEFAULT_ARCHIVE_ROOT_ID:
        return SimpleNamespace(
            id=DEFAULT_ARCHIVE_ROOT_ID,
            label="Default archive",
            root_path=str(Path(settings.storage_root).resolve(strict=False)),
            storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
            is_active=True,
            retired_at=None,
        )
    return row


def safe_resolve_relative_for_root(relative_path: str, root_row) -> Path:
    normalized = _normalize_relative(relative_path)
    root = _root_path(root_row).resolve()
    target = (root / normalized).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path_outside_archive_root") from exc
    return target


def resolve_segment_file_path(db: Session, segment, *, require_exists: bool = False) -> Path:
    root_row = archive_root_for_segment(db, segment)
    if root_row is None:
        raise ValueError("archive_root_missing")
    relative_path = getattr(segment, "relative_path", None)
    if not relative_path:
        file_path = getattr(segment, "file_path", None)
        if not file_path:
            raise ValueError("missing_relative_path")
        candidate = Path(file_path)
        if candidate.is_absolute():
            root = _root_path(root_row).resolve()
            try:
                candidate.resolve().relative_to(root)
            except ValueError as exc:
                raise ValueError("path_outside_archive_root") from exc
            target = candidate.resolve()
        else:
            target = safe_resolve_relative_for_root(str(candidate), root_row)
    else:
        target = safe_resolve_relative_for_root(relative_path, root_row)
    if require_exists and (not target.exists() or not target.is_file()):
        raise FileNotFoundError("recording_file_not_found")
    return target


def relative_to_archive_root(path: Path, root_row) -> str:
    root = _root_path(root_row).resolve()
    return path.resolve().relative_to(root).as_posix()


def segment_relative_path(db: Session, segment) -> str | None:
    if getattr(segment, "relative_path", None):
        root = archive_root_for_segment(db, segment)
        return relative_to_archive_root(safe_resolve_relative_for_root(segment.relative_path, root), root)
    if getattr(segment, "file_path", None):
        root = archive_root_for_segment(db, segment)
        return relative_to_archive_root(resolve_segment_file_path(db, segment), root)
    return None


def active_namespace_dir(db: Session, camera_id: int, job_id: str) -> Path:
    root = active_archive_root(db)
    if root is None:
        raise RuntimeError("active_archive_root_missing")
    path = build_namespace_dir(camera_id, job_id, root=_root_path(root))
    path.mkdir(parents=True, exist_ok=True)
    return path


def archive_root_public_status(root_row, *, include_path: bool = False) -> dict:
    root_path = _root_path(root_row)
    status = root_status(root_path)
    result = {
        "id": getattr(root_row, "id", None),
        "label": getattr(root_row, "label", None),
        "storage_namespace": getattr(root_row, "storage_namespace", KMVMS_RECORDINGS_NAMESPACE),
        "is_active": bool(getattr(root_row, "is_active", False)),
        "is_readable": bool(status["readable"]),
        "is_writable": bool(status["writable"]),
        "is_available": bool(status["available"]),
        "namespace_exists": bool(status["namespace_exists"]),
        "problem": status["problem"] or getattr(root_row, "problem", None),
        "retired": bool(getattr(root_row, "retired_at", None)),
    }
    if include_path:
        result["configured_path"] = str(root_path)
    else:
        result["path_label"] = root_path.name or "archive"
    return result


def root_usage(db: Session, root_row) -> dict:
    from app.models.recording import RecordingSegment

    root = _root_path(root_row)
    count = 0
    existing = 0
    missing = 0
    size = 0
    for segment in (
        db.query(RecordingSegment)
        .filter(RecordingSegment.archive_root_id == getattr(root_row, "id", None))
        .all()
    ):
        if getattr(segment, "status", None) == "deleted":
            continue
        count += 1
        try:
            target = resolve_segment_file_path(db, segment)
            if target.exists() and target.is_file():
                existing += 1
                size += int(target.stat().st_size)
            else:
                missing += 1
        except Exception:
            missing += 1
    return {"segments_count": count, "existing_file_count": existing, "missing_file_count": missing, "size_bytes": size}


def migration_preview(db: Session, *, target_root_id: str | None = None) -> dict:
    from app.models.recording import RecordingJob, RecordingSegment

    roots = list_archive_roots(db)
    active = active_archive_root(db)
    target = next((root for root in roots if root.id == target_root_id), None) if target_root_id else active
    active_job_count = db.query(RecordingJob).filter(RecordingJob.state.in_(("starting", "recording", "stopping", "restarting"))).count()
    per_root = []
    total_move_count = 0
    total_move_bytes = 0
    total_stay_count = 0
    blockers: list[dict] = []
    for root in roots:
        usage = root_usage(db, root)
        would_move = bool(target and root.id != target.id and not getattr(root, "retired_at", None))
        move_count = usage["segments_count"] if would_move else 0
        move_bytes = usage["size_bytes"] if would_move else 0
        stay_count = usage["segments_count"] - move_count
        stay_bytes = usage["size_bytes"] - move_bytes
        total_move_count += move_count
        total_move_bytes += move_bytes
        total_stay_count += stay_count
        per_root.append(
            {
                "root_id": root.id,
                "label": root.label,
                "is_active": bool(root.is_active),
                "would_move_count": move_count,
                "would_move_bytes": move_bytes,
                "would_stay_count": stay_count,
                "would_stay_bytes": stay_bytes,
                "missing_file_count": usage["missing_file_count"],
            }
        )
    if active_job_count:
        blockers.append({"reason": "active_recording_jobs", "count": int(active_job_count)})
    if target is None:
        blockers.append({"reason": "target_root_missing", "count": 1})
    return {
        "mode": "preview_only",
        "apply_available": False,
        "non_mutating": True,
        "target_root_id": getattr(target, "id", None),
        "total_would_move_count": total_move_count,
        "total_would_move_bytes": total_move_bytes,
        "total_would_stay_count": total_stay_count,
        "blockers": blockers,
        "per_root": per_root,
        "explanation": "Migration apply/file move is deferred; this preview does not move, copy, delete, adopt or import files.",
    }


def relative_to_storage(path: Path) -> str:
    root = storage_root().resolve()
    return path.resolve().relative_to(root).as_posix()


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def is_kmvms_namespace_relative(relative_path: str | None) -> bool:
    normalized = (relative_path or "").replace("\\", "/").lstrip("/")
    return normalized == KMVMS_RECORDINGS_NAMESPACE or normalized.startswith(f"{KMVMS_RECORDINGS_NAMESPACE}/")


def build_namespace_dir(camera_id: int, job_id: str, *, root: Path | None = None) -> Path:
    base = (root or storage_root()) / KMVMS_RECORDINGS_NAMESPACE
    return base / f"camera_{int(camera_id)}" / f"job_{safe_name(job_id)}"
