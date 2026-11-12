"""Explicit archive review, using the existing durable operation ledger.

Queue envelopes never own files. Physical deletion keeps the existing exact,
permissioned remediation path; rechecks own an exact metadata-operation lease.
No recurring filesystem scan, new schema, or development-only behaviour.
"""
from __future__ import annotations

import hashlib
import json
import stat
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.orm import Session

from app.core.permissions import user_has_permission
from app.models.archive_integrity import ArchiveIntegrityFinding, ArchiveIntegrityScan
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.storage_operation import StorageOperation
from app.models.user import User
from app.services.timezone_contract import format_system_iso, parse_api_timestamp, timestamp_matches_filename, timezone_context
from app.services.recording_storage import archive_root_runtime_access_state, safe_resolve_relative_for_root
from app.services.storage_operation_conflicts import operation_instance_id
from app.services.storage_operations_foundation import (
    ACTIVE_OPERATION_STATUSES, TERMINAL_OPERATION_STATUSES,
    OperationHeartbeatController, StorageOperationContractError, StorageOperationLeaseLost,
    actor_identity, assert_operation_owned, claim_operation, database_now,
    finish_operation, heartbeat_operation, lock_operation_owned,
    normalize_operation_scope, reclaim_operation,
    renew_worker_lease, request_fingerprint, request_operation_cancel, safe_reason_code,
    stage_operation_terminal, ensure_operation_terminal_audit,
)

BATCH_TYPE = "integrity_review_batch"
ITEM_TYPE = "integrity_review_item"
DECISION_TYPE = "integrity_review_decision"
REVIEW_OPERATION_TYPES = frozenset({BATCH_TYPE, ITEM_TYPE, DECISION_TYPE})
BATCH_LIMIT = 2000
PAGE_LIMIT = 100
ACTION_PERMISSIONS = {
    "recheck": "run_diagnostics", "repair": "manage_settings",
    "delete": "delete_recordings", "acknowledge": "manage_settings", "reopen": "manage_settings",
}
REVIEWABLE_CATEGORIES = frozenset({
    "zero_size_file", "corrupted_file", "partial_file",
    "stale_writing_segment", "orphan_file", "pre_metadata_km_vms_file",
    "legacy_archive_file", "foreign_file", "unknown_file",
})
DELETE_CATEGORIES = frozenset({"missing_file", "zero_size_file", "corrupted_file", "partial_file", "stale_writing_segment", "orphan_file"})
_next_poll = 0.0


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _object_key(finding: ArchiveIntegrityFinding) -> str:
    return "review:" + _hash([finding.root_id, finding.physical_identity, finding.segment_id, finding.relative_ref])


def _file_fingerprint(finding: ArchiveIntegrityFinding) -> str:
    facts = dict(finding.observed_facts or {})
    return _hash([finding.category, finding.physical_identity, finding.relative_ref,
                  {key: facts.get(key) for key in ("device_id", "inode", "size_bytes", "mtime_ns", "status")}])


def _snapshot_hash(finding: ArchiveIntegrityFinding) -> str:
    return _hash([finding.id, finding.state, finding.is_active, finding.metadata_version, _file_fingerprint(finding)])


def apply_saved_review(db: Session, finding: ArchiveIntegrityFinding) -> None:
    """A decision survives scan generations, never a changed file/category."""
    finding.state = "active"
    finding.is_active = True
    decision = (db.query(StorageOperation).filter(
        StorageOperation.operation_type == DECISION_TYPE,
        StorageOperation.domain_ref == _object_key(finding),
        StorageOperation.status == "completed",
    ).order_by(StorageOperation.created_at.desc(), StorageOperation.id.desc()).first())
    if decision is not None and (decision.result or {}).get("file_fingerprint") == _file_fingerprint(finding):
        if (decision.result or {}).get("acknowledged"):
            finding.state = "acknowledged"
            finding.is_active = False
            finding.resolved_at = decision.finished_at
    elif decision is not None:
        # An obsolete decision is not useful retained history; the finding and
        # audit preserve history, while this row is only current review state.
        db.delete(decision)


def review_counts(db: Session, scan_id: str) -> dict[str, int]:
    return {str(state): int(count) for state, count in db.query(
        ArchiveIntegrityFinding.state, func.count(ArchiveIntegrityFinding.id)
    ).filter(ArchiveIntegrityFinding.scan_id == scan_id).group_by(ArchiveIntegrityFinding.state).all()}


def finding_actions(finding: ArchiveIntegrityFinding, *, role: str | None) -> list[str]:
    state = str(finding.state or "active")
    if state == "resolved" or state in {"stale", "superseded"}:
        return []
    candidates = ["recheck"]
    if state == "acknowledged":
        candidates.append("reopen")
    else:
        if finding.category == "stale_writing_segment":
            candidates.append("repair")
        if finding.category in DELETE_CATEGORIES and (finding.category != "orphan_file" or finding.action_key == "delete_proven_orphan"):
            candidates.append("delete")
        if finding.category in REVIEWABLE_CATEGORIES:
            candidates.append("acknowledge")
    return [action for action in candidates if user_has_permission(str(role or ""), ACTION_PERMISSIONS[action])]


def _filters(value: dict | None) -> dict:
    if value is not None and not isinstance(value, dict):
        raise StorageOperationContractError("integrity_review_filter_invalid")
    value = dict(value or {})
    allowed = {"state", "category", "camera_id", "root_id", "search", "date_from", "date_to", "impact"}
    if set(value) - allowed:
        raise StorageOperationContractError("integrity_review_filter_invalid")
    result = {}
    for key, item in value.items():
        if item in (None, ""):
            continue
        if key == "camera_id":
            try:
                item = int(item)
            except (ValueError, TypeError) as exc:
                raise StorageOperationContractError("integrity_review_filter_invalid") from exc
            if item < 1:
                raise StorageOperationContractError("integrity_review_filter_invalid")
        elif key in {"date_from", "date_to"}:
            try:
                item = datetime.fromisoformat(str(item)).isoformat()
            except (ValueError, TypeError) as exc:
                raise StorageOperationContractError("integrity_review_filter_invalid") from exc
        else:
            item = str(item).strip()[:160]
        result[key] = item
    result.setdefault("state", "active")
    if result["state"] not in {"active", "acknowledged", "resolved", "all"}:
        raise StorageOperationContractError("integrity_review_filter_invalid")
    if result.get("date_from") and result.get("date_to") and result["date_from"] > result["date_to"]:
        raise StorageOperationContractError("integrity_review_filter_invalid")
    return result


def _journal_timezone(db: Session):
    # The existing ledger stores database_now() with its session offset removed.
    # Interpret those fields in the actual DB session zone, without rewriting
    # journals or changing lease/update authority timestamps in this UI task.
    if db.get_bind().dialect.name == "postgresql":
        name = db.execute(select(func.current_setting("TimeZone"))).scalar_one()
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    value = db.execute(select(func.current_timestamp())).scalar_one()
    return value.tzinfo if isinstance(value, datetime) and value.tzinfo else timezone.utc


def _query(db: Session, scan_id: str, filters: dict):
    query = db.query(ArchiveIntegrityFinding).outerjoin(
        RecordingSegment, RecordingSegment.id == ArchiveIntegrityFinding.segment_id
    ).filter(ArchiveIntegrityFinding.scan_id == scan_id)
    if filters.get("state", "active") != "all":
        query = query.filter(ArchiveIntegrityFinding.state == filters.get("state", "active"))
    for key, column in (("category", ArchiveIntegrityFinding.category), ("camera_id", ArchiveIntegrityFinding.camera_id),
                        ("root_id", ArchiveIntegrityFinding.root_id), ("impact", ArchiveIntegrityFinding.impact_key)):
        if filters.get(key):
            query = query.filter(column == filters[key])
    if filters.get("search"):
        query = query.filter(or_(ArchiveIntegrityFinding.display_name.contains(filters["search"], autoescape=True),
                                 ArchiveIntegrityFinding.camera_name_snapshot.contains(filters["search"], autoescape=True)))
    if filters.get("date_from") or filters.get("date_to"):
        # Match the archive's existing local-naive compatibility rule: recorder
        # timestamps matching their filename are product-local; other rows and
        # filesystem observations are UTC. Do not use the browser's timezone.
        ctx = timezone_context(db)
        stamp = (func.strftime("%Y-%m-%d-%H-%M-%S", RecordingSegment.started_at)
                 if db.get_bind().dialect.name == "sqlite"
                 else func.to_char(RecordingSegment.started_at, "YYYY-MM-DD-HH24-MI-SS"))
        legacy = and_(RecordingSegment.source == "recorder", func.coalesce(
            RecordingSegment.relative_path, RecordingSegment.file_path, "").like(literal("%") + stamp + literal(".%")))
        legacy = func.coalesce(legacy, False)
        journal_zone = _journal_timezone(db)
        for key, inclusive in (("date_from", True), ("date_to", False)):
            if not filters.get(key):
                continue
            bound = parse_api_timestamp(filters[key], ctx)
            storage = RecordingSegment.started_at >= bound.storage_utc if inclusive else RecordingSegment.started_at < bound.storage_utc
            local_bound = bound.compatibility_local or bound.storage_utc.replace(tzinfo=timezone.utc).astimezone(ctx.zone).replace(tzinfo=None)
            local = RecordingSegment.started_at >= local_bound if inclusive else RecordingSegment.started_at < local_bound
            journal_bound = bound.storage_utc.replace(tzinfo=timezone.utc).astimezone(journal_zone).replace(tzinfo=None)
            observation = ArchiveIntegrityFinding.first_observed_at >= journal_bound if inclusive else ArchiveIntegrityFinding.first_observed_at < journal_bound
            query = query.filter(or_(and_(RecordingSegment.id.isnot(None), or_(and_(legacy, local), and_(~legacy, storage))),
                                    and_(RecordingSegment.id.is_(None), observation)))
    return query


def list_review_findings(db: Session, scan_id: str, *, role: str | None, cursor: str | None = None,
                         limit: int = 50, filters: dict | None = None) -> dict:
    from app.services.archive_integrity import _public_finding
    scan = db.get(ArchiveIntegrityScan, scan_id)
    if scan is None:
        raise StorageOperationContractError("archive_integrity_scan_not_found")
    filters = _filters(filters)
    bounded = max(1, min(int(limit or 50), PAGE_LIMIT))
    query = _query(db, scan_id, filters)
    count = int(query.count())
    if cursor:
        query = query.filter(ArchiveIntegrityFinding.id > str(cursor))
    rows = query.order_by(ArchiveIntegrityFinding.id).limit(bounded + 1).all() if not scan.is_stale else []
    page = rows[:bounded]
    ids = [row.segment_id for row in page if row.segment_id]
    dates = {row.id: row for row in db.query(RecordingSegment.id, RecordingSegment.started_at, RecordingSegment.source).filter(RecordingSegment.id.in_(ids)).all()} if ids else {}
    ctx = timezone_context(db)
    journal_zone = _journal_timezone(db)
    journal_iso = lambda value: format_system_iso(value.replace(tzinfo=journal_zone), ctx) if value else None
    items = []
    for finding in page:
        item = _public_finding(finding, role=role)
        item.update({"state": finding.state, "stale": bool(scan.is_stale or finding.state in {"stale", "superseded"}),
                     "actions": [] if scan.is_stale else finding_actions(finding, role=role),
                     "camera_id": finding.camera_id, "root_id": finding.root_id,
                     "recording_started_at": format_system_iso(dates[finding.segment_id].started_at, ctx,
                         local_naive=dates[finding.segment_id].source == "recorder" and timestamp_matches_filename(
                             dates[finding.segment_id].started_at, finding.display_name)) if finding.segment_id in dates else None,
                     "observed_at": journal_iso(finding.last_observed_at),
                     "reviewed_at": journal_iso(finding.resolved_at),
                     "size_bytes": int((finding.observed_facts or {}).get("size_bytes") or 0),
                     "reason_code": safe_reason_code((finding.observed_facts or {}).get("probe_status"))})
        items.append(item)
    options = db.query(ArchiveIntegrityFinding.camera_id, ArchiveIntegrityFinding.camera_name_snapshot,
                       ArchiveIntegrityFinding.root_id, ArchiveIntegrityFinding.root_label_snapshot,
                       ArchiveIntegrityFinding.category).filter(ArchiveIntegrityFinding.scan_id == scan_id).distinct().all()
    cameras = {row[0]: row[1] for row in sorted(options, key=lambda row: str(row[1] or "")) if row[0]}
    roots = {row[2]: row[3] for row in sorted(options, key=lambda row: str(row[3] or "")) if row[2]}
    return {"scan_id": scan_id, "status": scan.status, "items": items, "total_count": 0 if scan.is_stale else count,
            "display_timezone": ctx.name, "last_checked_at": journal_iso(scan.finished_at or scan.started_at),
            "review_counts": review_counts(db, scan_id), "filters": filters,
            "cameras": [{"id": key, "name": name} for key, name in sorted(cameras.items())],
            "roots": [{"id": key, "name": name} for key, name in sorted(roots.items())],
            "categories": sorted({r[4] for r in options}),
            "next_cursor": page[-1].id if len(rows) > bounded and page else None, "has_more": len(rows) > bounded, "limit": bounded}


def _selected(db: Session, scan_id: str, selection: dict) -> list[ArchiveIntegrityFinding]:
    if not isinstance(selection, dict) or set(selection) - {"filters", "ids", "exclude_ids"} or not ({"filters", "ids"} & set(selection)):
        raise StorageOperationContractError("integrity_review_selection_invalid")
    filters = _filters(selection.get("filters"))
    query = _query(db, scan_id, filters)
    ids = selection.get("ids")
    excluded = selection.get("exclude_ids") or []
    if not isinstance(excluded, list) or len(excluded) > BATCH_LIMIT or any(not isinstance(i, str) or len(i) > 36 for i in excluded):
        raise StorageOperationContractError("integrity_review_selection_invalid")
    if excluded:
        query = query.filter(~ArchiveIntegrityFinding.id.in_(set(excluded)))
    if ids is not None:
        if not isinstance(ids, list) or not ids or len(ids) > BATCH_LIMIT or any(not isinstance(i, str) or len(i) > 36 for i in ids):
            raise StorageOperationContractError("integrity_review_selection_invalid")
        query = query.filter(ArchiveIntegrityFinding.id.in_(set(ids)))
    rows = query.order_by(ArchiveIntegrityFinding.id).limit(BATCH_LIMIT + 1).all()
    if len(rows) > BATCH_LIMIT:
        raise StorageOperationContractError("integrity_review_batch_too_large")
    if not rows or (ids is not None and len(rows) != len(set(ids))):
        raise StorageOperationContractError("integrity_review_selection_changed")
    return rows


def preview_review_action(db: Session, *, scan_id: str, action: str, selection: dict, actor: Any) -> dict:
    _require_action(actor, action)
    scan = db.get(ArchiveIntegrityScan, scan_id)
    if scan is None or scan.is_stale or scan.status not in {"completed", "partial"}:
        raise StorageOperationContractError("archive_integrity_scan_stale")
    rows = _selected(db, scan_id, selection)
    eligible = [row for row in rows if action in finding_actions(row, role=actor.role)]
    return {"scan_id": scan_id, "action": action, "selected_count": len(rows), "eligible_count": len(eligible),
            "unavailable_count": len(rows) - len(eligible),
            "total_bytes": sum(int((row.observed_facts or {}).get("size_bytes") or 0) for row in eligible) if action == "delete" else 0,
            "selection_hash": _hash([_snapshot_hash(row) for row in rows]), "batch_limit": BATCH_LIMIT,
            "destructive": action == "delete"}


def _require_action(actor: Any, action: str) -> None:
    permission = ACTION_PERMISSIONS.get(action)
    if not permission or not getattr(actor, "is_active", True) or not user_has_permission(str(getattr(actor, "role", "")), permission):
        raise StorageOperationContractError("integrity_review_permission_denied")


def _identity(progress: dict) -> dict:
    return {key: progress.get(key) for key in ("scan_id", "action", "selection_hash", "planned_count")}


def enqueue_review_action(db: Session, *, scan_id: str, action: str, selection: dict, actor: Any,
                          selection_hash: str, idempotency_key: str, confirm: bool) -> dict:
    global _next_poll
    _require_action(actor, action)
    if not confirm:
        raise StorageOperationContractError("integrity_review_confirmation_required")
    kind, actor_key, user_id, owner = actor_identity(actor)
    existing = db.query(StorageOperation).filter(StorageOperation.operation_type == BATCH_TYPE,
        StorageOperation.actor_key == actor_key, StorageOperation.idempotency_key == idempotency_key).first()
    if existing is not None:
        if (existing.progress or {}).get("selection_hash") != selection_hash or (existing.progress or {}).get("action") != action or (existing.progress or {}).get("scan_id") != scan_id:
            raise StorageOperationContractError("operation_identity_mismatch")
        return get_review_batch(db, existing.id, actor=actor)
    preview = preview_review_action(db, scan_id=scan_id, action=action, selection=selection, actor=actor)
    if preview["selection_hash"] != selection_hash:
        raise StorageOperationContractError("integrity_review_selection_changed")
    if not preview["eligible_count"]:
        raise StorageOperationContractError("integrity_review_action_unavailable")
    rows = _selected(db, scan_id, selection)
    if _hash([_snapshot_hash(row) for row in rows]) != selection_hash:
        raise StorageOperationContractError("integrity_review_selection_changed")
    now = database_now(db)
    batch_id = f"integrity-review-{uuid.uuid4().hex}"
    progress = {"scan_id": scan_id, "action": action, "selection_hash": selection_hash,
                "planned_count": preview["selected_count"], "phase": "queued"}
    scope = normalize_operation_scope({"global": False})
    batch = StorageOperation(id=batch_id, operation_type=BATCH_TYPE, actor_kind=kind, actor_key=actor_key,
        actor_user_id=user_id, system_owner=owner, idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint(_identity(progress)), status="queued", scope=scope,
        progress=progress, cancel_allowed=True, created_at=now, queued_at=now, updated_at=now)
    db.add(batch)
    # Persist the exact selection, not a live filter that could gain new objects.
    for index, finding in enumerate(rows):
        item_id = f"review-item-{uuid.uuid4().hex}"
        item_progress = {"batch_id": batch_id, "scan_id": scan_id, "finding_id": finding.id,
                         "action": action, "snapshot_hash": _snapshot_hash(finding), "index": index}
        db.add(StorageOperation(id=item_id, operation_type=ITEM_TYPE, actor_kind=kind, actor_key=actor_key,
            actor_user_id=user_id, system_owner=owner, idempotency_key=item_id,
            request_fingerprint=request_fingerprint(item_progress), status="prepared", scope=scope,
            domain_ref=_object_key(finding), progress=item_progress, created_at=now, queued_at=now, updated_at=now))
    db.commit()
    _next_poll = 0.0
    return get_review_batch(db, batch.id, actor=actor)


def _items(db: Session, batch_id: str):
    return db.query(StorageOperation).filter(StorageOperation.operation_type == ITEM_TYPE,
        StorageOperation.progress["batch_id"].as_string() == batch_id)


def _public_batch(db: Session, batch: StorageOperation) -> dict:
    counts = dict(_items(db, batch.id).with_entities(StorageOperation.status, func.count()).group_by(StorageOperation.status).all())
    finished = sum(int(counts.get(key, 0)) for key in TERMINAL_OPERATION_STATUSES)
    return {"batch_id": batch.id, "scan_id": (batch.progress or {}).get("scan_id"),
            "action": (batch.progress or {}).get("action"), "state": batch.status,
            "planned_count": int((batch.progress or {}).get("planned_count") or 0), "processed_count": finished,
            "completed_count": int(counts.get("completed", 0)),
            "failed_count": sum(int(counts.get(key, 0)) for key in ("failed", "blocked", "partial")),
            "cancelled_count": int(counts.get("cancelled", 0)),
            "reason_code": batch.reason_code, "result": dict(batch.result or {}),
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
            "finished_at": batch.finished_at.isoformat() if batch.finished_at else None}


def get_review_batch(db: Session, batch_id: str | None, *, actor: Any, cursor: str | None = None) -> dict | None:
    _kind, actor_key, _uid, _owner = actor_identity(actor)
    query = db.query(StorageOperation).filter(StorageOperation.operation_type == BATCH_TYPE, StorageOperation.actor_key == actor_key)
    if batch_id:
        query = query.filter(StorageOperation.id == batch_id)
    batch = query.order_by(StorageOperation.created_at.desc(), StorageOperation.id.desc()).first()
    if batch is None:
        return None
    result = _public_batch(db, batch)
    rows_query = _items(db, batch.id)
    if cursor:
        rows_query = rows_query.filter(StorageOperation.id > cursor)
    rows = rows_query.order_by(StorageOperation.id).limit(51).all()
    findings = {row.id: row for row in db.query(ArchiveIntegrityFinding).filter(
        ArchiveIntegrityFinding.id.in_([(r.progress or {}).get("finding_id") for r in rows[:50]])).all()} if rows else {}
    result["items"] = [{"id": row.id, "finding_id": (row.progress or {}).get("finding_id"),
                        "state": "queued" if row.status == "prepared" else row.status, "reason_code": row.reason_code,
                        "camera_name": str(findings[(row.progress or {}).get("finding_id")].camera_name_snapshot or "")[:120] if (row.progress or {}).get("finding_id") in findings else "",
                        "display_name": str(findings[(row.progress or {}).get("finding_id")].display_name or "")[:160] if (row.progress or {}).get("finding_id") in findings else "",
                        "result": dict(row.result or {})} for row in rows[:50]]
    result["next_cursor"] = rows[49].id if len(rows) > 50 else None
    return result


def cancel_review_batch(db: Session, batch_id: str, *, actor: Any) -> dict:
    current = get_review_batch(db, batch_id, actor=actor)
    if current is None:
        raise StorageOperationContractError("integrity_review_batch_not_found")
    if current["state"] not in TERMINAL_OPERATION_STATUSES:
        request_operation_cancel(db, batch_id, actor=actor)
        batch = db.get(StorageOperation, batch_id)
        if batch.status == "cancelled":
            for item in _items(db, batch_id).filter(StorageOperation.status == "prepared").all():
                item.status = "cancelled"
                item.finished_at = database_now(db)
                db.add(item)
            db.commit()
    return get_review_batch(db, batch_id, actor=actor)


def resolve_segment_findings(db: Session, segment: RecordingSegment) -> None:
    """Join ordinary deletion/retention to integrity lifecycle, in the same TX."""
    from app.services.archive_integrity import _refresh_scan_summary
    rows = db.query(ArchiveIntegrityFinding).filter(ArchiveIntegrityFinding.segment_id == int(segment.id),
        ArchiveIntegrityFinding.state.in_(("active", "acknowledged"))).all()
    scan_ids = set()
    for finding in rows:
        finding.state = "resolved"
        finding.is_active = False
        finding.resolved_at = database_now(db)
        db.add(finding)
        scan_ids.add(finding.scan_id)
        db.query(StorageOperation).filter(StorageOperation.operation_type == DECISION_TYPE,
            StorageOperation.domain_ref == _object_key(finding)).delete(synchronize_session=False)
    if rows:
        db.flush()
        for scan_id in scan_ids:
            scan = db.get(ArchiveIntegrityScan, scan_id)
            if scan:
                _refresh_scan_summary(db, scan)


def _fresh_file_facts(db: Session, finding: ArchiveIntegrityFinding) -> None:
    from app.services.archive_integrity import _file_within_recent_write_window, _metadata_version
    from app.services.archive_integrity_remediation import _active_write_exists
    root = db.get(ArchiveRoot, finding.root_id) if finding.root_id else None
    if root is None or str(root.physical_identity or "") != str(finding.physical_identity or ""):
        raise StorageOperationContractError("archive_integrity_root_changed")
    if archive_root_runtime_access_state(root).get("read_access_state") != "available":
        raise StorageOperationContractError("archive_integrity_root_unavailable")
    if finding.segment_id:
        segment = db.query(RecordingSegment).filter(RecordingSegment.id == finding.segment_id).with_for_update().first()
        if not segment or segment.deleted_at or _metadata_version(segment) != finding.metadata_version:
            raise StorageOperationContractError("archive_integrity_segment_changed")
        if _active_write_exists(db, segment):
            raise StorageOperationContractError("archive_integrity_active_write")
    try:
        current = safe_resolve_relative_for_root(str(finding.relative_ref or ""), root).lstat()
    except FileNotFoundError:
        if finding.category == "missing_file":
            return
        raise StorageOperationContractError("archive_integrity_file_changed")
    if finding.category == "missing_file" or not stat.S_ISREG(current.st_mode) or _file_within_recent_write_window(current):
        raise StorageOperationContractError("archive_integrity_file_changed")
    observed = dict(finding.observed_facts or {})
    if any(str(observed.get(key)) != str(value) for key, value in (
        ("device_id", int(current.st_dev)), ("inode", int(current.st_ino)),
        ("size_bytes", current.st_size), ("mtime_ns", current.st_mtime_ns))):
        raise StorageOperationContractError("archive_integrity_file_changed")


def _review_decision(db: Session, finding: ArchiveIntegrityFinding, *, actor: Any, acknowledge: bool, handle, item: StorageOperation) -> dict:
    from app.services.archive_integrity import _refresh_scan_summary
    _fresh_file_facts(db, finding)
    lock_operation_owned(db, handle)
    now = database_now(db)
    kind, actor_key, user_id, owner = actor_identity(actor)
    key = _object_key(finding)
    # One current decision per object; history remains in the original findings
    # and audit, not an ever-growing series of pinned journal records.
    db.query(StorageOperation).filter(StorageOperation.operation_type == DECISION_TYPE,
        StorageOperation.domain_ref == key).delete(synchronize_session=False)
    decision_id = f"review-decision-{uuid.uuid4().hex}"
    db.add(StorageOperation(id=decision_id, operation_type=DECISION_TYPE, actor_kind=kind, actor_key=actor_key,
        actor_user_id=user_id, system_owner=owner, idempotency_key=decision_id,
        request_fingerprint=_hash([key, acknowledge, _file_fingerprint(finding)]), domain_ref=key,
        status="completed", scope=normalize_operation_scope({"global": False}),
        progress={}, result={"acknowledged": acknowledge, "file_fingerprint": _file_fingerprint(finding),
                             "segment_id": finding.segment_id, "root_id": finding.root_id},
        created_at=now, queued_at=now, updated_at=now, finished_at=now))
    finding.state = "acknowledged" if acknowledge else "active"
    finding.is_active = not acknowledge
    finding.resolved_at = now if acknowledge else None
    db.add(finding)
    db.flush()
    scan = db.get(ArchiveIntegrityScan, finding.scan_id)
    _refresh_scan_summary(db, scan)
    outcome = {"acknowledged": acknowledge, "file_preserved": True}
    # The decision and its receipt commit atomically, so a restart between this
    # mutation and aggregate progress cannot leave a successfully reviewed item
    # looking failed or repeat a previous review decision.
    item.status = "completed"
    item.result = outcome
    item.finished_at = now
    item.updated_at = now
    db.add(item)
    db.commit()
    return outcome


def _recheck(db: Session, finding: ArchiveIntegrityFinding, *, actor: Any, item_id: str) -> dict:
    from app.services import archive_integrity as integrity
    from app.services.storage_operation_conflicts import claim_operation_with_conflicts
    segment = db.get(RecordingSegment, finding.segment_id) if finding.segment_id else None
    if segment is None and finding.segment_id:
        raise StorageOperationContractError("archive_integrity_segment_changed")
    if segment is not None and segment.deleted_at:
        resolve_segment_findings(db, segment)
        db.commit()
        return {"resolved": True}
    root = db.get(ArchiveRoot, finding.root_id) if finding.root_id else None
    if not root or archive_root_runtime_access_state(root).get("read_access_state") != "available":
        raise StorageOperationContractError("archive_integrity_root_unavailable")
    if str(root.physical_identity or "") != str(finding.physical_identity or ""):
        raise StorageOperationContractError("archive_integrity_root_changed")
    if segment is None:
        target = safe_resolve_relative_for_root(str(finding.relative_ref or ""), root)
        try:
            current_stat = target.lstat()
        except FileNotFoundError:
            if archive_root_runtime_access_state(root).get("read_access_state") != "available":
                raise StorageOperationContractError("archive_integrity_root_unavailable")
            finding.state = "resolved"
            finding.is_active = False
            finding.resolved_at = database_now(db)
            db.add(finding)
            db.flush()
            integrity._refresh_scan_summary(db, db.get(ArchiveIntegrityScan, finding.scan_id))
            db.commit()
            return {"resolved": True, "already_absent": True}
        if integrity._file_within_recent_write_window(current_stat):
            raise StorageOperationContractError("archive_integrity_recorder_window_active")
        if not stat.S_ISREG(current_stat.st_mode):
            raise StorageOperationContractError("archive_integrity_file_changed")
        snapshot = integrity._root_public_snapshot(root, archive_root_runtime_access_state(root))
        replaced = integrity._stable_object_key(str(root.id), str(finding.relative_ref), current_stat) != finding.stable_object_key
        catalog_owner = db.query(RecordingSegment.id).filter(
            RecordingSegment.archive_root_id == root.id, RecordingSegment.relative_path == finding.relative_ref,
            RecordingSegment.deleted_at.is_(None), RecordingSegment.status != "deleted").first()
        if replaced or catalog_owner is not None:
            finding.state = "resolved"
            finding.is_active = False
            finding.resolved_at = database_now(db)
        else:
            finding.state = "active"
            finding.is_active = True
        db.add(finding)
        db.flush()
        view = SimpleNamespace(id=finding.scan_id, scan_cutoff_at=integrity._utcnow(),
            segment_high_watermark=int(db.query(func.max(RecordingSegment.id)).scalar() or 0))
        integrity._classify_filesystem_entry(db, scan=view, root=root, snapshot=snapshot,
            path=target, relative_ref=str(finding.relative_ref), entry_stat=current_stat)
        db.flush()
        integrity._refresh_scan_summary(db, db.get(ArchiveIntegrityScan, finding.scan_id))
        db.commit()
        return {"unchanged": not replaced and catalog_owner is None, "replacement_requires_review": replaced,
                "resolved": finding.state == "resolved"}
    identity = {"review_item": item_id, "finding_id": finding.id}
    claim = claim_operation_with_conflicts(db, operation_type="integrity_metadata_repair",
        scope={"global": False, "root_ids": [root.id], "segment_ids": [segment.id]},
        request_identity=identity, actor=actor,
        operation_id=f"recheck-{item_id}", idempotency_key=item_id,
        owner_instance_id=operation_instance_id("integrity-review"))
    if claim.get("state") == "interrupted":
        claim = reclaim_operation(db, operation_type="integrity_metadata_repair", operation_id=f"recheck-{item_id}",
            request_identity=identity, idempotency_key=item_id,
            owner_instance_id=operation_instance_id("integrity-review-recovery"))
    if claim.get("state") in {"terminal", "terminal_replay"}:
        return dict(claim.get("terminal_result") or {})
    if claim.get("state") != "claimed":
        raise StorageOperationContractError("integrity_review_item_busy")
    recheck_handle = claim["handle"]
    try:
        access = archive_root_runtime_access_state(root)
        view = SimpleNamespace(id=finding.scan_id, root_snapshot=[integrity._root_public_snapshot(root, access)],
            scan_cutoff_at=integrity._utcnow(), checked_count=0, skipped_count=0, checked_bytes=0,
            failed_count=0, is_stale=False)
        # Keep a reviewed row's decision; a changed result becomes active again.
        original_state = finding.state
        finding.is_active = True
        finding.state = "active"
        db.flush()
        healthy = integrity._classify_metadata_page(db, view, [segment], OperationHeartbeatController(db.get_bind(), recheck_handle))
        if view.skipped_count or view.is_stale:
            finding.state = original_state
            finding.is_active = original_state == "active"
            db.flush()
            raise StorageOperationContractError("archive_integrity_recorder_window_active")
        # A successful convergence/healthy finalized file produces no finding.
        if int(segment.id) in healthy:
            finding.state = "resolved"
            finding.is_active = False
            finding.resolved_at = database_now(db)
            db.add(finding)
        db.flush()
        scan = db.get(ArchiveIntegrityScan, finding.scan_id)
        integrity._refresh_scan_summary(db, scan)
        outcome = {"resolved": not finding.is_active and finding.state == "resolved", "category": finding.category,
                   "reviewed": finding.state == "acknowledged"}
        # Catalog changes and the exact recheck receipt share one commit.
        stage_operation_terminal(db, recheck_handle, status="completed", result=outcome)
        db.commit()
        ensure_operation_terminal_audit(db, db.get(StorageOperation, recheck_handle.operation_id))
        return outcome
    except Exception as exc:
        db.rollback()
        finish_operation(db, recheck_handle, status="blocked", result={"status": "blocked"},
                         reason_code=safe_reason_code(str(exc), fallback="integrity_review_recheck_failed"))
        raise


def _execute_item(db: Session, item: StorageOperation, *, actor: Any, handle) -> dict:
    from app.services.archive_integrity_remediation import create_remediation_plan, apply_remediation_plan
    from app.services.archive_integrity import _apply_contract
    progress = dict(item.progress or {})
    finding = db.get(ArchiveIntegrityFinding, progress.get("finding_id"))
    scan = db.get(ArchiveIntegrityScan, progress.get("scan_id"))
    if not finding or not scan or scan.is_stale or scan.status not in {"completed", "partial"}:
        raise StorageOperationContractError("archive_integrity_scan_stale")
    action = str(progress.get("action"))
    _require_action(actor, action)
    # A durable exact child plan takes precedence during crash recovery.
    saved_plan = (item.result or {}).get("plan_id")
    if not saved_plan:
        if finding.state == "resolved":
            return {"already_resolved": True}
        if action not in {"recheck", "repair"} and _snapshot_hash(finding) != progress.get("snapshot_hash"):
            raise StorageOperationContractError("integrity_review_selection_changed")
        if action not in finding_actions(finding, role=actor.role):
            raise StorageOperationContractError("integrity_review_action_unavailable")
    assert_operation_owned(db, handle)
    if action in {"recheck", "repair"}:
        outcome = _recheck(db, finding, actor=actor, item_id=item.id)
        if action == "repair" and not outcome.get("resolved"):
            raise StorageOperationContractError((finding.observed_facts or {}).get("probe_status") or "automatic_finalization_failed")
        return outcome
    if action in {"acknowledge", "reopen"}:
        return _review_decision(db, finding, actor=actor, acknowledge=action == "acknowledge", handle=handle, item=item)
    if action == "delete":
        if not saved_plan:
            _apply_contract(finding, finding.category, orphan_actionable=bool(finding.action_key))
            db.add(finding)
            db.commit()
            plan = create_remediation_plan(db, finding_id=finding.id, action_key=str(finding.action_key or ""),
                actor=actor, idempotency_key=item.id)
            item.result = {"plan_id": plan["plan_id"]}
            db.add(item)
            db.commit()
            saved_plan = plan["plan_id"]
        result = apply_remediation_plan(db, plan_id=saved_plan, actor=actor, confirm=True,
                                        operation_id=f"review-delete-{item.id}")
        if result.get("state") not in {"completed", "partial"}:
            raise StorageOperationContractError(result.get("reason_code") or "integrity_review_delete_failed")
        return {"plan_id": saved_plan, **dict(result.get("result") or {}), "plan_state": result.get("state")}
    raise StorageOperationContractError("integrity_review_action_unavailable")


def run_review_batch_once(db: Session, worker_lease, *, force: bool = False) -> bool:
    """Called by the existing worker; idle DB lookup at most once per 30s."""
    global _next_poll
    if not force and time.monotonic() < _next_poll:
        return False
    _next_poll = time.monotonic() + 30
    batch = db.query(StorageOperation).filter(StorageOperation.operation_type == BATCH_TYPE,
        StorageOperation.status.in_(tuple(ACTIVE_OPERATION_STATUSES))).order_by(StorageOperation.created_at).first()
    if batch is None:
        return False
    actor = db.get(User, batch.actor_user_id) if batch.actor_user_id else None
    if actor is None or not getattr(actor, "is_active", True):
        _fail_unstarted_batch(db, batch, "integrity_review_permission_denied")
        return True
    claim = claim_operation(db, operation_type=BATCH_TYPE, scope=batch.scope,
        request_identity=_identity(batch.progress or {}), actor=actor, operation_id=batch.id,
        idempotency_key=batch.idempotency_key, owner_instance_id=operation_instance_id("integrity-review"), cancel_allowed=True)
    if claim.get("state") == "interrupted":
        claim = reclaim_operation(db, operation_type=BATCH_TYPE, operation_id=batch.id,
            request_identity=_identity(batch.progress or {}), idempotency_key=batch.idempotency_key,
            owner_instance_id=operation_instance_id("integrity-review-recovery"))
    if claim.get("state") != "claimed":
        return False
    handle = claim["handle"]
    heartbeat = OperationHeartbeatController(db.get_bind(), handle, interval_seconds=20)
    try:
        while True:
            renew_worker_lease(db, worker_lease)
            heartbeat.touch(force=True)
            owned = assert_operation_owned(db, handle)
            if owned.status == "cancel_requested":
                for pending in _items(db, batch.id).filter(StorageOperation.status == "prepared").all():
                    pending.status = "cancelled"
                    pending.finished_at = database_now(db)
                    db.add(pending)
                db.commit()
                break
            item = _items(db, batch.id).filter(StorageOperation.status.in_(("prepared", "running"))).order_by(StorageOperation.id).first()
            if item is None:
                break
            item.status = "running"
            item.started_at = item.started_at or database_now(db)
            db.add(item)
            db.commit()
            try:
                outcome = _execute_item(db, item, actor=actor, handle=handle)
                status = "partial" if outcome.get("plan_state") == "partial" else "completed"
                code = None
            except StorageOperationLeaseLost:
                raise
            except Exception as exc:
                db.rollback()
                outcome = dict(item.result or {})
                status = "blocked"
                code = safe_reason_code(getattr(exc, "reason_code", None) or str(exc), fallback="integrity_review_item_failed")
            lock_operation_owned(db, handle)
            item.status = status
            item.reason_code = code
            item.result = outcome
            item.finished_at = database_now(db)
            item.updated_at = item.finished_at
            db.add(item)
            db.commit()
        summary = _public_batch(db, batch)
        terminal = "cancelled" if summary["cancelled_count"] else "partial" if summary["failed_count"] else "completed"
        finish_operation(db, handle, status=terminal,
            result={key: summary[key] for key in ("planned_count", "processed_count", "completed_count", "failed_count", "cancelled_count")})
        from app.services.audit_log import create_event
        create_event(db=db, actor=actor, category="storage", event_type="archive_integrity.review_completed",
            severity="info" if terminal == "completed" else "warning", message_ru="Archive review completed",
            message_en="Archive review completed", target_type="archive_integrity_review", target_id=batch.id,
            metadata={"action": (batch.progress or {}).get("action"), "status": terminal, "count": summary["planned_count"]})
        _next_poll = 0.0
        return True
    except StorageOperationLeaseLost:
        db.rollback()
        _next_poll = 0.0
        return False


def _fail_unstarted_batch(db: Session, batch: StorageOperation, code: str) -> None:
    now = database_now(db)
    for item in _items(db, batch.id).filter(StorageOperation.status.in_(("prepared", "running"))).all():
        item.status = "blocked"
        item.reason_code = code
        item.finished_at = now
        db.add(item)
    batch.status = "blocked"
    batch.reason_code = code
    batch.finished_at = now
    db.add(batch)
    db.commit()
