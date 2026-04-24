import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.camera import Camera


def slugify_camera_name(name: str) -> str:
    value = name.strip()
    value = re.sub(r"[\\\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value[:120].strip("_") or "camera"


def build_unique_folder_name(db: Session, camera_name: str) -> str:
    base = slugify_camera_name(camera_name)
    candidate = base
    counter = 2

    while db.query(Camera).filter(Camera.storage_folder_name == candidate).first():
        candidate = f"{base}_{counter}"
        counter += 1

    return candidate


def ensure_camera_folder(folder_name: str) -> str:
    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)

    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    return str(folder)
