from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.config import settings
from app.services import hardware


@pytest.mark.parametrize("runtime_ok", [True, False])
def test_vaapi_generic_decoder_is_checked_and_requires_real_runtime_success(monkeypatch, runtime_ok):
    monkeypatch.setattr(settings, "live_hwaccel_mode", "auto")
    monkeypatch.setattr(hardware, "effective_hardware_backend_setting", lambda: "vaapi")
    monkeypatch.setattr(Path, "exists", lambda path: True)
    monkeypatch.setattr(hardware, "_device_access", lambda path: True)
    def ffmpeg(args, **kwargs):
        if "-hwaccels" in args:
            return 0, "Hardware acceleration methods:\nvaapi\n", ""
        if "-decoders" in args:
            return 0, " VFS..D h264 H.264\n VFS..D hevc HEVC\n", ""
        if "-encoders" in args:
            return 0, " V....D h264_vaapi H.264\n", ""
        return 0, "ffmpeg test build", ""
    monkeypatch.setattr(hardware, "_run_ffmpeg", ffmpeg)
    validator = Mock(return_value=(runtime_ok, None if runtime_ok else "device test failed"))
    monkeypatch.setattr(hardware, "_validate_vaapi", validator)
    capabilities = hardware.detect_hardware_capabilities()
    validator.assert_called_once()
    assert capabilities["backend_status"]["vaapi"]["candidate"] is True
    assert capabilities["vaapi_available"] is runtime_ok
    assert capabilities["selected_backend"] == ("vaapi" if runtime_ok else "cpu")
