from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable

from app.core.config import settings
from sqlalchemy import or_
from sqlalchemy.orm import Session

KMVMS_RECORDINGS_NAMESPACE = "kmvms/recordings"
VIDEO_EXTENSIONS = {".mp4", ".mkv"}
DEFAULT_ARCHIVE_ROOT_ID = "default"
ARCHIVE_ROOTS_RUNTIME_BASE = "/storage/archive-roots"
ARCHIVE_ROOTS_RUNTIME_MANIFEST = "archive-roots-runtime.json"
ARCHIVE_ROOTS_COMPOSE_OVERRIDE = "docker-compose.archive-roots.yml"
MAX_ARCHIVE_ROOTS_RUNTIME_MANIFEST_BYTES = 128 * 1024
ARCHIVE_ROOT_RUNTIME_TARGET_RE = re.compile(
    r"^/storage/archive-roots/[A-Za-z0-9_.-]{1,80}$"
)
ROOT_RESOLUTION_RESOLVED = "resolved"
ROOT_RESOLUTION_UNRESOLVED = "root_unresolved"
ROOT_RESOLUTION_AMBIGUOUS = "root_unresolved_ambiguous"
ROOT_RESOLUTION_INACCESSIBLE = "root_unresolved_inaccessible"
ROOT_RESOLUTION_CONFLICT = "root_identity_conflict"
ROOT_RESOLUTION_PROBLEMS = {
    ROOT_RESOLUTION_UNRESOLVED,
    ROOT_RESOLUTION_AMBIGUOUS,
    ROOT_RESOLUTION_INACCESSIBLE,
    ROOT_RESOLUTION_CONFLICT,
}


def storage_root() -> Path:
    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def archive_roots_runtime_base() -> Path:
    return Path(os.getenv("KMVMS_ARCHIVE_ROOTS_RUNTIME_BASE") or ARCHIVE_ROOTS_RUNTIME_BASE)


def archive_roots_manifest_path() -> Path:
    return Path(settings.storage_install_control) / ARCHIVE_ROOTS_RUNTIME_MANIFEST


def archive_roots_compose_override_path() -> Path:
    return Path(settings.storage_install_control) / ARCHIVE_ROOTS_COMPOSE_OVERRIDE


def _reject_json_constant(value: str):
    raise ValueError(f"archive_roots_runtime_non_finite:{value}")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("archive_roots_runtime_duplicate_key")
        result[key] = value
    return result


def _archive_roots_runtime_entries() -> dict[str, dict]:
    path = archive_roots_manifest_path()
    try:
        descriptor = os.lstat(path)
    except FileNotFoundError:
        return {}
    if stat.S_ISLNK(descriptor.st_mode) or not stat.S_ISREG(
        descriptor.st_mode
    ):
        raise ValueError("archive_roots_runtime_manifest_not_regular")
    if (
        descriptor.st_size <= 1
        or descriptor.st_size > MAX_ARCHIVE_ROOTS_RUNTIME_MANIFEST_BYTES
    ):
        raise ValueError("archive_roots_runtime_manifest_size_invalid")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(
            "archive_roots_runtime_manifest_unavailable"
        ) from exc
    if len(raw) != descriptor.st_size:
        raise ValueError("archive_roots_runtime_manifest_changed")
    try:
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "archive_roots_runtime_manifest_invalid"
        ) from exc
    except ValueError:
        raise
    expected_manifest_fields = {
        "schema_version",
        "runtime_base",
        "compose_override_file",
        "items",
        "raw_runtime_paths_user_visible",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_manifest_fields
        or manifest.get("schema_version") != 1
        or manifest.get("runtime_base")
        != archive_roots_runtime_base().as_posix()
        or manifest.get("compose_override_file")
        != ARCHIVE_ROOTS_COMPOSE_OVERRIDE
        or manifest.get("raw_runtime_paths_user_visible") is not False
        or not isinstance(manifest.get("items"), list)
        or len(manifest["items"]) > 128
    ):
        raise ValueError("archive_roots_runtime_manifest_invalid")
    expected_item_fields = {
        "root_id",
        "user_display_path",
        "backend_runtime_path",
        "physical_volume_id",
        "storage_namespace",
        "active_write_target",
    }
    entries: dict[str, dict] = {}
    seen_targets: set[str] = set()
    for item in manifest["items"]:
        if not isinstance(item, dict) or set(item) != expected_item_fields:
            raise ValueError("archive_roots_runtime_manifest_item_invalid")
        root_id = item.get("root_id")
        source = item.get("user_display_path")
        target = item.get("backend_runtime_path")
        physical_volume_id = item.get("physical_volume_id")
        namespace = item.get("storage_namespace")
        if (
            not isinstance(root_id, str)
            or not root_id
            or len(root_id) > 128
            or any(char in root_id for char in ("\x00", "\r", "\n"))
            or root_id in entries
            or not isinstance(source, str)
            or not source.startswith("/")
            or len(source) > 1024
            or any(char in source for char in ("\x00", "\r", "\n"))
            or any(part == ".." for part in Path(source).parts)
            or not isinstance(target, str)
            or not ARCHIVE_ROOT_RUNTIME_TARGET_RE.fullmatch(target)
            or target in seen_targets
            or not isinstance(physical_volume_id, str)
            or not physical_volume_id
            or len(physical_volume_id) > 1024
            or any(
                char in physical_volume_id
                for char in ("\x00", "\r", "\n")
            )
            or not isinstance(namespace, str)
            or not namespace
            or len(namespace) > 128
            or any(char in namespace for char in ("\x00", "\r", "\n"))
            or type(item.get("active_write_target")) is not bool
        ):
            raise ValueError(
                "archive_roots_runtime_manifest_item_invalid"
            )
        entries[root_id] = item
        seen_targets.add(target)
    return entries


def approved_archive_base() -> Path:
    return Path(settings.storage_root).resolve(strict=False).parent


def safe_name(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r'[\\\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text[:120].strip("_") or "camera"


def safe_archive_root_mount_id(value: str | None) -> str:
    raw = str(value or DEFAULT_ARCHIVE_ROOT_ID).strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-")
    return (text[:80] or DEFAULT_ARCHIVE_ROOT_ID).lower()


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


def _stored_root_path(root_row) -> Path:
    return Path(str(getattr(root_row, "root_path", "") or settings.storage_root))


def _configured_host_storage_root() -> str | None:
    value = (os.getenv("STORAGE_HOST_ROOT") or os.getenv("SURVEILLANCE_ROOT") or "").strip()
    return value or None


def archive_root_host_display_path(root_row) -> str:
    root_id = str(getattr(root_row, "id", "") or "")
    existing = _archive_roots_runtime_entries().get(root_id)
    if existing is not None:
        return str(existing["user_display_path"])
    stored = _stored_root_path(root_row)
    if stored.as_posix() == Path(settings.storage_root).as_posix():
        host_path = _configured_host_storage_root()
        if host_path:
            return str(host_path)
    return str(stored)


def _root_path(root_row) -> Path:
    return archive_root_runtime_path(root_row)


def archive_root_runtime_mount_path(root_row) -> Path:
    root_id = str(getattr(root_row, "id", "") or "")
    existing = _archive_roots_runtime_entries().get(root_id)
    if existing is not None:
        return Path(str(existing["backend_runtime_path"]))
    return archive_roots_runtime_base() / safe_archive_root_mount_id(getattr(root_row, "id", None))


def archive_root_runtime_path(root_row) -> Path:
    stored = _stored_root_path(root_row)
    runtime = Path(settings.storage_root)
    per_root_runtime = archive_root_runtime_mount_path(root_row)
    if per_root_runtime.exists():
        return per_root_runtime
    if (
        bool(getattr(root_row, "is_active", False))
        and stored.as_posix() != runtime.as_posix()
        and not stored.exists()
    ):
        return runtime
    return stored


def _inactive_runtime_activation_root(root_row) -> bool:
    stored = _stored_root_path(root_row)
    return (
        bool(root_row)
        and not bool(getattr(root_row, "is_active", False))
        and stored.as_posix() != Path(settings.storage_root).as_posix()
        and not stored.exists()
        and not archive_root_runtime_mount_path(root_row).exists()
    )


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
            root_readable = os.access(root_path, os.R_OK | os.X_OK)
        except OSError:
            root_readable = False
        if namespace_exists:
            try:
                next(namespace_root.iterdir(), None)
                namespace_readable = os.access(namespace_root, os.R_OK | os.X_OK)
            except OSError:
                namespace_readable = False
            readable = bool(root_readable and namespace_readable)
            writable = bool(os.access(namespace_root, os.W_OK | os.X_OK))
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


def archive_root_runtime_access_state(root_row) -> dict:
    runtime_path = archive_root_runtime_path(root_row)
    status = root_status(runtime_path)
    problem = status["problem"]
    if (
        not bool(status["exists"])
        and not bool(getattr(root_row, "is_active", False))
        and archive_root_runtime_mount_path(root_row).as_posix() == runtime_path.as_posix()
    ):
        problem = "archive_root_runtime_mount_missing"
    access_state = "available" if status["available"] else "unavailable"
    if status["exists"] and status["is_dir"] and (not status["readable"] or not status["namespace_exists"]):
        access_state = "degraded"
    return {
        **status,
        "runtime_path": runtime_path,
        "runtime_mount_path": archive_root_runtime_mount_path(root_row),
        "access_state": access_state,
        "read_access_state": "available" if status["readable"] and status["namespace_exists"] else "unavailable",
        "write_access_state": "available" if status["writable"] else "unavailable",
        "mount_access_state": access_state,
        "problem": problem,
    }


def verify_runtime_path_access(runtime_path: Path, *, require_write: bool, base_status: dict | None = None) -> dict:
    status = dict(base_status or root_status(runtime_path))
    status.setdefault("runtime_path", runtime_path)
    status.setdefault("read_access_state", "available" if status.get("readable") and status.get("namespace_exists") else "unavailable")
    status.setdefault("write_access_state", "available" if status.get("writable") else "unavailable")
    if status["read_access_state"] != "available":
        return {**status, "verified": False, "verification_error": status.get("problem") or "archive_root_unavailable"}
    if not require_write:
        return {**status, "verified": True, "verification_error": None}

    namespace = runtime_path / KMVMS_RECORDINGS_NAMESPACE
    probe = namespace / f".km-vms-access-probe-{uuid.uuid4().hex}"
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, b"km-vms-storage-access-probe\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if probe.read_bytes() != b"km-vms-storage-access-probe\n":
            raise OSError("archive_root_probe_readback_failed")
        probe.unlink()
    except OSError:
        try:
            probe.unlink()
        except OSError:
            pass
        return {
            **status,
            "verified": False,
            "writable": False,
            "write_access_state": "unavailable",
            "verification_error": "archive_root_write_probe_failed",
        }
    return {**status, "verified": True, "writable": True, "write_access_state": "available", "verification_error": None}


def verify_archive_root_access(root_row, *, require_write: bool) -> dict:
    status = archive_root_runtime_access_state(root_row)
    return verify_runtime_path_access(status["runtime_path"], require_write=require_write, base_status=status)


def archive_root_physical_volume_id(root_row) -> str:
    raw = archive_root_host_display_path(root_row).replace("\\", "/")
    parts = [part for part in raw.split("/") if part]
    if raw.startswith("/") and parts:
        if parts[0].lower().startswith("volume"):
            return f"/{parts[0]}"
        return f"/{parts[0]}"
    if len(parts) >= 2 and parts[0].endswith(":"):
        return f"{parts[0]}/{parts[1]}"
    return parts[0] if parts else "unknown"


def _compose_yaml_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_archive_roots_runtime_files(db: Session) -> dict:
    from app.models.recording import ArchiveRoot

    control_dir = Path(settings.storage_install_control)
    control_dir.mkdir(parents=True, exist_ok=True)
    roots = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.retired_at.is_(None))
        .order_by(ArchiveRoot.is_active.desc(), ArchiveRoot.created_at.asc(), ArchiveRoot.id.asc())
        .all()
    )
    existing_entries = _archive_roots_runtime_entries()
    items = []
    volume_lines = []
    seen_targets: set[str] = set()
    for root in roots:
        root_id = str(root.id or "")
        if not root_id:
            continue
        existing = existing_entries.get(root_id)
        if existing is not None:
            host_path = str(existing["user_display_path"])
            target_path = str(existing["backend_runtime_path"])
        else:
            host_path = archive_root_host_display_path(root)
            target_path = archive_root_runtime_mount_path(root).as_posix()
        if target_path in seen_targets:
            continue
        seen_targets.add(target_path)
        item = {
            "root_id": root_id,
            "user_display_path": host_path,
            "backend_runtime_path": target_path,
            "physical_volume_id": archive_root_physical_volume_id(root),
            "storage_namespace": getattr(root, "storage_namespace", KMVMS_RECORDINGS_NAMESPACE),
            "active_write_target": bool(getattr(root, "is_active", False)),
        }
        items.append(item)
        volume_lines.extend(
            [
                "      - type: bind",
                f"        source: {_compose_yaml_quote(host_path)}",
                f"        target: {_compose_yaml_quote(target_path)}",
                "        read_only: false",
                "        bind:",
                "          create_host_path: false",
            ]
        )
    manifest = {
        "schema_version": 1,
        "runtime_base": archive_roots_runtime_base().as_posix(),
        "compose_override_file": ARCHIVE_ROOTS_COMPOSE_OVERRIDE,
        "items": items,
        "raw_runtime_paths_user_visible": False,
    }
    manifest_path = archive_roots_manifest_path()
    tmp_manifest = manifest_path.with_name(f"{manifest_path.name}.tmp")
    tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_manifest.replace(manifest_path)

    compose_path = archive_roots_compose_override_path()
    tmp_compose = compose_path.with_name(f"{compose_path.name}.tmp")
    if volume_lines:
        compose_text = "\n".join(
            [
                "# Generated by KM VMS. Do not edit manually.",
                "services:",
                "  api:",
                "    volumes:",
                *volume_lines,
                "  schema-update:",
                "    volumes:",
                *volume_lines,
                "",
            ]
        )
    else:
        compose_text = "# Generated by KM VMS. No archive roots configured.\nservices: {}\n"
    tmp_compose.write_text(compose_text, encoding="utf-8")
    tmp_compose.replace(compose_path)
    for path in (manifest_path, compose_path):
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return {
        "manifest_path": str(manifest_path),
        "compose_override_path": str(compose_path),
        "root_count": len(items),
        "items": items,
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
    from app.models.recording import ArchiveRoot

    default_path = Path(settings.storage_root).resolve(strict=False)
    rows = db.query(ArchiveRoot).order_by(ArchiveRoot.created_at.asc(), ArchiveRoot.id.asc()).all()
    default = db.get(ArchiveRoot, DEFAULT_ARCHIVE_ROOT_ID)
    if default is None:
        default = next(
            (
                row
                for row in rows
                if Path(str(row.root_path or "")).resolve(strict=False) == default_path
                and row.retired_at is None
            ),
            None,
        )
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

    return (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.retired_at.is_(None))
        .order_by(ArchiveRoot.is_active.desc(), ArchiveRoot.created_at.asc(), ArchiveRoot.id.asc())
        .all()
    )


def legacy_archive_root_counts(db: Session) -> dict:
    from app.models.recording import RecordingSegment

    return {
        "null_archive_root_id_count": int(
            db.query(RecordingSegment)
            .filter(RecordingSegment.deleted_at.is_(None), RecordingSegment.archive_root_id.is_(None))
            .count()
        ),
        "root_unresolved_count": int(
            db.query(RecordingSegment)
            .filter(
                RecordingSegment.deleted_at.is_(None),
                RecordingSegment.archive_root_resolution_status.in_(tuple(ROOT_RESOLUTION_PROBLEMS)),
            )
            .count()
        ),
        "default_archive_root_id": DEFAULT_ARCHIVE_ROOT_ID,
    }


def backfill_legacy_archive_root_ids(db: Session) -> dict:
    return migrate_archive_root_identities(db)


def segment_archive_root_resolution(segment) -> str:
    explicit = str(getattr(segment, "archive_root_resolution_status", None) or "").strip()
    if explicit in ROOT_RESOLUTION_PROBLEMS:
        return explicit
    if not getattr(segment, "archive_root_id", None):
        return ROOT_RESOLUTION_UNRESOLVED
    return ROOT_RESOLUTION_RESOLVED


def segment_has_resolved_archive_root(segment) -> bool:
    return segment_archive_root_resolution(segment) == ROOT_RESOLUTION_RESOLVED


def _segment_candidate_relative_path(segment) -> str | None:
    raw = getattr(segment, "relative_path", None) or getattr(segment, "file_path", None)
    if not raw:
        return None
    candidate = Path(str(raw))
    if candidate.is_absolute():
        return None
    try:
        normalized = _normalize_relative(str(raw))
    except ValueError:
        return None
    return normalized if is_kmvms_namespace_relative(normalized) else None


def _set_segment_root_resolution(segment, *, root_id: str | None, status: str, detail: str | None) -> bool:
    changed = False
    same_resolution = (
        getattr(segment, "archive_root_id", None) == root_id
        and getattr(segment, "archive_root_resolution_status", None) == status
        and getattr(segment, "archive_root_resolution_detail", None) == detail
    )
    resolved_at = getattr(segment, "archive_root_resolved_at", None)
    if status == ROOT_RESOLUTION_RESOLVED and (not same_resolution or resolved_at is None):
        resolved_at = datetime.utcnow()
    elif status != ROOT_RESOLUTION_RESOLVED:
        resolved_at = None
    values = {
        "archive_root_id": root_id,
        "archive_root_resolution_status": status,
        "archive_root_resolution_detail": detail,
        "archive_root_resolved_at": resolved_at,
    }
    for field, value in values.items():
        if getattr(segment, field, None) != value:
            setattr(segment, field, value)
            changed = True
    if changed:
        segment.updated_at = datetime.utcnow()
    return changed


def migrate_archive_root_identities(db: Session) -> dict:
    from app.models.recording import ArchiveRoot, RecordingSegment

    ensure_archive_roots(db)
    roots = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.retired_at.is_(None))
        .order_by(ArchiveRoot.created_at.asc(), ArchiveRoot.id.asc())
        .all()
    )
    root_by_id = {str(root.id): root for root in roots}
    access_by_id = {str(root.id): archive_root_runtime_access_state(root) for root in roots}
    base_query = db.query(RecordingSegment).filter(
        RecordingSegment.deleted_at.is_(None),
        RecordingSegment.status != "deleted",
        RecordingSegment.ownership == "KM VMS",
        RecordingSegment.source == "recorder",
    )
    before_non_deleted_count = int(base_query.count())
    segments = (
        base_query.filter(
            or_(
                RecordingSegment.archive_root_id.is_(None),
                RecordingSegment.archive_root_resolution_status.is_(None),
                RecordingSegment.archive_root_resolution_status.in_(tuple(ROOT_RESOLUTION_PROBLEMS)),
            )
        )
        .order_by(RecordingSegment.id.asc())
        .all()
    )
    before_null = sum(1 for segment in segments if not segment.archive_root_id)
    counts = {
        "before_non_deleted_count": before_non_deleted_count,
        "evaluated_candidate_count": len(segments),
        "before_null_count": before_null,
        "uniquely_resolved_count": 0,
        "preserved_resolved_count": 0,
        "repaired_assigned_count": 0,
        "unresolved_count": 0,
        "ambiguous_count": 0,
        "inaccessible_during_resolution_count": 0,
        "root_identity_conflict_count": 0,
        "changed_count": 0,
    }

    for segment in segments:
        relative_path = _segment_candidate_relative_path(segment)
        assigned_root = root_by_id.get(str(segment.archive_root_id)) if segment.archive_root_id else None
        prior_resolution = str(getattr(segment, "archive_root_resolution_status", None) or "")
        prior_problem = prior_resolution in ROOT_RESOLUTION_PROBLEMS
        readable_roots = [root for root in roots if access_by_id[str(root.id)].get("read_access_state") == "available"]
        inaccessible_present = len(readable_roots) != len(roots)
        matches = []
        if relative_path:
            for root in readable_roots:
                try:
                    candidate = safe_resolve_relative_for_root(relative_path, root)
                    if candidate.exists() and candidate.is_file():
                        matches.append(root)
                except (OSError, ValueError):
                    continue

        assigned_matches = bool(assigned_root and any(root.id == assigned_root.id for root in matches))
        changed = False
        if assigned_matches:
            changed = _set_segment_root_resolution(
                segment,
                root_id=str(assigned_root.id),
                status=ROOT_RESOLUTION_RESOLVED,
                detail="assigned_root_file_verified",
            )
            counts["preserved_resolved_count"] += 1
        elif assigned_root is not None and prior_problem and access_by_id[str(assigned_root.id)].get("read_access_state") != "available":
            changed = _set_segment_root_resolution(
                segment,
                root_id=str(assigned_root.id),
                status=ROOT_RESOLUTION_INACCESSIBLE,
                detail="assigned_root_unavailable_for_resolution",
            )
            counts["inaccessible_during_resolution_count"] += 1
        elif assigned_root is not None and access_by_id[str(assigned_root.id)].get("read_access_state") != "available":
            changed = _set_segment_root_resolution(
                segment,
                root_id=str(assigned_root.id),
                status=ROOT_RESOLUTION_RESOLVED,
                detail="assigned_root_temporarily_unavailable",
            )
            counts["preserved_resolved_count"] += 1
        elif assigned_root is not None and len(matches) == 1 and not assigned_matches:
            changed = _set_segment_root_resolution(
                segment,
                root_id=str(assigned_root.id),
                status=ROOT_RESOLUTION_CONFLICT,
                detail="assigned_root_conflicts_with_unique_physical_file_evidence",
            )
            counts["root_identity_conflict_count"] += 1
        elif assigned_root is None and len(matches) == 1 and not inaccessible_present:
            matched_root = matches[0]
            changed = _set_segment_root_resolution(
                segment,
                root_id=str(matched_root.id),
                status=ROOT_RESOLUTION_RESOLVED,
                detail="unique_file_evidence",
            )
            counts["uniquely_resolved_count"] += 1
        elif len(matches) > 1:
            changed = _set_segment_root_resolution(
                segment,
                root_id=segment.archive_root_id,
                status=ROOT_RESOLUTION_AMBIGUOUS,
                detail="multiple_readable_roots_contain_candidate",
            )
            counts["ambiguous_count"] += 1
        elif inaccessible_present and assigned_root is None:
            changed = _set_segment_root_resolution(
                segment,
                root_id=None,
                status=ROOT_RESOLUTION_INACCESSIBLE,
                detail="one_or_more_roots_unavailable_for_unique_proof",
            )
            counts["inaccessible_during_resolution_count"] += 1
        elif assigned_root is None:
            changed = _set_segment_root_resolution(
                segment,
                root_id=None,
                status=ROOT_RESOLUTION_UNRESOLVED,
                detail="no_unique_root_evidence",
            )
            counts["unresolved_count"] += 1
        elif prior_problem:
            unresolved_status = ROOT_RESOLUTION_CONFLICT if prior_resolution == ROOT_RESOLUTION_CONFLICT else ROOT_RESOLUTION_UNRESOLVED
            changed = _set_segment_root_resolution(
                segment,
                root_id=str(assigned_root.id),
                status=unresolved_status,
                detail=getattr(segment, "archive_root_resolution_detail", None) or "no_unique_root_evidence",
            )
            counts["root_identity_conflict_count" if unresolved_status == ROOT_RESOLUTION_CONFLICT else "unresolved_count"] += 1
        else:
            changed = _set_segment_root_resolution(
                segment,
                root_id=str(assigned_root.id),
                status=ROOT_RESOLUTION_RESOLVED,
                detail="assigned_root_file_missing",
            )
            counts["preserved_resolved_count"] += 1

        if changed:
            db.add(segment)
            counts["changed_count"] += 1

    if counts["changed_count"]:
        db.commit()
    counts["after_null_count"] = int(
        db.query(RecordingSegment)
        .filter(RecordingSegment.deleted_at.is_(None), RecordingSegment.archive_root_id.is_(None))
        .count()
    )
    counts["no_destructive_candidate_count"] = int(
        db.query(RecordingSegment)
        .filter(
            RecordingSegment.deleted_at.is_(None),
            RecordingSegment.archive_root_resolution_status.in_(tuple(ROOT_RESOLUTION_PROBLEMS)),
        )
        .count()
    )
    counts["migration_schema_version"] = 1
    counts["migration_status"] = "completed"
    counts["idempotent_noop"] = counts["changed_count"] == 0
    return counts


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

    resolution = segment_archive_root_resolution(segment)
    if resolution != ROOT_RESOLUTION_RESOLVED:
        raise ValueError(resolution)
    root_id = getattr(segment, "archive_root_id", None)
    if not root_id:
        raise ValueError(ROOT_RESOLUTION_UNRESOLVED)
    row = db.get(ArchiveRoot, root_id)
    if row is None:
        raise ValueError(ROOT_RESOLUTION_UNRESOLVED)
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
    path = build_namespace_dir(camera_id, job_id, root=Path(settings.storage_root))
    path.mkdir(parents=True, exist_ok=True)
    return path


def archive_root_public_status(root_row, *, include_path: bool = False) -> dict:
    configured_path = _stored_root_path(root_row)
    requires_activation = _inactive_runtime_activation_root(root_row)
    status = archive_root_runtime_access_state(root_row)
    read_problem = status["problem"] if status["read_access_state"] != "available" else None
    write_problem = status["problem"] if status["write_access_state"] != "available" else None
    operational_problem = read_problem or (write_problem if bool(getattr(root_row, "is_active", False)) else None)
    result = {
        "id": getattr(root_row, "id", None),
        "label": getattr(root_row, "label", None),
        "storage_namespace": getattr(root_row, "storage_namespace", KMVMS_RECORDINGS_NAMESPACE),
        "is_active": bool(getattr(root_row, "is_active", False)),
        "is_readable": bool(status["readable"]),
        "is_writable": bool(status["writable"]),
        "is_available": bool(status["available"]),
        "namespace_exists": bool(status["namespace_exists"]),
        "problem": operational_problem or getattr(root_row, "problem", None),
        "problem_category": operational_problem or getattr(root_row, "problem", None),
        "read_problem": read_problem,
        "write_problem": write_problem,
        "access_state": status["access_state"],
        "read_access_state": status["read_access_state"],
        "write_access_state": status["write_access_state"],
        "mount_access_state": status["mount_access_state"],
        "physical_volume_id": archive_root_physical_volume_id(root_row),
        "active_write_target": bool(getattr(root_row, "is_active", False)),
        "retired": bool(getattr(root_row, "retired_at", None)),
        "retirement_status": getattr(root_row, "retirement_status", None),
        "retirement_cleanup_status": getattr(root_row, "retirement_cleanup_status", None),
        "retirement_operation_id": getattr(root_row, "retirement_operation_id", None),
        "requires_activation": requires_activation,
    }
    if include_path:
        result["configured_path"] = archive_root_host_display_path(root_row)
    else:
        result["path_label"] = configured_path.name or "archive"
    return result


def root_usage(db: Session, root_row) -> dict:
    from app.models.recording import RecordingSegment

    status = archive_root_runtime_access_state(root_row)
    count = 0
    existing = 0
    missing = 0
    inaccessible = 0
    resolution_problems = 0
    size = 0
    for segment in (
        db.query(RecordingSegment)
        .filter(RecordingSegment.archive_root_id == getattr(root_row, "id", None))
        .all()
    ):
        if getattr(segment, "deleted_at", None) is not None:
            continue
        if getattr(segment, "status", None) in {"deleted", "writing", "starting"}:
            continue
        if getattr(segment, "ownership", None) != "KM VMS" or getattr(segment, "source", None) != "recorder":
            continue
        if getattr(segment, "status", None) not in {"finalized", "ready"}:
            continue
        count += 1
        resolution = segment_archive_root_resolution(segment)
        if resolution != ROOT_RESOLUTION_RESOLVED:
            resolution_problems += 1
            continue
        if status["read_access_state"] != "available":
            inaccessible += 1
            continue
        try:
            target = resolve_segment_file_path(db, segment)
            if target.exists() and target.is_file():
                existing += 1
                size += int(target.stat().st_size)
            else:
                missing += 1
        except ValueError:
            resolution_problems += 1
        except OSError:
            inaccessible += 1
    problem_count = missing + resolution_problems
    expected_unmounted_empty_root = bool(
        not getattr(root_row, "is_active", False)
        and count == 0
        and _inactive_runtime_activation_root(root_row)
    )
    root_access_problem = status["read_access_state"] != "available" and not expected_unmounted_empty_root
    return {
        "segments_count": count,
        "existing_file_count": existing,
        "missing_file_count": missing,
        "inaccessible_file_count": inaccessible,
        "root_resolution_problem_count": resolution_problems,
        "problem_file_count": problem_count,
        "size_bytes": size,
        "root_access_problem_count": 1 if root_access_problem else 0,
        "root_access_problem": status["problem"] if root_access_problem else None,
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
