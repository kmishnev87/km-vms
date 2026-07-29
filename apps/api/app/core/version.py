from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


APP_VERSION = "0.8.3"
DEVELOPMENT_BUILD_ID = "development"


def _safe_text(value: Any, *, max_length: int = 120) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_length]


def _metadata_file_path() -> Path:
    return Path(os.getenv("KMVMS_BUILD_METADATA_FILE") or "/app/build-info.json")


def _load_build_metadata_file() -> dict[str, Any]:
    path = _metadata_file_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def installed_build_metadata() -> dict[str, Any]:
    payload = _load_build_metadata_file()
    build_id = _safe_text(os.getenv("KMVMS_BUILD_ID") or payload.get("build_id"))
    git_commit = _safe_text(os.getenv("KMVMS_GIT_COMMIT") or payload.get("git_commit"))
    build_time = _safe_text(os.getenv("KMVMS_BUILD_TIME") or payload.get("build_time"))
    install_source = _safe_text(os.getenv("KMVMS_INSTALL_SOURCE") or payload.get("install_source"))
    source_channel_id = _safe_text(os.getenv("KMVMS_SOURCE_CHANNEL_ID") or payload.get("source_channel_id"))
    metadata_source = "build_metadata_file_or_env" if any([build_id, git_commit, build_time, install_source, source_channel_id]) else "development_fallback"
    return {
        "product_name": "KM VMS",
        "app_version": _safe_text(payload.get("app_version")) or APP_VERSION,
        "build_id": build_id or DEVELOPMENT_BUILD_ID,
        "git_commit": git_commit,
        "build_time": build_time,
        "install_source": install_source or "development",
        "source_channel_id": source_channel_id,
        "schema_compatibility_min": payload.get("schema_compatibility_min"),
        "schema_compatibility_max": payload.get("schema_compatibility_max") or payload.get("supported_schema_version"),
        "supported_schema_version": payload.get("supported_schema_version"),
        "metadata_source": metadata_source,
        "status": "installed_build_known" if metadata_source == "build_metadata_file_or_env" else "development_build",
        "limitation": None if metadata_source == "build_metadata_file_or_env" else "No installed build metadata file/env is configured; update identity is development-only.",
    }


APP_BUILD_VERSION = installed_build_metadata()["build_id"]
