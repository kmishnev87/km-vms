"""Complete ONVIF catalogues and Live deletion/start lifecycle regressions."""
import contextlib
import importlib
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Response

from app.models.camera import Camera
from app.routers import cameras, live as routes
from app.services import onvif_service as onvif
from test_self_recovery_stage1374 import camera, Process
from test_storage_recording_contract import db, add_camera, FakeRequest, actor

live = importlib.import_module("app.services.live_engine.manager")


@pytest.mark.parametrize("channels", [11, 32])
def test_probe_returns_every_source_profile_with_real_metadata_conversion(monkeypatch, channels):
    profiles = []
    for channel in range(1, channels + 1):
        for subtype in (0, 1):
            profiles.append(SimpleNamespace(
                token=f"{channel}-{subtype}", Name="MainStream" if subtype == 0 else "SubStream",
                VideoSourceConfiguration=SimpleNamespace(SourceToken=f"source-{channel}"),
                VideoEncoderConfiguration=SimpleNamespace(
                    Encoding="H264", Resolution=SimpleNamespace(Width=1920 if subtype == 0 else 640, Height=1080 if subtype == 0 else 360),
                    RateControl=SimpleNamespace(FrameRateLimit=25, BitrateLimit=2048), Quality=4,
                ),
            ))
    def get_uri(payload):
        channel, subtype = payload["ProfileToken"].split("-")
        return SimpleNamespace(Uri=f"rtsp://camera.invalid/cam/realmonitor?channel={channel}&subtype={subtype}")
    media = SimpleNamespace(GetProfiles=lambda: profiles, GetStreamUri=get_uri)
    device = SimpleNamespace(devicemgmt=SimpleNamespace(GetDeviceInformation=lambda: SimpleNamespace(Model="dummy-multisource")))
    monkeypatch.setattr(onvif, "_prepare_camera", lambda *args: (device, []))
    monkeypatch.setattr(onvif, "_get_media_service", lambda *args: (media, "http://camera.invalid/onvif/media"))
    result = onvif.probe_onvif_device("camera.invalid", username="dummy", password="dummy")
    assert len(result["profiles"]) == channels * 2
    assert len(result["source_groups"]) == channels
    catalogue = {profile["token"]: profile for profile in result["profiles"]}
    for group in result["source_groups"]:
        assert all(token in catalogue for token in group["profile_tokens"])
        assert catalogue[group["main_profile_token"]]["stream_path"]
        assert catalogue[group["sub_profile_token"]]["video"]["width"] == 640
        assert catalogue[group["main_profile_token"]]["source_id"] == group["id"]
        assert catalogue[group["sub_profile_token"]]["source_id"] == group["id"]


@pytest.mark.parametrize("surface", ["viewer", "token", "playlist", "segment"])
def test_deleted_camera_denied_at_every_live_media_admission(db, monkeypatch, surface):
    row = add_camera(db)
    row.deleted_at = datetime.utcnow()
    row.rtsp_main_url = "rtsp://camera.invalid/main"
    db.commit()
    monkeypatch.setattr(routes.manager, "open_viewer", lambda *args: pytest.fail("deleted camera reached manager"))
    monkeypatch.setattr(routes, "create_media_token", lambda **kwargs: pytest.fail("deleted camera got a token"))
    monkeypatch.setattr(routes, "_authorize_live_media_token", lambda *args, **kwargs: actor("viewer"))
    monkeypatch.setattr(routes, "_serve_live_playlist", lambda *args, **kwargs: pytest.fail("deleted camera served media"))
    payload = routes.LiveViewerPayload(camera_id=row.id, stream="main")
    with pytest.raises(HTTPException) as error:
        if surface == "viewer":
            routes.open_live_viewer(payload, db=db, current_user=actor("viewer"))
        elif surface == "token":
            routes.issue_live_media_token(payload, FakeRequest(), Response(), db=db, current_user=actor("viewer"))
        elif surface == "playlist":
            routes.live_playlist(camera_id=row.id, stream="main", request=FakeRequest(), media_token="dummy", db=db)
        else:
            routes.live_segment(camera_id=row.id, stream="main", filename="segment0.ts", request=FakeRequest(), media_token="dummy", db=db)
    assert error.value.status_code == 404


def test_camera_delete_stops_both_live_roles_without_stopping_other_camera(db, monkeypatch):
    row = add_camera(db)
    row.rtsp_main_url, row.rtsp_sub_url = "/main", "/sub"
    db.commit()
    manager = live.StreamManager()
    monkeypatch.setattr(manager, "_launch_viewer_start", lambda *args: None)
    monkeypatch.setattr(cameras, "live_manager", manager)
    monkeypatch.setattr(cameras, "audit_camera_delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(live.StreamInstance, "_audit_live_event", lambda *args, **kwargs: None)
    viewers = [manager.open_viewer(row, role) for role in ("main", "sub")]
    other = camera()
    other_view = manager.open_viewer(other, "main")
    for instance in manager.streams.values():
        instance.proc = Process()
    stopped = [manager.streams[(row.id, role)].proc for role in ("main", "sub")]
    other_proc = manager.streams[(other.id, "main")].proc
    result = cameras.delete_camera(row.id, FakeRequest(), delete_files=False, operation_id=None, db=db, current_user=actor())
    assert result["ok"] and row.deleted_at is not None
    assert all(process.terminated for process in stopped)
    assert all(not manager.touch_viewer(viewer["viewer_id"]) for viewer in viewers)
    assert manager.touch_viewer(other_view["viewer_id"]) and not other_proc.terminated
    assert row.rtsp_main_url == "/main", "deletion must not erase historical archive identity"


def test_stale_admission_cannot_launch_camera_deleted_before_background_start(monkeypatch, tmp_path):
    manager = live.StreamManager()
    row = camera()
    row.deleted_at = datetime.utcnow()
    monkeypatch.setattr(live.settings, "storage_previews", str(tmp_path))
    monkeypatch.setattr(live, "SessionLocal", lambda: contextlib.nullcontext(SimpleNamespace(get=lambda *args: row)))
    monkeypatch.setattr(live.StreamInstance, "_audit_live_event", lambda *args, **kwargs: None)
    start = Mock()
    monkeypatch.setattr(live.StreamInstance, "start", start)
    viewer = manager.open_viewer(camera(), "sub")
    instance = manager.streams[(1374, "sub")]
    instance.start_thread.join(3)
    assert not instance.start_thread.is_alive()
    start.assert_not_called()
    assert instance.start_cancelled.is_set() and not manager.touch_viewer(viewer["viewer_id"])


def test_background_start_exception_is_redacted_and_schedules_retry(monkeypatch, tmp_path, caplog):
    manager = live.StreamManager()
    monkeypatch.setattr(live.settings, "storage_previews", str(tmp_path))
    monkeypatch.setattr(live, "SessionLocal", lambda: contextlib.nullcontext(SimpleNamespace(get=lambda *args: camera())))
    monkeypatch.setattr(live.StreamInstance, "start", Mock(side_effect=OSError(
        "dummy failure rtsp://dummy:dummy-pass@camera.invalid/main?auth=dummy-query-secret")))
    monkeypatch.setattr(live.StreamInstance, "_audit_live_event", lambda *args, **kwargs: None)
    manager.open_viewer(camera(), "sub")
    instance = manager.streams[(1374, "sub")]
    instance.start_thread.join(3)
    assert not instance.start_thread.is_alive()
    assert instance.retry_count == 1 and instance.next_retry_at is not None
    assert "dummy-query-secret" not in caplog.text and "dummy-pass" not in caplog.text
    assert "background start failed" in caplog.text


def test_delete_during_launch_sets_cancellation_before_late_start_can_run(monkeypatch, tmp_path):
    manager = live.StreamManager()
    entered, proceed = threading.Event(), threading.Event()
    def load_current(*args):
        entered.set()
        assert proceed.wait(3)
        return camera()
    monkeypatch.setattr(live.settings, "storage_previews", str(tmp_path))
    monkeypatch.setattr(live, "SessionLocal", lambda: contextlib.nullcontext(SimpleNamespace(get=load_current)))
    monkeypatch.setattr(live.StreamInstance, "_audit_live_event", lambda *args, **kwargs: None)
    # Keep the real start cancellation gate, but fail if it ever reaches probing.
    monkeypatch.setattr(live.StreamInstance, "_choose_mode", lambda *args, **kwargs: pytest.fail("cancelled start reached probing"))
    manager.open_viewer(camera(), "sub")
    instance = manager.streams[(1374, "sub")]
    assert entered.wait(2)
    try:
        manager.stop_camera(1374)
    finally:
        proceed.set()
        instance.start_thread.join(3)
    assert not instance.start_thread.is_alive()
    assert instance.start_cancelled.is_set() and not manager.viewers
    assert instance.proc is None
