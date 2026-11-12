import subprocess
from datetime import datetime, timedelta

import pytest

from test_stage13_5_4_10_3_archive_integrity import (
    stage4103, add_camera, add_segment, run_scan, active_findings,
)
from app.models.user import User
from app.models.recording import RecordingSegment
from app.models.archive_integrity import ArchiveIntegrityFinding
from app.models.storage_operation import StorageOperation
from app.services import archive_integrity as integrity
from app.services import archive_integrity_review as review
from app.services.storage_operations_foundation import (
    StorageOperationContractError, acquire_worker_lease, release_worker_lease,
    cleanup_terminal_operations,
)

REAL_PROBE = integrity._safe_probe


def actor(ctx):
    current = ctx.db.get(User, 4103)
    if current is None:
        current = User(id=4103, username="archive-review-unit", password_hash="not-used-for-authentication",
                       role="owner", is_active=True)
        ctx.db.add(current)
        ctx.db.commit()
    return current


def enqueue(ctx, scan_id, action="acknowledge", selection=None, key="review-unit-request"):
    current = actor(ctx)
    selection = selection or {"filters": {"state": "active"}}
    preview = review.preview_review_action(ctx.db, scan_id=scan_id, action=action, selection=selection, actor=current)
    result = review.enqueue_review_action(ctx.db, scan_id=scan_id, action=action, selection=selection,
        selection_hash=preview["selection_hash"], actor=current, confirm=True, idempotency_key=key)
    return result


def drive(ctx):
    lease = acquire_worker_lease(ctx.db, worker_key="review-unit", owner_instance_id="review-unit")
    assert lease is not None
    try:
        result = review.run_review_batch_once(ctx.db, lease, force=True)
    finally:
        release_worker_lease(ctx.db, lease)
    ctx.db.expire_all()
    return result


def sample(ctx, count=1, *, status="failed", content=b"partial-media"):
    camera = add_camera(ctx)
    pairs = [add_segment(ctx, camera, name=f"review-{i}.mkv", status=status, content=content) for i in range(count)]
    scan = run_scan(ctx, key=f"review-scan-{count}")
    return scan, pairs


def test_acknowledgement_preserves_video_and_is_visible_in_history(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx, 3)
    batch = enqueue(ctx, scan["scan_id"])
    assert drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "completed", result
    assert result["completed_count"] == 3
    assert all(path.exists() for _, path in pairs)
    assert not active_findings(ctx, scan["scan_id"])
    history = review.list_review_findings(ctx.db, scan["scan_id"], role="owner", filters={"state": "acknowledged"})
    assert history["total_count"] == 3
    assert all("reopen" in item["actions"] and item["state"] == "acknowledged" for item in history["items"])
    assert integrity.latest_integrity_summary_for_status(ctx.db)["problem_count"] == 0


def test_acknowledged_file_is_not_new_problem_on_next_scan_but_changed_file_is(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx)
    enqueue(ctx, scan["scan_id"])
    drive(ctx)
    second = run_scan(ctx, key="review-second-scan")
    assert second["progress"]["found_count"] == 0
    assert review.review_counts(ctx.db, second["scan_id"])["acknowledged"] == 1
    # Change metadata/content identity without marking it a recent live write.
    segment, path = pairs[0]
    path.write_bytes(b"different-media-content")
    import os
    old = (datetime.utcnow() - timedelta(hours=1)).timestamp()
    os.utime(path, (old, old))
    third = run_scan(ctx, key="review-third-scan")
    assert third["progress"]["found_count"] == 1


def test_bulk_selection_includes_unloaded_pages(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx, 63)
    page = review.list_review_findings(ctx.db, scan["scan_id"], role="owner", limit=50)
    assert len(page["items"]) == 50 and page["total_count"] == 63 and page["has_more"]
    batch = enqueue(ctx, scan["scan_id"])
    assert batch["planned_count"] == 63
    drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["completed_count"] == 63, result
    assert all(path.exists() for _, path in pairs)


def test_filters_are_server_side_and_literal_search_is_escaped(stage4103):
    ctx = stage4103
    camera = add_camera(ctx, name="Camera 100%")
    other = add_camera(ctx, name="Other camera")
    add_segment(ctx, camera, name="literal_100%.mkv", status="failed")
    add_segment(ctx, other, name="other.mkv", status="failed")
    scan = run_scan(ctx, key="review-filter-scan")
    page = review.list_review_findings(ctx.db, scan["scan_id"], role="owner", filters={"search": "%"})
    assert page["total_count"] == 1
    assert review.list_review_findings(ctx.db, scan["scan_id"], role="owner", filters={"camera_id": other.id})["total_count"] == 1
    assert review.list_review_findings(ctx.db, scan["scan_id"], role="owner", filters={"category": "zero_size_file"})["total_count"] == 0


def test_selection_change_requires_new_confirmation(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx)
    current = actor(ctx)
    selection = {"filters": {"state": "active"}}
    preview = review.preview_review_action(ctx.db, scan_id=scan["scan_id"], action="acknowledge", selection=selection, actor=current)
    finding = active_findings(ctx, scan["scan_id"])[0]
    finding.metadata_version = "changed"
    ctx.db.commit()
    with pytest.raises(StorageOperationContractError, match="selection_changed"):
        review.enqueue_review_action(ctx.db, scan_id=scan["scan_id"], action="acknowledge", selection=selection,
            selection_hash=preview["selection_hash"], actor=current, confirm=True, idempotency_key="review-selection-change")


@pytest.mark.parametrize("status", ["failed", "stale_writing", "writing", "finalized"])
def test_empty_old_file_can_be_deleted_in_all_inactive_states(stage4103, status):
    ctx = stage4103
    scan, pairs = sample(ctx, status=status, content=b"")
    batch = enqueue(ctx, scan["scan_id"], action="delete")
    drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "completed", result
    assert not pairs[0][1].exists()
    assert ctx.db.get(RecordingSegment, pairs[0][0].id).deleted_at is not None
    assert not active_findings(ctx, scan["scan_id"])


def test_cancel_before_worker_finishes_all_prepared_items(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx, 3)
    batch = enqueue(ctx, scan["scan_id"])
    result = review.cancel_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "cancelled"
    assert result["cancelled_count"] == 3
    assert review._items(ctx.db, batch["batch_id"]).filter(StorageOperation.status == "prepared").count() == 0
    assert all(path.exists() for _, path in pairs)


def test_changed_file_does_not_abort_other_items(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx, 2)
    batch = enqueue(ctx, scan["scan_id"])
    pairs[0][1].write_bytes(b"changed-during-confirmation")
    drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "partial", result
    assert result["failed_count"] == 1 and result["completed_count"] == 1
    assert all(path.exists() for _, path in pairs)


def test_batch_submission_replay_does_not_repeat_action(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx)
    current = actor(ctx)
    selection = {"filters": {"state": "active"}}
    preview = review.preview_review_action(ctx.db, scan_id=scan["scan_id"], action="acknowledge", selection=selection, actor=current)
    args = dict(scan_id=scan["scan_id"], action="acknowledge", selection=selection,
                selection_hash=preview["selection_hash"], actor=current, confirm=True, idempotency_key="review-replayed-batch")
    first = review.enqueue_review_action(ctx.db, **args)
    drive(ctx)
    replay = review.enqueue_review_action(ctx.db, **args)
    assert replay["batch_id"] == first["batch_id"] and replay["completed_count"] == 1


def test_decision_survives_history_cleanup_and_deleted_segment_closes_it(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx)
    enqueue(ctx, scan["scan_id"])
    drive(ctx)
    cleanup_terminal_operations(ctx.db, now=datetime.utcnow() + timedelta(days=60))
    assert ctx.db.query(StorageOperation).filter(StorageOperation.operation_type == review.DECISION_TYPE).count() == 1
    segment = ctx.db.get(RecordingSegment, pairs[0][0].id)
    segment.deleted_at = datetime.utcnow()
    segment.status = "deleted"
    review.resolve_segment_findings(ctx.db, segment)
    ctx.db.commit()
    assert ctx.db.query(StorageOperation).filter(StorageOperation.operation_type == review.DECISION_TYPE).count() == 0
    assert review.review_counts(ctx.db, scan["scan_id"])["resolved"] == 1


def test_owner_can_reopen_reviewed_case(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx)
    enqueue(ctx, scan["scan_id"])
    drive(ctx)
    enqueue(ctx, scan["scan_id"], action="reopen", selection={"filters": {"state": "acknowledged"}}, key="review-reopen-case")
    drive(ctx)
    assert len(active_findings(ctx, scan["scan_id"])) == 1


def test_timeout_is_not_proof_of_corruption(monkeypatch, tmp_path):
    monkeypatch.setattr(integrity.shutil, "which", lambda _name: "ffprobe")
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffprobe", 5)
    monkeypatch.setattr(integrity.subprocess, "run", timeout)
    assert REAL_PROBE(tmp_path / "video.mkv") == (None, "probe_timeout")


def test_review_does_not_allow_unprivileged_mutation(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx)
    viewer = User(id=99, username="review-viewer", role="viewer", password_hash="not-used", is_active=True)
    with pytest.raises(StorageOperationContractError, match="permission_denied"):
        review.preview_review_action(ctx.db, scan_id=scan["scan_id"], action="delete", selection={"filters": {}}, actor=viewer)


def test_unresolved_repair_is_not_reported_as_success(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx, status="stale_writing")
    batch = enqueue(ctx, scan["scan_id"], action="repair")
    drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["failed_count"] == 1 and result["state"] == "partial", result
    assert pairs[0][1].exists()
    assert len(active_findings(ctx, scan["scan_id"])) == 1


def test_archive_findings_have_explanation_without_mislabeling_healthy_disk(stage4103):
    from app.services.storage_monitoring import build_lightweight_storage_monitoring_summary
    from app.services.system_runtime_status import _build_storage_domain, _build_reconciliation_domain
    from app.services.timezone_contract import timezone_context
    ctx = stage4103
    sample(ctx)
    summary = build_lightweight_storage_monitoring_summary(ctx.db)
    zone = timezone_context(ctx.db)
    archive = _build_reconciliation_domain(summary, zone)
    assert archive["severity"] == "warning" and archive["problem_file_count"] == 1
    assert "reconciliation_problems_found" in archive["reason_codes"]
    assert archive["category_counts"] == {"partial_file": 1}
    assert _build_storage_domain(summary, zone)["severity"] == "ok"


def test_recheck_uses_current_probe_not_old_healthy_catalog_flag(stage4103):
    ctx = stage4103
    camera = add_camera(ctx)
    segment, path = add_segment(ctx, camera, content=b"bad-media", status="finalized")
    segment.integrity_status = "ok_owned_finalized"
    segment.updated_at = datetime.utcnow() - timedelta(hours=1)
    ctx.db.commit()
    ctx.monkeypatch.setattr(integrity, "_safe_probe", lambda _path: (False, "probe_failed"))
    scan = run_scan(ctx, key="review-corrupt-old-health")
    batch = enqueue(ctx, scan["scan_id"], action="recheck")
    drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "completed", result
    assert path.exists()
    findings = active_findings(ctx, scan["scan_id"])
    assert len(findings) == 1 and findings[0].category == "corrupted_file"
    assert result["items"][0]["result"]["resolved"] is False


def test_interrupted_batch_reclaims_lease_and_preserves_exact_items(stage4103):
    from app.services.storage_operations_foundation import claim_operation
    ctx = stage4103
    scan, pairs = sample(ctx, 2)
    created = enqueue(ctx, scan["scan_id"])
    batch = ctx.db.get(StorageOperation, created["batch_id"])
    claim_operation(ctx.db, operation_type=review.BATCH_TYPE, scope=batch.scope,
        request_identity=review._identity(batch.progress), actor=actor(ctx), operation_id=batch.id,
        idempotency_key=batch.idempotency_key, owner_instance_id="stopped-review-worker")
    batch.lease_expires_at = datetime.utcnow() - timedelta(seconds=10)
    ctx.db.commit()
    assert drive(ctx)
    result = review.get_review_batch(ctx.db, batch.id, actor=actor(ctx))
    assert result["state"] == "completed" and result["completed_count"] == 2, result
    assert all(path.exists() for _, path in pairs)


def test_bulk_all_matching_can_exclude_a_single_object(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx, 3)
    finding = active_findings(ctx, scan["scan_id"])[0]
    batch = enqueue(ctx, scan["scan_id"], selection={"filters": {"state": "active"}, "exclude_ids": [finding.id]})
    assert batch["planned_count"] == 2
    drive(ctx)
    assert [row.id for row in active_findings(ctx, scan["scan_id"])] == [finding.id]


def test_review_receipt_survives_worker_loss_after_decision_commit(stage4103, monkeypatch):
    from app.services.storage_operations_foundation import StorageOperationLeaseLost
    ctx = stage4103
    scan, pairs = sample(ctx)
    batch = enqueue(ctx, scan["scan_id"])
    execute = review._execute_item
    def lost_after_commit(*args, **kwargs):
        execute(*args, **kwargs)
        raise StorageOperationLeaseLost("storage_operation_lease_lost")
    monkeypatch.setattr(review, "_execute_item", lost_after_commit)
    assert drive(ctx) is False
    receipt = review._items(ctx.db, batch["batch_id"]).one()
    assert receipt.status == "completed" and receipt.result["acknowledged"]
    assert not active_findings(ctx, scan["scan_id"]) and pairs[0][1].exists()
    parent = ctx.db.get(StorageOperation, batch["batch_id"])
    parent.lease_expires_at = datetime.utcnow() - timedelta(seconds=10)
    ctx.db.commit()
    monkeypatch.setattr(review, "_execute_item", execute)
    assert drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "completed" and result["completed_count"] == 1


def test_active_batch_protects_finished_item_from_history_cleanup(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx)
    batch = enqueue(ctx, scan["scan_id"])
    item = review._items(ctx.db, batch["batch_id"]).one()
    item.status = "completed"
    item.finished_at = datetime.utcnow() - timedelta(days=60)
    ctx.db.commit()
    cleanup_terminal_operations(ctx.db)
    assert ctx.db.get(StorageOperation, item.id) is not None


def test_root_deletion_closes_integrity_observation_in_same_transaction(stage4103):
    from app.routers.storage import _mark_root_segment_deleted
    ctx = stage4103
    scan, pairs = sample(ctx)
    _mark_root_segment_deleted(ctx.db, pairs[0][0], actor(ctx), now=datetime.utcnow())
    ctx.db.commit()
    assert not active_findings(ctx, scan["scan_id"])
    assert review.review_counts(ctx.db, scan["scan_id"])["resolved"] == 1


def test_recheck_resolves_unindexed_file_that_was_removed(stage4103):
    ctx = stage4103
    path = ctx.namespace / "review-unindexed.mkv"
    path.write_bytes(b"old unindexed media")
    import os
    old = (datetime.utcnow() - timedelta(hours=1)).timestamp()
    os.utime(path, (old, old))
    scan = run_scan(ctx, key="review-unindexed-gone")
    finding = next(row for row in active_findings(ctx, scan["scan_id"]) if row.display_name == path.name)
    path.unlink()
    batch = enqueue(ctx, scan["scan_id"], action="recheck", selection={"ids": [finding.id]})
    drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "completed" and result["items"][0]["result"]["already_absent"]
    assert not active_findings(ctx, scan["scan_id"])


def test_calendar_filter_and_display_use_product_timezone_including_legacy_names(stage4103):
    from app.models.system_settings import SystemSettings
    ctx = stage4103
    ctx.db.query(SystemSettings).first().timezone = "Asia/Yekaterinburg"
    camera = add_camera(ctx)
    dates = [("utc-before.mkv", datetime(2026, 11, 11, 18, 59)),
             ("utc-inside.mkv", datetime(2026, 11, 11, 20, 0)),
             ("utc-after.mkv", datetime(2026, 11, 12, 19, 0)),
             ("legacy-2026-11-12-00-30-00.mkv", datetime(2026, 11, 12, 0, 30)),
             ("legacy-2026-11-13-00-00-00.mkv", datetime(2026, 11, 13, 0, 0))]
    for name, started in dates:
        segment, _ = add_segment(ctx, camera, name=name, status="failed")
        segment.started_at = started
        segment.updated_at = datetime.utcnow() - timedelta(hours=1)
    ctx.db.commit()
    scan = run_scan(ctx, key="review-calendar-boundaries")
    page = review.list_review_findings(ctx.db, scan["scan_id"], role="owner",
        filters={"date_from": "2026-11-12", "date_to": "2026-11-13"})
    assert {row["display_name"] for row in page["items"]} == {"utc-inside.mkv", "legacy-2026-11-12-00-30-00.mkv"}
    assert page["display_timezone"] == "Asia/Yekaterinburg"
    values = {row["display_name"]: row["recording_started_at"] for row in page["items"]}
    assert values["utc-inside.mkv"] == "2026-11-12T01:00:00+05:00"
    assert values["legacy-2026-11-12-00-30-00.mkv"] == "2026-11-12T00:30:00+05:00"
