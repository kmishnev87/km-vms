"""Bounded validation of closed, seekable recorder containers."""
from __future__ import annotations

import json
import math
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .event_buffer import PART_STAMP


def same_file_version(left: os.stat_result, right: os.stat_result) -> bool:
    return stat.S_ISREG(right.st_mode) and all(
        getattr(left, key) == getattr(right, key)
        for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )


@dataclass(frozen=True)
class ClosedSegment:
    stat: os.stat_result
    duration_seconds: float


def _vint(handle, *, identifier=False):
    first = handle.read(1)
    if not first:
        raise ValueError("truncated EBML element")
    width, mask = 1, 0x80
    while width <= 8 and not first[0] & mask:
        width, mask = width + 1, mask >> 1
    if width > 8:
        raise ValueError("invalid EBML integer")
    tail = handle.read(width - 1)
    if len(tail) != width - 1:
        raise ValueError("truncated EBML integer")
    value = int.from_bytes(first + tail, "big")
    if identifier:
        return value
    value &= (1 << (7 * width)) - 1
    return None if value == (1 << (7 * width)) - 1 else value


def matroska_has_seek_index(path: Path, size: int) -> bool:
    """Read the small SeekHead, then verify its Cues target; never scan media."""
    import io

    def elements(handle, end):
        for _ in range(128):
            if handle.tell() >= end:
                return
            key, length = _vint(handle, identifier=True), _vint(handle)
            if length is None or handle.tell() + length > end:
                raise ValueError("unfinished EBML element")
            start = handle.tell()
            yield key, start, length
            handle.seek(start + length)
        raise ValueError("oversized EBML header")

    try:
        with path.open("rb") as handle:
            if _vint(handle, identifier=True) != 0x1A45DFA3:
                return False
            header_size = _vint(handle)
            if header_size is None or header_size > 65536:
                return False
            handle.seek(header_size, 1)
            if _vint(handle, identifier=True) != 0x18538067:
                return False
            _vint(handle)
            segment_start = handle.tell()
            cue_position = None
            for key, start, length in elements(handle, size):
                if key == 0x1F43B675:
                    break
                if key == 0x1C53BB6B:
                    cue_position = start
                    break
                if key != 0x114D9B74:
                    continue
                if length > 65536:
                    return False
                seek_head = io.BytesIO(handle.read(length))
                for entry, entry_start, entry_size in elements(seek_head, length):
                    if entry != 0x4DBB:
                        continue
                    seek_id = seek_offset = None
                    for field, field_start, field_size in elements(seek_head, entry_start + entry_size):
                        if field_size > 8:
                            continue
                        value = int.from_bytes(seek_head.read(field_size), "big")
                        if field == 0x53AB:
                            seek_id = value
                        elif field == 0x53AC:
                            seek_offset = value
                    if seek_id == 0x1C53BB6B and seek_offset is not None:
                        target = segment_start + seek_offset
                        if not segment_start <= target < size:
                            return False
                        handle.seek(target)
                        if _vint(handle, identifier=True) != 0x1C53BB6B:
                            return False
                        cue_size = _vint(handle)
                        if cue_size is None or not 0 < cue_size <= size - handle.tell():
                            return False
                        # FFmpeg can prefix Cues with its CRC-32 element.
                        for cue_key, cue_start, cue_length in elements(handle, handle.tell() + cue_size):
                            if cue_key == 0xBB:
                                return cue_length > 0
                            if cue_key not in {0xBF, 0xEC}:
                                return False
                        return False
            if cue_position is not None:
                handle.seek(cue_position)
                for cue_key, cue_start, cue_length in elements(handle, cue_position + length):
                    if cue_key == 0xBB:
                        return cue_length > 0
                    if cue_key not in {0xBF, 0xEC}:
                        return False
            return False
    except (OSError, ValueError):
        return False


def buffered_mp4_has_seek_index(path: Path, size: int) -> bool:
    """Fragmented MP4 needs SIDX for Media3 seek and clipped timeline sources.

    A repaired unfragmented MP4 is also valid. Read only top-level headers,
    never payload; FFprobe independently proves duration and video.
    """
    with path.open("rb") as stream:
        position = 0
        movie = indexed = False
        for _ in range(128):
            if position == size:
                return movie
            stream.seek(position)
            header = stream.read(8)
            if len(header) != 8:
                return False
            length, kind = int.from_bytes(header[:4], "big"), header[4:]
            header_size = 8
            if length == 1:
                raw = stream.read(8)
                if len(raw) != 8:
                    return False
                length, header_size = int.from_bytes(raw, "big"), 16
            elif length == 0:
                length = size - position
            if length < header_size or length > size - position:
                return False
            if kind == b"moof":
                return movie and indexed
            if kind == b"moov":
                movie = True
            if kind == b"sidx" and length >= header_size + 24:
                indexed = True
            position += length
    return False


def inspect_closed_segment(path: Path, *, timeout: float = 3.0) -> tuple[ClosedSegment | None, str]:
    """Require duration, a seek index, and decodable video, not just first bytes."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            return None, "segment_recovery_empty_or_invalid"
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-read_intervals", "%+#32",
             "-show_entries", "frame=media_type:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=False, timeout=timeout,
        )
        if result.returncode != 0:
            return None, "segment_recovery_probe_failed"
        if not same_file_version(before, path.lstat()):
            return None, "segment_recovery_file_changed"
        payload = json.loads(result.stdout or "{}")
        frames = payload.get("frames") if isinstance(payload, dict) else None
        if not isinstance(frames, list) or not any(
            isinstance(frame, dict) and frame.get("media_type") == "video" for frame in frames
        ):
            return None, "segment_recovery_no_video"
        try:
            duration = float(payload.get("format", {}).get("duration", "nan"))
        except (TypeError, ValueError, AttributeError):
            duration = float("nan")
        if not math.isfinite(duration) or duration <= 0:
            return None, "segment_recovery_missing_duration"
        if path.suffix.lower() == ".mkv" and not matroska_has_seek_index(path, before.st_size):
            return None, "segment_recovery_missing_seek_index"
        if path.suffix.lower() == ".mp4" and PART_STAMP.search(path.name) and not buffered_mp4_has_seek_index(path, before.st_size):
            return None, "segment_recovery_missing_seek_index"
        if not same_file_version(before, path.lstat()):
            return None, "segment_recovery_file_changed"
        return ClosedSegment(before, duration), "probe_ok"
    except subprocess.TimeoutExpired:
        return None, "segment_recovery_probe_timeout"
    except (OSError, ValueError):
        return None, "segment_recovery_probe_unavailable"


def probe_interrupted_segment(path: Path, *, timeout: float = 3.0) -> tuple[os.stat_result | None, str]:
    verified, reason = inspect_closed_segment(path, timeout=timeout)
    return (verified.stat if verified else None), reason
