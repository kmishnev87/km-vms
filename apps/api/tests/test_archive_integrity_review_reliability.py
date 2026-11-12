"""Regression coverage for staged actions, recorder age and old missing rows."""
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from test_archive_integrity_review import stage4103, actor, drive, enqueue, sample
from test_stage13_5_4_10_3_archive_integrity import add_camera, add_segment, run_scan, active_findings, eligible_orphan
from app.models.recording import RecordingJob, RecordingSegment
from app.models.archive_integrity import ArchiveIntegrityScan
from app.services import archive_integrity as integrity
from app.services import archive_integrity_remediation as remediation
from app.services import archive_integrity_review as review
from app.services.storage_operations_foundation import StorageOperationContractError, database_now


def test_mixed_plan_runs_each_confirmed_action_and_replays_exactly(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx, 3, content=b"")
    rows = active_findings(ctx, scan["scan_id"])
    assignments = {rows[0].id: "delete", rows[1].id: "acknowledge"}
    args = dict(scan_id=scan["scan_id"], action="recheck", selection={"ids": [row.id for row in rows]},
                action_overrides=assignments, actor=actor(ctx))
    preview = review.preview_review_action(ctx.db, **args)
    assert preview["action"] == "mixed" and preview["destructive"]
    assert {c["action"]: c["eligible_count"] for c in preview["action_counts"]} == {
        "delete": 1, "acknowledge": 1, "recheck": 1}
    assert all(path.exists() for _, path in pairs), "preview cannot modify media"
    apply_args = {**args, "selection_hash": preview["selection_hash"], "confirm": True,
                  "idempotency_key": "review-mixed-replay"}
    batch = review.enqueue_review_action(ctx.db, **apply_args)
    assert drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "completed" and result["completed_count"] == 3, result
    assert {item["finding_id"]: item["action"] for item in result["items"]} == {
        row.id: assignments.get(row.id, "recheck") for row in rows}
    deleted_id = rows[0].segment_id
    for segment, path in pairs:
        assert path.exists() == (segment.id != deleted_id)
    assert review.enqueue_review_action(ctx.db, **apply_args)["batch_id"] == batch["batch_id"]
    with pytest.raises(StorageOperationContractError, match="identity_mismatch"):
        review.enqueue_review_action(ctx.db, **{**apply_args, "action_overrides": {rows[0].id: "acknowledge"}})


def test_changed_row_action_requires_new_confirmation(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx, 2)
    rows = active_findings(ctx, scan["scan_id"])
    args = dict(scan_id=scan["scan_id"], action="recheck", selection={"ids": [r.id for r in rows]}, actor=actor(ctx))
    preview = review.preview_review_action(ctx.db, **args, action_overrides={rows[0].id: "acknowledge"})
    with pytest.raises(StorageOperationContractError, match="selection_changed"):
        review.enqueue_review_action(ctx.db, **args, action_overrides={rows[0].id: "delete"},
            selection_hash=preview["selection_hash"], idempotency_key="review-mixed-change", confirm=True)
    assert ctx.db.query(RecordingSegment).filter(RecordingSegment.deleted_at.isnot(None)).count() == 0


def test_override_must_belong_to_selection_and_be_permissioned(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx, 2)
    rows = active_findings(ctx, scan["scan_id"])
    args = dict(scan_id=scan["scan_id"], action="recheck", selection={"ids": [rows[0].id]}, actor=actor(ctx))
    with pytest.raises(StorageOperationContractError, match="selection_changed"):
        review.preview_review_action(ctx.db, **args, action_overrides={rows[1].id: "delete"})
    current = actor(ctx)
    current.role = "viewer"
    with pytest.raises(StorageOperationContractError, match="permission_denied"):
        review.preview_review_action(ctx.db, **{**args, "actor": current}, action_overrides={rows[0].id: "delete"})


@pytest.mark.parametrize("status", ["queued", "running", "cancel_requested", "failed", "cancelled"])
def test_action_cannot_apply_to_incomplete_scan(stage4103, status):
    ctx = stage4103
    scan, _ = sample(ctx)
    stored = ctx.db.get(ArchiveIntegrityScan, scan["scan_id"])
    stored.status = status
    ctx.db.commit()
    with pytest.raises(StorageOperationContractError, match="scan_stale"):
        review.preview_review_action(ctx.db, scan_id=stored.id, action="delete", selection={"filters": {}}, actor=actor(ctx))


@pytest.mark.parametrize("offset", [-7, 0, 5, 8])
@pytest.mark.parametrize("age_minutes", [5, 14, 16, 65])
@pytest.mark.parametrize("kind", ["missing", "missing_finalized", "partial", "stale"])
def test_recorder_window_uses_database_wall_clock(stage4103, offset, age_minutes, kind):
    ctx = stage4103
    camera = add_camera(ctx)
    status = "stale_writing" if kind == "stale" else "finalized" if kind == "missing_finalized" else "failed"
    segment, _ = add_segment(ctx, camera, status=status, create_file=not kind.startswith("missing"))
    scan = run_scan(ctx, key="review-wall-clock")
    finding = active_findings(ctx, scan["scan_id"])[0]
    wall_now = datetime.utcnow() + timedelta(hours=offset)
    segment.updated_at = wall_now - timedelta(minutes=age_minutes)
    segment.started_at = segment.updated_at - timedelta(minutes=5)
    if status == "finalized":
        segment.finalized_at = segment.updated_at
    ctx.db.commit()
    finding.metadata_version = integrity._metadata_version(segment)
    ctx.db.commit()
    ctx.monkeypatch.setattr(remediation, "database_now", lambda _db: wall_now)
    validate = {"missing": remediation._revalidate_missing, "missing_finalized": remediation._revalidate_missing,
                "partial": remediation._revalidate_unusable,
                "stale": remediation._revalidate_stale}[kind]
    if age_minutes < 15:
        with pytest.raises(remediation.IntegrityRemediationBlocked, match="recorder_window_active"):
            validate(ctx.db, finding)
    else:
        assert validate(ctx.db, finding)[0].id == segment.id


@pytest.mark.parametrize("job_state", ["stopped", "recording", "starting", "stopping", "restarting"])
def test_missing_stale_record_requires_inactive_writer(stage4103, job_state):
    ctx = stage4103
    camera = add_camera(ctx)
    old = datetime.utcnow() - timedelta(hours=2)
    job = RecordingJob(id="review-missing-old-job", camera_id=camera.id, state=job_state,
                       started_at=old, stopped_at=old if job_state == "stopped" else None)
    ctx.db.add(job)
    ctx.db.commit()
    segment, path = add_segment(ctx, camera, status="stale_writing", create_file=False)
    segment.job_id = job.id
    segment.updated_at = old
    ctx.db.commit()
    scan = run_scan(ctx, key="review-missing-stale")
    # Active jobs may be skipped by scanning; construct an old missing
    # observation with a stopped owner, then restore only its job state.
    if not active_findings(ctx, scan["scan_id"]):
        job.state = "stopped"
        ctx.db.commit()
        scan = run_scan(ctx, key="review-missing-before-job-change")
        job.state = job_state
        ctx.db.commit()
    finding = active_findings(ctx, scan["scan_id"])[0]
    if job_state != "stopped":
        with pytest.raises(remediation.IntegrityRemediationBlocked, match="active_write"):
            remediation._revalidate_missing(ctx.db, finding)
        assert segment.deleted_at is None
    else:
        batch = enqueue(ctx, scan["scan_id"], action="delete", selection={"ids": [finding.id]})
        assert drive(ctx)
        result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
        assert result["state"] == "completed", result
        assert ctx.db.get(RecordingSegment, segment.id).deleted_at is not None
        assert result["items"][0]["result"]["retired_count"] == 1
    assert not path.exists()


def test_missing_stale_record_reappearing_file_is_preserved(stage4103):
    ctx = stage4103
    camera = add_camera(ctx)
    _, path = add_segment(ctx, camera, status="stale_writing", create_file=False)
    scan = run_scan(ctx, key="review-missing-reappeared")
    finding = active_findings(ctx, scan["scan_id"])[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"reappeared-video")
    with pytest.raises(remediation.IntegrityRemediationBlocked, match="reappeared"):
        remediation._revalidate_missing(ctx.db, finding)
    assert path.read_bytes() == b"reappeared-video"


@pytest.mark.parametrize("zone", ["Asia/Yekaterinburg", "America/Phoenix", "UTC"])
def test_actual_postgres_session_clock_matches_recorder_now(stage4103, zone):
    url = os.getenv("KMVMS_STAGE2_POSTGRES_URL")
    if not url:
        pytest.skip("isolated PostgreSQL test service not configured")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SELECT set_config('TimeZone', :zone, true)"), {"zone": zone})
            db = Session(bind=connection)
            wall_now = database_now(db)
            recorder_stamp = connection.execute(text("SELECT CAST(NOW() - INTERVAL '20 minutes' AS timestamp)")).scalar_one()
            assert timedelta(minutes=19) < wall_now - recorder_stamp < timedelta(minutes=21)
            ctx = stage4103
            camera = add_camera(ctx)
            segment, _ = add_segment(ctx, camera, status="failed", create_file=False)
            scan = run_scan(ctx, key="review-real-pg-clock")
            finding = active_findings(ctx, scan["scan_id"])[0]
            segment.updated_at = recorder_stamp
            ctx.db.commit()
            finding.metadata_version = integrity._metadata_version(segment)
            ctx.db.commit()
            ctx.monkeypatch.setattr(remediation, "database_now", lambda _db: database_now(db))
            assert remediation._revalidate_missing(ctx.db, finding)[0].id == segment.id
            transaction.rollback()
    finally:
        engine.dispose()


def test_recheck_observation_uses_scan_ledger_clock(stage4103):
    ctx = stage4103
    scan, _ = sample(ctx)
    cutoff = datetime.utcnow() + timedelta(hours=5)
    ctx.monkeypatch.setattr(review, "database_now", lambda _db: cutoff)
    batch = enqueue(ctx, scan["scan_id"], action="recheck")
    assert drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "completed", result
    finding = active_findings(ctx, scan["scan_id"])[0]
    assert finding.last_observed_at == cutoff


def test_filesystem_recheck_observation_uses_scan_ledger_clock(stage4103):
    ctx = stage4103
    path, finding = eligible_orphan(ctx, name="review-observation.mkv", receipt_id="review-observation-receipt",
                                    key_prefix="review-observation-clock")
    cutoff = datetime.utcnow() + timedelta(hours=5)
    ctx.monkeypatch.setattr(review, "database_now", lambda _db: cutoff)
    batch = enqueue(ctx, finding.scan_id, action="recheck", selection={"ids": [finding.id]})
    assert drive(ctx)
    result = review.get_review_batch(ctx.db, batch["batch_id"], actor=actor(ctx))
    assert result["state"] == "completed", result
    ctx.db.refresh(finding)
    assert finding.last_observed_at == cutoff and path.exists()


@pytest.mark.parametrize("offset", [-7, 5])
@pytest.mark.parametrize("old_enough", [False, True])
def test_orphan_grace_uses_ledger_clock_and_file_age_uses_epoch(stage4103, offset, old_enough):
    ctx = stage4103
    path, finding = eligible_orphan(ctx, name="review-age.mkv", receipt_id="review-age-receipt",
                                    key_prefix="review-orphan-age")
    wall = datetime.utcnow() + timedelta(hours=offset)
    finding.first_observed_at = wall - (integrity.ORPHAN_OBSERVATION_GRACE + timedelta(seconds=1)
                                        if old_enough else timedelta(seconds=1))
    ctx.db.commit()
    ctx.monkeypatch.setattr(remediation, "database_now", lambda _db: wall)

    class HostLocalDateTime(datetime):
        @classmethod
        def fromtimestamp(cls, stamp, tz=None):
            return datetime.utcfromtimestamp(stamp) + timedelta(hours=5)

    ctx.monkeypatch.setattr(remediation, "datetime", HostLocalDateTime)
    if old_enough:
        assert remediation._revalidate_orphan(ctx.db, finding)[1].id == ctx.root.id
    else:
        with pytest.raises(remediation.IntegrityRemediationBlocked, match="orphan_evidence_insufficient"):
            remediation._revalidate_orphan(ctx.db, finding)
    assert path.exists()


def test_clock_fix_does_not_allow_recent_physical_file_to_be_deleted(stage4103):
    ctx = stage4103
    scan, pairs = sample(ctx, content=b"")
    finding = active_findings(ctx, scan["scan_id"])[0]
    path = pairs[0][1]
    os.utime(path, None)
    current = path.stat()
    finding.observed_facts = {**finding.observed_facts, "mtime_ns": current.st_mtime_ns}
    ctx.db.commit()
    ctx.monkeypatch.setattr(remediation, "database_now", lambda _db: datetime.utcnow() + timedelta(hours=5))
    with pytest.raises(remediation.IntegrityRemediationBlocked, match="recorder_window_active"):
        remediation._revalidate_unusable(ctx.db, finding)
    assert path.exists()


def test_http_contract_carries_exact_row_assignments(stage4103):
    from app.routers.storage import (IntegrityReviewRequest, IntegrityReviewApplyRequest,
        storage_integrity_review_preview, storage_integrity_review_apply)
    ctx = stage4103
    scan, _ = sample(ctx, 2)
    rows = active_findings(ctx, scan["scan_id"])
    payload = {"action": "recheck", "selection": {"ids": [r.id for r in rows]},
               "action_overrides": {rows[0].id: "delete", rows[1].id: "acknowledge"}}
    current = actor(ctx)
    preview = storage_integrity_review_preview(scan["scan_id"], IntegrityReviewRequest(**payload), ctx.db, current)
    assert preview["action"] == "mixed"
    applied = storage_integrity_review_apply(scan["scan_id"], IntegrityReviewApplyRequest(**payload,
        selection_hash=preview["selection_hash"], idempotency_key="review-http-mixed", confirm=True), ctx.db, current)
    assert {row["finding_id"]: row["action"] for row in applied["items"]} == payload["action_overrides"]
