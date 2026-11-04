"""Bounded, regular-file state I/O; no Docker or command dispatch."""
from __future__ import annotations
import json
import os
import uuid
from pathlib import Path
from typing import Any
from .contracts import MAX_CONTROL_BYTES, BridgeError, utcnow

def read_json_object(
    path: Path,
    *,
    missing_ok: bool = False,
    max_bytes: int = MAX_CONTROL_BYTES,
) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            raise BridgeError("control_file_invalid", "Update control data is not a regular file.")
        if not path.exists():
            if missing_ok:
                return None
            raise BridgeError("control_file_missing", "Required update control data is missing.")
        if not path.is_file() or path.is_symlink():
            raise BridgeError("control_file_invalid", "Update control data is not a regular file.")
        if path.stat().st_size > max_bytes:
            raise BridgeError("control_file_too_large", "Update control data exceeds its size limit.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except BridgeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise BridgeError("control_file_invalid", "Update control data is unavailable or invalid.") from exc
    if not isinstance(payload, dict):
        raise BridgeError("control_file_invalid", "Update control data must be a JSON object.")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(rendered.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise BridgeError("receipt_too_large", "Update-helper refresh receipt exceeds its size limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(tmp, 0o600)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise BridgeError("receipt_write_failed", "Cannot persist update-helper refresh receipt.") from exc


def read_regular_text(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise BridgeError(
                "archive_roots_override_invalid",
                "Generated archive-roots Compose override is not a regular file.",
            )
        if path.stat().st_size > MAX_CONTROL_BYTES:
            raise BridgeError(
                "archive_roots_override_invalid",
                "Generated archive-roots Compose override exceeds its size limit.",
            )
        return path.read_text(encoding="utf-8")
    except BridgeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BridgeError(
            "archive_roots_override_invalid",
            "Generated archive-roots Compose override is unavailable.",
        ) from exc


def atomic_write_text(path: Path, rendered: str) -> None:
    if len(rendered.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise BridgeError(
            "archive_roots_override_invalid",
            "Generated archive-roots Compose override exceeds its size limit.",
        )
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(tmp, 0o600)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise BridgeError(
            "archive_roots_override_write_failed",
            "Cannot normalize the generated archive-roots Compose override.",
        ) from exc


def write_receipt(
    receipt_file: Path,
    *,
    request_id: str,
    status: str,
    expected_image_id: str,
    message: str,
    helper_container_id: str | None = None,
) -> None:
    atomic_write_json(
        receipt_file,
        {
            "schema_version": 1,
            "request_id": request_id,
            "status": status,
            "expected_image_id": expected_image_id,
            "helper_container_id": helper_container_id,
            "updated_at": utcnow(),
            "message": message,
        },
    )
