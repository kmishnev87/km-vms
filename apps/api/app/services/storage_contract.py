from __future__ import annotations

import os
from pathlib import Path

from app.core.config import settings
from app.services.recording_storage import KMVMS_RECORDINGS_NAMESPACE

RECORDING_FORMATS = {"mkv", "mp4"}
RECORDING_PROFILE_BY_FORMAT = {
    "mkv": "reliability",
    "mp4": "compatibility",
}
RECORDING_FORMAT_BY_PROFILE = {
    "reliability": "mkv",
    "compatibility": "mp4",
}


def normalize_recording_format(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in RECORDING_FORMATS else "mkv"


def recording_profile_for_format(value: str | None) -> str:
    return RECORDING_PROFILE_BY_FORMAT[normalize_recording_format(value)]


def storage_contract(*, db_storage_path: str | None = None) -> dict:
    runtime_root = str(Path(settings.storage_root))
    namespace_root = str(Path(settings.storage_root) / KMVMS_RECORDINGS_NAMESPACE)
    host_path = os.getenv("STORAGE_HOST_ROOT") or os.getenv("SURVEILLANCE_ROOT") or None
    return {
        "archive_host_path": host_path,
        "archive_primary_path": host_path or runtime_root,
        "archive_primary_path_source": "host_bind_env" if host_path else "container_runtime_fallback",
        "host_storage_path": host_path,
        "storage_host_path": host_path,
        "container_runtime_storage_root": runtime_root,
        "storage_root": runtime_root,
        "storage_container_path": runtime_root,
        "container_recordings_namespace_root": namespace_root,
        "storage_recordings_path": namespace_root,
        "storage_namespace": KMVMS_RECORDINGS_NAMESPACE,
        "relative_recording_path_contract": f"{KMVMS_RECORDINGS_NAMESPACE}/...",
        "storage_previews_path": str(Path(settings.storage_previews)),
        "storage_exports_path": str(Path(settings.storage_exports)),
        "db_storage_path": db_storage_path,
        "storage_path_role": "db_reference_container_path",
        "storage_runtime_source": "settings.storage_root_env",
        "storage_editable": False,
        "storage_change_requires": "installer_or_deploy_remount",
    }


def recording_format_contract(recording_format: str | None) -> dict:
    normalized = normalize_recording_format(recording_format)
    return {
        "recording_format": normalized,
        "recording_profile": recording_profile_for_format(normalized),
        "valid_recording_formats": sorted(RECORDING_FORMATS),
        "profile_mapping": dict(RECORDING_FORMAT_BY_PROFILE),
        "active_change_behavior": "blocked_while_active_recording_jobs_exist",
    }
