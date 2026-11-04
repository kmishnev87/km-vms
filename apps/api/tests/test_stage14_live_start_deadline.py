from unittest.mock import Mock
import pytest
from test_self_recovery_stage1374 import live, camera, Process, live_module


@pytest.mark.parametrize("progress,partial", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("expired", [False, True])
def test_hardware_partial_output_cannot_extend_startup_forever(live, monkeypatch, progress, partial, expired):
    now = 1000.
    monkeypatch.setattr(live_module.time, "time", lambda: now)
    live.proc = Process()
    live.mode = "hardware_transcode"
    live.camera_source = camera()
    live.started_at = now - 200
    live.start_deadline = now - 100
    live.start_hard_deadline = now - 1 if expired else now + 1
    live.last_ffmpeg_progress_at = now
    if partial:
        live.stream_dir.mkdir(parents=True, exist_ok=True)
        live.playlist_path.write_text("#EXTM3U\n")
    monkeypatch.setattr(live, "_read_log_tail", lambda *a: "")
    monkeypatch.setattr(live, "process_info", lambda: {"running_verified": True})
    monkeypatch.setattr(live, "_detect_progress_locked", lambda *a: None)
    monkeypatch.setattr(live, "_hardware_progress_detected", lambda: progress)
    monkeypatch.setattr(live, "is_ready", lambda: False)
    fallback = Mock(return_value=True)
    monkeypatch.setattr(live, "_retry_next_backend_or_cpu_locked", fallback)
    live.maintain_startup()
    assert fallback.called is expired


def test_ready_video_has_no_startup_time_limit(live, monkeypatch):
    live.start_hard_deadline = 1
    live.camera_source = camera()
    monkeypatch.setattr(live, "is_ready", lambda: True)
    monkeypatch.setattr(live, "process_info", lambda: {"running_verified": True})
    monkeypatch.setattr(live, "_maybe_escalate_cpu_to_hardware_locked", lambda *a: False)
    fallback=Mock()
    monkeypatch.setattr(live, "_retry_next_backend_or_cpu_locked", fallback)
    live.maintain_startup()
    fallback.assert_not_called()
    assert live.ever_ready


def test_brief_readiness_loss_uses_media_staleness_not_old_start_deadline(live, monkeypatch):
    monkeypatch.setattr(live_module.time, "time", lambda: 10000.)
    live.proc=Process()
    live.mode="hardware_transcode"
    live.camera_source=camera()
    live.start_deadline=10.
    live.start_hard_deadline=20.
    live.last_ready_at=9999.
    monkeypatch.setattr(live,"_read_log_tail",lambda *a: "")
    monkeypatch.setattr(live,"process_info",lambda: {"running_verified":True})
    monkeypatch.setattr(live,"_detect_progress_locked",lambda *a:None)
    monkeypatch.setattr(live,"is_ready",lambda:False)
    fallback=Mock()
    monkeypatch.setattr(live,"_retry_next_backend_or_cpu_locked",fallback)
    assert live.maintain_startup() is False
    fallback.assert_not_called()
