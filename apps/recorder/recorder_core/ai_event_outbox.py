"""Bounded handoff of confirmed AI starts and snapshots to the local API."""
from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path

MAX_PENDING = 256
MAX_JPEG = 128 * 1024
PROGRESS_INTERVAL = 5
SOURCE_LIMIT = 64
PTZ_PHOTO_DELAY = 1.0
PTZ_PHOTO_DEADLINE = 4.0


def thumbnail(packet):
    import av
    from recorder_core.ai_engine import decode_keyframe

    frame = decode_keyframe(packet.codec, packet.extra, packet.data)
    if frame is None:
        return b""
    ratio = min(1, 640 / frame.width, 360 / frame.height)
    width = max(2, int(frame.width * ratio) // 2 * 2)
    height = max(2, int(frame.height * ratio) // 2 * 2)
    encoder = av.CodecContext.create("mjpeg", "w")
    encoder.width, encoder.height = width, height
    encoder.pix_fmt = "yuvj420p"
    encoder.time_base = Fraction(1, 1)
    encoder.thread_count = 1
    still = frame.reformat(width=width, height=height, format="yuvj420p")
    still.pts = 0
    blob = b"".join(bytes(part) for part in (*encoder.encode(still), *encoder.encode(None)))
    return blob if 0 < len(blob) <= MAX_JPEG else b""


def isolated_thumbnail(packet):
    # Keep a second decode out of the recorder process and its environment.
    data = struct.pack("!II", len(packet.extra), len(packet.data)) + packet.extra + packet.data
    opts = {"user": 65534, "group": 65534, "extra_groups": ()} if os.geteuid() == 0 else {}
    result = subprocess.run(
        [sys.executable, "-m", "recorder_core.ai_event_outbox", packet.codec],
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": os.defpath + ":/usr/local/bin", "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1"},
        input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, close_fds=True, **opts,
    )
    return result.stdout if result.returncode == 0 and 0 < len(result.stdout) <= MAX_JPEG else b""


def _thumbnail_worker(codec):
    import resource
    from types import SimpleNamespace
    from recorder_core.ai_engine import CODECS, EXTRA_LIMIT, PACKET_LIMIT

    if codec not in CODECS:
        return
    os.nice(12)
    resource.setrlimit(resource.RLIMIT_AS, (1024**3,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    source = sys.stdin.buffer
    extra_size, packet_size = struct.unpack("!II", source.read(8))
    if extra_size > EXTRA_LIMIT or not 0 < packet_size <= PACKET_LIMIT:
        return
    extra, data = source.read(extra_size), source.read(packet_size)
    if len(extra) == extra_size and len(data) == packet_size:
        sys.stdout.buffer.write(thumbnail(SimpleNamespace(codec=codec, extra=extra, data=data)))


class AiEventOutbox:
    def __init__(self, root):
        self.root = Path(root) / "inbox" if root else None
        self.queue = queue.Queue(SOURCE_LIMIT)
        self.closed = threading.Event()
        self.thread = None
        self.guard = threading.Lock()
        self.recent = OrderedDict()
        self.progress = {}
        self.pending_starts = set()
        self.delayed_photos = {}

    def _queue_start(self, key, captured, rule_id, condition, kinds, frame):
        try:
            self.queue.put_nowait((key, captured, rule_id, condition, kinds,
                                   frame if self.queue.qsize() < 4 else None))
            return True
        except queue.Full:
            return False

    def offer(self, frame, revision, starts, window_seconds=60, *, ptz=False):
        if self.root is None or self.closed.is_set() or not revision:
            return
        key = (frame.camera, revision)
        with self.guard:
            previous = self.recent.get(key)
            if previous is not None and frame.captured <= previous:
                return
            opening = previous is None or frame.captured >= previous + window_seconds
            if opening:
                if not starts or key in self.pending_starts or len(self.pending_starts) >= SOURCE_LIMIT:
                    return
                rule_id, condition, kinds = starts[0]
                start = (key, frame.captured, rule_id, condition, kinds)
                if ptz and condition == "scene":
                    detected = time.monotonic()
                    # Keep the trigger timestamp, not the blurred trigger frame.
                    self.delayed_photos[key] = (detected, frame.generation, start)
                elif not self._queue_start(*start, frame):
                    return
                self.pending_starts.add(key)
            else:
                # Retime from the latest positive frame; coalesce the previous
                # positives, including sustained presence without a new start.
                due, _ = self.progress.get(key, (time.monotonic() + PROGRESS_INTERVAL, None))
                self.progress[key] = due, frame.captured
            self.recent[key] = frame.captured
            self.recent.move_to_end(key)
            while len(self.recent) > SOURCE_LIMIT:
                dropped, _ = self.recent.popitem(last=False)
                self.progress.pop(dropped, None)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="ai-event-evidence", daemon=True)
                self.thread.start()

    def follow_frame(self, frame, revision):
        """Use a fresh captured keyframe; the media receiver never waits for evidence."""
        if not self.delayed_photos or self.closed.is_set() or not self.guard.acquire(blocking=False):
            return
        try:
            key = (frame.camera, revision)
            pending = self.delayed_photos.get(key)
            if pending is None:
                return
            detected, generation, start = pending
            if (generation == frame.generation and PTZ_PHOTO_DELAY <= frame.arrival - detected <= PTZ_PHOTO_DEADLINE
                    and self.queue.qsize() < 4 and self._queue_start(*start, frame)):
                del self.delayed_photos[key]
        finally:
            self.guard.release()

    def _flush_photo_deadlines(self):
        now = time.monotonic()
        with self.guard:
            for key, (detected, _generation, start) in tuple(self.delayed_photos.items()):
                if (self.closed.is_set() or now - detected >= PTZ_PHOTO_DEADLINE) and self._queue_start(*start, None):
                    del self.delayed_photos[key]

    def _write(self, header, jpeg=b""):
        self.root.mkdir(parents=True, exist_ok=True)
        with os.scandir(self.root) as entries:
            if sum(item.name.endswith(".evt") for item in entries) >= MAX_PENDING:
                return
        event_id = uuid.uuid4().hex
        path = self.root / (event_id + ".evt")
        temporary = self.root / (event_id + ".part")
        payload = json.dumps(header, separators=(",", ":"), allow_nan=False).encode()
        try:
            with temporary.open("xb") as writer:
                writer.write(payload + b"\n")
                writer.write(jpeg)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_progress(self, key, captured):
        self._write({"v": 2, "camera_id": key[0], "revision": key[1], "captured": captured, "progress": True})

    def _flush_progress(self):
        now = time.monotonic()
        with self.guard:
            due = [(key, value[1]) for key, value in self.progress.items()
                   if key not in self.pending_starts and (self.closed.is_set() or now >= value[0])]
            for key, _ in due:
                del self.progress[key]
        for key, captured in due:
            try:
                self._write_progress(key, captured)
            except (OSError, ValueError):
                continue

    def _run(self):
        while True:
            self._flush_photo_deadlines()
            self._flush_progress()
            if self.closed.is_set() and self.queue.empty():
                break
            try:
                key, captured, rule_id, condition, kinds, frame = self.queue.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                with self.guard:
                    older = self.progress.get(key)
                    if older and older[1] < captured:
                        del self.progress[key]
                    else:
                        older = None
                if older:
                    self._write_progress(key, older[1])
                try:
                    jpeg = isolated_thumbnail(frame) if frame is not None else b""
                except Exception:
                    jpeg = b""  # Detection remains useful without a still.
                self._write({"v": 2, "camera_id": key[0], "revision": key[1], "captured": captured,
                             "rule_id": rule_id, "condition": condition, "objects": kinds}, jpeg)
            except (OSError, ValueError):
                pass
            finally:
                with self.guard:
                    self.pending_starts.discard(key)
        self._flush_progress()

    def stop(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(timeout=1)


if __name__ == "__main__" and len(sys.argv) == 2:
    _thumbnail_worker(sys.argv[1])
