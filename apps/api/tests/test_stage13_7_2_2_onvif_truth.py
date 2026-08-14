import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.onvif_service as service
from app.routers.camera_onvif_routes import onvif_config_http_detail


def ns(**values):
    return SimpleNamespace(**values)


class ZeepLike:
    def __init__(self, **values):
        self.__values__ = values


def encoder_config(*, bitrate=4096, wrapped=False, zeep=False):
    values = {
        "token": "cfg-main",
        "Encoding": "H264",
        "Resolution": {"Width": 3840, "Height": 2160},
        "RateControl": {
            "FrameRateLimit": 25,
            "BitrateLimit": bitrate,
            "EncodingInterval": 1,
        },
        "H264": {"GovLength": 50, "H264Profile": "High"},
        "Quality": 5.0,
        "VendorExtension": {"SmartCodec": True, "Watermark": "kept"},
    }
    if zeep:
        value = ZeepLike(
            token="cfg-main",
            Encoding="H264",
            Resolution=ZeepLike(Width=3840, Height=2160),
            RateControl=ZeepLike(
                FrameRateLimit=25,
                BitrateLimit=bitrate,
                EncodingInterval=1,
            ),
            H264=ZeepLike(GovLength=50, H264Profile="High"),
            Quality=5.0,
            VendorExtension=ZeepLike(SmartCodec=True, Watermark="kept"),
        )
    else:
        value = values
    return {"VideoEncoderConfiguration": [value]} if wrapped else value


def encoder_options(*, wrapped=False, zeep=False):
    values = {
        "H264": {
            "ResolutionsAvailable": [
                {"Width": 3840, "Height": 2160},
                {"Width": 1920, "Height": 1080},
            ],
            "FrameRateRange": {"Min": 1, "Max": 25},
            "BitrateRange": {"Min": 32, "Max": 8192},
            "GovLengthRange": {"Min": 1, "Max": 150},
        },
        "QualityRange": {"Min": 1, "Max": 6},
    }
    if zeep:
        value = ZeepLike(
            H264=ZeepLike(
                ResolutionsAvailable=[
                    ZeepLike(Width=3840, Height=2160),
                    ZeepLike(Width=1920, Height=1080),
                ],
                FrameRateRange=ZeepLike(Min=1, Max=25),
                BitrateRange=ZeepLike(Min=32, Max=8192),
                GovLengthRange=ZeepLike(Min=1, Max=150),
            ),
            QualityRange=ZeepLike(Min=1, Max=6),
        )
    else:
        value = values
    return {"Options": [value]} if wrapped else value


def profile(token="main-token"):
    return {
        "token": token,
        "Name": "MainStream",
        "VideoEncoderConfiguration": {"token": "cfg-main"},
    }


class FakeMedia:
    def __init__(
        self,
        *,
        config=None,
        options=None,
        profiles=None,
        config_error=False,
        options_error=False,
        set_error=False,
        mismatch=False,
    ):
        self.config = config if config is not None else encoder_config()
        self.options = options if options is not None else encoder_options()
        self.profiles = profiles or [profile()]
        self.config_error = config_error
        self.options_error = options_error
        self.set_error = set_error
        self.mismatch = mismatch
        self.get_config_calls = []
        self.options_calls = []
        self.set_calls = []

    def GetProfiles(self):
        return self.profiles

    def GetVideoEncoderConfiguration(self, payload):
        self.get_config_calls.append(copy.deepcopy(payload))
        if self.config_error:
            raise RuntimeError("SOAP secret-camera-pass read error")
        return copy.deepcopy(self.config)

    def GetVideoEncoderConfigurationOptions(self, payload):
        self.options_calls.append(copy.deepcopy(payload))
        if self.options_error:
            raise RuntimeError("SOAP secret-camera-pass options error")
        return copy.deepcopy(self.options)

    def SetVideoEncoderConfiguration(self, payload):
        self.set_calls.append(copy.deepcopy(payload))
        if self.set_error:
            raise RuntimeError("SOAP secret-camera-pass refused")
        if not self.mismatch:
            self.config = copy.deepcopy(payload["Configuration"])


def bind_media(monkeypatch, media):
    monkeypatch.setattr(service, "_prepare_camera", lambda *args, **kwargs: (object(), ["http://camera/onvif/media"]))
    monkeypatch.setattr(service, "_get_media_service", lambda cam, candidates: (media, candidates[0]))


def read_config(monkeypatch, media, token="main-token"):
    bind_media(monkeypatch, media)
    return service.get_onvif_profile_config("camera", 80, "operator", "secret-camera-pass", token)


@pytest.mark.parametrize(
    ("config", "options"),
    [
        (encoder_config(), encoder_options()),
        (encoder_config(wrapped=True), encoder_options(wrapped=True)),
        (encoder_config(zeep=True), encoder_options(zeep=True)),
    ],
)
def test_dahua_object_mapping_wrapper_and_zeep_shapes_return_current_truth(monkeypatch, config, options):
    result = read_config(monkeypatch, FakeMedia(config=config, options=options))

    assert result["status"] == "ok"
    assert result["config"] == {
        "codec": "H264",
        "width": 3840,
        "height": 2160,
        "resolution": "3840x2160",
        "fps": 25,
        "bitrate": 4096,
        "iframe_interval": 50,
        "quality": 5.0,
    }
    assert result["supported"]["resolution"]["options"] == ["3840x2160", "1920x1080"]
    assert result["supported"]["bitrate"]["range"] == {"min": 32, "max": 8192}
    assert all(result["supported"][name]["readable"] for name in service.ONVIF_VIDEO_CONFIG_FIELDS)
    assert all(result["supported"][name]["writable"] for name in service.ONVIF_VIDEO_CONFIG_FIELDS)
    assert {"name", "value", "readable", "writable", "options", "range"} <= set(result["supported"]["codec"])


def test_read_failure_options_failure_and_unsupported_are_distinct(monkeypatch):
    read_failed = read_config(monkeypatch, FakeMedia(config_error=True))
    assert read_failed["status"] == "error"
    assert "video_encoder_configuration_read_failed" in read_failed["reason_codes"]
    assert not any(item["writable"] for item in read_failed["supported"].values())

    options_failed = read_config(monkeypatch, FakeMedia(options_error=True))
    assert options_failed["status"] == "error"
    assert options_failed["config"]["bitrate"] == 4096
    assert "video_encoder_options_read_failed" in options_failed["reason_codes"]
    assert not any(item["writable"] for item in options_failed["supported"].values())

    unsupported = read_config(monkeypatch, FakeMedia(options={}))
    assert unsupported["status"] == "unsupported"
    assert unsupported["config"]["codec"] == "H264"
    assert unsupported["supported"]["codec"]["state"] == "unsupported"
    assert not any(item["writable"] for item in unsupported["supported"].values())


def test_present_but_empty_dahua_configuration_is_unavailable_not_unsupported(monkeypatch):
    empty_config = {
        "token": "cfg-main",
        "Name": "VideoEncoderConfig_Channel1_MainStream",
        "Encoding": None,
        "Resolution": None,
        "RateControl": None,
        "H264": None,
        "Quality": None,
    }

    result = read_config(
        monkeypatch,
        FakeMedia(config=empty_config, options=encoder_options()),
    )

    assert result["status"] == "unavailable"
    assert result["current_read"] is True
    assert result["options_read"] is True
    assert not any(item["readable"] for item in result["supported"].values())
    assert not any(item["writable"] for item in result["supported"].values())
    assert "video_encoder_codec_current_unavailable" in result["reason_codes"]


def test_one_field_update_sends_full_vendor_object_and_verifies_exact_profile(monkeypatch):
    media = FakeMedia()
    bind_media(monkeypatch, media)

    result = service.update_onvif_profile(
        "camera", 80, "operator", "secret-camera-pass", "main-token", {"bitrate": 2048}
    )

    assert result["ok"] is True
    assert result["changed_fields"] == ["bitrate"]
    assert result["verification"] == {"status": "matched", "matched": True}
    assert result["config"]["bitrate"] == 2048
    assert media.get_config_calls == [
        {"ConfigurationToken": "cfg-main"},
        {"ConfigurationToken": "cfg-main"},
    ]
    assert all(call["ProfileToken"] == "main-token" for call in media.options_calls)
    sent = media.set_calls[0]["Configuration"]
    assert sent["RateControl"]["BitrateLimit"] == 2048
    assert sent["RateControl"]["FrameRateLimit"] == 25
    assert sent["H264"]["GovLength"] == 50
    assert sent["VendorExtension"] == {"SmartCodec": True, "Watermark": "kept"}


def test_backend_same_value_is_noop_and_does_not_call_set(monkeypatch):
    media = FakeMedia()
    bind_media(monkeypatch, media)

    result = service.update_onvif_profile(
        "camera", 80, "operator", "secret-camera-pass", "main-token", {"bitrate": "4096"}
    )

    assert result["changed"] is False
    assert result["verification"]["status"] == "not_needed"
    assert media.set_calls == []


@pytest.mark.parametrize(
    ("media", "code"),
    [
        (FakeMedia(mismatch=True), "video_encoder_configuration_mismatch"),
        (FakeMedia(set_error=True), "video_encoder_configuration_set_failed"),
    ],
)
def test_update_mismatch_and_camera_refusal_are_truthful_errors(monkeypatch, media, code):
    bind_media(monkeypatch, media)

    with pytest.raises(service.OnvifConfigurationError) as exc:
        service.update_onvif_profile(
            "camera", 80, "operator", "secret-camera-pass", "main-token", {"bitrate": 2048}
        )

    assert exc.value.code == code
    assert "secret-camera-pass" not in str(exc.value)
    assert "SOAP" not in str(exc.value)


def test_exact_profile_token_has_no_name_or_position_fallback(monkeypatch):
    media = FakeMedia(profiles=[profile("main-token"), profile("sub-token")])
    bind_media(monkeypatch, media)

    with pytest.raises(service.OnvifConfigurationError) as exc:
        service.get_onvif_profile_config("camera", 80, "operator", "secret", "missing-token")

    assert exc.value.code == "onvif_profile_not_found"
    assert media.get_config_calls == []


def test_health_profile_selection_is_exact_or_bounded_single_fallback():
    assigned = ns(onvif_profile_token="main-token")
    assert service.select_profile_config_health_target(
        assigned, [{"token": "main-token"}, {"token": "sub-token"}]
    ) == {"profile_token": "main-token", "source": "assigned_main", "reason_codes": []}

    single = service.select_profile_config_health_target(
        ns(onvif_profile_token=None), [{"token": "only-token"}]
    )
    assert single["profile_token"] == "only-token"
    assert single["source"] == "single_profile_fallback"

    ambiguous = service.select_profile_config_health_target(
        ns(onvif_profile_token=None), [{"token": "a"}, {"token": "b"}]
    )
    assert ambiguous["profile_token"] is None
    assert ambiguous["reason_codes"] == ["profile_config_exact_profile_unavailable"]


def health_camera():
    return ns(
        id=7,
        name="Dahua",
        enabled=True,
        deleted_at=None,
        protocol="onvif",
        host="camera",
        port=80,
        username="operator",
        onvif_profile_token="main-token",
        onvif_sub_profile_token="sub-token",
        rtsp_main_url="/main",
        rtsp_sub_url="/sub",
        rtsp_host="camera",
        rtsp_port=554,
        recording_mode="always",
        default_record_stream="main",
    )


@pytest.mark.parametrize(
    ("config_status", "expected"),
    [("ok", "ok"), ("unsupported", "unsupported"), ("unavailable", "error"), ("error", "error")],
)
def test_health_profile_config_domain_uses_actual_read_result(config_status, expected):
    result = service.build_onvif_health_contract(
        health_camera(),
        password="secret",
        profiles_result={"profiles": [{"token": "main-token", "stream_path": "/main"}]},
        profile_config_checked=True,
        profile_config_result={
            "status": config_status,
            "profile_token": "main-token",
            "profile_source": "assigned_main",
            "current_read": config_status != "error",
            "options_read": config_status == "ok",
            "supported": {"bitrate": {"writable": config_status == "ok"}},
            "reason_codes": [] if config_status == "ok" else [f"profile_config_{config_status}"],
        },
        check_performed=True,
    )
    domain = result["compatibility_matrix"]["profile_config_options"]
    assert domain["status"] == expected
    assert domain["evidence_level"] == "real_runtime"
    assert domain["profile_token"] == "main-token"


def test_health_profile_config_not_checked_is_not_optimistic_ok():
    result = service.build_onvif_health_contract(
        health_camera(),
        password="secret",
        profiles_result={"profiles": [{"token": "main-token", "stream_path": "/main"}]},
        check_performed=True,
    )
    assert result["compatibility_matrix"]["profile_config_options"]["status"] == "not_checked"


def test_configuration_errors_are_structured_and_sanitized():
    detail = onvif_config_http_detail(
        service.OnvifConfigurationError(
            "video_encoder_configuration_set_failed",
            "The camera refused the ONVIF encoder configuration update.",
        )
    )
    assert detail["code"] == "video_encoder_configuration_set_failed"
    assert detail["raw_secret_exposed"] is False
    assert "secret-camera-pass" not in str(detail)
