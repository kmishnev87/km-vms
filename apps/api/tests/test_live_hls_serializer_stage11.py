import json
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER
from app.core.security import create_access_token
from app.db.session import Base, get_db
from app.main import app
from app.models.camera import Camera
from app.models.user import User
from app.services.live_engine.ffmpeg import command_text, mask_rtsp_credentials
from app.services.live_engine import manager as live_manager
from app.services.media_tokens import create_media_token


FORBIDDEN_KEYS = {
    "debug",
    "internal",
    "private",
    "_raw",
    "raw",
    "trace",
    "stack",
    "exception",
    "rtsp_url",
    "rtsp",
    "credentials",
    "username",
    "password",
    "secret",
    "authorization",
    "cookie",
    "ffmpeg_cmd",
    "command",
    "args",
    "env",
    "pid",
    "pid_cmdline",
    "process",
    "stdout",
    "stderr",
    "stderr_tail",
    "log",
    "file_path",
    "segment_path",
    "playlist_path",
    "storage_path",
    "root",
}
FORBIDDEN_TEXT = (
    "rtsp://",
    "Authorization",
    "Bearer ",
    "password",
    "secret",
    "/Volume",
    "/storage/",
    "/dev/",
    "ffmpeg -",
)
LIVE_STATUS_ALLOWED = {
    "stream_key",
    "camera_id",
    "stream",
    "stream_type",
    "running",
    "ready",
    "status",
    "mode",
    "selected_mode",
    "input_codec",
    "input_resolution",
    "input_fps",
    "output_fps",
    "browser_compatible",
    "reason_for_transcode",
    "high_cpu_risk",
    "resource_limit",
    "viewers",
    "uptime_seconds",
    "idle_seconds",
    "startup_elapsed_seconds",
    "speed_state",
    "last_fps",
    "last_speed",
    "state_changed_at",
    "last_state_transition",
    "playlist_exists",
    "segment_count",
    "failure_reason",
    "safe_failure_reason",
}


@pytest.fixture
def api_db():
    tmp = tempfile.TemporaryDirectory(prefix="stage11_live_hls_")
    root = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    settings.storage_root = str(root / "archive")
    settings.storage_previews = str(root / "previews")

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
        username=username or f"stage11_live_{role}",
        full_name=f"stage11 live {role}",
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


def add_camera(db):
    camera = Camera(
        name="stage11_live_camera",
        storage_folder_name="stage11_live_camera",
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        rtsp_main_url="/main",
        rtsp_sub_url="/sub",
        recording_mode="always",
        default_live_stream="sub",
        default_record_stream="main",
        status="enabled",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def assert_no_forbidden(value):
    rendered = json.dumps(value, ensure_ascii=False).lower()
    for key in FORBIDDEN_KEYS:
        assert f'"{key.lower()}"' not in rendered
    for text in FORBIDDEN_TEXT:
        assert text.lower() not in rendered


def unsafe_manager_item(camera_id=1):
    unsafe_rtsp = "".join(["rt", "sp://user:se", "cret@example.invalid/live"])
    unsafe_command = "".join(["ff", "mpeg -i ", unsafe_rtsp])
    unsafe_password = "".join(["pass", "word=se", "cret"])
    unsafe_storage_path = "".join(["/sto", "rage/previews/live/1/sub/index.m3u8"])
    return {
        "stream_key": f"{camera_id}_sub",
        "camera_id": camera_id,
        "stream": "sub",
        "stream_type": "sub",
        "running": True,
        "ready": False,
        "status": "starting",
        "mode": "transcode",
        "selected_mode": "hardware_transcode",
        "input_codec": "h264",
        "input_resolution": "1920x1080",
        "input_fps": 25,
        "output_fps": 20,
        "browser_compatible": True,
        "reason_for_transcode": "browser_compatibility",
        "high_cpu_risk": False,
        "viewers": 1,
        "uptime_seconds": 5,
        "idle_seconds": 0,
        "startup_elapsed_seconds": 5,
        "speed_state": "normal",
        "last_fps": 24.5,
        "last_speed": 1.0,
        "playlist_exists": False,
        "segment_count": 0,
        "failure_reason": "startup_timeout_no_hls",
        "stop_reason": "traceback in /dev/dri ffmpeg failure",
        "last_error": unsafe_command,
        "stderr_tail": unsafe_password,
        "command": unsafe_command,
        "pid": 123,
        "pid_cmdline": "ffmpeg secret",
        "playlist_path": unsafe_storage_path,
        "viewer_ids": ["private-viewer-id"],
        "viewer_sessions": [{"id": "private-viewer-id"}],
    }


def test_live_status_response_is_allowlisted_and_safe(client, api_db, monkeypatch):
    db, _root = api_db
    user = add_user(db, role=ROLE_OPERATOR)
    camera = add_camera(db)
    monkeypatch.setattr(live_manager, "status", lambda camera_id=None, stream=None: [unsafe_manager_item(camera.id)])

    response = client.get(f"/live/status?camera_id={camera.id}&stream=sub", headers=auth_headers(user))
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert set(item) <= LIVE_STATUS_ALLOWED
    assert item["camera_id"] == camera.id
    assert item["running"] is True
    assert item["ready"] is False
    assert item["safe_failure_reason"] == "startup_timeout_no_hls"
    assert_no_forbidden(body)


def test_live_viewer_response_and_errors_do_not_expose_debug(client, api_db, monkeypatch):
    db, _root = api_db
    user = add_user(db, role=ROLE_OPERATOR)
    camera = add_camera(db)

    def open_ok(camera_arg, stream):
        return {
            "ok": True,
            "viewer_id": "viewer-safe-id",
            "stream_url": f"/api/live/{camera_arg.id}/{stream}/index.m3u8",
            **unsafe_manager_item(camera_arg.id),
        }

    monkeypatch.setattr(live_manager, "open_viewer", open_ok)
    response = client.post("/live/viewers", json={"camera_id": camera.id, "stream": "sub"}, headers=auth_headers(user))
    assert response.status_code == 200
    body = response.json()
    assert body["viewer_id"] == "viewer-safe-id"
    assert body["stream_url"].endswith("/index.m3u8")
    assert set(body) <= (LIVE_STATUS_ALLOWED | {"ok", "viewer_id", "stream_url", "recoverable_start_error"})
    assert_no_forbidden(body)

    monkeypatch.setattr(
        live_manager,
        "open_viewer",
        lambda camera_arg, stream: {
            "ok": False,
            "error_code": "resource_limit",
            "error": "".join(["ff", "mpeg -i rt", "sp://user:se", "cret@example.invalid/live"]),
            "debug": unsafe_manager_item(camera_arg.id),
        },
    )
    failed = client.post("/live/viewers", json={"camera_id": camera.id, "stream": "sub"}, headers=auth_headers(user))
    assert failed.status_code == 503
    detail = failed.json()["detail"]
    assert detail == {"message": "Live stream could not be started", "code": "resource_limit"}
    assert_no_forbidden(detail)


def test_live_debug_route_is_sanitized_even_for_admin(client, api_db, monkeypatch):
    db, _root = api_db
    user = add_user(db, role=ROLE_ADMIN)
    camera = add_camera(db)
    monkeypatch.setattr(
        live_manager,
        "debug",
        lambda camera_id=None, stream=None: {
            "items": [unsafe_manager_item(camera.id)],
            "count": 1,
            "viewers": [{"id": "private-viewer-id", "camera_id": camera.id}],
            "viewers_count": 1,
            "hardware_capabilities": {
                "hardware_accel_available": True,
                "docker_device_access_ok": True,
                "hardware_misconfigured": False,
                "available_backends": ["qsv", "vaapi"],
                "render_device": "/dev/dri/renderD128",
            },
        },
    )

    response = client.get("/live/debug", headers=auth_headers(user))
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["viewers_count"] == 1
    assert "viewers" not in body
    assert set(body["items"][0]) <= LIVE_STATUS_ALLOWED
    assert_no_forbidden(body)


def test_live_media_token_and_hls_access_contract(client, api_db):
    db, root = api_db
    operator = add_user(db, role=ROLE_OPERATOR)
    viewer = add_user(db, role=ROLE_VIEWER, username="stage11_live_viewer")
    camera = add_camera(db)
    stream_dir = root / "previews" / "live" / str(camera.id) / "sub"
    stream_dir.mkdir(parents=True)
    (stream_dir / "index.m3u8").write_text("#EXTM3U\nseg_1.ts\n", encoding="utf-8")
    (stream_dir / "seg_1.ts").write_bytes(b"stage11")

    assert client.post("/live/media-token", json={"camera_id": camera.id, "stream": "sub"}).status_code == 401
    assert client.post("/live/media-token", json={"camera_id": camera.id, "stream": "sub"}, headers=auth_headers(viewer)).status_code == 200

    token_response = client.post("/live/media-token", json={"camera_id": camera.id, "stream": "sub"}, headers=auth_headers(operator))
    assert token_response.status_code == 200
    token_body = token_response.json()
    assert set(token_body) == {"media_token", "token_type", "expires_at", "expires_in"}
    assert token_body["token_type"] == "media"
    assert isinstance(token_body["expires_in"], int)

    assert client.get(f"/live/{camera.id}/sub/index.m3u8").status_code == 401
    playlist = client.get(f"/live/{camera.id}/sub/index.m3u8", params={"media_token": token_body["media_token"]})
    assert playlist.status_code == 200
    assert "/api/live/" in playlist.text

    segment = client.get(f"/live/{camera.id}/sub/seg_1.ts", params={"media_token": token_body["media_token"]})
    assert segment.status_code == 200

    other = Camera(
        name="stage11_other_live_camera",
        storage_folder_name="stage11_other_live_camera",
        enabled=True,
        protocol="rtsp",
        host="127.0.0.2",
        port=554,
    )
    db.add(other)
    db.commit()
    db.refresh(other)
    cross = client.get(f"/live/{other.id}/sub/index.m3u8", params={"media_token": token_body["media_token"]})
    assert cross.status_code == 403

    wrong_scope, _expires = create_media_token(user=operator, scope="recordings", resource={"camera_id": camera.id, "stream": "sub"})
    assert client.get(f"/live/{camera.id}/sub/index.m3u8", params={"media_token": wrong_scope}).status_code == 403


def test_live_close_touch_responses_do_not_echo_viewer_ids(client, api_db, monkeypatch):
    db, _root = api_db
    user = add_user(db, role=ROLE_OPERATOR)
    monkeypatch.setattr(live_manager, "close_viewer", lambda viewer_id: True)
    monkeypatch.setattr(live_manager, "touch_viewer", lambda viewer_id: True)

    closed = client.delete("/live/viewers/private-viewer-id", headers=auth_headers(user))
    touched = client.post("/live/viewers/private-viewer-id/touch", headers=auth_headers(user))
    assert closed.status_code == 200
    assert touched.status_code == 200
    assert closed.json() == {"ok": True, "closed": True}
    assert touched.json() == {"ok": True, "touched": True}


def test_live_engine_log_sanitizers_remove_raw_rtsp_urls():
    raw_url = "".join(["rt", "sp://user:se", "cret@example.invalid/live"])
    assert mask_rtsp_credentials(raw_url) == "[rtsp-url-redacted]"
    rendered = command_text(["ffmpeg", "-i", raw_url, "-f", "hls", "index.m3u8"], raw_url)
    assert "".join(["rt", "sp://"]) not in rendered
    assert "".join(["se", "cret"]) not in rendered
    assert "[rtsp-url-redacted]" in rendered
