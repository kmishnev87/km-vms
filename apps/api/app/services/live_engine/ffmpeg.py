from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import settings
from app.models.camera import Camera

logger = logging.getLogger(__name__)
RTSP_CREDENTIALS_RE = re.compile(r"(rtsp://[^:\s/@]+):([^@\s]+)@", re.IGNORECASE)
RTSP_URL_RE = re.compile(r"rtsp://[^\s'\"\]]+", re.IGNORECASE)


@dataclass(frozen=True)
class ProbeResult:
    codec: str | None
    safe_for_copy: bool
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    error: str | None = None


def choose_input_url(camera: Camera, stream: str) -> str | None:
    stream = (stream or "").lower()
    if stream == "sub":
        return camera.rtsp_sub_url or camera.rtsp_main_url
    return camera.rtsp_main_url or camera.rtsp_sub_url


def mask_url_password(url: str | None) -> str:
    if not url:
        return ""
    return mask_rtsp_credentials(url)


def mask_rtsp_credentials(text: str | None) -> str:
    if not text:
        return ""
    masked = RTSP_CREDENTIALS_RE.sub(r"\1:***@", text)

    try:
        parsed = urlsplit(masked)
    except Exception:
        return RTSP_URL_RE.sub("[rtsp-url-redacted]", masked)

    if not parsed.password:
        return RTSP_URL_RE.sub("[rtsp-url-redacted]", masked)

    masked = masked.replace(f":{parsed.password}@", ":***@")
    return RTSP_URL_RE.sub("[rtsp-url-redacted]", masked)


def inspect_input_url(camera: Camera, stream: str, input_url: str):
    try:
        parsed = urlsplit(input_url)
        logger.info(
            "Live Engine RTSP input camera_id=%s stream=%s selected_url=%s scheme=%s host=%s port=%s has_credentials=%s fallback_to_main=%s fallback_to_sub=%s",
            camera.id,
            stream,
            mask_url_password(input_url),
            parsed.scheme,
            parsed.hostname,
            parsed.port,
            bool(parsed.username or parsed.password),
            stream == "sub" and bool(camera.rtsp_main_url) and not bool(camera.rtsp_sub_url),
            stream != "sub" and bool(camera.rtsp_sub_url) and not bool(camera.rtsp_main_url),
        )
        if parsed.scheme.lower() != "rtsp":
            logger.warning(
                "Live Engine input has unexpected scheme camera_id=%s stream=%s scheme=%s url=%s",
                camera.id,
                stream,
                parsed.scheme,
                mask_url_password(input_url),
            )
        if not parsed.hostname:
            logger.warning(
                "Live Engine input has no hostname camera_id=%s stream=%s url=%s",
                camera.id,
                stream,
                mask_url_password(input_url),
            )
    except Exception:
        logger.exception(
            "Failed to inspect Live Engine RTSP URL camera_id=%s stream=%s url=%s",
            camera.id,
            stream,
            mask_url_password(input_url),
        )


def command_text(cmd: list[str], input_url: str | None = None) -> str:
    return mask_rtsp_credentials(shlex.join(cmd))


def select_output_fps(input_fps: float | None, force_stable_fps: bool) -> tuple[float | None, bool]:
    if force_stable_fps:
        return float(max(10, min(int(settings.live_unstable_source_target_fps or 20), 25))), True
    if input_fps is None or input_fps <= 0:
        return float(max(10, min(int(settings.live_unstable_source_target_fps or 20), 25))), True
    if input_fps < 5 or input_fps > 60:
        return float(max(10, min(int(settings.live_unstable_source_target_fps or 20), 25))), True
    return None, False


def probe_video_codec(input_url: str, rtsp_transport: str) -> ProbeResult:
    cmd = [
        "ffprobe",
        "-hide_banner",
        "-v",
        "error",
        "-rtsp_transport",
        rtsp_transport,
        "-timeout",
        "5000000",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of",
        "default=noprint_wrappers=1",
        input_url,
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        return ProbeResult(codec=None, safe_for_copy=False, error=str(exc))

    values = {}
    for line in (result.stdout or "").strip().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    def parse_int(value: str | None) -> int | None:
        try:
            return int(value) if value else None
        except Exception:
            return None

    def parse_fps(value: str | None) -> float | None:
        if not value or value in {"0/0", "N/A"}:
            return None
        try:
            if "/" in value:
                num, den = value.split("/", 1)
                den_value = float(den)
                return round(float(num) / den_value, 3) if den_value else None
            return round(float(value), 3)
        except Exception:
            return None

    codec_name = (values.get("codec_name") or "").strip().lower() or None
    fps = parse_fps(values.get("avg_frame_rate")) or parse_fps(values.get("r_frame_rate"))
    width = parse_int(values.get("width"))
    height = parse_int(values.get("height"))
    if result.returncode != 0:
        return ProbeResult(
            codec=codec_name,
            safe_for_copy=False,
            width=width,
            height=height,
            fps=fps,
            error=(result.stderr or "").strip()[-1200:] or f"ffprobe exit {result.returncode}",
        )

    return ProbeResult(
        codec=codec_name,
        safe_for_copy=codec_name == "h264",
        width=width,
        height=height,
        fps=fps,
        error=None,
    )


def build_hls_command(
    *,
    camera: Camera,
    stream: str,
    input_url: str,
    out_dir: Path,
    mode: str,
    input_fps: float | None = None,
    force_stable_fps: bool = False,
    hw_backend: str | None = None,
    hw_device: str | None = None,
) -> list[str]:
    playlist = out_dir / "index.m3u8"
    segment_pattern = out_dir / "seg_%06d.ts"
    rtsp_transport = (camera.rtsp_transport or "tcp").lower()
    hls_time = 2
    transcode_profile = (settings.live_transcode_profile or "stable").lower()
    output_fps, forced_fps = select_output_fps(input_fps, force_stable_fps)

    input_hw_args: list[str] = []

    if mode == "copy":
        video_args = ["-c:v", "copy"]
    elif mode == "hardware_transcode" and hw_backend == "vaapi":
        fps_for_gop = output_fps or (input_fps if input_fps and input_fps > 0 else 25)
        fps_for_gop = max(10, min(float(fps_for_gop), 60))
        gop_size = max(20, min(int(round(fps_for_gop * hls_time)), 120))
        keyint_min = max(10, min(int(round(fps_for_gop)), gop_size))
        fps_args = ["-r", str(int(round(output_fps))), "-vsync", "1"] if forced_fps and output_fps else []
        input_hw_args = [
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            hw_device or settings.live_hwaccel_device,
            "-hwaccel_output_format",
            "vaapi",
        ]
        video_args = [
            "-vf",
            "scale_vaapi=format=nv12",
            "-c:v",
            "h264_vaapi",
            "-qp",
            "24",
            "-g",
            str(gop_size),
            "-keyint_min",
            str(keyint_min),
            "-bf",
            "0",
            *fps_args,
            "-force_key_frames",
            f"expr:gte(t,n_forced*{hls_time})",
        ]
    elif mode == "hardware_transcode" and hw_backend == "qsv":
        fps_for_gop = output_fps or (input_fps if input_fps and input_fps > 0 else 25)
        fps_for_gop = max(10, min(float(fps_for_gop), 60))
        gop_size = max(20, min(int(round(fps_for_gop * hls_time)), 120))
        keyint_min = max(10, min(int(round(fps_for_gop)), gop_size))
        fps_args = ["-r", str(int(round(output_fps))), "-vsync", "1"] if forced_fps and output_fps else []
        input_hw_args = [
            "-init_hw_device",
            "qsv=qsv:hw_any",
            "-filter_hw_device",
            "qsv",
            "-hwaccel",
            "qsv",
            "-hwaccel_output_format",
            "qsv",
        ]
        video_args = [
            "-c:v",
            "h264_qsv",
            "-global_quality",
            "24",
            "-g",
            str(gop_size),
            "-keyint_min",
            str(keyint_min),
            "-bf",
            "0",
            *fps_args,
        ]
    elif mode == "hardware_transcode" and hw_backend == "nvenc":
        fps_for_gop = output_fps or (input_fps if input_fps and input_fps > 0 else 25)
        fps_for_gop = max(10, min(float(fps_for_gop), 60))
        gop_size = max(20, min(int(round(fps_for_gop * hls_time)), 120))
        keyint_min = max(10, min(int(round(fps_for_gop)), gop_size))
        fps_args = ["-r", str(int(round(output_fps))), "-vsync", "1"] if forced_fps and output_fps else []
        input_hw_args = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        video_args = [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-tune",
            "ll",
            "-g",
            str(gop_size),
            "-keyint_min",
            str(keyint_min),
            "-bf",
            "0",
            *fps_args,
        ]
    elif mode == "hardware_transcode" and hw_backend == "amf":
        fps_for_gop = output_fps or (input_fps if input_fps and input_fps > 0 else 25)
        fps_for_gop = max(10, min(float(fps_for_gop), 60))
        gop_size = max(20, min(int(round(fps_for_gop * hls_time)), 120))
        keyint_min = max(10, min(int(round(fps_for_gop)), gop_size))
        fps_args = ["-r", str(int(round(output_fps))), "-vsync", "1"] if forced_fps and output_fps else []
        video_args = [
            "-c:v",
            "h264_amf",
            "-quality",
            "speed",
            "-g",
            str(gop_size),
            "-keyint_min",
            str(keyint_min),
            "-bf",
            "0",
            *fps_args,
        ]
    else:
        fps_for_gop = output_fps or (input_fps if input_fps and input_fps > 0 else 25)
        fps_for_gop = max(10, min(float(fps_for_gop), 60))
        gop_size = max(20, min(int(round(fps_for_gop * hls_time)), 120))
        keyint_min = max(10, min(int(round(fps_for_gop)), gop_size))
        fps_args = ["-r", str(int(round(output_fps))), "-vsync", "1"] if forced_fps and output_fps else []
        video_args = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(gop_size),
            "-keyint_min",
            str(keyint_min),
            "-sc_threshold",
            "0",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{hls_time})",
            *fps_args,
        ]
        if transcode_profile in {"low_latency", "zerolatency"}:
            video_args[6:6] = ["-tune", "zerolatency"]

    audio_mode = (settings.live_audio_mode or "none").lower()
    if audio_mode in {"none", "off", "disable", "disabled"}:
        audio_args = ["-an"]
    elif audio_mode == "copy":
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", "aac", "-ar", "44100", "-ac", "1"]

    input_timing_args = []
    if mode != "copy":
        input_timing_args = [
            "-fflags",
            "+genpts",
            "-use_wallclock_as_timestamps",
            "1",
        ]

    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        *input_timing_args,
        "-rtsp_transport",
        rtsp_transport,
        "-timeout",
        "5000000",
        "-analyzeduration",
        "1000000",
        "-probesize",
        "1000000",
        *input_hw_args,
        "-i",
        input_url,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        *video_args,
        *audio_args,
        "-f",
        "hls",
        "-hls_time",
        str(hls_time),
        "-hls_list_size",
        "6",
        "-hls_flags",
        "delete_segments+omit_endlist+program_date_time+independent_segments",
        "-hls_segment_filename",
        str(segment_pattern),
        str(playlist),
    ]
