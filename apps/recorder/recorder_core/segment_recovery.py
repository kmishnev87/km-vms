"""Bounded, read-only validation of a stopped writer's remaining video."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


def same_file_version(left: os.stat_result, right: os.stat_result) -> bool:
    return stat.S_ISREG(right.st_mode) and all(
        getattr(left, key) == getattr(right, key)
        for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )


def probe_interrupted_segment(path: Path, *, timeout: float = 3.0) -> tuple[os.stat_result | None, str]:
    """Confirm a decodable video frame, not the health of every byte in the file.

    This runs once after a writer exits, never as a healthcheck. No remux,
    truncation or deletion is performed. Missing evidence remains unresolved.
    """
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            return None, "segment_recovery_empty_or_invalid"
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-read_intervals", "%+#32", "-show_entries", "frame=media_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=False, timeout=timeout,
        )
        if result.returncode != 0:
            return None, "segment_recovery_probe_failed"
        payload = json.loads(result.stdout or "{}")
        frames = payload.get("frames") if isinstance(payload, dict) else None
        if not isinstance(frames, list) or not any(
            isinstance(frame, dict) and frame.get("media_type") == "video" for frame in frames
        ):
            return None, "segment_recovery_no_video"
        if not same_file_version(before, path.lstat()):
            return None, "segment_recovery_file_changed"
        return before, "probe_ok"
    except subprocess.TimeoutExpired:
        return None, "segment_recovery_probe_timeout"
    except (OSError, ValueError):
        return None, "segment_recovery_probe_unavailable"
