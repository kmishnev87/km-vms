from __future__ import annotations

import re
from pathlib import Path

from app.core.config import settings

KMVMS_RECORDINGS_NAMESPACE = "kmvms/recordings"
VIDEO_EXTENSIONS = {".mp4", ".mkv"}


def storage_root() -> Path:
    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


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
