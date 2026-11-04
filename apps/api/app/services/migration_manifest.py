"""Canonical payloads shared by planning and execution."""
from __future__ import annotations
import hashlib
import json
from typing import Any
from app.models.archive_migration import ArchiveMigrationItem
from app.models.recording import RecordingSegment

def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _segment_metadata_fingerprint(segment: RecordingSegment) -> str:
    return _sha256_json(
        {
            "segment_id": int(segment.id),
            "camera_id": int(segment.camera_id),
            "archive_root_id": str(segment.archive_root_id or ""),
            "relative_path": str(segment.relative_path or ""),
            "status": str(segment.status or ""),
            "ownership": str(segment.ownership or ""),
            "source": str(segment.source or ""),
            "storage_namespace": str(segment.storage_namespace or ""),
            "deleted_at": segment.deleted_at.isoformat() if segment.deleted_at else None,
            "finalized_at": segment.finalized_at.isoformat() if segment.finalized_at else None,
            "updated_at": segment.updated_at.isoformat() if segment.updated_at else None,
        }
    )


def _item_canonical_payload(item: ArchiveMigrationItem) -> dict[str, Any]:
    return {
        "plan_id": str(item.plan_id),
        "item_index": int(item.item_index),
        "segment_id": int(item.segment_id),
        "camera_id": int(item.camera_id),
        "source_root_id": str(item.source_root_id),
        "target_root_id": str(item.target_root_id),
        "source_physical_identity": str(item.source_physical_identity),
        "target_physical_identity": str(item.target_physical_identity),
        "source_snapshot_key": str(item.source_snapshot_key),
        "target_snapshot_key": str(item.target_snapshot_key),
        "source_access_identity": str(item.source_access_identity),
        "target_access_identity": str(item.target_access_identity),
        "source_relative_path": str(item.source_relative_path),
        "target_final_relative_path": str(item.target_final_relative_path),
        "target_temp_relative_path": str(item.target_temp_relative_path),
        "source_quarantine_relative_path": str(item.source_quarantine_relative_path),
        "source_size_bytes": int(item.source_size_bytes),
        "source_mtime_ns": int(item.source_mtime_ns),
        "source_device": int(item.source_device),
        "source_inode": int(item.source_inode),
        "source_mode": int(item.source_mode),
        "source_uid": item.source_uid,
        "source_gid": item.source_gid,
        "source_metadata_fingerprint": str(item.source_metadata_fingerprint),
        "source_sha256": str(item.source_sha256 or ""),
        "intended_transition": str(item.intended_transition),
    }


def _item_snapshot(item: ArchiveMigrationItem, *, source: bool) -> dict[str, Any]:
    prefix = "source" if source else "target"
    return {
        "root_id": str(getattr(item, f"{prefix}_root_id")),
        "physical_identity": str(getattr(item, f"{prefix}_physical_identity")),
        "snapshot_key": str(getattr(item, f"{prefix}_snapshot_key")),
        "access_identity": str(getattr(item, f"{prefix}_access_identity")),
    }


def _item_manifest_valid(item: ArchiveMigrationItem) -> bool:
    return bool(item.canonical_hash == _sha256_json(_item_canonical_payload(item)))
