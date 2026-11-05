import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.camera import Camera


def slugify_camera_name(name: str) -> str:
    value = name.strip()
    value = re.sub(r"[\x00-\x1f\x7f%\\\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value[:120].encode("utf-8")[:231].decode("utf-8", errors="ignore")
    return value.strip("_") or "camera"


def build_unique_folder_name(db: Session, camera_name: str) -> str:
    base = slugify_camera_name(camera_name)
    candidate = base
    counter = 2

    while db.query(Camera).filter(Camera.storage_folder_name == candidate).first():
        suffix = f"_{counter}"
        candidate = base.encode("utf-8")[:255 - len(suffix)].decode("utf-8", errors="ignore") + suffix
        counter += 1

    return candidate


def ensure_camera_folder(folder_name: str) -> str:
    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)

    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    return str(folder)
