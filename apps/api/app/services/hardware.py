from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from app.core.config import settings
from app.services.system_settings import effective_hardware_backend_setting

logger = logging.getLogger(__name__)

BACKEND_PRIORITY = ("qsv", "vaapi", "nvenc", "amf")
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


def _tail(text: str, limit: int = 1000) -> str:
    return (text or "").strip()[-limit:]


def _contains(output: str, value: str) -> bool:
    return value in (output or "")


def _device_access(path: Path) -> bool:
    return path.exists() and os.access(path, os.R_OK | os.W_OK)


def _parse_hwaccels(output: str) -> list[str]:
    result: list[str] = []
    for line in output.splitlines():
        value = line.strip().lower()
        if value and not value.startswith("hardware acceleration"):
            result.append(value)
    return sorted(set(result))


def _matching_codecs(output: str, names: tuple[str, ...]) -> list[str]:
    found = [name for name in names if _contains(output, name)]
    return sorted(set(found))


def _backend_order(mode: str, backend_setting: str) -> list[str]:
    requested = backend_setting if backend_setting in BACKEND_PRIORITY else mode
    if requested in BACKEND_PRIORITY:
        return [requested, *[backend for backend in BACKEND_PRIORITY if backend != requested]]
    return list(BACKEND_PRIORITY)


def _empty_capabilities(reason: str) -> dict:
    backend_setting = effective_hardware_backend_setting()
    return {
        "detected_at": time.time(),
        "ffmpeg_build": {},
        "hardware_accel_available": False,
        "available_backends": [],
        "backend_priority": list(BACKEND_PRIORITY),
        "selected_backend": "cpu",
        "preferred_backend": "cpu",
        "attempted_backends": [],
        "failed_backends": {},
        "backend_status": {},
        "vaapi_available": False,
        "qsv_available": False,
        "nvenc_available": False,
        "amf_available": False,
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
        "supported_hwaccels": [],
        "supported_encoders": [],
        "supported_decoders": [],
        "config": {
            "mode": settings.live_hwaccel_mode,
            "backend": backend_setting,
            "env_backend": settings.live_hwaccel_backend,
            "device": settings.live_hwaccel_device,
        },
        "errors": [],
        "warnings": [reason],
    }


def _validate_vaapi(render_device: str) -> tuple[bool, str | None]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-init_hw_device",
        f"vaapi=va:{render_device}",
        "-filter_hw_device",
        "va",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=1",
        "-vf",
        "format=nv12,hwupload,scale_vaapi=w=64:h=64:format=nv12",
        "-frames:v",
        "1",
        "-c:v",
        "h264_vaapi",
        "-f",
        "null",
        "-",
    ]
    code, stdout, stderr = _run_ffmpeg(cmd, timeout=8)
    if code == 0:
        return True, None
    return False, _tail(stderr or stdout or f"ffmpeg exit {code}")


def _validate_qsv() -> tuple[bool, str | None]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-init_hw_device",
        "qsv=qsv:hw_any",
        "-filter_hw_device",
        "qsv",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=1",
        "-vf",
        "format=nv12,hwupload=extra_hw_frames=16",
        "-frames:v",
        "1",
        "-c:v",
        "h264_qsv",
        "-f",
        "null",
        "-",
    ]
    code, stdout, stderr = _run_ffmpeg(cmd, timeout=8)
    if code == 0:
        return True, None
    return False, _tail(stderr or stdout or f"ffmpeg exit {code}")


def _validate_nvenc() -> tuple[bool, str | None]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=1",
        "-frames:v",
        "1",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    code, stdout, stderr = _run_ffmpeg(cmd, timeout=8)
    if code == 0:
        return True, None
    return False, _tail(stderr or stdout or f"ffmpeg exit {code}")


def _validate_amf() -> tuple[bool, str | None]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=1",
        "-frames:v",
        "1",
        "-c:v",
        "h264_amf",
        "-f",
        "null",
        "-",
    ]
    code, stdout, stderr = _run_ffmpeg(cmd, timeout=8)
    if code == 0:
        return True, None
    return False, _tail(stderr or stdout or f"ffmpeg exit {code}")


def detect_hardware_capabilities() -> dict:
    mode = (settings.live_hwaccel_mode or "auto").lower()
    backend_setting = effective_hardware_backend_setting()
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
    hardware_misconfigured = bool(not dri_exists or not docker_access_ok)

    if not dri_exists:
        warnings.append("/dev/dri is not visible inside the container")
    if not render_exists:
        warnings.append(f"{render_device} is not visible inside the container")
    elif not docker_access_ok:
        warnings.append(f"{render_device} exists but is not readable/writable by the api process")

    if hardware_misconfigured:
        logger.warning(
            "Hardware acceleration is misconfigured dri_exists=%s render_device=%s render_exists=%s docker_device_access_ok=%s",
            dri_exists,
            render_device,
            render_exists,
            docker_access_ok,
        )

    version_code, version_stdout, version_stderr = _run_ffmpeg(["ffmpeg", "-hide_banner", "-version"])
    ffmpeg_build = {
        "version": (version_stdout.splitlines() or [""])[0],
        "configuration": "",
    }
    if version_code == 0:
        for line in version_stdout.splitlines():
            if line.startswith("configuration:"):
                ffmpeg_build["configuration"] = line.removeprefix("configuration:").strip()
                break
    else:
        errors.append(f"ffmpeg -version failed: {_tail(version_stderr)}")

    hw_code, hw_stdout, hw_stderr = _run_ffmpeg(["ffmpeg", "-hide_banner", "-hwaccels"])
    supported_hwaccels = _parse_hwaccels(hw_stdout) if hw_code == 0 else []
    if hw_code != 0:
        errors.append(f"ffmpeg -hwaccels failed: {_tail(hw_stderr)}")

    dec_code, dec_stdout, dec_stderr = _run_ffmpeg(["ffmpeg", "-hide_banner", "-decoders"])
    enc_code, enc_stdout, enc_stderr = _run_ffmpeg(["ffmpeg", "-hide_banner", "-encoders"])
    decoder_output = dec_stdout + dec_stderr
    encoder_output = enc_stdout + enc_stderr
    if dec_code != 0:
        errors.append(f"ffmpeg -decoders failed: {_tail(dec_stderr)}")
    if enc_code != 0:
        errors.append(f"ffmpeg -encoders failed: {_tail(enc_stderr)}")

    encoder_names = (
        "libx264",
        "libx265",
        "h264_vaapi",
        "hevc_vaapi",
        "h264_qsv",
        "hevc_qsv",
        "h264_nvenc",
        "hevc_nvenc",
        "h264_amf",
        "hevc_amf",
    )
    decoder_names = (
        "h264",
        "hevc",
        "h264_vaapi",
        "hevc_vaapi",
        "h264_qsv",
        "hevc_qsv",
        "h264_cuvid",
        "hevc_cuvid",
    )
    supported_encoders = _matching_codecs(encoder_output, encoder_names)
    supported_decoders = _matching_codecs(decoder_output, decoder_names)

    codecs = {
        "h264_decode": [name for name in ("h264", "h264_vaapi", "h264_qsv", "h264_cuvid") if name in supported_decoders],
        "h264_encode": [name for name in ("libx264", "h264_vaapi", "h264_qsv", "h264_nvenc", "h264_amf") if name in supported_encoders],
        "hevc_decode": [name for name in ("hevc", "hevc_vaapi", "hevc_qsv", "hevc_cuvid") if name in supported_decoders],
        "hevc_encode": [name for name in ("libx265", "hevc_vaapi", "hevc_qsv", "hevc_nvenc", "hevc_amf") if name in supported_encoders],
    }

    backend_priority = _backend_order(mode, backend_setting)
    attempted_backends: list[str] = []
    failed_backends: dict[str, str] = {}
    backend_status: dict[str, dict] = {}

    candidates = {
        "qsv": bool("qsv" in supported_hwaccels and docker_access_ok and "h264_qsv" in supported_encoders and "hevc_qsv" in supported_decoders),
        "vaapi": bool("vaapi" in supported_hwaccels and docker_access_ok and "h264_vaapi" in supported_encoders and "hevc_vaapi" in supported_decoders),
        "nvenc": bool("cuda" in supported_hwaccels and "h264_nvenc" in supported_encoders and ("hevc_cuvid" in supported_decoders or "hevc" in supported_decoders)),
        "amf": bool("h264_amf" in supported_encoders and ("hevc" in supported_decoders or "h264" in supported_decoders)),
    }

    validators = {
        "qsv": _validate_qsv,
        "vaapi": lambda: _validate_vaapi(render_device),
        "nvenc": _validate_nvenc,
        "amf": _validate_amf,
    }

    available_backends: list[str] = []
    for backend in backend_priority:
        status = {
            "candidate": candidates.get(backend, False),
            "runtime_ok": False,
            "failed_runtime": False,
            "runtime_check_skipped": False,
            "reason": None,
        }
        if not status["candidate"]:
            status["reason"] = "missing_hwaccel_or_codec_or_device_access"
            backend_status[backend] = status
            continue

        if (
            backend == "nvenc"
            and available_backends
            and mode != "nvenc"
            and backend_setting != "nvenc"
        ):
            status["runtime_check_skipped"] = True
            status["reason"] = "runtime_validation_skipped_not_selected"
            backend_status[backend] = status
            continue

        if (
            backend == "amf"
            and available_backends
            and mode != "amf"
            and backend_setting != "amf"
        ):
            status["runtime_check_skipped"] = True
            status["reason"] = "runtime_validation_skipped_not_selected"
            backend_status[backend] = status
            continue

        attempted_backends.append(backend)
        ok, reason = validators[backend]()
        status["runtime_ok"] = ok
        status["failed_runtime"] = not ok
        status["reason"] = reason
        if ok:
            available_backends.append(backend)
        else:
            failed_backends[backend] = reason or "runtime_validation_failed"
            logger.warning("Hardware backend runtime validation failed backend=%s reason=%s", backend, reason)
        backend_status[backend] = status

    selected_backend = "cpu" if backend_setting == "cpu" else (available_backends[0] if available_backends else "cpu")
    drm_available = "drm" in supported_hwaccels or dri_exists

    return {
        "detected_at": time.time(),
        "ffmpeg_build": ffmpeg_build,
        "hardware_accel_available": selected_backend != "cpu",
        "available_backends": available_backends,
        "backend_priority": backend_priority,
        "selected_backend": selected_backend,
        "preferred_backend": selected_backend,
        "attempted_backends": attempted_backends,
        "failed_backends": failed_backends,
        "backend_status": backend_status,
        "vaapi_available": "vaapi" in available_backends,
        "qsv_available": "qsv" in available_backends,
        "nvenc_available": "nvenc" in available_backends,
        "amf_available": "amf" in available_backends,
        "drm_available": drm_available,
        "render_device": render_device,
        "codecs": codecs,
        "docker_device_access_ok": docker_access_ok,
        "hardware_misconfigured": hardware_misconfigured,
        "ffmpeg_hwaccels": supported_hwaccels,
        "supported_hwaccels": supported_hwaccels,
        "supported_encoders": supported_encoders,
        "supported_decoders": supported_decoders,
        "config": {
            "mode": settings.live_hwaccel_mode,
            "backend": backend_setting,
            "env_backend": settings.live_hwaccel_backend,
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


def invalidate_hardware_capabilities() -> None:
    global _CAPABILITIES_CACHE
    with _CACHE_LOCK:
        _CAPABILITIES_CACHE = None


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
        "backend_priority": caps.get("backend_priority", []),
        "selected_backend": caps.get("selected_backend"),
        "preferred_backend": caps.get("preferred_backend"),
        "vaapi_available": caps.get("vaapi_available"),
        "qsv_available": caps.get("qsv_available"),
        "nvenc_available": caps.get("nvenc_available"),
        "amf_available": caps.get("amf_available"),
        "drm_available": caps.get("drm_available"),
        "render_device": caps.get("render_device"),
        "docker_device_access_ok": caps.get("docker_device_access_ok"),
        "hardware_misconfigured": caps.get("hardware_misconfigured"),
        "ffmpeg_build": caps.get("ffmpeg_build", {}),
        "supported_hwaccels": caps.get("supported_hwaccels", []),
        "supported_encoders": caps.get("supported_encoders", []),
        "supported_decoders": caps.get("supported_decoders", []),
        "attempted_backends": caps.get("attempted_backends", []),
        "failed_backends": caps.get("failed_backends", {}),
        "codecs": caps.get("codecs", {}),
        "warnings": caps.get("warnings", []),
        "errors": caps.get("errors", []),
    }
