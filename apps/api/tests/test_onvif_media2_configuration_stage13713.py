"""Protocol regressions: real wire shapes, not only invented SDK objects."""
import copy
from types import SimpleNamespace as NS

import pytest
import requests
from lxml import etree as E

from app.services import onvif_service as ov
from app.services.onvif_media2 import (
    ADDRESS_NS, MEDIA1_NS, MEDIA2_NS, SCHEMA_NS, SOAP_NS,
    EncoderConfiguration, Media2Error, Media2Service, MediaObject, xml_object,
)


def configuration(token="encoder-opaque", **attrs):
    node = E.fromstring(f'''<m:Configurations xmlns:m="{MEDIA2_NS}" xmlns:t="{SCHEMA_NS}"
        xmlns:v="urn:vendor:test" token="{token}" GovLength="25" Profile="Main" v:Keep="yes">
        <t:Name>Example stream</t:Name><t:UseCount>1</t:UseCount><t:Encoding>H265</t:Encoding>
        <t:Resolution><t:Width>1920</t:Width><t:Height>1080</t:Height></t:Resolution>
        <t:RateControl ConstantBitRate="false"><t:FrameRateLimit>29.97</t:FrameRateLimit>
        <t:BitrateLimit>2048</t:BitrateLimit></t:RateControl>
        <t:Quality>4</t:Quality><v:Extension><v:Untouched>original</v:Untouched></v:Extension>
        </m:Configurations>''')
    for name, value in attrs.items():
        node.set(name, str(value))
    return EncoderConfiguration(node)


def options():
    return [
        {"Encoding": "H264", "ResolutionsAvailable": [{"Width": "1920", "Height": "1080"}],
         "FrameRatesSupported": "29.97 25 15 5", "BitrateRange": {"Min": "64", "Max": "8192"},
         "QualityRange": {"Min": "1", "Max": "6"}, "GovLengthRange": "1 150",
         "ProfilesSupported": "Main High", "ConstantBitRateSupported": "true"},
        {"Encoding": "H265", "ResolutionsAvailable": [{"Width": "1920", "Height": "1080"}],
         "FrameRatesSupported": "29.97 25 15 5", "BitrateRange": {"Min": "128", "Max": "4096"},
         "QualityRange": {"Min": "1", "Max": "6"}, "GovLengthRange": "1 150",
         "ProfilesSupported": "Main", "ConstantBitRateSupported": "true"},
    ]


class Response:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, *args):
        yield self.content


def soap(operation, inner=""):
    return f'<s:Envelope xmlns:s="{SOAP_NS}" xmlns:m="{MEDIA2_NS}" xmlns:t="{SCHEMA_NS}"><s:Body><m:{operation}Response>{inner}</m:{operation}Response></s:Body></s:Envelope>'.encode()


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, address, **kwargs):
        self.calls.append((address, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def service(response):
    session = Session(response)
    wsse = NS(apply=lambda envelope, headers: None)
    return Media2Service("http://camera.test/onvif/media2", "test", "synthetic-secret", wsse=wsse, session=session), session


def test_media2_attributes_fractional_fps_and_discrete_options_are_readable():
    cfg = configuration()
    assert ov._config_values(cfg) == {"codec": "H265", "width": 1920, "height": 1080,
        "resolution": "1920x1080", "fps": 29.97, "bitrate": 2048, "iframe_interval": 25,
        "quality": 4, "bitrate_mode": "VBR", "codec_profile": "Main"}
    fields = ov._supported_video_fields(None, cfg, options=options())
    assert all(fields[name]["writable"] for name in ov.ONVIF_VIDEO_CONFIG_FIELDS)
    assert fields["fps"]["options"] == [29.97, 25, 15, 5]
    assert fields["iframe_interval"]["range"] == {"min": 1, "max": 150}
    assert fields["bitrate"]["range"] == {"min": 128, "max": 4096}
    assert fields["codec_profile"]["options"] == ["Main"]


def test_media1_extension_augments_options_instead_of_disabling_fields():
    opts = {"QualityRange": {"Min": 1, "Max": 6}, "H264": {
        "FrameRateRange": {"Min": 1, "Max": 25}, "GovLengthRange": {"Min": 1, "Max": 150},
        "ResolutionsAvailable": [{"Width": 1920, "Height": 1080}]},
        "Extension": {"H264": {"FrameRateRange": {"Min": 1, "Max": 25},
                                 "BitrateRange": {"Min": 32, "Max": 4096}}}}
    selected = ov._encoding_options(opts, "H264")
    assert ov._single_range(selected, "FrameRateRange") == {"min": 1, "max": 25}
    assert ov._single_range(selected, "BitrateRange") == {"min": 32, "max": 4096}


def test_encoding_interval_is_never_misrepresented_or_written_as_gop():
    cfg = {"Encoding": "H264", "RateControl": {"FrameRateLimit": 25, "EncodingInterval": 2}}
    assert ov._config_values(cfg)["iframe_interval"] is None
    with pytest.raises(ov.OnvifConfigurationError):
        ov._apply_video_config_diff(cfg, {"iframe_interval": 50})
    assert cfg["RateControl"]["EncodingInterval"] == 2


def test_set_preserves_vendor_xml_and_uses_media2_attributes_without_media1_flags():
    media, session = service(Response(soap("SetVideoEncoderConfiguration")))
    cfg = configuration()
    changed = copy.deepcopy(cfg)
    ov._apply_video_config_diff(changed, {"fps": 12.5, "iframe_interval": 30, "bitrate_mode": "CBR", "bitrate": 1024})
    media.SetVideoEncoderConfiguration({"Configuration": changed, "ForcePersistence": True})
    envelope = E.fromstring(session.calls[0][1]["data"])
    sent = envelope.find(f'{{{SOAP_NS}}}Body/{{{MEDIA2_NS}}}SetVideoEncoderConfiguration/{{{MEDIA2_NS}}}Configuration')
    assert sent.get("GovLength") == "30"
    assert sent.get("{urn:vendor:test}Keep") == "yes"
    assert sent.findtext('{urn:vendor:test}Extension/{urn:vendor:test}Untouched') == "original"
    assert sent.find(f'{{{SCHEMA_NS}}}RateControl').get("ConstantBitRate") == "true"
    assert sent.findtext(f'{{{SCHEMA_NS}}}RateControl/{{{SCHEMA_NS}}}FrameRateLimit') == "12.5"
    assert b"ForcePersistence" not in session.calls[0][1]["data"]
    assert cfg["GovLength"] == "25"
    assert session.calls[0][1]["allow_redirects"] is False


def test_exact_encoder_token_not_first_or_lookalike():
    inner = E.tostring(configuration("wrong").xml, encoding="unicode")
    media, _ = service(Response(soap("GetVideoEncoderConfigurations", inner)))
    with pytest.raises(Media2Error, match="exact requested encoder"):
        media.GetVideoEncoderConfiguration({"ConfigurationToken": "right"})


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_error_does_not_leak_request_or_credentials(status):
    media, _ = service(Response(b"sensitive echo", status))
    with pytest.raises(Media2Error) as exc:
        media.GetProfiles()
    assert exc.value.code == "onvif_authentication_failed"
    assert "synthetic-secret" not in str(exc.value)


@pytest.mark.parametrize("response,code", [
    (Response(b"<html>not SOAP</html>"), "onvif_media_response_invalid"),
    (Response(b"<!DOCTYPE x [<!ENTITY y 'expanded'>]><x>&y;</x>"), "onvif_media_response_invalid"),
    (Response(soap("AnotherOperation")), "onvif_media_response_invalid"),
    (requests.Timeout("synthetic-secret"), "onvif_media_timeout"),
    (requests.ConnectionError("synthetic-secret"), "onvif_media_unreachable"),
])
def test_bad_reply_and_transport_errors_are_bounded_and_safe(response, code):
    media, _ = service(response)
    with pytest.raises(Media2Error) as exc:
        media.GetProfiles()
    assert exc.value.code == code
    assert "synthetic-secret" not in str(exc.value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", True, "12x"])
def test_invalid_numbers_are_not_forwarded_to_camera(value):
    fields = ov._supported_video_fields(None, configuration(), options=options())
    with pytest.raises(ValueError):
        ov._validate_profile_config_request({"bitrate": value}, fields)


def test_fractional_fps_is_not_truncated_and_unsupported_discrete_fps_is_rejected():
    fields = ov._supported_video_fields(None, configuration(), options=options())
    assert ov._validate_profile_config_request({"fps": 29.97}, fields) == {"fps": 29.97}
    with pytest.raises(ValueError):
        ov._validate_profile_config_request({"fps": 24}, fields)


def test_distinct_resolution_options_keep_their_own_frame_rates():
    opts = options()
    opts.append({**opts[1], "ResolutionsAvailable": [{"Width": "3840", "Height": "2160"}], "FrameRatesSupported": "15 5"})
    fields = ov._supported_video_fields(None, configuration(), options=opts, resolution_override="3840x2160")
    assert fields["resolution"]["options"] == ["1920x1080", "3840x2160"]
    assert fields["fps"]["options"] == [15, 5]


def test_xml_repeated_resolutions_are_kept_as_list_and_vendor_names_do_not_override_standard():
    node = E.fromstring(f'<m:Options xmlns:m="{MEDIA2_NS}" xmlns:t="{SCHEMA_NS}" xmlns:v="urn:test"><t:Encoding>H265</t:Encoding><v:Encoding>wrong</v:Encoding><t:ResolutionsAvailable><t:Width>1920</t:Width></t:ResolutionsAvailable><t:ResolutionsAvailable><t:Width>640</t:Width></t:ResolutionsAvailable></m:Options>')
    parsed = xml_object(node)
    assert parsed["Encoding"] == "H265"
    assert len(parsed["ResolutionsAvailable"]) == 2


class FakeMedia:
    protocol = "media2"

    def __init__(self, *, lost_reply=False, delayed=False, refuse=False):
        self.config = configuration()
        self.lost_reply = lost_reply
        self.delayed = delayed
        self.refuse = refuse
        self.set_calls = 0
        self.reads_after_set = 0

    def GetProfiles(self):
        return [MediaObject(token="opaque-profile", VideoEncoderConfiguration={"token": "encoder-opaque"},
                            VideoSourceConfiguration={"SourceToken": "lens-opaque"})]

    def GetVideoEncoderConfiguration(self, params):
        if self.set_calls:
            self.reads_after_set += 1
            if self.delayed and self.reads_after_set < 3:
                raise Media2Error("onvif_media_timeout", "Reading is temporarily unavailable.")
        return copy.deepcopy(self.config)

    def GetVideoEncoderConfigurationOptions(self, params):
        return options()

    def SetVideoEncoderConfiguration(self, params):
        self.set_calls += 1
        if not self.refuse:
            self.config = copy.deepcopy(params["Configuration"])
        if self.lost_reply:
            raise Media2Error("onvif_media_timeout", "Reply was lost.")


def bind(monkeypatch, media):
    monkeypatch.setattr(ov, "_prepare_camera", lambda *args: (object(), ["http://camera.test/onvif/media2"]))
    monkeypatch.setattr(ov, "_get_media_service", lambda *args: (media, "http://camera.test/onvif/media2"))
    monkeypatch.setattr(ov.time, "sleep", lambda *args: None)


@pytest.mark.parametrize("lost_reply,delayed", [(False, False), (True, False), (False, True)])
def test_roundtrip_reconciles_lost_or_delayed_replies_without_resending_write(monkeypatch, lost_reply, delayed):
    media = FakeMedia(lost_reply=lost_reply, delayed=delayed)
    bind(monkeypatch, media)
    result = ov.update_onvif_profile("camera.test", 80, "test", "secret", "opaque-profile", {"bitrate": 1024})
    assert result["verification"]["matched"] is True
    assert result["config"]["bitrate"] == 1024
    assert result["video_source_token"] == "lens-opaque"
    assert media.set_calls == 1


def test_lost_reply_without_confirmed_application_is_unknown_not_refused(monkeypatch):
    media = FakeMedia(lost_reply=True, refuse=True)
    bind(monkeypatch, media)
    with pytest.raises(ov.OnvifConfigurationError) as exc:
        ov.update_onvif_profile("camera.test", 80, "test", "secret", "opaque-profile", {"bitrate": 1024})
    assert exc.value.code == "video_encoder_configuration_result_unknown"
    assert media.set_calls == 1


def test_codec_specific_options_and_source_identity_reach_api_contract(monkeypatch):
    media = FakeMedia()
    bind(monkeypatch, media)
    result = ov.get_onvif_profile_config("camera.test", 80, "test", "secret", "opaque-profile")
    assert result["options_by_codec"]["H264"]["fields"]["codec_profile"]["options"] == ["Main", "High"]
    assert result["options_by_codec"]["H265"]["fields"]["codec_profile"]["options"] == ["Main"]
    assert result["configuration_token"] == "encoder-opaque"
    assert result["media_service_type"] == "media2"


def test_codec_change_validates_new_codec_not_old_one(monkeypatch):
    media = FakeMedia()
    bind(monkeypatch, media)
    changed = ov.update_onvif_profile("camera.test", 80, "test", "secret", "opaque-profile", {"codec": "H264", "codec_profile": "High"})
    assert changed["config"]["codec_profile"] == "High"
    assert changed["config"]["codec"] == "H264"
    with pytest.raises(ValueError):
        ov.update_onvif_profile("camera.test", 80, "test", "secret", "opaque-profile", {"codec": "H265", "codec_profile": "High"})


def test_media2_service_is_selected_by_namespace_without_reusing_media1_binding(monkeypatch):
    selected = []
    class Adapter:
        def __init__(self, address, *args, **kwargs):
            selected.append(address)
        def GetProfiles(self):
            return [{"token": "profile"}]
    monkeypatch.setattr(ov, "Media2Service", Adapter)
    cam = NS(_km_media_services=[(MEDIA1_NS, "http://camera.test/media"), (MEDIA2_NS, "http://camera.test/media2")],
             _km_media_credentials=("test", "synthetic-secret"),
             devicemgmt=NS(zeep_client=NS(wsse=object(), transport=NS(session=object()))))
    adapter, address = ov._get_media_service(cam, ["http://camera.test/media"])
    assert address == "http://camera.test/media2"
    assert selected == [address]


def test_shared_encoder_profiles_are_explicit_not_guessed_by_name(monkeypatch):
    media = FakeMedia()
    media.config["UseCount"] = 2
    original_profiles = media.GetProfiles
    media.GetProfiles = lambda: original_profiles() + [MediaObject(token="another-profile", VideoEncoderConfiguration={"token": "encoder-opaque"})]
    bind(monkeypatch, media)
    result = ov.get_onvif_profile_config("camera.test", 80, "test", "secret", "opaque-profile")
    assert result["shared_profile_tokens"] == ["opaque-profile", "another-profile"]


def test_constructing_client_does_not_create_event_subscriptions(monkeypatch):
    device = NS()
    monkeypatch.setattr(ov.ONVIFCamera, "create_devicemgmt_service", lambda self: device)
    cam = ov.ONVIFCamera("camera.test", 80, "test", "secret", "/unused/wsdl")
    assert cam.devicemgmt is device
    assert cam.xaddrs == {}
    assert cam.transport.operation_timeout == 5
    assert isinstance(cam.transport.session.auth, requests.auth.HTTPDigestAuth)


def test_client_clock_alignment_does_not_set_camera_time():
    clock = NS(Date=NS(Year=2026, Month=1, Day=2), Time=NS(Hour=3, Minute=4, Second=5))
    token = NS(dt_diff=None)
    cam = NS(devicemgmt=NS(GetSystemDateAndTime=lambda: NS(UTCDateTime=clock), zeep_client=NS(wsse=token)))
    ov._align_client_clock(cam)
    assert cam.dt_diff == token.dt_diff
    assert cam.dt_diff.total_seconds() < 0


def test_service_port_is_preserved_only_on_the_same_camera_host():
    assert ov._rewrite_xaddr("http://camera.test:8899/media2", "camera.test", 80, "/fallback") == "http://camera.test:8899/media2"
    assert ov._rewrite_xaddr("http://unrelated.test:8899/media2", "camera.test", 80, "/fallback") == "http://camera.test:80/media2"


def test_mismatch_includes_actual_safe_configuration_for_ui(monkeypatch):
    media = FakeMedia(refuse=True)
    bind(monkeypatch, media)
    with pytest.raises(ov.OnvifConfigurationError) as exc:
        ov.update_onvif_profile("camera.test", 80, "test", "secret", "opaque-profile", {"bitrate": 1024})
    from app.routers.camera_onvif_routes import onvif_config_http_detail
    detail = onvif_config_http_detail(exc.value)
    assert detail["current"]["config"]["bitrate"] == 2048
    assert "synthetic-secret" not in str(detail)


@pytest.mark.parametrize("code", ["onvif_media_unreachable", "onvif_media_response_invalid", "onvif_media_http_error"])
def test_other_lost_set_responses_are_unknown_not_a_confirmed_refusal(monkeypatch, code):
    media = FakeMedia(refuse=True)
    def broken_set(payload):
        raise Media2Error(code, "Reply was not received.")
    media.SetVideoEncoderConfiguration = broken_set
    bind(monkeypatch, media)
    with pytest.raises(ov.OnvifConfigurationError) as exc:
        ov.update_onvif_profile("camera.test", 80, "test", "secret", "opaque-profile", {"bitrate": 1024})
    assert exc.value.code == "video_encoder_configuration_result_unknown"


def test_average_bitrate_mode_is_not_misreported_as_constant_and_explicit_mode_change_disables_it():
    cfg = configuration()
    cfg["RateControl"]["AverageBitRate"] = "1024"
    cfg["RateControl"]["ConstantBitRate"] = "true"
    assert ov._config_values(cfg)["bitrate_mode"] == "ABR"
    ov._apply_video_config_diff(cfg, {"bitrate_mode": "CBR"})
    assert cfg["RateControl"]["AverageBitRate"] == 0
    assert ov._config_values(cfg)["bitrate_mode"] == "CBR"
