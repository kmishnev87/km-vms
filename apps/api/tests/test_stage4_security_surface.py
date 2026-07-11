import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.security import create_access_token, decode_access_token
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers.auth import me
from app.routers.chronology import issue_chronology_media_token
from app.routers.chronology import chronology_file
from app.routers.deps import get_current_user
from app.routers.live import live_playlist
from app.routers.recordings import issue_recording_media_token, RecordingMediaTokenRequest
from app.routers.recordings import stream_recording
from app.routers.settings import system_status
from app.services.audit_log import redact_text
from app.services.media_tokens import create_media_token, validate_media_token
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
)


class FakeRequest:
    headers = {}


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage4_security_")
    root = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    original_app_env = settings.app_env
    original_cors = settings.cors_allowed_origins
    settings.storage_root = str(root / "archive")
    settings.storage_previews = str(root / "previews")
    settings.app_env = "production"
    settings.cors_allowed_origins = ""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        settings.storage_previews = original_storage_previews
        settings.app_env = original_app_env
        settings.cors_allowed_origins = original_cors
        tmp.cleanup()


def add_user(db, *, username="stage4_owner", role="owner", active=True):
    user = User(username=username, full_name=username, password_hash="hash", role=role, is_active=active)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def add_segment(db):
    ensure_archive_roots(db)
    camera = Camera(
        name="stage4_security_camera",
        storage_folder_name="stage4_security_camera",
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="user",
        password_encrypted=None,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=1,
        storage_quota_gb=1,
        status="created",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)

    rel = f"kmvms/recordings/{camera.storage_folder_name}/stage4_security_segment.mkv"
    path = Path(settings.storage_root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stage4")
    started = datetime.utcnow() - timedelta(minutes=5)
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(path),
        relative_path=rel,
        started_at=started,
        ended_at=started + timedelta(seconds=10),
        duration_sec=10,
        size_bytes=path.stat().st_size,
        stream_type="main",
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=datetime.utcnow(),
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        finalized_at=started + timedelta(seconds=10),
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return camera, segment


def recording_resource(segment, action="stream"):
    return {
        "segment_id": segment.id,
        "archive_root_id": segment.archive_root_id,
        "action": action,
    }


def chronology_resource(segment, action="stream"):
    return {
        "segment_id": segment.id,
        "archive_root_id": segment.archive_root_id,
        "action": action,
    }


def test_media_token_issue_and_validate_recording_scope(db):
    user = add_user(db, role="operator")
    _camera, segment = add_segment(db)

    response = issue_recording_media_token(
        RecordingMediaTokenRequest(path=segment.relative_path, action="stream"),
        db=db,
        current_user=user,
    )
    token = response["media_token"]
    validated = validate_media_token(
        db,
        token=token,
        scope="recording",
        resource=recording_resource(segment),
        permission="view_recordings",
    )

    assert validated.username == user.username
    with pytest.raises(HTTPException) as exc:
        validate_media_token(
            db,
            token=token,
            scope="recording",
            resource=recording_resource(segment, "download"),
            permission="view_recordings",
        )
    assert exc.value.status_code == 403


def test_access_tokens_are_explicitly_typed_and_media_tokens_cannot_authenticate_api(db):
    user = add_user(db, role="owner")
    access_token = create_access_token(user.username)
    access_payload = decode_access_token(access_token)
    assert access_payload["typ"] == "access"

    current_user = get_current_user(
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=access_token),
        db=db,
    )
    assert me(current_user).username == user.username

    media_token, _expires = create_media_token(
        user=user,
        scope="live",
        resource={"camera_id": 1, "stream": "main"},
    )
    with pytest.raises(HTTPException) as exc:
        get_current_user(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=media_token),
            db=db,
        )
    assert exc.value.status_code == 401


def test_legacy_missing_type_access_token_is_accepted_only_by_access_decoder(db):
    user = add_user(db, role="owner")
    now = datetime.now(timezone.utc)
    legacy_access = jwt.encode(
        {
            "sub": user.username,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    assert decode_access_token(legacy_access)["sub"] == user.username
    with pytest.raises(HTTPException) as exc:
        validate_media_token(
            db,
            token=legacy_access,
            scope="live",
            resource={"camera_id": 1, "stream": "main"},
            permission="view_live",
        )
    assert exc.value.status_code == 403


def test_media_token_rejects_wrong_scope_expired_and_wrong_permission(db):
    user = add_user(db, role="viewer")
    token, _expires = create_media_token(user=user, scope="live", resource={"camera_id": 1, "stream": "main"})

    with pytest.raises(HTTPException) as exc:
        validate_media_token(
            db,
            token=token,
            scope="recording",
            resource={"path": "x", "action": "stream"},
            permission="view_recordings",
        )
    assert exc.value.status_code == 403

    expired = jwt.encode(
        {
            "typ": "media",
            "sub": user.username,
            "scope": "live",
            "resource": {"camera_id": 1, "stream": "main"},
            "exp": int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        validate_media_token(
            db,
            token=expired,
            scope="live",
            resource={"camera_id": 1, "stream": "main"},
            permission="view_live",
        )
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        validate_media_token(
            db,
            token=token,
            scope="live",
            resource={"camera_id": 1, "stream": "main"},
            permission="view_recordings",
        )
    assert exc.value.status_code == 403


def test_chronology_media_token_is_bound_to_segment_and_archive_root(db):
    user = add_user(db, role="operator")
    camera, segment = add_segment(db)

    response = issue_chronology_media_token(
        camera_id=camera.id,
        rel_path=segment.relative_path,
        db=db,
        current_user=user,
    )
    token = response["media_token"]

    assert validate_media_token(
        db,
        token=token,
        scope="chronology",
        resource=chronology_resource(segment),
        permission="view_timeline",
    )
    with pytest.raises(HTTPException) as exc:
        validate_media_token(
            db,
            token=token,
            scope="chronology",
            resource={**chronology_resource(segment), "archive_root_id": "another-root"},
            permission="view_timeline",
        )
    assert exc.value.status_code == 403


def test_live_media_token_is_bound_to_camera_and_stream(db):
    user = add_user(db, role="viewer")
    token, _expires = create_media_token(user=user, scope="live", resource={"camera_id": 10, "stream": "main"})

    assert validate_media_token(
        db,
        token=token,
        scope="live",
        resource={"camera_id": 10, "stream": "main"},
        permission="view_live",
    )
    for resource in ({"camera_id": 11, "stream": "main"}, {"camera_id": 10, "stream": "sub"}):
        with pytest.raises(HTTPException) as exc:
            validate_media_token(
                db,
                token=token,
                scope="live",
                resource=resource,
                permission="view_live",
            )
        assert exc.value.status_code == 403


def test_media_endpoints_reject_access_tokens_and_accept_only_scoped_media_tokens(db, monkeypatch):
    user = add_user(db, role="owner")
    camera, segment = add_segment(db)
    access_token = create_access_token(user.username)

    playlist_path = Path(settings.storage_previews) / "live" / str(camera.id) / "sub" / "index.m3u8"
    playlist_path.parent.mkdir(parents=True, exist_ok=True)
    playlist_path.write_text("#EXTM3U\n#EXTINF:1.0,\nseg_0.ts\n", encoding="utf-8")
    monkeypatch.setattr("app.routers.live.manager.get_playlist_file", lambda camera_id, stream: playlist_path)

    for call in (
        lambda token: stream_recording(FakeRequest(), path=segment.relative_path, media_token=token, db=db),
        lambda token: chronology_file(camera_id=camera.id, rel_path=segment.relative_path, media_token=token, db=db),
        lambda token: live_playlist(FakeRequest(), camera_id=camera.id, stream="sub", media_token=token, db=db),
    ):
        with pytest.raises(HTTPException) as exc:
            call(access_token)
        assert exc.value.status_code == 403

    recording_token, _ = create_media_token(
        user=user,
        scope="recording",
        resource=recording_resource(segment),
    )
    chronology_token, _ = create_media_token(
        user=user,
        scope="chronology",
        resource=chronology_resource(segment),
    )
    live_token, _ = create_media_token(
        user=user,
        scope="live",
        resource={"camera_id": camera.id, "stream": "sub"},
    )

    assert stream_recording(FakeRequest(), path=segment.relative_path, media_token=recording_token, db=db).status_code == 200
    assert chronology_file(camera_id=camera.id, rel_path=segment.relative_path, media_token=chronology_token, db=db).status_code == 200
    assert live_playlist(FakeRequest(), camera_id=camera.id, stream="sub", media_token=live_token, db=db).status_code == 200


def test_media_endpoints_reject_expired_and_wrong_resource_tokens(db, monkeypatch):
    user = add_user(db, role="owner")
    camera, segment = add_segment(db)
    playlist_path = Path(settings.storage_previews) / "live" / str(camera.id) / "sub" / "index.m3u8"
    playlist_path.parent.mkdir(parents=True, exist_ok=True)
    playlist_path.write_text("#EXTM3U\n#EXTINF:1.0,\nseg_0.ts\n", encoding="utf-8")
    monkeypatch.setattr("app.routers.live.manager.get_playlist_file", lambda camera_id, stream: playlist_path)

    expired_recording = jwt.encode(
        {
            "typ": "media",
            "sub": user.username,
            "scope": "recording",
            "resource": recording_resource(segment),
            "exp": int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    expired_chronology = jwt.encode(
        {
            "typ": "media",
            "sub": user.username,
            "scope": "chronology",
            "resource": chronology_resource(segment),
            "exp": int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    expired_live = jwt.encode(
        {
            "typ": "media",
            "sub": user.username,
            "scope": "live",
            "resource": {"camera_id": camera.id, "stream": "sub"},
            "exp": int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    for call, token in (
        (lambda token: stream_recording(FakeRequest(), path=segment.relative_path, media_token=token, db=db), expired_recording),
        (lambda token: chronology_file(camera_id=camera.id, rel_path=segment.relative_path, media_token=token, db=db), expired_chronology),
        (lambda token: live_playlist(FakeRequest(), camera_id=camera.id, stream="sub", media_token=token, db=db), expired_live),
    ):
        with pytest.raises(HTTPException) as exc:
            call(token)
        assert exc.value.status_code == 401

    wrong_recording, _ = create_media_token(
        user=user,
        scope="recording",
        resource=recording_resource(segment, "download"),
    )
    wrong_chronology, _ = create_media_token(
        user=user,
        scope="chronology",
        resource={**chronology_resource(segment), "archive_root_id": "another-root"},
    )
    wrong_live, _ = create_media_token(
        user=user,
        scope="live",
        resource={"camera_id": camera.id, "stream": "main"},
    )

    for call, token in (
        (lambda token: stream_recording(FakeRequest(), path=segment.relative_path, media_token=token, db=db), wrong_recording),
        (lambda token: chronology_file(camera_id=camera.id, rel_path=segment.relative_path, media_token=token, db=db), wrong_chronology),
        (lambda token: live_playlist(FakeRequest(), camera_id=camera.id, stream="sub", media_token=token, db=db), wrong_live),
    ):
        with pytest.raises(HTTPException) as exc:
            call(token)
        assert exc.value.status_code == 403


def test_public_system_status_is_minimal_after_initialization(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/storage/archive"))
    db.commit()

    response = system_status(db)

    assert response["initialized"] is True
    assert response["setup_required"] is False
    assert "runtime" not in response


def test_setup_required_system_status_remains_available(db):
    db.add(SystemSettings(system_initialized=False, timezone="UTC", language="ru", storage_path="/storage/archive"))
    db.commit()

    response = system_status(db)

    assert response["initialized"] is False
    assert response["setup_required"] is True
    assert response["runtime"]["setup_required"] is True


def test_cors_production_default_is_not_wildcard_with_credentials():
    assert settings.cors_origins() == []
    settings.app_env = "development"
    assert "http://localhost:3000" in settings.cors_origins()


def test_redaction_covers_media_and_auth_tokens_and_rtsp_credentials():
    raw = (
        "Authorization: Bearer abc.def.ghi "
        "/api/live/1/main/index.m3u8?media_token=secret-media "
        "/api/recordings/stream?token=secret-jwt&access_token=secret-access "
        "rtsp://user:password@192.0.2.10/stream"
    )
    redacted = redact_text(raw)

    assert "secret-media" not in redacted
    assert "secret-jwt" not in redacted
    assert "secret-access" not in redacted
    assert "abc.def.ghi" not in redacted
    assert "password@" not in redacted


def test_endpoint_permission_registry_covers_media_token_routes():
    routes = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}

    assert ("POST", "/live/media-token", "view_live") in routes
    assert ("POST", "/recordings/media-token", "view_recordings") in routes
    assert ("POST", "/chronology/media-token", "view_timeline") in routes
