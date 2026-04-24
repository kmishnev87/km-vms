from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

from app.core.config import settings
from app.models.camera import Camera

StreamKey = Literal["main", "sub"]

_LOCK = threading.Lock()
_PROCESSES: dict[str, dict] = {}
IDLE_TTL_SEC = 90


def _stream_id(camera_id: int, stream: str) -> str:
    return f"{camera_id}_{stream}"


def _base_dir() -> Path:
    root = Path(settings.storage_previews) / "live"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _stream_dir(camera_id: int, stream: str) -> Path:
    path = _base_dir() / str(camera_id) / stream
    path.mkdir(parents=True, exist_ok=True)
    return path


def _playlist_path(camera_id: int, stream: str) -> Path:
    return _stream_dir(camera_id, stream) / "index.m3u8"


def _choose_input_url(camera: Camera, stream: str) -> str | None:
    stream = (stream or "").lower()
    if stream == "sub":
        return camera.rtsp_sub_url or camera.rtsp_main_url
    return camera.rtsp_main_url or camera.rtsp_sub_url


def _ffmpeg_cmd(camera: Camera, stream: str, out_dir: Path) -> list[str] | None:
    input_url = _choose_input_url(camera, stream)
    if not input_url:
        return None

    playlist = out_dir / "index.m3u8"
    segment_pattern = out_dir / "seg_%06d.ts"
    rtsp_transport = (camera.rtsp_transport or "tcp").lower()

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-rtsp_transport", rtsp_transport,
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-i", input_url,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-ar", "44100",
        "-ac", "1",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments",
        "-hls_segment_filename", str(segment_pattern),
        str(playlist),
    ]


def _cleanup_dir(path: Path):
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def cleanup_stale_streams():
    now = time.time()
    with _LOCK:
        for sid, info in list(_PROCESSES.items()):
            proc: subprocess.Popen = info["proc"]
            last_access = info.get("last_access", 0)
            if proc.poll() is not None:
                try:
                    info["stderr_file"].close()
                except Exception:
                    pass
                _PROCESSES.pop(sid, None)
                continue
            if now - last_access > IDLE_TTL_SEC:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    info["stderr_file"].close()
                except Exception:
                    pass
                _PROCESSES.pop(sid, None)


def ensure_stream(camera: Camera, stream: str) -> dict:
    cleanup_stale_streams()

    sid = _stream_id(camera.id, stream)
    out_dir = _stream_dir(camera.id, stream)
    playlist = _playlist_path(camera.id, stream)

    with _LOCK:
        info = _PROCESSES.get(sid)
        if info and info["proc"].poll() is None:
            info["last_access"] = time.time()
            return {
                "ok": True,
                "camera_id": camera.id,
                "stream": stream,
                "playlist_exists": playlist.exists(),
            }

        _cleanup_dir(out_dir)
        cmd = _ffmpeg_cmd(camera, stream, out_dir)
        if not cmd:
            return {
                "ok": False,
                "error": "Не найден RTSP URL для выбранного потока",
            }

        stderr_path = out_dir / "ffmpeg.log"
        stderr_file = open(stderr_path, "a", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            text=True,
        )

        _PROCESSES[sid] = {
            "proc": proc,
            "stderr_file": stderr_file,
            "last_access": time.time(),
            "started_at": time.time(),
            "stderr_path": stderr_path,
        }

    for _ in range(20):
        if playlist.exists() and playlist.stat().st_size > 0:
            break
        time.sleep(0.25)

    return {
        "ok": True,
        "camera_id": camera.id,
        "stream": stream,
        "playlist_exists": playlist.exists(),
    }


def touch_stream(camera_id: int, stream: str):
    sid = _stream_id(camera_id, stream)
    with _LOCK:
        info = _PROCESSES.get(sid)
        if info:
            info["last_access"] = time.time()


def get_playlist_file(camera_id: int, stream: str) -> Path:
    touch_stream(camera_id, stream)
    return _playlist_path(camera_id, stream)


def get_segment_file(camera_id: int, stream: str, filename: str) -> Path:
    touch_stream(camera_id, stream)
    return _stream_dir(camera_id, stream) / filename


def get_stream_debug(camera_id: int, stream: str) -> dict:
    sid = _stream_id(camera_id, stream)
    playlist = _playlist_path(camera_id, stream)
    with _LOCK:
        info = _PROCESSES.get(sid)
        running = bool(info and info["proc"].poll() is None)
        log_path = info.get("stderr_path") if info else None

    log_tail = ""
    if log_path and Path(log_path).exists():
        try:
            log_tail = Path(log_path).read_text(encoding="utf-8")[-4000:]
        except Exception:
            log_tail = ""

    return {
        "running": running,
        "playlist_exists": playlist.exists(),
        "playlist_size": playlist.stat().st_size if playlist.exists() else 0,
        "log_tail": log_tail,
    }
