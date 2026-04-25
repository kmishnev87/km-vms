from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import settings
from app.models.camera import Camera

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    codec: str | None
    safe_for_copy: bool
    error: str | None = None


def choose_input_url(camera: Camera, stream: str) -> str | None:
    stream = (stream or "").lower()
    if stream == "sub":
        return camera.rtsp_sub_url or camera.rtsp_main_url
    return camera.rtsp_main_url or camera.rtsp_sub_url


def mask_url_password(url: str | None) -> str:
    if not url:
        return ""

    try:
        parsed = urlsplit(url)
    except Exception:
        return url

    if not parsed.password:
        return url

    return url.replace(f":{parsed.password}@", ":***@")


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
    return shlex.join([mask_url_password(part) if input_url and part == input_url else part for part in cmd])


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
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
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

    codec = (result.stdout or "").strip().splitlines()
    codec_name = codec[0].strip().lower() if codec else None
    if result.returncode != 0:
        return ProbeResult(
            codec=codec_name,
            safe_for_copy=False,
            error=(result.stderr or "").strip()[-1200:] or f"ffprobe exit {result.returncode}",
        )

    return ProbeResult(codec=codec_name, safe_for_copy=codec_name == "h264", error=None)


def build_hls_command(
    *,
    camera: Camera,
    stream: str,
    input_url: str,
    out_dir: Path,
    mode: str,
) -> list[str]:
    playlist = out_dir / "index.m3u8"
    segment_pattern = out_dir / "seg_%06d.ts"
    rtsp_transport = (camera.rtsp_transport or "tcp").lower()

    if mode == "copy":
        video_args = ["-c:v", "copy"]
    else:
        video_args = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
        ]

    audio_mode = (settings.live_audio_mode or "none").lower()
    if audio_mode in {"none", "off", "disable", "disabled"}:
        audio_args = ["-an"]
    elif audio_mode == "copy":
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", "aac", "-ar", "44100", "-ac", "1"]

    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        rtsp_transport,
        "-timeout",
        "5000000",
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
        "2",
        "-hls_list_size",
        "6",
        "-hls_flags",
        "delete_segments+omit_endlist+program_date_time",
        "-hls_segment_filename",
        str(segment_pattern),
        str(playlist),
    ]
