import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.models.camera import Camera
from app.routers.live import _serialize_live_status_item
from app.services.live_engine.ffmpeg import build_hls_command


def make_camera() -> Camera:
    return Camera(
        id=21,
        name="stage21_live_audio",
        storage_folder_name="stage21_live_audio",
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        rtsp_main_url="rtsp://example.invalid/main",
        rtsp_sub_url="rtsp://example.invalid/sub",
        rtsp_transport="tcp",
        recording_mode="always",
        default_live_stream="sub",
        default_record_stream="main",
        status="enabled",
    )


def command_for_audio_mode(mode: str) -> list[str]:
    original = settings.live_audio_mode
    settings.live_audio_mode = mode
    try:
        with tempfile.TemporaryDirectory(prefix="stage21_live_audio_") as tmp:
            return build_hls_command(
                camera=make_camera(),
                stream="sub",
                input_url="rtsp://example.invalid/sub",
                out_dir=Path(tmp),
                mode="fallback_transcode",
            )
    finally:
        settings.live_audio_mode = original


def test_live_audio_mode_none_disables_audio_with_optional_map_preserved():
    cmd = command_for_audio_mode("none")
    assert ["-map", "0:v:0"] == cmd[cmd.index("-map"):cmd.index("-map") + 2]
    assert "0:a?" in cmd
    assert "-an" in cmd
    assert "-c:a" not in cmd


def test_live_audio_mode_aac_transcodes_optional_audio_for_browser_hls():
    cmd = command_for_audio_mode("aac")
    rendered = " ".join(cmd)
    assert "0:a?" in cmd
    assert "-an" not in cmd
    assert "-c:a aac" in rendered
    assert "-ar 44100" in rendered
    assert "-ac 1" in rendered


def test_live_audio_mode_transcode_uses_same_browser_safe_aac_path():
    cmd = command_for_audio_mode("transcode")
    rendered = " ".join(cmd)
    assert "0:a?" in cmd
    assert "-c:a aac" in rendered
    assert "-f hls" in rendered


def test_live_audio_copy_is_supported_but_not_product_default():
    assert settings.live_audio_mode != "copy"
    cmd = command_for_audio_mode("copy")
    rendered = " ".join(cmd)
    assert "0:a?" in cmd
    assert "-c:a copy" in rendered


def test_live_status_audio_fields_are_safe_scalars_only():
    unsafe_item = {
        "camera_id": 21,
        "stream": "sub",
        "running": True,
        "ready": True,
        "status": "ready",
        "audio_mode": "aac",
        "audio_enabled": True,
        "audio_available": True,
        "input_audio_codec": "aac",
        "input_audio_channels": 1,
        "input_audio_sample_rate": 44100,
        "audio_reason": "input_has_audio",
        "audio_debug": "rtsp://user:secret@example.invalid/live",
        "command": "ffmpeg -i rtsp://user:secret@example.invalid/live",
        "stderr_tail": "password=secret",
        "playlist_path": "/storage/previews/live/21/sub/index.m3u8",
        "media_token": "secret-token",
    }
    safe = _serialize_live_status_item(unsafe_item)
    assert safe["audio_mode"] == "aac"
    assert safe["audio_enabled"] is True
    assert safe["audio_available"] is True
    assert safe["input_audio_codec"] == "aac"
    assert safe["input_audio_channels"] == 1
    assert safe["input_audio_sample_rate"] == 44100
    assert safe["audio_reason"] == "input_has_audio"
    rendered = json.dumps(safe, ensure_ascii=False).lower()
    for forbidden in ("rtsp://", "secret", "password", "ffmpeg -", "/storage/", "media_token"):
        assert forbidden not in rendered


def test_live_status_video_only_audio_fact_is_truthful():
    safe = _serialize_live_status_item(
        {
            "camera_id": 21,
            "stream": "sub",
            "running": True,
            "ready": True,
            "audio_mode": "aac",
            "audio_enabled": True,
            "audio_available": False,
            "audio_reason": "input_no_audio",
        }
    )
    assert safe["audio_enabled"] is True
    assert safe["audio_available"] is False
    assert safe["audio_reason"] == "input_no_audio"
