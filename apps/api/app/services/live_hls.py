from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from app.core.config import settings
from app.models.camera import Camera

StreamKey = Literal["main", "sub"]

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PROCESSES: dict[str, dict] = {}
_WORKER_THREAD: threading.Thread | None = None
_WORKER_STOP_EVENT = threading.Event()


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


def _mask_url_password(url: str | None) -> str:
    if not url:
        return ""

    try:
        parsed = urlsplit(url)
    except Exception:
        return url

    if not parsed.password:
        return url

    return url.replace(f":{parsed.password}@", ":***@")


def _log_input_url(camera: Camera, stream: str, input_url: str):
    try:
        parsed = urlsplit(input_url)
        logger.info(
            "Live RTSP input camera_id=%s stream=%s selected_url=%s scheme=%s host=%s port=%s has_credentials=%s fallback_to_main=%s fallback_to_sub=%s",
            camera.id,
            stream,
            _mask_url_password(input_url),
            parsed.scheme,
            parsed.hostname,
            parsed.port,
            bool(parsed.username or parsed.password),
            stream == "sub" and bool(camera.rtsp_main_url) and not bool(camera.rtsp_sub_url),
            stream != "sub" and bool(camera.rtsp_sub_url) and not bool(camera.rtsp_main_url),
        )
        if parsed.scheme.lower() != "rtsp":
            logger.warning(
                "Live RTSP input has unexpected scheme camera_id=%s stream=%s scheme=%s url=%s",
                camera.id,
                stream,
                parsed.scheme,
                _mask_url_password(input_url),
            )
        if not parsed.hostname:
            logger.warning(
                "Live RTSP input has no hostname camera_id=%s stream=%s url=%s",
                camera.id,
                stream,
                _mask_url_password(input_url),
            )
    except Exception:
        logger.exception(
            "Failed to inspect live RTSP URL camera_id=%s stream=%s url=%s",
            camera.id,
            stream,
            _mask_url_password(input_url),
        )


def _stderr_log_path(camera_id: int, stream: str) -> Path:
    return _stream_dir(camera_id, stream) / "ffmpeg.log"


def _cleanup_dir(path: Path):
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _build_codec_args() -> list[str]:
    codec = (settings.live_video_codec or "copy").lower()
    if settings.live_transcode:
        codec = "libx264"

    if codec == "copy":
        return ["-c:v", "copy"]

    return [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
    ]


def _build_audio_args() -> list[str]:
    audio_mode = (settings.live_audio_mode or "none").lower()
    if audio_mode in {"none", "off", "disable", "disabled"}:
        return ["-an"]

    if audio_mode == "copy":
        return ["-c:a", "copy"]

    return ["-c:a", "aac", "-ar", "44100", "-ac", "1"]


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
        "-nostdin",
        "-loglevel", "warning",
        "-rtsp_transport", rtsp_transport,
        "-timeout", "5000000",
        "-i", input_url,
        "-map", "0:v:0",
        "-map", "0:a?",
        *_build_codec_args(),
        *_build_audio_args(),
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+omit_endlist+program_date_time",
        "-hls_segment_filename", str(segment_pattern),
        str(playlist),
    ]


def _process_running(info: dict | None) -> bool:
    return bool(info and info["proc"].poll() is None)


def _is_ready(camera_id: int, stream: str) -> bool:
    playlist = _playlist_path(camera_id, stream)
    stream_dir = _stream_dir(camera_id, stream)
    segments = list(stream_dir.glob("seg_*.ts"))
    return playlist.exists() and playlist.stat().st_size > 0 and bool(segments)


def _touch_stream_locked(camera_id: int, stream: str):
    info = _PROCESSES.get(_stream_id(camera_id, stream))
    if info:
        info["last_access"] = time.time()


def _read_log_tail(path: Path | None, max_chars: int = 4000) -> str:
    if not path or not Path(path).exists():
        return ""

    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except Exception:
        return ""


def _stop_process_locked(sid: str, info: dict, reason: str, cleanup_files: bool = True):
    proc: subprocess.Popen = info["proc"]
    camera_id = info["camera_id"]
    stream = info["stream"]
    stream_dir = _stream_dir(camera_id, stream)

    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    try:
        info["stderr_file"].flush()
        info["stderr_file"].close()
    except Exception:
        pass

    exit_code = proc.poll()
    stderr_tail = _read_log_tail(info.get("stderr_path"))
    logger.warning(
        "Live ffmpeg stopped camera_id=%s stream=%s pid=%s reason=%s exit_code=%s command=%s stderr_tail=%s",
        camera_id,
        stream,
        proc.pid,
        reason,
        exit_code,
        info.get("cmd_text", ""),
        stderr_tail,
    )

    _PROCESSES.pop(sid, None)

    if cleanup_files:
        shutil.rmtree(stream_dir, ignore_errors=True)

    logger.info(
        "Stopped live stream camera_id=%s stream=%s reason=%s",
        camera_id,
        stream,
        reason,
    )


def _cleanup_stale_streams(now: float | None = None):
    now = now or time.time()
    ttl = max(int(settings.live_idle_ttl_seconds), 5)

    with _LOCK:
        for sid, info in list(_PROCESSES.items()):
            if info["proc"].poll() is not None:
                _stop_process_locked(sid, info, reason="process_exit", cleanup_files=True)
                continue

            idle_seconds = now - info.get("last_access", 0)
            if idle_seconds > ttl:
                _stop_process_locked(sid, info, reason=f"idle_ttl:{int(idle_seconds)}s", cleanup_files=True)


def _cleanup_worker():
    interval = max(int(settings.live_cleanup_interval_seconds), 5)
    logger.info(
        "Starting live cleanup worker interval=%ss ttl=%ss",
        interval,
        settings.live_idle_ttl_seconds,
    )

    while not _WORKER_STOP_EVENT.wait(interval):
        try:
            _cleanup_stale_streams()
        except Exception:
            logger.exception("Live cleanup worker failed")

    logger.info("Live cleanup worker stopped")


def start_cleanup_worker():
    global _WORKER_THREAD

    with _LOCK:
        if _WORKER_THREAD and _WORKER_THREAD.is_alive():
            return
        _WORKER_STOP_EVENT.clear()
        _WORKER_THREAD = threading.Thread(
            target=_cleanup_worker,
            name="live-hls-cleanup",
            daemon=True,
        )
        _WORKER_THREAD.start()


def stop_cleanup_worker():
    global _WORKER_THREAD

    _WORKER_STOP_EVENT.set()
    thread = _WORKER_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _WORKER_THREAD = None


def ensure_stream(camera: Camera, stream: str, wait_for_ready: bool = True) -> dict:
    _cleanup_stale_streams()

    sid = _stream_id(camera.id, stream)
    out_dir = _stream_dir(camera.id, stream)
    playlist = _playlist_path(camera.id, stream)

    with _LOCK:
        info = _PROCESSES.get(sid)
        if _process_running(info):
            _touch_stream_locked(camera.id, stream)
            return {
                "ok": True,
                "camera_id": camera.id,
                "stream": stream,
                "pid": info["proc"].pid,
                "playlist_exists": playlist.exists(),
                "ready": _is_ready(camera.id, stream),
                "started_at": info["started_at"],
                "last_access": info["last_access"],
            }

        if info:
            _stop_process_locked(sid, info, reason="restart_dead_process", cleanup_files=True)

        _cleanup_dir(out_dir)
        cmd = _ffmpeg_cmd(camera, stream, out_dir)
        if not cmd:
            return {
                "ok": False,
                "error": "Не найден RTSP URL для выбранного потока",
            }

        input_url = _choose_input_url(camera, stream)
        if input_url:
            _log_input_url(camera, stream, input_url)

        cmd_text = shlex.join(
            [_mask_url_password(part) if part == input_url else part for part in cmd]
        )
        logger.info(
            "Starting live ffmpeg camera_id=%s stream=%s command=%s",
            camera.id,
            stream,
            cmd_text,
        )

        stderr_path = _stderr_log_path(camera.id, stream)
        stderr_file = open(stderr_path, "a", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            text=True,
        )

        started_at = time.time()
        _PROCESSES[sid] = {
            "camera_id": camera.id,
            "stream": stream,
            "proc": proc,
            "stderr_file": stderr_file,
            "stderr_path": stderr_path,
            "cmd_text": cmd_text,
            "last_access": started_at,
            "started_at": started_at,
        }

        logger.info(
            "Started live stream camera_id=%s stream=%s pid=%s codec=%s audio=%s",
            camera.id,
            stream,
            proc.pid,
            "libx264" if settings.live_transcode else settings.live_video_codec,
            settings.live_audio_mode,
        )

    ready = False
    if wait_for_ready:
        deadline = time.time() + max(int(settings.live_start_timeout_seconds), 2)
        while time.time() < deadline:
            with _LOCK:
                current = _PROCESSES.get(sid)
                if not _process_running(current):
                    if current:
                        _stop_process_locked(
                            sid,
                            current,
                            reason="process_exit",
                            cleanup_files=False,
                        )
                    break
            if _is_ready(camera.id, stream):
                ready = True
                break
            time.sleep(0.25)
    else:
        ready = _is_ready(camera.id, stream)

    with _LOCK:
        current = _PROCESSES.get(sid)
        pid = current["proc"].pid if _process_running(current) else None
        started_at = current["started_at"] if current else None
        last_access = current["last_access"] if current else None

    return {
        "ok": True,
        "camera_id": camera.id,
        "stream": stream,
        "pid": pid,
        "playlist_exists": playlist.exists(),
        "ready": ready,
        "started_at": started_at,
        "last_access": last_access,
        "stream_url": f"/api/live/{camera.id}/{stream}/index.m3u8",
    }


def touch_stream(camera_id: int, stream: str):
    _cleanup_stale_streams()
    with _LOCK:
        _touch_stream_locked(camera_id, stream)


def stop_stream(camera_id: int, stream: str) -> bool:
    sid = _stream_id(camera_id, stream)
    with _LOCK:
        info = _PROCESSES.get(sid)
        if not info:
            return False
        _stop_process_locked(sid, info, reason="client_stop", cleanup_files=True)
        return True


def stop_all_streams() -> int:
    with _LOCK:
        items = list(_PROCESSES.items())
        for sid, info in items:
            _stop_process_locked(sid, info, reason="stop_all", cleanup_files=True)
        return len(items)


def list_stream_status(camera_id: int | None = None, stream: str | None = None) -> list[dict]:
    now = time.time()
    _cleanup_stale_streams(now=now)

    result = []
    with _LOCK:
        items = list(_PROCESSES.items())

    for _sid, info in items:
        if camera_id is not None and info["camera_id"] != camera_id:
            continue
        if stream is not None and info["stream"] != stream:
            continue

        proc: subprocess.Popen = info["proc"]
        running = proc.poll() is None
        result.append(
            {
                "camera_id": info["camera_id"],
                "stream": info["stream"],
                "pid": proc.pid if running else None,
                "running": running,
                "started_at": info["started_at"],
                "last_access": info["last_access"],
                "age_seconds": round(now - info["started_at"], 2),
                "idle_seconds": round(now - info["last_access"], 2),
                "ready": _is_ready(info["camera_id"], info["stream"]),
                "playlist_exists": _playlist_path(info["camera_id"], info["stream"]).exists(),
            }
        )

    return result


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
        running = _process_running(info)
        log_path = info.get("stderr_path") if info else _stderr_log_path(camera_id, stream)
        pid = info["proc"].pid if running and info else None
        started_at = info.get("started_at") if info else None
        last_access = info.get("last_access") if info else None

    log_tail = _read_log_tail(log_path)

    return {
        "running": running,
        "pid": pid,
        "ready": _is_ready(camera_id, stream),
        "playlist_exists": playlist.exists(),
        "playlist_size": playlist.stat().st_size if playlist.exists() else 0,
        "started_at": started_at,
        "last_access": last_access,
        "log_tail": log_tail,
    }
