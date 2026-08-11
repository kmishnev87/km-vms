import sys
import tempfile
import stat
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.storage_operation import StorageOperation
from app.models.user import User
import app.routers.cameras as cameras_module
import app.routers.camera_connection_helpers as connection_helpers_module
import app.routers.camera_onvif_routes as onvif_routes_module
from app.routers.cameras import (
    delete_camera,
    list_cameras,
    list_viewer_cameras,
    onvif_profile_config,
    onvif_profiles,
    preview_delete_camera,
    test_camera as camera_test_endpoint,
    update_onvif_profile_route,
)
from app.schemas.camera import CameraResponse
from app.services.recording_retention import execute_segments
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
)


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


def actor(role="owner"):
    return SimpleNamespace(id=1, username=f"{role}_user", role=role, is_active=True)


def make_db():
    tmp = tempfile.TemporaryDirectory(prefix="stage1_delete_test_")
    tmp_path = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    settings.storage_root = str(tmp_path / "storage")
    settings.storage_previews = str(tmp_path / "previews")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    return session, tmp, original_storage_root, original_storage_previews


def cleanup_db(session, tmp, original_storage_root, original_storage_previews):
    try:
        session.close()
    finally:
        settings.storage_root = original_storage_root
        settings.storage_previews = original_storage_previews
        tmp.cleanup()


def with_db(test_func):
    db, tmp, original_storage_root, original_storage_previews = make_db()
    try:
        test_func(db)
    finally:
        cleanup_db(db, tmp, original_storage_root, original_storage_previews)


@pytest.fixture
def db():
    session, tmp, original_storage_root, original_storage_previews = make_db()
    try:
        yield session
    finally:
        cleanup_db(session, tmp, original_storage_root, original_storage_previews)


class raises_http:
    def __init__(self):
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            raise AssertionError("HTTPException was not raised")
        if not isinstance(exc, HTTPException):
            return False
        self.value = exc
        return True


def add_camera(db, *, name="stage1_delete_test_camera"):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="user",
        password_encrypted=None,
        recording_mode="manual",
        default_live_stream="main",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=1,
        storage_quota_gb=50,
        status="created",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def add_segment(
    db,
    camera,
    *,
    relative_path=None,
    status="finalized",
    ownership="KM VMS",
    source="recorder",
    integrity_status=None,
    job_id=None,
    file_exists=True,
):
    ensure_archive_roots(db)
    Path(settings.storage_root, "kmvms", "recordings").mkdir(parents=True, exist_ok=True)
    rel = relative_path or f"kmvms/recordings/camera_{camera.id}/stage1_delete_test_segment.mkv"
    path = Path(settings.storage_root) / rel
    if file_exists:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stage1")
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id=job_id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(path),
        relative_path=rel,
        started_at=datetime.utcnow(),
        ended_at=datetime.utcnow(),
        duration_sec=1,
        size_bytes=path.stat().st_size if path.exists() else 6,
        stream_type="main",
        status=status,
        ownership=ownership,
        source=source,
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=datetime.utcnow(),
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status=integrity_status,
        finalized_at=datetime.utcnow() if status == "finalized" else None,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment, path


def seed_terminal_camera_delete_operation(
    db,
    camera,
    *,
    operation_id,
    terminal_status,
    reason_code,
    next_action,
    retry_mode,
    retry_allowed,
):
    current_actor = actor("owner")
    claim = cameras_module.claim_operation_with_conflicts(
        db,
        operation_type="camera_delete_with_files",
        scope=cameras_module.camera_delete_operation_scope(camera, []),
        request_identity={"camera_id": camera.id, "delete_files": True},
        actor=current_actor,
        operation_id=operation_id,
        idempotency_key=operation_id,
        owner_instance_id=cameras_module.operation_instance_id("camera-delete-test"),
    )
    assert claim["state"] == "claimed"
    lifecycle = cameras_module.StorageOperationLifecycle(
        db,
        claim["handle"],
        failure_reason="camera_delete_with_files_failed",
    )
    lifecycle.finish(
        status=terminal_status,
        result={"status": terminal_status},
        reason_code=reason_code,
        next_action=next_action,
        retry_mode=retry_mode,
        retry_allowed=retry_allowed,
    )
    return current_actor


def assert_unremoved_camera_terminal_replay(
    db,
    *,
    terminal_status,
    reason_code,
    next_action,
    retry_mode,
    retry_allowed,
):
    camera = add_camera(db, name=f"stage41012_camera_{terminal_status}_replay")
    operation_id = f"stage41012-camera-delete-{terminal_status}-replay"
    current_actor = seed_terminal_camera_delete_operation(
        db,
        camera,
        operation_id=operation_id,
        terminal_status=terminal_status,
        reason_code=reason_code,
        next_action=next_action,
        retry_mode=retry_mode,
        retry_allowed=retry_allowed,
    )
    operation_count = db.query(StorageOperation).count()
    audit_count = db.query(AuditEvent).count()

    replay = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=current_actor,
    )

    assert replay["status"] == terminal_status
    assert replay["ok"] is False
    assert replay["camera_removed"] is False
    assert replay["replayed"] is True
    assert replay["reason_code"] == reason_code
    assert replay["next_action"] == next_action
    assert replay["retry_mode"] == retry_mode
    assert replay["retry_allowed"] is retry_allowed
    assert replay["cancel_allowed"] is False
    assert replay["recordings"]["status"] == terminal_status
    assert replay["recordings"]["camera_removed"] is False
    assert db.get(Camera, camera.id).deleted_at is None
    assert db.query(StorageOperation).count() == operation_count
    assert db.query(AuditEvent).count() == audit_count


def test_camera_delete_without_files_removes_camera_and_retains_recordings(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)

    result = delete_camera(camera.id, FakeRequest(), delete_files=False, db=db, current_user=actor("owner"))

    assert result["ok"] is True
    assert result["camera_removed"] is True
    assert result["status"] == "deleted"
    assert result["recordings"]["skipped_count"] == 1
    assert result["recordings"]["reason_counts"]["recordings_exist_delete_files_false_requires_safe_policy"] == 1
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(Camera, camera.id).deleted_at is not None
    assert db.get(RecordingSegment, segment.id) is not None


def test_camera_delete_with_files_without_recording_permission_removes_camera_and_skips_archive(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)

    result = delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("operator"))

    assert result["ok"] is True
    assert result["camera_removed"] is True
    assert result["status"] == "deleted_archive_cleanup_partial"
    assert result["recordings"]["reason_counts"]["delete_recordings_permission_missing"] == 1
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(Camera, camera.id).deleted_at is not None
    assert db.get(RecordingSegment, segment.id) is not None


def test_camera_delete_with_files_uses_safe_model_and_returns_summary(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)

    result = delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["ok"] is True
    assert result["status"] == "deleted"
    assert result["recordings"]["deleted_count"] == 1
    assert result["recordings"]["failed_count"] == 0
    assert result["recordings"]["skipped_count"] == 0
    assert not file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(Camera, camera.id).deleted_at is not None
    remaining = db.get(RecordingSegment, segment.id)
    if remaining is not None:
        assert remaining.status == "deleted"


def test_camera_delete_with_files_terminal_replay_does_not_delete_twice(db):
    camera = add_camera(db, name="stage41011_camera_replay")
    _segment, file_path = add_segment(db, camera)
    operation_id = "stage41011-camera-delete-replay"

    first = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=actor("owner"),
    )
    audit_count_after_first = db.query(AuditEvent).count()
    second = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=actor("owner"),
    )

    assert first["status"] == "deleted"
    assert "replayed" not in first
    assert second["status"] == "deleted"
    assert second["ok"] is True
    assert second["camera_removed"] is True
    assert second["replayed"] is True
    assert second["camera_name"] == first["camera_name"]
    assert second["delete_files"] == first["delete_files"]
    assert second["preview_cleanup"] == first["preview_cleanup"]
    assert second["recordings"]["replayed"] is True
    assert not file_path.exists()
    operation = db.query(StorageOperation).filter(StorageOperation.operation_type == "camera_delete_with_files").one()
    assert operation.status == "completed"
    assert db.query(AuditEvent).count() == audit_count_after_first


def test_camera_delete_partial_cleanup_terminal_replay_preserves_product_truth(db):
    camera = add_camera(db, name="stage41012_camera_partial_replay")
    _segment, file_path = add_segment(db, camera, ownership="third_party")
    operation_id = "stage41012-camera-delete-partial-replay"

    first = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=actor("owner"),
    )
    audit_count_after_first = db.query(AuditEvent).count()
    second = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=actor("owner"),
    )

    assert first["status"] == "deleted_archive_cleanup_partial"
    assert "replayed" not in first
    assert second["status"] == "deleted_archive_cleanup_partial"
    assert second["ok"] is True
    assert second["camera_removed"] is True
    assert second["replayed"] is True
    assert second["camera_name"] == first["camera_name"]
    assert second["recordings"]["reason_counts"] == first["recordings"]["reason_counts"]
    assert second["warnings"] == first["warnings"]
    assert file_path.exists()
    operation = db.query(StorageOperation).filter(StorageOperation.operation_type == "camera_delete_with_files").one()
    assert operation.status == "partial"
    assert db.query(AuditEvent).count() == audit_count_after_first


def test_camera_delete_without_recording_permission_replays_partial_outcome(db):
    camera = add_camera(db, name="stage41012_camera_permission_replay")
    _segment, file_path = add_segment(db, camera)
    operation_id = "stage41012-camera-delete-permission-replay"

    first = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=actor("operator"),
    )
    audit_count_after_first = db.query(AuditEvent).count()
    second = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=actor("operator"),
    )

    assert first["status"] == "deleted_archive_cleanup_partial"
    assert second["status"] == first["status"]
    assert second["ok"] is True
    assert second["camera_removed"] is True
    assert second["replayed"] is True
    assert second["recordings"]["status"] == first["recordings"]["status"] == "blocked"
    assert second["recordings"]["reason_counts"] == first["recordings"]["reason_counts"]
    assert file_path.exists()
    assert db.query(StorageOperation).filter(StorageOperation.operation_type == "camera_delete_with_files").count() == 1
    assert db.query(AuditEvent).count() == audit_count_after_first


def test_camera_delete_terminal_replay_remains_actor_bound(db):
    camera = add_camera(db, name="stage41012_camera_actor_replay")
    add_segment(db, camera)
    operation_id = "stage41012-camera-delete-actor-replay"
    delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=actor("owner"),
    )
    foreign_actor = SimpleNamespace(id=2, username="other_owner", role="owner", is_active=True)

    with pytest.raises(HTTPException) as exc:
        delete_camera(
            camera.id,
            FakeRequest(),
            delete_files=True,
            operation_id=operation_id,
            db=db,
            current_user=foreign_actor,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason_code"] == "storage_operation_identity_mismatch"


def test_camera_delete_blocked_terminal_replay_preserves_terminal_truth(db):
    assert_unremoved_camera_terminal_replay(
        db,
        terminal_status="blocked",
        reason_code="camera_delete_blocked",
        next_action="resolve_camera_delete_blocker",
        retry_mode="refresh",
        retry_allowed=True,
    )


def test_camera_delete_cancelled_terminal_replay_preserves_terminal_truth(db):
    assert_unremoved_camera_terminal_replay(
        db,
        terminal_status="cancelled",
        reason_code="camera_delete_cancelled",
        next_action="restart_camera_delete_if_needed",
        retry_mode=None,
        retry_allowed=False,
    )


def test_camera_delete_failed_terminal_replay_preserves_failure_truth(db, monkeypatch):
    camera = add_camera(db)
    add_segment(db, camera)
    operation_id = "stage41012-camera-delete-failed-replay"

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected camera deletion failure")

    monkeypatch.setattr(cameras_module, "execute_segments", fail)

    with pytest.raises(RuntimeError, match="injected camera deletion failure"):
        delete_camera(
            camera.id,
            FakeRequest(),
            delete_files=True,
            operation_id=operation_id,
            db=db,
            current_user=actor("owner"),
        )

    row = (
        db.query(StorageOperation)
        .filter(StorageOperation.operation_type == "camera_delete_with_files")
        .order_by(StorageOperation.created_at.desc())
        .first()
    )
    assert row.status == "failed"
    assert row.lease_expires_at is None
    assert db.get(Camera, camera.id).deleted_at is None
    audit_count = db.query(AuditEvent).count()

    replay = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=True,
        operation_id=operation_id,
        db=db,
        current_user=actor("owner"),
    )

    assert replay["status"] == "failed"
    assert replay["ok"] is False
    assert replay["camera_removed"] is False
    assert replay["replayed"] is True
    assert replay["reason_code"] == "camera_delete_with_files_failed"
    assert replay["retry_allowed"] is True
    assert replay["retry_mode"] == "immediate"
    assert db.get(Camera, camera.id).deleted_at is None
    assert db.query(StorageOperation).filter(StorageOperation.operation_type == "camera_delete_with_files").count() == 1
    assert db.query(AuditEvent).count() == audit_count


def test_camera_delete_with_files_skips_active_recording_but_removes_camera(db):
    camera = add_camera(db)
    job = RecordingJob(
        id="stage1_delete_test_job",
        camera_id=camera.id,
        state="recording",
        started_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    segment, file_path = add_segment(db, camera, job_id=job.id)

    result = delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["ok"] is True
    assert result["camera_removed"] is True
    assert result["status"] == "deleted_archive_cleanup_partial"
    assert result["recordings"]["reason_counts"]["active_job"] == 1
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(Camera, camera.id).deleted_at is not None
    assert db.get(RecordingSegment, segment.id) is not None


def test_camera_delete_with_files_skips_foreign_and_removes_camera(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera, ownership="third_party")

    result = delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["ok"] is True
    assert result["camera_removed"] is True
    assert result["status"] == "deleted_archive_cleanup_partial"
    assert result["recordings"]["reason_counts"]["unowned"] == 1
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(Camera, camera.id).deleted_at is not None
    assert db.get(RecordingSegment, segment.id) is not None


def test_camera_delete_with_files_skips_path_traversal_and_removes_camera(db):
    camera = add_camera(db)
    segment, _file_path = add_segment(db, camera, relative_path="../stage1_delete_test_escape.mkv", file_exists=False)

    result = delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["ok"] is True
    assert result["camera_removed"] is True
    assert result["status"] == "deleted_archive_cleanup_partial"
    assert result["recordings"]["reason_counts"]["path_outside_storage"] == 1
    assert db.get(Camera, camera.id) is not None
    assert db.get(Camera, camera.id).deleted_at is not None
    assert db.get(RecordingSegment, segment.id) is not None


def test_disabled_offline_camera_without_recordings_deletes_with_and_without_file_flag(db):
    for delete_files in (False, True):
        camera = add_camera(db, name=f"stage1_delete_no_recordings_{delete_files}")
        camera.status = "offline"
        camera.enabled = False
        db.add(camera)
        db.commit()

        result = delete_camera(camera.id, FakeRequest(), delete_files=delete_files, db=db, current_user=actor("owner"))

        assert result["ok"] is True
        assert result["camera_removed"] is True
        assert result["status"] == "deleted"
        assert result["recordings"]["requested_count"] == 0
        assert db.get(Camera, camera.id).deleted_at is not None


def assert_safe_deleted_camera_error(exc: HTTPException):
    assert exc.status_code == 404
    assert exc.detail["code"] == "camera_not_active"
    serialized = str(exc.detail).lower()
    assert "deleted.example.test" not in serialized
    assert "operator" not in serialized
    assert "camera-" + "pass" not in serialized
    assert "rtsp://" not in serialized
    assert "traceback" not in serialized


def test_deleted_camera_is_hidden_from_active_and_viewer_lists(db):
    camera = add_camera(db, name="stage102_deleted_visibility")

    result = delete_camera(camera.id, FakeRequest(), delete_files=False, db=db, current_user=actor("owner"))

    assert result["ok"] is True
    assert db.get(Camera, camera.id).deleted_at is not None
    assert camera.id not in [item.id for item in list_cameras(db=db, current_user=actor("owner"))]
    assert camera.id not in [item["id"] for item in list_viewer_cameras(db=db, current_user=actor("viewer"))]


def test_stage16_active_camera_response_strips_deleted_marker_without_mutating_deleted_history(db):
    active = add_camera(db, name="stage16_active__deleted_7_1777777777")
    active.storage_folder_name = "stage16_active_folder__deleted_7_1777777777"
    deleted = add_camera(db, name="stage16_deleted_seed")
    deleted.name = "stage16_deleted_seed__deleted_8_1777777777"
    deleted.storage_folder_name = "stage16_deleted_folder__deleted_8_1777777777"
    deleted.deleted_at = datetime.utcnow()
    db.add(active)
    db.add(deleted)
    db.commit()
    db.refresh(active)
    db.refresh(deleted)

    active_payload = CameraResponse.model_validate(active).model_dump(mode="json")
    deleted_payload = CameraResponse.model_validate(deleted).model_dump(mode="json")

    assert "__deleted_" not in active_payload["name"]
    assert "__deleted_" not in active_payload["storage_folder_name"]
    assert "__deleted_" in db.get(Camera, active.id).storage_folder_name
    assert "__deleted_" in deleted_payload["name"]
    assert "__deleted_" in deleted_payload["storage_folder_name"]


def test_stage16_soft_delete_allows_same_name_reuse_without_active_marker_leak(db):
    original = add_camera(db, name="stage16_reuse_camera")

    result = delete_camera(original.id, FakeRequest(), delete_files=False, db=db, current_user=actor("owner"))
    deleted_row = db.get(Camera, original.id)
    replacement = add_camera(db, name="stage16_reuse_camera")
    replacement_payload = CameraResponse.model_validate(replacement).model_dump(mode="json")

    assert result["ok"] is True
    assert deleted_row.deleted_at is not None
    assert "__deleted_" in deleted_row.name
    assert "__deleted_" in deleted_row.storage_folder_name
    assert replacement.name == "stage16_reuse_camera"
    assert replacement.storage_folder_name == "stage16_reuse_camera"
    assert "__deleted_" not in replacement_payload["name"]
    assert "__deleted_" not in replacement_payload["storage_folder_name"]


def test_deleted_camera_id_cannot_reuse_credentials_for_test_or_onvif(db, monkeypatch):
    camera = add_camera(db, name="stage102_deleted_credential_reuse")
    camera.host = "deleted.example.test"
    camera.port = 20003
    camera.username = "operator"
    camera.password_encrypted = "stored-secret-placeholder"
    db.add(camera)
    db.commit()
    camera_id = camera.id

    delete_result = delete_camera(camera_id, FakeRequest(), delete_files=False, db=db, current_user=actor("owner"))
    assert delete_result["ok"] is True

    def fail_if_password_is_decrypted(value):
        raise AssertionError("deleted camera password must not be decrypted")

    def fail_if_network_is_used(*args, **kwargs):
        raise AssertionError("deleted camera credentials must not reach network helpers")

    monkeypatch.setattr(connection_helpers_module, "decrypt_text", fail_if_password_is_decrypted)
    monkeypatch.setattr(cameras_module.subprocess, "run", fail_if_network_is_used)
    monkeypatch.setattr(onvif_routes_module, "fetch_onvif_profiles", fail_if_network_is_used)
    monkeypatch.setattr(onvif_routes_module, "get_onvif_profile_config", fail_if_network_is_used)
    monkeypatch.setattr(onvif_routes_module, "update_onvif_profile", fail_if_network_is_used)

    deleted_payload = {
        "camera_id": camera_id,
        "host": "manual.example.test",
        "port": 554,
        "username": "manual",
        "password": "manual-" + "pass",
        "rtsp_main_url": "/live",
        "profile_token": "main",
        "config": {"fps": 25},
    }

    for call in (
        lambda: camera_test_endpoint(deleted_payload, db=db, current_user=actor("owner")),
        lambda: onvif_profiles(deleted_payload, db=db, current_user=actor("owner")),
        lambda: onvif_profile_config(deleted_payload, db=db, current_user=actor("owner")),
        lambda: update_onvif_profile_route(deleted_payload, db=db, current_user=actor("owner")),
    ):
        with raises_http() as exc:
            call()
        assert_safe_deleted_camera_error(exc.value)


def test_manual_camera_test_without_camera_id_still_builds_explicit_url(db, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout='{"streams":[{"codec_type":"video","codec_name":"h264"}]}')

    monkeypatch.setattr(cameras_module.subprocess, "run", fake_run)
    monkeypatch.setattr(cameras_module, "capture_camera_preview", lambda *args, **kwargs: False)

    result = camera_test_endpoint(
        {
            "host": "manual.example.test",
            "port": 554,
            "username": "operator",
            "password": "camera-" + "pass",
            "rtsp_main_url": "/live",
        },
        db=db,
        current_user=actor("owner"),
    )

    assert result["ok"] is True
    assert captured["cmd"][-1].startswith("rtsp://operator:")


def test_captured_preview_is_readable_by_static_nginx_mount(tmp_path, monkeypatch):
    original_storage_previews = settings.storage_previews
    settings.storage_previews = str(tmp_path / "previews")
    preview_path = settings.camera_test_preview_path("stage6preview")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"jpg")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cameras_module.subprocess, "run", fake_run)
    try:
        assert cameras_module.capture_camera_preview("rtsp://camera.local/live", "tcp", preview_path)
        assert stat.S_IMODE(preview_path.parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(preview_path.stat().st_mode) == 0o644
    finally:
        settings.storage_previews = original_storage_previews


def test_attached_camera_preview_is_readable_by_static_nginx_mount(tmp_path):
    original_storage_previews = settings.storage_previews
    settings.storage_previews = str(tmp_path / "previews")
    source = settings.camera_test_preview_path("stage6copy")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"jpg")

    try:
        cameras_module.attach_test_preview_to_camera("stage6copy", 42)
        destination = settings.camera_preview_path(42)
        assert destination.exists()
        assert not source.exists()
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    finally:
        settings.storage_previews = original_storage_previews


def test_camera_delete_preview_does_not_mutate_files_or_metadata(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)

    result = preview_delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["status"] == "preview_safe"
    assert result["recordings"]["planned_count"] == 1
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id).status == "finalized"


def test_camera_delete_preview_marks_unsafe_segments_blocked(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera, ownership="third_party")

    result = preview_delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["status"] == "preview_blocked"
    assert result["recordings"]["ok"] is False
    assert result["recordings"]["skipped_count"] == 1
    assert result["recordings"]["reason_counts"]["unowned"] == 1
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id).status == "finalized"


def test_execute_segments_recovers_metadata_if_commit_fails_after_file_delete(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)
    original_commit = db.commit
    calls = {"failed": False}

    def fail_once():
        if not file_path.exists() and not calls["failed"]:
            calls["failed"] = True
            raise RuntimeError("stage1 simulated metadata commit failure")
        return original_commit()

    db.commit = fail_once
    result = execute_segments(
        db,
        [segment],
        actor=actor("owner"),
        operation="manual_single_delete",
        reason="stage1_commit_failure_test",
        max_candidates=1,
    )
    db.commit = original_commit

    recovered = db.get(RecordingSegment, segment.id)
    assert not file_path.exists()
    assert recovered.status == "deleted"
    assert result["failed_count"] == 1
    assert result["items"][0]["reason"] == "metadata_update_failed_recovered"


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            with_db(value)
    print("camera_delete_safety_tests_ok")
