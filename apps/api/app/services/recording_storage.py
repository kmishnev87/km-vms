from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import datetime
from hashlib import sha256
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
    if exists and is_dir:
        try:
            next(root_path.iterdir(), None)
            readable = True
        except OSError:
            readable = False
        if namespace_exists:
            writable = os.access(namespace_root, os.W_OK)
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
        problem = "archive_root_not_writable"
    return {
        "exists": exists,
        "is_dir": is_dir,
        "readable": readable,
        "writable": writable,
        "available": bool(exists and is_dir and readable and namespace_exists),
        "namespace_exists": namespace_exists,
        "problem": problem,
    }


def sanitize_archive_root_path(path_value: str, *, allow_create: bool = False, allowed_base: str | Path | None = None) -> Path:
    raw = str(path_value or "").strip()
    if not raw:
        raise ValueError("archive_root_path_required")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("archive_root_path_must_be_absolute")
    if any(part == ".." for part in candidate.parts):
        raise ValueError("archive_root_path_traversal")

    resolved = candidate.resolve(strict=False)
    bases = [approved_archive_base().resolve()]
    if allowed_base is not None:
        bases.append(Path(allowed_base).resolve(strict=False))
    if not any(_path_is_relative_to(resolved, base) for base in bases):
        raise ValueError("archive_root_outside_approved_storage_base")

    normalized = resolved.as_posix().lower()
    if normalized.endswith("/surveillance") or "/surveillance/" in normalized:
        raise ValueError("foreign_surveillance_root_rejected")

    if resolved.exists() and not resolved.is_dir():
        raise ValueError("archive_root_path_not_directory")

    if allow_create:
        (resolved / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    return resolved


def _path_is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


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


def _safe_root_label(root_row) -> str:
    return str(getattr(root_row, "label", None) or getattr(root_row, "id", None) or "archive")


def _sanitize_migration_error(value: str | Exception) -> str:
    text = str(value)
    for root_value in {str(settings.storage_root), str(approved_archive_base())}:
        if root_value:
            text = text.replace(root_value, "[storage]")
    return text


def _archive_root_safety(root_row, *, require_writable: bool) -> list[dict]:
    blockers: list[dict] = []
    if root_row is None:
        return [{"reason": "archive_root_missing", "count": 1}]
    if getattr(root_row, "storage_namespace", KMVMS_RECORDINGS_NAMESPACE) != KMVMS_RECORDINGS_NAMESPACE:
        blockers.append({"reason": "archive_root_namespace_mismatch", "root_id": getattr(root_row, "id", None), "count": 1})
    try:
        sanitize_archive_root_path(str(_root_path(root_row)), allow_create=False)
    except ValueError as exc:
        blockers.append({"reason": str(exc), "root_id": getattr(root_row, "id", None), "count": 1})
    status = root_status(_root_path(root_row))
    if not status["available"]:
        blockers.append({"reason": status["problem"] or "archive_root_unavailable", "root_id": getattr(root_row, "id", None), "count": 1})
    if require_writable and not status["writable"]:
        blockers.append({"reason": status["problem"] or "archive_root_not_writable", "root_id": getattr(root_row, "id", None), "count": 1})
    return blockers


def _roots_overlap(source_root, target_root) -> bool:
    source = _root_path(source_root).resolve()
    target = _root_path(target_root).resolve()
    try:
        source.relative_to(target)
        return True
    except ValueError:
        pass
    try:
        target.relative_to(source)
        return True
    except ValueError:
        return False


def _segment_is_kmvms_owned(segment) -> bool:
    return (
        getattr(segment, "ownership", None) == "KM VMS"
        and getattr(segment, "source", None) == "recorder"
        and getattr(segment, "storage_namespace", None) in (None, KMVMS_RECORDINGS_NAMESPACE)
        and is_kmvms_namespace_relative(getattr(segment, "relative_path", None))
        and getattr(segment, "status", None) not in {"deleted", "writing"}
    )


def _migration_plan_id(rows: list[dict], target_root_id: str | None) -> str:
    payload = {
        "target_root_id": target_root_id,
        "items": [
            {
                "segment_id": item["segment_id"],
                "source_root_id": item["source_root_id"],
                "relative_path": item["relative_path"],
                "size_bytes": item["size_bytes"],
                "mtime_ns": item["mtime_ns"],
            }
            for item in rows
        ],
    }
    return sha256(str(payload).encode("utf-8")).hexdigest()[:24]


def _file_checksum(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_file_stat(path: Path) -> dict:
    stat = path.stat()
    return {
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "inode": int(getattr(stat, "st_ino", 0) or 0),
        "device": int(getattr(stat, "st_dev", 0) or 0),
    }


def _source_stat_matches(stat: dict, item: dict) -> bool:
    return int(stat["size_bytes"]) == int(item["size_bytes"]) and int(stat["mtime_ns"]) == int(item["mtime_ns"])


def _cleanup_temp_target(temp_path: Path, target_root) -> bool:
    try:
        root = _root_path(target_root).resolve()
        temp_path.resolve(strict=False).relative_to(root)
        if temp_path.exists() and temp_path.is_file() and temp_path.name.startswith(".kmvms_migration_tmp_"):
            temp_path.unlink()
        return True
    except OSError:
        return False
    except ValueError:
        return False


class StorageMigrationCopyError(RuntimeError):
    def __init__(self, reason: str, report: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.report = report or {}


def _copy_failure_report(reason: str, *, copy_finalized: bool, verified_bytes: int = 0) -> dict:
    return {
        "result": reason,
        "verification_method": "sha256_streaming_size_stat_source_stability",
        "checksum_algorithm": "sha256",
        "verified_bytes": int(verified_bytes),
        "source_preserved": True,
        "copy_finalized": bool(copy_finalized),
        "metadata_update_staged": False,
        "metadata_persisted": False,
        "cleanup_pending": bool(copy_finalized),
        "manual_review_required": bool(copy_finalized),
    }


def _verified_copy_to_final(source_path: Path, target_path: Path, target_root, item: dict) -> dict:
    target_root_path = _root_path(target_root).resolve()
    target_parent = target_path.parent.resolve(strict=False)
    target_parent.relative_to(target_root_path)
    if target_path.exists():
        raise RuntimeError("target_collision")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".kmvms_migration_tmp_{uuid.uuid4().hex}_{target_path.name}")
    temp_path.resolve(strict=False).relative_to(target_root_path)
    if temp_path.exists():
        raise RuntimeError("temp_target_collision")

    source_stat_before = _stable_file_stat(source_path)
    if not _source_stat_matches(source_stat_before, item):
        raise RuntimeError("source_file_changed_after_plan")

    temp_created = False
    try:
        source_checksum = _file_checksum(source_path)
        shutil.copy2(source_path, temp_path)
        temp_created = True
        if not temp_path.exists() or not temp_path.is_file():
            raise RuntimeError("verification_unavailable")
        temp_stat = _stable_file_stat(temp_path)
        if int(temp_stat["size_bytes"]) != int(item["size_bytes"]):
            raise RuntimeError("copy_size_mismatch")
        temp_checksum = _file_checksum(temp_path)
        if source_checksum != temp_checksum:
            raise RuntimeError("checksum_mismatch")
        source_stat_after = _stable_file_stat(source_path)
        if source_stat_after != source_stat_before:
            raise RuntimeError("source_changed_during_copy")
        if target_path.exists():
            raise RuntimeError("target_collision")
        try:
            temp_path.replace(target_path)
        except OSError as exc:
            raise RuntimeError("finalization_failed") from exc
        final_stat = _stable_file_stat(target_path)
        if int(final_stat["size_bytes"]) != int(item["size_bytes"]):
            raise StorageMigrationCopyError(
                "final_verification_failed",
                _copy_failure_report("final_verification_failed", copy_finalized=True, verified_bytes=final_stat["size_bytes"]),
            )
        final_checksum = _file_checksum(target_path)
        if final_checksum != source_checksum:
            raise StorageMigrationCopyError(
                "final_checksum_mismatch",
                _copy_failure_report("final_checksum_mismatch", copy_finalized=True, verified_bytes=final_stat["size_bytes"]),
            )
        source_stat_final = _stable_file_stat(source_path)
        if source_stat_final != source_stat_before:
            raise StorageMigrationCopyError(
                "source_changed_after_finalization",
                _copy_failure_report("source_changed_after_finalization", copy_finalized=True, verified_bytes=final_stat["size_bytes"]),
            )
        return {
            "verification_method": "sha256_streaming_size_stat_source_stability",
            "checksum_algorithm": "sha256",
            "verified_bytes": int(final_stat["size_bytes"]),
            "source_preserved": True,
            "copy_finalized": True,
            "metadata_update_staged": False,
            "metadata_persisted": False,
            "cleanup_pending": True,
            "manual_review_required": False,
        }
    except Exception:
        if temp_created:
            _cleanup_temp_target(temp_path, target_root)
        raise


def storage_migration_apply_plan(db: Session, *, target_root_id: str | None = None) -> dict:
    from app.models.recording import RecordingJob, RecordingSegment

    roots = list_archive_roots(db)
    active = active_archive_root(db)
    target = next((root for root in roots if root.id == target_root_id), None) if target_root_id else active
    blockers: list[dict] = []
    skipped: list[dict] = []
    planned: list[dict] = []
    active_job_count = db.query(RecordingJob).filter(RecordingJob.state.in_(("starting", "recording", "stopping", "restarting"))).count()
    if active_job_count:
        blockers.append({"reason": "active_recording_jobs", "count": int(active_job_count)})
    blockers.extend(_archive_root_safety(target, require_writable=True))

    root_by_id = {getattr(root, "id", None): root for root in roots}
    for root in roots:
        if target is not None and root.id != target.id:
            blockers.extend(_archive_root_safety(root, require_writable=False))
            if _roots_overlap(root, target):
                blockers.append({"reason": "archive_root_overlap", "root_id": root.id, "target_root_id": target.id, "count": 1})

    if target is None:
        blockers.append({"reason": "target_root_missing", "count": 1})

    for segment in db.query(RecordingSegment).order_by(RecordingSegment.id.asc()).all():
        source_root = root_by_id.get(getattr(segment, "archive_root_id", None) or DEFAULT_ARCHIVE_ROOT_ID)
        if target is None or source_root is None or getattr(source_root, "id", None) == getattr(target, "id", None):
            continue
        if (
            getattr(segment, "ownership", None) == "KM VMS"
            and getattr(segment, "source", None) == "recorder"
            and not is_kmvms_namespace_relative(getattr(segment, "relative_path", None))
        ):
            blockers.append({"reason": "path_outside_archive_root", "segment_id": int(segment.id), "count": 1})
            continue
        if not _segment_is_kmvms_owned(segment):
            skipped.append({"segment_id": segment.id, "reason": "not_kmvms_owned_or_not_finalized"})
            continue
        try:
            source_path = resolve_segment_file_path(db, segment, require_exists=True)
            relative_path = relative_to_archive_root(source_path, source_root)
            if not is_kmvms_namespace_relative(relative_path):
                raise ValueError("path_outside_kmvms_namespace")
            target_path = safe_resolve_relative_for_root(relative_path, target)
            target_parent = target_path.parent.resolve(strict=False)
            target_parent.relative_to(_root_path(target).resolve())
            if target_path.exists():
                raise ValueError("target_collision")
            stat = source_path.stat()
            planned.append(
                {
                    "segment_id": int(segment.id),
                    "source_root_id": source_root.id,
                    "target_root_id": target.id,
                    "relative_path": relative_path,
                    "size_bytes": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                    "inode": int(getattr(stat, "st_ino", 0) or 0),
                    "device": int(getattr(stat, "st_dev", 0) or 0),
                    "source_label": _safe_root_label(source_root),
                    "target_label": _safe_root_label(target),
                }
            )
        except Exception as exc:
            blockers.append({"reason": _sanitize_migration_error(exc), "segment_id": int(segment.id), "count": 1})

    total_bytes = sum(item["size_bytes"] for item in planned)
    if target is not None:
        try:
            free_bytes = shutil.disk_usage(_root_path(target)).free
            if total_bytes > free_bytes:
                blockers.append({"reason": "insufficient_target_free_space", "count": 1})
        except OSError as exc:
            blockers.append({"reason": _sanitize_migration_error(exc), "count": 1})

    return {
        "mode": "server_side_apply_plan",
        "target_root_id": getattr(target, "id", None),
        "target_label": _safe_root_label(target) if target is not None else None,
        "plan_id": _migration_plan_id(planned, getattr(target, "id", None)),
        "apply_available": bool(target is not None and not blockers and planned),
        "copy_only": True,
        "source_preserved": True,
        "cleanup_pending": bool(planned),
        "planned_count": len(planned),
        "planned_bytes": total_bytes,
        "skipped_count": len(skipped),
        "blockers": blockers,
        "skipped": skipped[:20],
        "planned": [
            {
                "segment_id": item["segment_id"],
                "source_root_id": item["source_root_id"],
                "target_root_id": item["target_root_id"],
                "size_bytes": item["size_bytes"],
                "source_label": item["source_label"],
                "target_label": item["target_label"],
            }
            for item in planned[:50]
        ],
        "_internal_items": planned,
    }


def apply_storage_migration(db: Session, *, target_root_id: str | None = None, expected_plan_id: str | None = None) -> dict:
    from app.models.recording import ArchiveRoot, RecordingSegment

    plan = storage_migration_apply_plan(db, target_root_id=target_root_id)
    if expected_plan_id and expected_plan_id != plan["plan_id"]:
        plan["blockers"].append({"reason": "stale_or_tampered_plan", "count": 1})
        plan["apply_available"] = False
    if not plan["apply_available"]:
        return {
            "status": "blocked",
            "mutation_performed": False,
            "plan_id": plan["plan_id"],
            "target_root_id": plan["target_root_id"],
            "blockers": plan["blockers"],
            "executed": [],
            "skipped": plan["skipped"],
            "rollback_strategy": "No mutation was performed. Resolve blockers and rerun preview before applying.",
            "source_preserved": True,
            "cleanup_pending": False,
            "recorder_runtime_affected": False,
        }

    executed: list[dict] = []
    try:
        for item in plan["_internal_items"]:
            source_root = db.get(ArchiveRoot, item["source_root_id"])
            target_root = db.get(ArchiveRoot, item["target_root_id"])
            segment = db.get(RecordingSegment, item["segment_id"])
            if segment is None or source_root is None or target_root is None:
                raise RuntimeError("plan_item_missing_after_validation")
            source_path = resolve_segment_file_path(db, segment, require_exists=True)
            current_stat = source_path.stat()
            if int(current_stat.st_size) != item["size_bytes"] or int(current_stat.st_mtime_ns) != item["mtime_ns"]:
                raise RuntimeError("source_file_changed_after_plan")
            target_path = safe_resolve_relative_for_root(item["relative_path"], target_root)
            verification = _verified_copy_to_final(source_path, target_path, target_root, item)
            segment.archive_root_id = item["target_root_id"]
            segment.relative_path = item["relative_path"]
            segment.file_path = item["relative_path"]
            segment.size_bytes = item["size_bytes"]
            segment.updated_at = datetime.utcnow()
            db.add(segment)
            verification["metadata_update_staged"] = True
            executed.append({"segment_id": item["segment_id"], "bytes": item["size_bytes"], "result": "copied_verified_finalized_metadata_staged", **verification})
        db.commit()
        for executed_item in executed:
            executed_item["metadata_persisted"] = True
            executed_item["result"] = "copied_verified_finalized_and_metadata_persisted"
    except Exception as exc:
        db.rollback()
        for executed_item in executed:
            if executed_item.get("copy_finalized"):
                executed_item["metadata_persisted"] = False
                executed_item["cleanup_pending"] = True
                executed_item["manual_review_required"] = True
                executed_item["result"] = "copy_finalized_metadata_rolled_back"
        if isinstance(exc, StorageMigrationCopyError) and exc.report:
            failed_segment_id = None
            failed_bytes = 0
            try:
                failed_segment_id = item["segment_id"]
                failed_bytes = item["size_bytes"]
            except (NameError, KeyError):
                pass
            failed_report = {"segment_id": failed_segment_id, "bytes": failed_bytes, **exc.report}
            executed.append(failed_report)
        return {
            "status": "failed",
            "mutation_performed": bool(executed),
            "plan_id": plan["plan_id"],
            "target_root_id": plan["target_root_id"],
            "blockers": [{"reason": getattr(exc, "reason", _sanitize_migration_error(exc)), "count": 1}],
            "executed": executed,
            "skipped": plan["skipped"],
            "rollback_strategy": "Source files were preserved. Review copied target files for executed rows before retrying; automatic cleanup is not performed.",
            "source_preserved": True,
            "cleanup_pending": bool(executed),
            "recorder_runtime_affected": False,
            "verification_method": "sha256_streaming_size_stat_source_stability",
            "checksum_algorithm": "sha256",
        }

    return {
        "status": "completed",
        "mutation_performed": bool(executed),
        "plan_id": plan["plan_id"],
        "target_root_id": plan["target_root_id"],
        "planned_count": plan["planned_count"],
        "planned_bytes": plan["planned_bytes"],
        "executed": executed,
        "skipped": plan["skipped"],
        "blockers": [],
        "rollback_strategy": "Copy-only migration preserves source files. Cleanup of old roots remains manual after operator review.",
        "source_preserved": True,
        "cleanup_pending": bool(executed),
        "recorder_runtime_affected": False,
        "verification_method": "sha256_streaming_size_stat_source_stability",
        "checksum_algorithm": "sha256",
        "verified_item_count": len(executed),
        "verified_bytes": sum(int(item.get("verified_bytes") or item.get("bytes") or 0) for item in executed),
    }


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
    apply_plan = storage_migration_apply_plan(db, target_root_id=target_root_id)
    return {
        "mode": "preview_only",
        "plan_id": apply_plan["plan_id"],
        "apply_available": bool(apply_plan["apply_available"]),
        "copy_only_apply": True,
        "source_preserved": True,
        "cleanup_pending": bool(apply_plan["cleanup_pending"]),
        "non_mutating": True,
        "target_root_id": getattr(target, "id", None),
        "total_would_move_count": total_move_count,
        "total_would_move_bytes": total_move_bytes,
        "total_would_stay_count": total_stay_count,
        "blockers": apply_plan["blockers"],
        "per_root": per_root,
        "apply_contract": "copy_only_server_side_plan_confirm_required_source_preserved",
        "explanation": "Preview is read-only. Apply copies only KM VMS-owned finalized metadata segments between configured archive roots, verifies size, updates metadata after copy, and preserves source files.",
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
