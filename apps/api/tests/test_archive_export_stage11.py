import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.permissions import (
    PERMISSION_EXPORT_RECORDINGS,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_OWNER,
    ROLE_PERMISSIONS,
    ROLE_VIEWER,
)
from app.core.security import create_access_token
from app.db.session import Base, get_db
from app.main import app
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveExportJob, RecordingSegment
from app.models.user import User
from app.services import archive_exports as export_service


FORBIDDEN_PATTERNS = (
    "rtsp://",
    "password",
    "secret",
    "token",
    "jwt",
    "authorization",
    "cookie",
    "/Volume",
)


@pytest.fixture
def api_db():
    tmp = tempfile.TemporaryDirectory(prefix="stage11_archive_export_")
    root = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    original_storage_exports = settings.storage_exports
    settings.storage_root = str(root / "archive")
    settings.storage_previews = str(root / "previews")
    settings.storage_exports = str(root / "exports")

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = Session()
    try:
        yield session, root
    finally:
        session.close()
        app.dependency_overrides.clear()
        settings.storage_root = original_storage_root
        settings.storage_previews = original_storage_previews
        settings.storage_exports = original_storage_exports
        tmp.cleanup()


@pytest.fixture
def client(api_db):
    session, _root = api_db

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def add_user(db, *, role=ROLE_ADMIN, username=None):
    user = User(
        username=username or f"stage11_{role}",
        full_name=f"stage11 {role}",
        password_hash="hash",
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.username)}"}


def add_camera(db, *, deleted=False, name="stage11_export_camera"):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=not deleted,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        status="deleted" if deleted else "enabled",
        deleted_at=datetime.utcnow() if deleted else None,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def add_segment(db, camera, *, started=None, seconds=60, write_file=True, name="segment.mkv", size=16):
    started = started or (datetime.utcnow() - timedelta(minutes=10))
    relative_path = f"kmvms/recordings/{camera.storage_folder_name}/{name}"
    file_path = Path(settings.storage_root) / relative_path
    if write_file:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"x" * size)
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=relative_path,
        relative_path=relative_path,
        started_at=started,
        ended_at=started + timedelta(seconds=seconds),
        duration_sec=seconds,
        size_bytes=size,
        stream_type="main",
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        finalized_at=started + timedelta(seconds=seconds),
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def valid_payload(camera, segment, **overrides):
    payload = {
        "camera_id": camera.id,
        "start_ts": (segment.started_at + timedelta(seconds=5)).replace(tzinfo=timezone.utc).isoformat(),
        "end_ts": (segment.started_at + timedelta(seconds=25)).replace(tzinfo=timezone.utc).isoformat(),
        "reason": "stage11 contract validation",
    }
    payload.update(overrides)
    return payload


def test_stage11_endpoint_registry_and_role_matrix():
    registered = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("POST", "/archive/exports", PERMISSION_EXPORT_RECORDINGS) in registered
    assert ("GET", "/archive/exports", PERMISSION_EXPORT_RECORDINGS) in registered
    assert ("GET", "/archive/exports/{export_id}", PERMISSION_EXPORT_RECORDINGS) in registered
    assert PERMISSION_EXPORT_RECORDINGS in ROLE_PERMISSIONS[ROLE_OWNER]
    assert PERMISSION_EXPORT_RECORDINGS in ROLE_PERMISSIONS[ROLE_ADMIN]
    assert PERMISSION_EXPORT_RECORDINGS not in ROLE_PERMISSIONS[ROLE_OPERATOR]
    assert PERMISSION_EXPORT_RECORDINGS not in ROLE_PERMISSIONS[ROLE_VIEWER]


def test_stage11_no_auth_and_non_export_user_are_denied(client, api_db):
    db, _root = api_db
    operator = add_user(db, role=ROLE_OPERATOR)
    camera = add_camera(db)
    segment = add_segment(db, camera)

    response = client.post("/archive/exports", json=valid_payload(camera, segment))
    assert response.status_code == 401

    response = client.post("/archive/exports", json=valid_payload(camera, segment), headers=auth_headers(operator))
    assert response.status_code == 403


def test_stage11_admin_can_create_queued_metadata_only_job(client, api_db):
    db, root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    segment = add_segment(db, camera)

    response = client.post("/archive/exports", json=valid_payload(camera, segment), headers=auth_headers(admin))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["camera"] == {"id": camera.id, "label": camera.name}
    assert body["source_segment_count"] == 1
    assert body["estimated_source_bytes"] == 16
    assert body["expires_at"]
    assert "download_url" not in body
    assert "output_path" not in body
    assert "manifest_path" not in body
    assert not any(root.rglob("*.mp4"))
    assert not any(root.rglob("*.json"))

    job = db.get(ArchiveExportJob, body["id"])
    assert job is not None
    assert job.status == "queued"
    assert job.internal_output_path is None
    assert job.internal_manifest_path is None
    assert job.internal_checksum is None

    audit = db.query(AuditEvent).filter(AuditEvent.event_type == "archive_export_requested").first()
    assert audit is not None
    assert audit.category == "archive"
    assert audit.target_id == job.id

    safe_text = str(body).lower() + str(audit.event_metadata).lower()
    assert str(root).lower() not in safe_text
    for pattern in FORBIDDEN_PATTERNS:
        assert pattern not in safe_text


def test_stage11_job_read_and_list_are_permission_gated(client, api_db):
    db, _root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    operator = add_user(db, role=ROLE_OPERATOR, username="stage11_operator_read")
    camera = add_camera(db)
    segment = add_segment(db, camera)
    created = client.post("/archive/exports", json=valid_payload(camera, segment), headers=auth_headers(admin)).json()

    assert client.get("/archive/exports", headers=auth_headers(admin)).status_code == 200
    assert client.get(f"/archive/exports/{created['id']}", headers=auth_headers(admin)).status_code == 200
    assert client.get("/archive/exports", headers=auth_headers(operator)).status_code == 403
    assert client.get(f"/archive/exports/{created['id']}", headers=auth_headers(operator)).status_code == 403


def test_stage11_invalid_ranges_and_future_ranges_are_rejected_safely(client, api_db):
    db, _root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    segment = add_segment(db, camera)

    assert client.post(
        "/archive/exports",
        json=valid_payload(camera, segment, start_ts=segment.started_at.isoformat(), end_ts=segment.started_at.isoformat()),
        headers=auth_headers(admin),
    ).status_code == 422

    assert client.post(
        "/archive/exports",
        json=valid_payload(camera, segment, end_ts=(segment.started_at + timedelta(hours=1)).isoformat()),
        headers=auth_headers(admin),
    ).status_code == 413

    future = datetime.utcnow() + timedelta(days=2)
    assert client.post(
        "/archive/exports",
        json=valid_payload(camera, segment, start_ts=future.isoformat(), end_ts=(future + timedelta(minutes=1)).isoformat()),
        headers=auth_headers(admin),
    ).status_code == 422


def test_stage11_missing_soft_deleted_and_no_source_camera_cases(client, api_db):
    db, _root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    active = add_camera(db)
    segment = add_segment(db, active)
    deleted = add_camera(db, deleted=True, name="stage11_deleted__deleted_10_1777777777")

    assert client.post(
        "/archive/exports",
        json=valid_payload(active, segment, camera_id=999999),
        headers=auth_headers(admin),
    ).status_code == 404
    assert client.post(
        "/archive/exports",
        json=valid_payload(active, segment, camera_id=deleted.id),
        headers=auth_headers(admin),
    ).status_code == 404

    empty = add_camera(db, name="stage11_empty_camera")
    assert client.post(
        "/archive/exports",
        json=valid_payload(empty, segment, camera_id=empty.id),
        headers=auth_headers(admin),
    ).status_code == 404


def test_stage11_missing_source_file_is_safe_and_creates_no_job(client, api_db):
    db, root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    segment = add_segment(db, camera, write_file=False)

    response = client.post("/archive/exports", json=valid_payload(camera, segment), headers=auth_headers(admin))

    assert response.status_code == 409
    assert db.query(ArchiveExportJob).count() == 0
    text = response.text.lower()
    assert str(root).lower() not in text
    assert "/volume" not in text


def test_stage11_segment_size_and_active_job_limits(client, api_db, monkeypatch):
    db, _root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    first = add_segment(db, camera, name="first.mkv", size=4)
    add_segment(db, camera, name="second.mkv", started=first.started_at + timedelta(seconds=70), size=4)

    monkeypatch.setattr(export_service, "MAX_SOURCE_SEGMENTS", 1)
    response = client.post(
        "/archive/exports",
        json=valid_payload(camera, first, end_ts=(first.started_at + timedelta(seconds=90)).isoformat()),
        headers=auth_headers(admin),
    )
    assert response.status_code == 413

    monkeypatch.setattr(export_service, "MAX_SOURCE_SEGMENTS", 120)
    monkeypatch.setattr(export_service, "MAX_ESTIMATED_SOURCE_BYTES", 1)
    response = client.post("/archive/exports", json=valid_payload(camera, first), headers=auth_headers(admin))
    assert response.status_code == 413

    monkeypatch.setattr(export_service, "MAX_ESTIMATED_SOURCE_BYTES", 4 * 1024 * 1024 * 1024)
    for index in range(export_service.MAX_ACTIVE_JOBS_PER_USER):
        db.add(
            ArchiveExportJob(
                id=f"stage11-active-{index}",
                actor_user_id=admin.id,
                camera_id=camera.id,
                camera_label_snapshot=camera.name,
                start_ts=first.started_at,
                end_ts=first.ended_at,
                duration_seconds=60,
                status="queued",
                source_segment_ids=[first.id],
                source_segment_count=1,
                estimated_source_bytes=4,
                gap_warnings=[],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(hours=1),
            )
        )
    db.commit()
    response = client.post("/archive/exports", json=valid_payload(camera, first), headers=auth_headers(admin))
    assert response.status_code == 429


def test_stage11_request_rejects_path_like_client_fields(client, api_db):
    db, _root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    segment = add_segment(db, camera)
    payload = valid_payload(camera, segment)
    payload["output_path"] = "/tmp/forbidden.mp4"

    response = client.post("/archive/exports", json=payload, headers=auth_headers(admin))

    assert response.status_code == 422
    assert db.query(ArchiveExportJob).count() == 0


def test_stage11_public_statuses_are_exact():
    assert export_service.EXPORT_STATUSES == {"queued", "running", "done", "failed", "expired"}
