import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.camera import Camera
from app.routers.cameras import apply_profile_assignments, assemble_rtsp_url, build_test_url
from app.services.live_engine_v2.ffmpeg import choose_input_url as live_choose_input_url
from app.services.onvif_service import _profile_to_dict, rtsp_display_uri, rtsp_path_from_uri


def obj(**kwargs):
    return SimpleNamespace(**kwargs)


class FakeMedia:
    def __init__(self, stream_uri):
        self.stream_uri = stream_uri

    def GetVideoEncoderConfiguration(self, payload):
        return obj(
            token=payload["ConfigurationToken"],
            Encoding="H264",
            H264Profile="High",
            Quality=4,
            Resolution=obj(Width=2560, Height=1440),
            RateControl=obj(FrameRateLimit=25, BitrateLimit=4096, EncodingInterval=50),
        )

    def GetStreamUri(self, payload):
        return obj(Uri=self.stream_uri)


class FakeQuery:
    def __init__(self, camera):
        self.camera = camera

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.camera


class FakeDb:
    def __init__(self, camera):
        self.camera = camera

    def query(self, model):
        return FakeQuery(self.camera)


def camera(**overrides):
    rtsp_scheme = "rtsp"
    data = {
        "id": 22,
        "name": "Stage2",
        "storage_folder_name": "stage2",
        "enabled": True,
        "protocol": "onvif",
        "host": "onvif.example.test",
        "port": 20080,
        "username": "operator",
        "password_encrypted": None,
        "rtsp_main_url": f"{rtsp_scheme}://operator:" + "secret" + "@rtsp.example.test:1554/main",
        "rtsp_sub_url": f"{rtsp_scheme}://operator:" + "secret" + "@rtsp.example.test:1554/sub",
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 1554,
        "rtsp_transport": "tcp",
        "onvif_profile_token": "main-token",
        "onvif_channel_id": None,
        "recording_mode": "always",
        "default_live_stream": "sub",
        "default_record_stream": "main",
        "segment_minutes": 5,
        "retention_days": 30,
        "storage_quota_gb": 50,
        "status": "created",
        "last_error": None,
    }
    data.update(overrides)
    return Camera(**data)


def test_profile_summary_is_stable_sanitized_and_uses_reachable_override():
    profile = obj(
        token="main-token",
        Name="Main",
        VideoEncoderConfiguration=obj(token="vec-main"),
        AudioEncoderConfiguration=obj(Encoding="AAC", SampleRate=8000, Channels=1),
    )
    rtsp_scheme = "rtsp"
    media = FakeMedia(f"{rtsp_scheme}://internal:" + "camera-pass" + "@192.168.1.50:554/main?profile=1")

    item = _profile_to_dict(
        profile,
        media,
        username="operator",
        password="camera-pass",
        host="onvif.example.test",
        port=20080,
        rtsp_host="rtsp.example.test",
        rtsp_port=1554,
    )

    assert item["token"] == "main-token"
    assert item["video"]["codec"] == "H264"
    assert item["video"]["width"] == 2560
    assert item["video"]["height"] == 1440
    assert item["video"]["fps"] == 25
    assert item["video"]["bitrate_limit"] == 4096
    assert item["video"]["iframe_interval"] == 50
    assert item["stream_path"] == "/main?profile=1"
    assert item["stream_uri"] == "rtsp://rtsp.example.test:1554/main?profile=1"
    assert item["rtsp_reachable"]["source"] == "user_reachable"
    assert "raw_stream_uri" not in item
    assert "camera-pass" not in str(item)
    assert "operator:camera-pass" not in str(item)


def test_missing_profile_fields_are_truthful_not_fake_defaults():
    profile = obj(token="sub-token", Name="Sub", VideoEncoderConfiguration=None, AudioEncoderConfiguration=None)
    media = FakeMedia(None)

    item = _profile_to_dict(profile, media, None, None, "onvif.example.test", 80)

    assert item["video"]["codec"] is None
    assert item["video"]["width"] is None
    assert item["video"]["bitrate_type"] is None
    assert item["rtsp_ready"] is False
    assert item["video_config_state"] == "unavailable"


def test_get_stream_uri_path_extraction_rewrites_host_without_credentials():
    rtsp_scheme = "rtsp"
    uri = f"{rtsp_scheme}://user:" + "secret" + "@internal.lan:554/cam/realmonitor?channel=1&subtype=0"

    assert rtsp_path_from_uri(uri) == "/cam/realmonitor?channel=1&subtype=0"
    assert (
        rtsp_display_uri(uri, override_host="reachable.example.test", override_port=15540)
        == "rtsp://reachable.example.test:15540/cam/realmonitor?channel=1&subtype=0"
    )
    assert "secret" not in rtsp_display_uri(uri, override_host="reachable.example.test", override_port=15540)


def test_main_and_sub_assignments_are_returned_from_saved_stream_contract():
    cam = camera()
    data = {
        "profiles": [
            {"token": "main-token", "name": "Main", "stream_path": "/main"},
            {"token": "sub-token", "name": "Sub", "stream_path": "/sub"},
        ],
    }

    result = apply_profile_assignments(data, cam)

    assert result["profiles"][0]["assigned_roles"] == ["main"]
    assert result["profiles"][0]["assigned_role"] == "main"
    assert result["profiles"][1]["assigned_roles"] == ["sub"]
    assert result["profiles"][1]["assigned_role"] == "sub"
    assert result["assignments"]["main"]["profile_token"] == "main-token"
    assert result["assignments"]["sub"]["profile_token"] == "sub-token"
    assert "secret" not in str(result)


def test_manual_rtsp_full_url_is_preserved():
    rtsp_scheme = "rtsp"
    url = f"{rtsp_scheme}://user:" + "secret" + "@manual.example.test:8554/live"

    assert assemble_rtsp_url("other.example.test", 554, "user", "new-secret", url) == url


def test_camera_test_url_uses_saved_main_stream_from_camera_id():
    cam = camera(rtsp_main_url="/main", rtsp_sub_url="/sub")

    assert build_test_url({"camera_id": cam.id, "protocol": "onvif"}, db=FakeDb(cam)).endswith("/main")


def test_camera_test_url_uses_saved_sub_stream_when_main_is_empty():
    cam = camera(rtsp_main_url=None, rtsp_sub_url="/sub")

    assert build_test_url({"camera_id": cam.id, "protocol": "onvif"}, db=FakeDb(cam)).endswith("/sub")


def test_camera_test_url_missing_saved_streams_returns_none_safely():
    cam = camera(rtsp_main_url=None, rtsp_sub_url=None)

    assert build_test_url({"camera_id": cam.id, "protocol": "onvif"}, db=FakeDb(cam)) is None


def test_live_uses_selected_stream_contract_with_fallback():
    cam = camera()

    assert live_choose_input_url(cam, "sub") == cam.rtsp_sub_url
    assert live_choose_input_url(cam, "main") == cam.rtsp_main_url
    assert live_choose_input_url(camera(rtsp_sub_url=None), "sub").endswith("/main")


def test_recorder_resolver_uses_default_record_stream_contract():
    text = Path("apps/recorder/main.py").read_text(encoding="utf-8")

    assert "default_record_stream" in text
    assert "row.rtsp_sub_url or row.rtsp_main_url" in text
    assert "row.rtsp_main_url or row.rtsp_sub_url" in text
