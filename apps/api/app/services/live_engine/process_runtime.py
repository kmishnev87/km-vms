"""OS process identity and HLS readiness; no viewer or retry state."""
from __future__ import annotations
import os
import time
from pathlib import Path
from .ffmpeg import mask_rtsp_credentials

def _read_proc_cmdline(pid: int | None) -> str:
    if not pid:
        return ""
    path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = path.read_bytes()
    except Exception:
        return ""
    return mask_rtsp_credentials(raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip())


def _read_proc_state(pid: int | None) -> str:
    if not pid:
        return ""
    path = Path("/proc") / str(pid) / "stat"
    try:
        parts = path.read_text(encoding="utf-8", errors="replace").split()
        return parts[2] if len(parts) > 2 else ""
    except Exception:
        return ""


def _pid_exists(pid: int | None) -> bool:
    if not pid:
        return False
    if Path("/proc").exists():
        return (Path("/proc") / str(pid)).exists()
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def process_info(proc, started_at, *, pid_exists_fn, read_cmdline, read_state) -> dict:
    poll_value = proc.poll() if proc else None
    pid = proc.pid if proc else None
    pid_exists = pid_exists_fn(pid)
    cmdline = read_cmdline(pid)
    state = read_state(pid)
    is_zombie = state == "Z"
    proc_fs_available = Path("/proc").exists()
    cmdline_ok = "ffmpeg" in cmdline.lower() if cmdline else not proc_fs_available
    running_verified = bool(proc and poll_value is None and pid_exists and not is_zombie and cmdline_ok)
    return {
        "pid": pid,
        "pid_exists": pid_exists,
        "pid_cmdline": cmdline,
        "process_poll": poll_value,
        "is_zombie": is_zombie,
        "running_verified": running_verified,
        "process_started_at": started_at,
        "process_age_seconds": round(time.time() - started_at, 2) if started_at else 0,
    }


def hls_is_ready(stream_dir, playlist_path, *, running, stale_seconds, now) -> bool:
    try:
        if not running:
            return False
        segments = list(stream_dir.glob("seg_*.ts"))
        if not segments or not playlist_path.exists() or playlist_path.stat().st_size <= 0:
            return False
        return (
            now - playlist_path.stat().st_mtime <= stale_seconds
            and now - max(segment.stat().st_mtime for segment in segments) <= stale_seconds
        )
    except Exception:
        return False


def hls_update_times(stream_dir, playlist_path) -> tuple[float | None, float | None]:
    try:
        playlist_updated_at = playlist_path.stat().st_mtime if playlist_path.exists() else None
        segment_times = [segment.stat().st_mtime for segment in stream_dir.glob("seg_*.ts")]
        return playlist_updated_at, max(segment_times) if segment_times else None
    except Exception:
        return None, None
