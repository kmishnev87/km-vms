"""Read-only, paged presentation of user archive actions, not worker receipts."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, and_, cast, exists, func, or_, text
from sqlalchemy.orm import aliased

from app.models.archive_integrity import ArchiveIntegrityFinding, ArchiveIntegrityRemediationPlan
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.storage_operation import StorageOperation
from app.services.storage_operations_foundation import database_now, safe_reason_code

BAD_RESULTS = ("failed", "blocked", "partial", "interrupted")
AUTO_TYPES = ("retention_auto_run", "retention_auto_free_space")
DELETED_SUFFIX = re.compile(r"__deleted_\d+_\d+$")


def _public_label(value, limit=255):
    return re.sub(r"[\x00-\x1f]+", " ", DELETED_SUFFIX.sub("", str(value or ""))).strip()[:limit]


def _finding_state(finding):
    if finding is None:
        return "unconfirmed"
    if finding.state in {"resolved", "superseded", "stale"} or not finding.is_active:
        return "resolved" if finding.state == "resolved" else "historical"
    return "attention" if finding.state == "active" else "historical"


def _history_clock_zone(db):
    if db.get_bind().dialect.name == "postgresql":
        name = db.execute(text("SELECT current_setting('TimeZone')")).scalar_one()
        try:
            return ZoneInfo(str(name))
        except (ValueError, KeyError):
            # PostgreSQL also accepts fixed-offset zone specifications.
            return db.execute(text("SELECT current_timestamp")).scalar_one().tzinfo or timezone.utc
    return timezone.utc


def _history_timestamp(value, clock_zone):
    return value.replace(tzinfo=clock_zone).astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _item(db, row, *, details=True, clock_zone=timezone.utc):
    progress, result, scope = row.progress or {}, row.result or {}, row.scope or {}
    camera_ids = [int(value) for value in scope.get("camera_ids", []) if str(value).isdigit()]
    cameras = db.query(Camera).filter(Camera.id.in_(camera_ids)).all() if camera_ids else []
    finding_id = progress.get("finding_id")
    finding = db.get(ArchiveIntegrityFinding, finding_id) if finding_id else None
    current_state = "historical" if row.status not in BAD_RESULTS else "unconfirmed"
    if row.operation_type in AUTO_TYPES and row.status in BAD_RESULTS:
        current_state = "attention"
        # A later successful pass of the same rule/scope supersedes the old
        # warning without rewriting the failed attempt in the durable journal.
        later = db.query(StorageOperation).filter(StorageOperation.operation_type == row.operation_type,
            StorageOperation.finished_at > row.finished_at, StorageOperation.status == "completed"
        ).order_by(StorageOperation.finished_at.desc()).limit(20).all()
        for candidate in later:
            if (candidate.scope or {}) == scope:
                current_state = "historical"
                break
    if finding_id:
        current_state = _finding_state(finding)
    if row.operation_type == "camera_delete_with_files" and camera_ids:
        remaining = db.query(RecordingSegment.id).filter(
            RecordingSegment.camera_id.in_(camera_ids), RecordingSegment.deleted_at.is_(None),
            RecordingSegment.status != "deleted").first()
        if not remaining and all(camera.deleted_at is not None for camera in cameras):
            current_state = "object_removed"
        elif row.status in BAD_RESULTS:
            current_state = "attention"
    children = []
    detail_count = 0
    reason = safe_reason_code(row.reason_code, fallback="storage_operation_attention_required") if row.status in BAD_RESULTS else None
    if row.operation_type == "integrity_review_batch":
        children_query = db.query(StorageOperation).filter(
            StorageOperation.operation_type == "integrity_review_item",
            StorageOperation.progress["batch_id"].as_string() == row.id)
        detail_count = children_query.count()
        linked = db.query(ArchiveIntegrityFinding.state, ArchiveIntegrityFinding.is_active, func.count(StorageOperation.id)).join(
            StorageOperation, StorageOperation.progress["finding_id"].as_string() == ArchiveIntegrityFinding.id
        ).filter(StorageOperation.operation_type == "integrity_review_item",
                 StorageOperation.progress["batch_id"].as_string() == row.id
        ).group_by(ArchiveIntegrityFinding.state, ArchiveIntegrityFinding.is_active).all()
        linked_count = sum(int(count) for _state, _active, count in linked)
        if any(state == "active" and active for state, active, _count in linked):
            current_state = "attention"
        elif linked_count and linked_count == detail_count:
            current_state = "resolved" if all(state == "resolved" for state, _active, _count in linked) else "historical"
        if details:
            for child in children_query.order_by(StorageOperation.finished_at.desc(), StorageOperation.id).limit(10).all():
                children.append(_item(db, child, details=False, clock_zone=clock_zone))
            reason = next((child["reason_code"] for child in children if child["status"] in BAD_RESULTS and child["reason_code"]), reason)
    return {
        "id": str(row.id), "operation_type": str(row.operation_type), "status": str(row.status),
        "finished_at": _history_timestamp(row.finished_at, clock_zone),
        "current_state": current_state, "reason_code": reason,
        "camera_name": _public_label(finding.camera_name_snapshot) if finding else ", ".join(_public_label(camera.name) for camera in cameras),
        "file_name": _public_label(finding.display_name) if finding else "",
        "action": progress.get("action") if progress.get("action") in {"delete", "repair", "acknowledge", "reopen", "retire"} else None,
        "completed_count": int(result.get("completed_count") or result.get("deleted_count") or 0),
        "failed_count": int(result.get("failed_count") or 0), "bytes_freed": int(result.get("bytes_freed") or 0),
        "detail_count": detail_count, "details": children,
        "can_open_integrity": row.operation_type.startswith("integrity_") or row.operation_type == "orphan_file_cleanup",
        "can_open_cameras": row.operation_type == "camera_delete_with_files" and current_state == "attention",
    }


def archive_operation_history_items(db, *, period_days=30, operation_type=None, result_status=None, limit=20, offset=0):
    period_days = max(1, min(int(period_days), 30))
    limit, offset = max(1, min(int(limit), 50)), max(0, int(offset))
    parent, child = aliased(StorageOperation), aliased(StorageOperation)
    plan, plan_item, plan_batch = aliased(ArchiveIntegrityRemediationPlan), aliased(StorageOperation), aliased(StorageOperation)
    child_of_batch = and_(StorageOperation.operation_type == "integrity_review_item", exists().where(
        and_(parent.id == StorageOperation.progress["batch_id"].as_string(), parent.operation_type == "integrity_review_batch")))
    nested_recheck = and_(StorageOperation.operation_type == "integrity_metadata_repair", exists().where(
        and_(child.operation_type == "integrity_review_item", StorageOperation.id == "recheck-" + child.id)))
    nested_remediation = exists().where(and_(
        or_(plan.operation_id == StorageOperation.id, plan.apply_operation_id == StorageOperation.id),
        plan_item.operation_type == "integrity_review_item", plan_item.result["plan_id"].as_string() == plan.id,
        plan_batch.operation_type == "integrity_review_batch", plan_batch.id == plan_item.progress["batch_id"].as_string(),
    ))
    query = db.query(StorageOperation).filter(
        StorageOperation.finished_at >= database_now(db) - timedelta(days=period_days),
        ~child_of_batch, ~nested_recheck, ~nested_remediation,
        # Automatic no-op cycles belong in the aggregate, not in a noisy list.
        or_(~StorageOperation.operation_type.in_(AUTO_TYPES), StorageOperation.status.in_(BAD_RESULTS),
            func.coalesce(cast(StorageOperation.result["deleted_count"].as_string(), BigInteger), 0) > 0,
            func.coalesce(cast(StorageOperation.result["bytes_freed"].as_string(), BigInteger), 0) > 0),
    )
    available_types = [str(value[0]) for value in query.with_entities(StorageOperation.operation_type).distinct().all()]
    if operation_type:
        query = query.filter(StorageOperation.operation_type == str(operation_type))
    if result_status:
        query = query.filter(StorageOperation.status == str(result_status))
    total = query.count()
    rows = query.order_by(StorageOperation.finished_at.desc(), StorageOperation.id.desc()).offset(offset).limit(limit).all()
    clock_zone = _history_clock_zone(db)
    items = [_item(db, row, clock_zone=clock_zone) for row in rows]
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "has_more": offset + len(items) < total, "period_days": period_days,
            "operation_types": sorted(available_types),
            "attention_items": [item for item in items if item["current_state"] == "attention"]}
