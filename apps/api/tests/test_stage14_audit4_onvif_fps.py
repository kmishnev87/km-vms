"""Media1 integer wire contract must not restrict Media2 fractional rates."""
from types import SimpleNamespace as NS

import pytest
from lxml import etree
from zeep import xsd

from app.services import onvif_service as ov


def media1_fields():
    cfg = {"token": "encoder", "Encoding": "H264", "RateControl": {"FrameRateLimit": 25}}
    options = {"H264": {"FrameRateRange": {"Min": 1, "Max": 30}}}
    return ov._supported_video_fields(NS(), cfg, options=options)


@pytest.mark.parametrize("value", [12.5, "29.97", True, float("nan"), float("inf")])
def test_media1_rejects_non_integer_fps_before_set(value):
    with pytest.raises(ValueError):
        ov._validate_profile_config_request({"fps": value}, media1_fields())


@pytest.mark.parametrize("value", [12, 12.0, "12"])
def test_media1_integral_input_is_serialized_as_integer_not_float(value):
    fields = media1_fields()
    assert fields["fps"]["range"] == {"min": 1, "max": 30, "step": 1}
    assert fields["fps"]["number_type"] == "integer"
    request = ov._validate_profile_config_request({"fps": value}, fields)
    cfg = {"RateControl": {"FrameRateLimit": 25}}
    ov._apply_video_config_diff(cfg, request, media=NS())
    fps = cfg["RateControl"]["FrameRateLimit"]
    assert type(fps) is int
    # Real Zeep serializer: xs:int must render 12, never invalid 12.0.
    root = etree.Element("root")
    xsd.Element("FrameRateLimit", xsd.Int()).render(root, fps)
    assert root[0].text == "12"


def test_integer_contract_still_applies_without_range_or_with_discrete_options():
    fields = {"fps": {"writable": True, "number_type": "integer", "options": [12.5, 25]}}
    with pytest.raises(ValueError, match="integer"):
        ov._validate_profile_config_request({"fps": 12.5}, fields)


@pytest.mark.parametrize("value", [12.5, 29.97])
def test_media2_keeps_fractional_fps_with_non_adapter_config(value):
    media = NS(protocol="media2")
    cfg = {"token": "encoder", "Encoding": "H264", "RateControl": {"FrameRateLimit": 25}}
    fields = ov._supported_video_fields(media, cfg, options={
        "H264": {"FrameRateRange": {"Min": 1, "Max": 30}}})
    assert fields["fps"]["number_type"] == "float"
    assert "step" not in fields["fps"]["range"]
    request = ov._validate_profile_config_request({"fps": value}, fields)
    ov._apply_video_config_diff(cfg, request, media=media)
    assert cfg["RateControl"]["FrameRateLimit"] == value
