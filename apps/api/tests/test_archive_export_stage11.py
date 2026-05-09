import sys
import tempfile
import shutil
import subprocess
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


def require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for Stage 2 generation tests")


def write_tiny_media(path: Path, *, seconds=2, color="testsrc") -> int:
    require_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    source = f"{color}=size=64x64:rate=10"
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source,
            "-t",
            str(seconds),
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            str(path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip("ffmpeg test media generation is unavailable")
    return path.stat().st_size


def add_media_segment(db, camera, *, started=None, seconds=2, name="media.mkv", color="testsrc"):
    started = started or (datetime.utcnow() - timedelta(minutes=10))
    relative_path = f"kmvms/recordings/{camera.storage_folder_name}/{name}"
    file_path = Path(settings.storage_root) / relative_path
    size = write_tiny_media(file_path, seconds=seconds, color=color)
    return add_segment(db, camera, started=started, seconds=seconds, write_file=False, name=name, size=size)


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


def test_stage11_stage2_generate_route_is_registered_and_permission_gated():
    registered = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("POST", "/archive/exports/{export_id}/generate", PERMISSION_EXPORT_RECORDINGS) in registered


def test_stage11_stage2_generates_single_segment_clip_without_public_paths(client, api_db):
    db, root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    segment = add_media_segment(db, camera, seconds=3)
    source_path = Path(settings.storage_root) / segment.relative_path
    source_size = source_path.stat().st_size
    source_mtime = source_path.stat().st_mtime_ns

    created = client.post(
        "/archive/exports",
        json=valid_payload(
            camera,
            segment,
            start_ts=(segment.started_at + timedelta(seconds=0.2)).replace(tzinfo=timezone.utc).isoformat(),
            end_ts=(segment.started_at + timedelta(seconds=2.0)).replace(tzinfo=timezone.utc).isoformat(),
        ),
        headers=auth_headers(admin),
    )
    assert created.status_code == 200

    generated = client.post(f"/archive/exports/{created.json()['id']}/generate", headers=auth_headers(admin))

    assert generated.status_code == 200
    body = generated.json()
    assert body["status"] == "done"
    assert body["has_generated_clip"] is True
    assert body["clip_ready"] is True
    assert body["output_container"] == "mkv"
    assert body["output_size_bytes"] > 0
    assert "download_url" not in body
    assert "output_path" not in body
    assert "manifest_path" not in body
    assert "checksum" not in body
    assert str(root) not in str(body)

    job = db.get(ArchiveExportJob, body["id"])
    output_path = Path(settings.storage_exports) / job.internal_output_path
    assert output_path.resolve().is_relative_to(Path(settings.storage_exports).resolve())
    assert output_path.exists()
    assert job.internal_checksum
    assert source_path.stat().st_size == source_size
    assert source_path.stat().st_mtime_ns == source_mtime


def test_stage11_stage2_generates_compatible_multi_segment_clip(client, api_db):
    db, _root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    start = datetime.utcnow() - timedelta(minutes=10)
    first = add_media_segment(db, camera, started=start, seconds=2, name="first.mkv")
    add_media_segment(db, camera, started=start + timedelta(seconds=2), seconds=2, name="second.mkv")

    created = client.post(
        "/archive/exports",
        json=valid_payload(
            camera,
            first,
            start_ts=start.replace(tzinfo=timezone.utc).isoformat(),
            end_ts=(start + timedelta(seconds=4)).replace(tzinfo=timezone.utc).isoformat(),
        ),
        headers=auth_headers(admin),
    )
    assert created.status_code == 200

    generated = client.post(f"/archive/exports/{created.json()['id']}/generate", headers=auth_headers(admin))

    assert generated.status_code == 200
    body = generated.json()
    assert body["status"] == "done"
    assert body["source_segment_count"] == 2
    assert body["output_size_bytes"] > 0


def test_stage11_stage2_missing_source_marks_failed_without_raw_path(client, api_db):
    db, root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    segment = add_media_segment(db, camera, seconds=2)
    created = client.post(
        "/archive/exports",
        json=valid_payload(
            camera,
            segment,
            start_ts=(segment.started_at + timedelta(seconds=0.2)).replace(tzinfo=timezone.utc).isoformat(),
            end_ts=(segment.started_at + timedelta(seconds=1.8)).replace(tzinfo=timezone.utc).isoformat(),
        ),
        headers=auth_headers(admin),
    )
    assert created.status_code == 200
    (Path(settings.storage_root) / segment.relative_path).unlink()

    generated = client.post(f"/archive/exports/{created.json()['id']}/generate", headers=auth_headers(admin))

    assert generated.status_code == 200
    body = generated.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "source_missing"
    assert str(root) not in str(body)


def test_stage11_stage2_gap_marks_failed_safely(client, api_db):
    db, _root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    start = datetime.utcnow() - timedelta(minutes=10)
    first = add_media_segment(db, camera, started=start, seconds=2, name="gap_first.mkv")
    add_media_segment(db, camera, started=start + timedelta(seconds=8), seconds=2, name="gap_second.mkv")

    created = client.post(
        "/archive/exports",
        json=valid_payload(
            camera,
            first,
            start_ts=start.replace(tzinfo=timezone.utc).isoformat(),
            end_ts=(start + timedelta(seconds=10)).replace(tzinfo=timezone.utc).isoformat(),
        ),
        headers=auth_headers(admin),
    )
    assert created.status_code == 200

    generated = client.post(f"/archive/exports/{created.json()['id']}/generate", headers=auth_headers(admin))

    assert generated.status_code == 200
    body = generated.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "source_gap_detected"
    assert body["has_generated_clip"] is False


def test_stage11_stage2_expired_and_lower_permission_generate_are_denied(client, api_db):
    db, _root = api_db
    admin = add_user(db, role=ROLE_ADMIN)
    operator = add_user(db, role=ROLE_OPERATOR, username="stage11_operator_generate")
    viewer = add_user(db, role=ROLE_VIEWER, username="stage11_viewer_generate")
    camera = add_camera(db)
    segment = add_media_segment(db, camera, seconds=2)
    created = client.post(
        "/archive/exports",
        json=valid_payload(
            camera,
            segment,
            start_ts=(segment.started_at + timedelta(seconds=0.2)).replace(tzinfo=timezone.utc).isoformat(),
            end_ts=(segment.started_at + timedelta(seconds=1.8)).replace(tzinfo=timezone.utc).isoformat(),
        ),
        headers=auth_headers(admin),
    )
    assert created.status_code == 200
    job = db.get(ArchiveExportJob, created.json()["id"])

    assert client.post(f"/archive/exports/{job.id}/generate", headers=auth_headers(operator)).status_code == 403
    assert client.post(f"/archive/exports/{job.id}/generate", headers=auth_headers(viewer)).status_code == 403

    job.status = "expired"
    db.add(job)
    db.commit()
    expired = client.post(f"/archive/exports/{job.id}/generate", headers=auth_headers(admin))
    assert expired.status_code == 409
