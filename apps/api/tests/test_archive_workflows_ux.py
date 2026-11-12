from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from test_archive_export_stage11 import (
    api_db, client, add_user, add_camera, add_segment, add_media_segment,
    auth_headers, valid_payload, create_done_export,
)
from app.core.config import settings
from app.models.archive_integrity import ArchiveIntegrityFinding, ArchiveIntegrityScan, ArchiveIntegrityRemediationPlan
from app.models.storage_operation import StorageOperation
from app.services import archive_exports as exports
from app.services.archive_operation_history import archive_operation_history_items, _history_timestamp
from app.services.storage_monitoring import _metadata_usage_aggregates, _safe_camera_usage, useful_storage_operation_history


def operation(db, ident, kind, *, status="blocked", scope=None, progress=None, result=None, reason=None):
    now = datetime.utcnow()
    row = StorageOperation(id=ident, operation_type=kind, actor_kind="system", actor_key="test",
        idempotency_key=ident, request_fingerprint="test", status=status, scope=scope or {},
        progress=progress or {}, result=result, reason_code=reason, created_at=now,
        queued_at=now, started_at=now, finished_at=now, updated_at=now)
    db.add(row); db.commit()
    return row


def test_export_no_four_gib_limit_and_revalidation(api_db, client):
    db, _root = api_db
    user, camera = add_user(db), add_camera(db)
    segment = add_segment(db, camera)
    segment.size_bytes = 8 * 1024**3
    db.commit()
    limits = client.get("/archive/exports/limits", headers=auth_headers(user)).json()
    assert limits["max_estimated_source_bytes"] is None
    response = client.post("/archive/exports", json=valid_payload(camera, segment), headers=auth_headers(user))
    assert response.status_code == 200, response.text
    job = db.get(exports.ArchiveExportJob, response.json()["id"])
    assert job.estimated_source_bytes == 8 * 1024**3
    assert len(exports.revalidate_job_sources(db, job)) == 1


def test_three_hours_with_180_one_minute_files(api_db, client):
    db, _root = api_db
    user, camera = add_user(db), add_camera(db)
    start = datetime.utcnow().replace(microsecond=0) - timedelta(hours=4)
    first = None
    for index in range(180):
        segment = add_segment(db, camera, name=f"minute-{index}.mkv", started=start + timedelta(minutes=index))
        first = first or segment
    response = client.post("/archive/exports", json=valid_payload(camera, first, end_ts=(start + timedelta(hours=3)).isoformat()), headers=auth_headers(user))
    assert response.status_code == 200, response.text
    assert response.json()["source_segment_count"] == 180


def test_large_copy_timeout_and_space_estimate(monkeypatch, tmp_path):
    assert exports._copy_timeout(12 * 1024**3) > 120
    assert exports._copy_timeout(1) == 120
    assert exports._copy_timeout(10**15) <= exports.MAX_EXPORT_DURATION_SECONDS
    start = datetime.utcnow()
    source = exports.ResolvedExportSegment(None, tmp_path / "source", start, start + timedelta(hours=1), 8 * 1024**3, 0)
    job = SimpleNamespace(start_ts=start, end_ts=start + timedelta(minutes=2))
    monkeypatch.setattr(exports, "_export_root", lambda: tmp_path)
    monkeypatch.setattr(exports.shutil, "disk_usage", lambda _path: SimpleNamespace(free=2 * 1024**3))
    exports._check_export_space([source], job)  # Small excerpt, not the whole 8 GiB source.
    monkeypatch.setattr(exports.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1))
    with pytest.raises(RuntimeError, match="insufficient_export_space"):
        exports._check_export_space([source], job)


def test_delete_clip_confirm_idempotent_sources_preserved(api_db, client):
    db, _root = api_db
    user, camera = add_user(db), add_camera(db)
    segment = add_media_segment(db, camera, seconds=2)
    original = Path(settings.storage_root) / segment.relative_path
    before = original.read_bytes()
    job = create_done_export(client, db, user, camera, segment)
    assert client.post(f"/archive/exports/{job.id}/manifest", headers=auth_headers(user)).status_code == 200
    db.refresh(job)
    clip = Path(settings.storage_exports) / job.internal_output_path
    passport = Path(settings.storage_exports) / job.internal_manifest_path
    endpoint = f"/archive/exports/{job.id}"
    assert client.delete(endpoint, headers=auth_headers(user)).status_code == 422
    assert clip.exists() and passport.exists()
    lock = exports._generation_lock(job.id)
    lock.acquire()
    try:
        assert client.delete(endpoint + "?confirm=true", headers=auth_headers(user)).status_code == 409
    finally:
        lock.release()
    response = client.delete(endpoint + "?confirm=true", headers=auth_headers(user))
    assert response.status_code == 200, response.text
    assert not clip.exists() and not passport.exists()
    assert original.read_bytes() == before
    assert client.delete(endpoint + "?confirm=true", headers=auth_headers(user)).json()["already_deleted"]
    assert client.get("/archive/exports?saved_only=true", headers=auth_headers(user)).json()["total"] == 0
    assert not client.get(endpoint, headers=auth_headers(user)).json()["download_ready"]


def test_completed_clip_is_not_expired_by_cleanup(api_db, client):
    db, _root = api_db
    user, camera = add_user(db), add_camera(db)
    job = create_done_export(client, db, user, camera, add_media_segment(db, camera, seconds=2))
    job.expires_at = datetime.utcnow() - timedelta(days=10)
    db.commit()
    response = client.post("/archive/exports/cleanup", json={}, headers=auth_headers(user))
    assert response.status_code == 200
    db.refresh(job)
    assert job.status == "done"
    assert (Path(settings.storage_exports) / job.internal_output_path).is_file()
    metadata = client.get(f"/archive/exports/{job.id}", headers=auth_headers(user)).json()
    assert metadata["expires_at"] is None and metadata["retention"] == "until_deleted"


def test_delete_clip_rejects_another_job_path_and_viewer(api_db, client):
    db, _root = api_db
    user, camera = add_user(db), add_camera(db)
    segment = add_segment(db, camera)
    created = client.post("/archive/exports", json=valid_payload(camera, segment), headers=auth_headers(user)).json()
    job = db.get(exports.ArchiveExportJob, created["id"])
    other = Path(settings.storage_exports) / exports.EXPORT_OUTPUT_DIR / "stage11_clip_someone_else.mkv"
    other.parent.mkdir(parents=True, exist_ok=True); other.write_bytes(b"other clip")
    job.internal_output_path = f"{exports.EXPORT_OUTPUT_DIR}/{other.name}"
    db.commit()
    assert client.delete(f"/archive/exports/{job.id}?confirm=true", headers=auth_headers(user)).status_code == 409
    assert other.read_bytes() == b"other clip"
    viewer = add_user(db, role="viewer")
    assert client.delete(f"/archive/exports/{job.id}?confirm=true", headers=auth_headers(viewer)).status_code == 403


def test_storage_deleted_camera_keeps_snapshot_name_and_id(api_db):
    db, _root = api_db
    camera = add_camera(db, name="Original name")
    add_segment(db, camera)
    camera.name = f"Original name__deleted_{camera.id}_1234"
    camera.deleted_at = datetime.utcnow()
    db.commit()
    data = _metadata_usage_aggregates(db, {camera.id: camera})
    row = data["camera_usage"][0] if isinstance(data["camera_usage"], list) else data["camera_usage"][camera.id]
    assert row["camera_id"] == camera.id
    assert row["camera_name"] == "Original name"
    assert row["camera_deleted"] and row["segment_count"] == 1
    assert _safe_camera_usage([row])[0]["camera_deleted"] is True


def test_deleted_camera_retained_recording_can_be_exported_with_original_label(api_db, client):
    db, _root = api_db
    user, camera = add_user(db), add_camera(db, name="Original camera")
    segment = add_media_segment(db, camera, seconds=2)
    source = Path(settings.storage_root) / segment.relative_path
    original = source.read_bytes()
    camera.deleted_at = datetime.utcnow()
    camera.name = f"Original camera__deleted_{camera.id}_1234"
    camera.enabled = False
    db.commit()
    options = client.get("/recordings/cameras", headers=auth_headers(user)).json()["export_items"]
    assert {"id": str(camera.id), "name": "Original camera", "deleted": True} in options
    job = create_done_export(client, db, user, camera, segment)
    assert job.camera_label_snapshot == "Original camera"
    assert (Path(settings.storage_exports) / job.internal_output_path).is_file()
    assert source.read_bytes() == original
    viewer = add_user(db, role="viewer")
    assert client.post("/archive/exports", json=valid_payload(camera, segment), headers=auth_headers(viewer)).status_code == 403


def test_deleted_camera_does_not_make_deleted_or_unusable_sources_exportable(api_db, client):
    db, _root = api_db
    user, camera = add_user(db), add_camera(db, deleted=True)
    segment = add_segment(db, camera)
    for attribute, value in (("deleted_at", datetime.utcnow()), ("integrity_status", "missing_file")):
        segment.deleted_at = None
        setattr(segment, attribute, value)
        db.commit()
        options = client.get("/recordings/cameras", headers=auth_headers(user)).json()["export_items"]
        assert not any(item["id"] == str(camera.id) for item in options)
        assert client.post("/archive/exports", json=valid_payload(camera, segment), headers=auth_headers(user)).status_code == 404


def test_history_camera_deleted_not_a_current_failure(api_db):
    db, _root = api_db
    camera = add_camera(db, deleted=True)
    operation(db, "old-delete", "camera_delete_with_files", scope={"camera_ids": [camera.id]})
    result = archive_operation_history_items(db)
    assert result["total"] == 1
    assert result["items"][0]["status"] == "blocked"
    assert result["items"][0]["current_state"] == "object_removed"
    assert result["attention_items"] == []


def test_history_groups_batch_and_recheck_and_preserves_actual_finding(api_db):
    db, _root = api_db
    scan_op = operation(db, "scan-op", "integrity_scan", status="completed")
    scan = ArchiveIntegrityScan(id="scan", operation_id=scan_op.id, actor_key="test", status="completed",
        phase="completed", root_snapshot_hash="test", scan_cutoff_at=datetime.utcnow())
    db.add(scan); db.commit()
    finding = ArchiveIntegrityFinding(id="finding", scan_id=scan.id, finding_scope="metadata", category="partial_file",
        severity="warning", impact_key="test", camera_name_snapshot="QA", display_name="test.mkv", state="resolved", is_active=False)
    db.add(finding); db.commit()
    operation(db, "batch", "integrity_review_batch", status="partial", result={"failed_count": 1})
    operation(db, "review-item-one", "integrity_review_item", progress={"batch_id": "batch", "finding_id": finding.id}, reason="probe_failed")
    operation(db, "recheck-review-item-one", "integrity_metadata_repair", reason="probe_failed")
    data = archive_operation_history_items(db, operation_type="integrity_review_batch")
    assert data["total"] == 1
    row = data["items"][0]
    assert row["status"] == "partial" and row["current_state"] == "resolved"
    assert row["details"][0]["file_name"] == "test.mkv"
    assert len(archive_operation_history_items(db)["items"]) == 2  # Scan + one user action.
    prepared = operation(db, "prepare", "integrity_plan_prepare", status="completed")
    applied = operation(db, "apply", "integrity_recording_delete", status="completed")
    plan = ArchiveIntegrityRemediationPlan(id="plan", scan_id=scan.id, finding_id=finding.id,
        operation_id=prepared.id, apply_operation_id=applied.id, actor_key="test", idempotency_key="plan",
        request_fingerprint="test", action_kind="delete", required_permission="delete_recordings",
        confirmation_level="destructive_media", canonical_hash="test", expires_at=datetime.utcnow()+timedelta(hours=1))
    db.add(plan)
    db.get(StorageOperation, "review-item-one").result = {"plan_id": plan.id}
    db.commit()
    assert len(archive_operation_history_items(db)["items"]) == 2  # Plan/apply receipts stay inside their batch.
    finding.state = "active"; finding.is_active = True; db.commit()
    data = archive_operation_history_items(db, operation_type="integrity_review_batch")
    assert data["items"][0]["current_state"] == "attention"


def test_history_pagination_filters_and_noop_summary(api_db):
    db, _root = api_db
    for index in range(24):
        operation(db, f"action-{index}", "manual_single_delete", status="completed")
    operation(db, "auto-noop", "retention_auto_run", status="completed", result={"deleted_count": 0, "bytes_freed": 0})
    result = archive_operation_history_items(db, limit=20)
    assert result["total"] == 24 and result["has_more"]
    second = archive_operation_history_items(db, limit=20, offset=20)
    assert len(second["items"]) == 4 and not second["has_more"]
    assert archive_operation_history_items(db, result_status="failed")["total"] == 0
    summary = useful_storage_operation_history(db)["summary"]
    assert summary["camera_retention"]["state"] == "no_deletion_required"
    assert summary["auto_free_space"]["state"] == "not_run"


def test_history_db_wall_clock_is_not_mislabeled_as_utc():
    timestamp = _history_timestamp(datetime(2026, 11, 12, 15, 31, 10), ZoneInfo("Asia/Yekaterinburg"))
    assert timestamp == "2026-11-12T10:31:10Z"
    assert _history_timestamp(None, ZoneInfo("UTC")) is None
