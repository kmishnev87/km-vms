from __future__ import annotations

import os
import logging
import subprocess
import threading
import time
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)
_CACHE_LOCK = threading.RLock()
_CAPABILITIES_CACHE: dict | None = None


def _run_ffmpeg(args: list[str], timeout: int = 8) -> tuple[int | None, str, str]:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except Exception as exc:
        return None, "", str(exc)


def _contains_codec(output: str, codec: str) -> bool:
    return codec in (output or "")


def _device_access(path: Path) -> bool:
    return path.exists() and os.access(path, os.R_OK | os.W_OK)


def _empty_capabilities(reason: str) -> dict:
    return {
        "detected_at": time.time(),
        "hardware_accel_available": False,
        "available_backends": [],
        "preferred_backend": "cpu",
        "vaapi_available": False,
        "qsv_available": False,
        "drm_available": False,
        "render_device": settings.live_hwaccel_device,
        "codecs": {
            "h264_decode": [],
            "h264_encode": ["libx264"] if reason == "hwaccel_off" else [],
            "hevc_decode": [],
            "hevc_encode": [],
        },
        "docker_device_access_ok": False,
        "hardware_misconfigured": reason != "hwaccel_off",
        "ffmpeg_hwaccels": [],
        "config": {
            "mode": settings.live_hwaccel_mode,
            "backend": settings.live_hwaccel_backend,
            "device": settings.live_hwaccel_device,
        },
        "errors": [],
        "warnings": [reason],
    }


def detect_hardware_capabilities() -> dict:
    mode = (settings.live_hwaccel_mode or "auto").lower()
    backend_setting = (settings.live_hwaccel_backend or "auto").lower()
    render_device = settings.live_hwaccel_device or "/dev/dri/renderD128"
    render_path = Path(render_device)
    dri_path = Path("/dev/dri")
    warnings: list[str] = []
    errors: list[str] = []

    if mode in {"off", "disabled", "false", "0"}:
        return _empty_capabilities("hwaccel_off")

    dri_exists = dri_path.exists()
    render_exists = render_path.exists()
    docker_access_ok = _device_access(render_path)

    if not dri_exists:
        warnings.append("/dev/dri is not visible inside the container")
    if not render_exists:
        warnings.append(f"{render_device} is not visible inside the container")
    elif not docker_access_ok:
        warnings.append(f"{render_device} exists but is not readable/writable by the api process")

    hardware_misconfigured = bool(not dri_exists or not docker_access_ok)
    if hardware_misconfigured:
        logger.warning(
            "Hardware acceleration is misconfigured dri_exists=%s render_device=%s render_exists=%s docker_device_access_ok=%s",
            dri_exists,
            render_device,
            render_exists,
            docker_access_ok,
        )

    hw_code, hw_stdout, hw_stderr = _run_ffmpeg(["ffmpeg", "-hide_banner", "-hwaccels"])
    ffmpeg_hwaccels: list[str] = []
    if hw_code == 0:
        for line in hw_stdout.splitlines():
            value = line.strip().lower()
            if value and not value.startswith("hardware acceleration"):
                ffmpeg_hwaccels.append(value)
    else:
        errors.append(f"ffmpeg -hwaccels failed: {(hw_stderr or '').strip()[-600:]}")

    dec_code, dec_stdout, dec_stderr = _run_ffmpeg(["ffmpeg", "-hide_banner", "-decoders"])
    enc_code, enc_stdout, enc_stderr = _run_ffmpeg(["ffmpeg", "-hide_banner", "-encoders"])
    decoder_output = dec_stdout + dec_stderr
    encoder_output = enc_stdout + enc_stderr
    if dec_code != 0:
        errors.append(f"ffmpeg -decoders failed: {(dec_stderr or '').strip()[-600:]}")
    if enc_code != 0:
        errors.append(f"ffmpeg -encoders failed: {(enc_stderr or '').strip()[-600:]}")

    codecs = {
        "h264_decode": [],
        "h264_encode": [],
        "hevc_decode": [],
        "hevc_encode": [],
    }

    if _contains_codec(encoder_output, "libx264"):
        codecs["h264_encode"].append("libx264")
    if _contains_codec(decoder_output, " h264 "):
        codecs["h264_decode"].append("h264")
    if _contains_codec(decoder_output, " hevc "):
        codecs["hevc_decode"].append("hevc")

    vaapi_hw = "vaapi" in ffmpeg_hwaccels
    qsv_hw = "qsv" in ffmpeg_hwaccels
    drm_available = "drm" in ffmpeg_hwaccels or dri_exists

    hevc_vaapi_decode = _contains_codec(decoder_output, "hevc_vaapi")
    h264_vaapi_decode = _contains_codec(decoder_output, "h264_vaapi")
    h264_vaapi_encode = _contains_codec(encoder_output, "h264_vaapi")
    hevc_vaapi_encode = _contains_codec(encoder_output, "hevc_vaapi")
    hevc_qsv_decode = _contains_codec(decoder_output, "hevc_qsv")
    h264_qsv_decode = _contains_codec(decoder_output, "h264_qsv")
    h264_qsv_encode = _contains_codec(encoder_output, "h264_qsv")
    hevc_qsv_encode = _contains_codec(encoder_output, "hevc_qsv")

    if h264_vaapi_decode:
        codecs["h264_decode"].append("h264_vaapi")
    if hevc_vaapi_decode:
        codecs["hevc_decode"].append("hevc_vaapi")
    if h264_vaapi_encode:
        codecs["h264_encode"].append("h264_vaapi")
    if hevc_vaapi_encode:
        codecs["hevc_encode"].append("hevc_vaapi")
    if h264_qsv_decode:
        codecs["h264_decode"].append("h264_qsv")
    if hevc_qsv_decode:
        codecs["hevc_decode"].append("hevc_qsv")
    if h264_qsv_encode:
        codecs["h264_encode"].append("h264_qsv")
    if hevc_qsv_encode:
        codecs["hevc_encode"].append("hevc_qsv")

    vaapi_available = bool(vaapi_hw and docker_access_ok and h264_vaapi_encode and hevc_vaapi_decode)
    qsv_available = bool(qsv_hw and docker_access_ok and h264_qsv_encode and hevc_qsv_decode)

    available_backends = []
    if vaapi_available:
        available_backends.append("vaapi")
    if qsv_available:
        available_backends.append("qsv")

    preferred_backend = "cpu"
    if mode in {"vaapi", "qsv"}:
        if mode in available_backends:
            preferred_backend = mode
        else:
            warnings.append(f"requested hwaccel mode {mode} is not available")
    elif backend_setting in {"vaapi", "qsv"}:
        if backend_setting in available_backends:
            preferred_backend = backend_setting
        else:
            warnings.append(f"requested hwaccel backend {backend_setting} is not available")
    elif "vaapi" in available_backends:
        preferred_backend = "vaapi"
    elif "qsv" in available_backends:
        preferred_backend = "qsv"

    return {
        "detected_at": time.time(),
        "hardware_accel_available": preferred_backend != "cpu",
        "available_backends": available_backends,
        "preferred_backend": preferred_backend,
        "vaapi_available": vaapi_available,
        "qsv_available": qsv_available,
        "drm_available": drm_available,
        "render_device": render_device,
        "codecs": codecs,
        "docker_device_access_ok": docker_access_ok,
        "hardware_misconfigured": hardware_misconfigured,
        "ffmpeg_hwaccels": ffmpeg_hwaccels,
        "config": {
            "mode": settings.live_hwaccel_mode,
            "backend": settings.live_hwaccel_backend,
            "device": render_device,
        },
        "errors": errors,
        "warnings": warnings,
    }


def refresh_hardware_capabilities() -> dict:
    global _CAPABILITIES_CACHE
    result = detect_hardware_capabilities()
    with _CACHE_LOCK:
        _CAPABILITIES_CACHE = result
    return result


def get_hardware_capabilities() -> dict:
    with _CACHE_LOCK:
        cached = _CAPABILITIES_CACHE
    if cached is not None:
        return cached
    return refresh_hardware_capabilities()


def hardware_capabilities_summary() -> dict:
    caps = get_hardware_capabilities()
    return {
        "hardware_accel_available": caps.get("hardware_accel_available"),
        "available_backends": caps.get("available_backends", []),
        "preferred_backend": caps.get("preferred_backend"),
        "vaapi_available": caps.get("vaapi_available"),
        "qsv_available": caps.get("qsv_available"),
        "drm_available": caps.get("drm_available"),
        "render_device": caps.get("render_device"),
        "docker_device_access_ok": caps.get("docker_device_access_ok"),
        "hardware_misconfigured": caps.get("hardware_misconfigured"),
        "codecs": caps.get("codecs", {}),
        "warnings": caps.get("warnings", []),
        "errors": caps.get("errors", []),
    }
