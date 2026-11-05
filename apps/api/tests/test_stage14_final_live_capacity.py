"""F05: actual start paths share atomic capacity, without RTSP or FFmpeg."""
import threading
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.live_engine.manager import StreamManager
from app.services.live_engine import stream_instance as live


class Process:
    pid = 424242
    code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    def kill(self):
        self.code = -9

    def wait(self, **kwargs):
        self.code = 0
        return self.code


def camera(identity):
    return SimpleNamespace(id=identity, name=f"synthetic-{identity}", rtsp_main_url="rtsp://camera.invalid/main",
                           rtsp_sub_url="rtsp://camera.invalid/sub", rtsp_transport="tcp")


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_previews", str(tmp_path))
    monkeypatch.setattr(settings, "live_max_concurrent_transcodes", 1)
    monkeypatch.setattr(live, "inspect_input_url", lambda *args: None)
    monkeypatch.setattr(live, "build_hls_command", lambda **kwargs: ["ffmpeg", "-i", kwargs["input_url"]])
    monkeypatch.setattr(live.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(live.StreamInstance, "_audit_live_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(live.StreamInstance, "is_running", lambda self: self.proc is not None and self.proc.poll() is None)
    monkeypatch.setattr(live.StreamInstance, "snapshot", lambda self, **kwargs: {"status": self.status, "mode": self.mode})
    def choose(self, camera, input_url, force_mode=None, force_hw_backend=None):
        if getattr(self, "probe_barrier", None):
            self.probe_barrier.wait(timeout=3)
        return force_mode or ("hardware_transcode" if force_hw_backend else getattr(self, "planned_mode", "fallback_transcode"))
    monkeypatch.setattr(live.StreamInstance, "_choose_mode", choose)
    return StreamManager()


def test_two_parallel_transcodes_admit_exactly_one_not_zero_or_two(manager):
    barrier = threading.Barrier(2)
    instances = [manager._get_stream_locked(index, "main") for index in (1, 2)]
    for instance in instances:
        instance.probe_barrier = barrier
    results = []
    threads = [threading.Thread(target=lambda item=item: results.append(item.start(camera(item.camera_id)))) for item in instances]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4)
        assert not thread.is_alive()
    assert len(results) == 2
    assert sum(result.get("ok") is True for result in results) == 1
    assert sum(result.get("error_code") == "resource_limit" for result in results) == 1
    assert len(manager.transcode_reservations) == 1
    manager.stop_all_streams()
    assert manager.transcode_reservations == {}


def test_copy_does_not_use_capacity_but_fallback_cannot_bypass_it(manager):
    transcode = manager._get_stream_locked(1, "main")
    copy = manager._get_stream_locked(2, "sub")
    assert transcode.start(camera(1))["ok"]
    copy.planned_mode = "copy"
    assert copy.start(camera(2))["ok"]
    assert len(manager.transcode_reservations) == 1
    copy.stop("copy_failed")
    fallback = copy.start(camera(2), force_mode="fallback_transcode", transcode_allowed=True)
    assert fallback["error_code"] == "resource_limit"
    assert transcode.proc.poll() is None
    transcode.stop("client_stop")
    assert copy.start(camera(2), force_hw_backend="vaapi")["ok"]
    assert manager.transcode_reservations == {(2, "sub"): copy}
    manager.stop_all_streams()


def test_failed_process_start_releases_capacity_and_old_owner_cannot_release_new(manager, monkeypatch):
    old = manager._get_stream_locked(1, "main")
    monkeypatch.setattr(live.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic launch failure")))
    assert not old.start(camera(1))["ok"]
    assert manager.transcode_reservations == {}
    monkeypatch.setattr(live.subprocess, "Popen", lambda *args, **kwargs: Process())
    assert old.start(camera(1))["ok"]
    old.proc.code = 1
    old._mark_process_exit_locked()
    assert manager.transcode_reservations == {}
    manager.streams.pop((1, "main"))
    new = manager._get_stream_locked(1, "main")
    assert new.start(camera(1))["ok"]
    old.release_transcode()
    assert manager.transcode_reservations == {(1, "main"): new}
    assert not old.acquire_transcode()
    manager.stop_all_streams()


def test_unlimited_and_cancellation_keep_original_behavior(manager, monkeypatch):
    monkeypatch.setattr(settings, "live_max_concurrent_transcodes", 0)
    for identity in range(1, 4):
        instance = manager._get_stream_locked(identity, "main")
        assert instance.start(camera(identity))["ok"]
    cancelled = manager._get_stream_locked(4, "main")
    cancelled.viewer_managed = True
    cancelled.start_cancelled.set()
    assert cancelled.start(camera(4))["error_code"] == "client_stop"
    assert len(manager.transcode_reservations) == 3
    manager.stop_all_streams()
    assert manager.transcode_reservations == {}
