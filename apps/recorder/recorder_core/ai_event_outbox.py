"""Bounded handoff of confirmed AI starts and snapshots to the local API."""
from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path

MAX_PENDING = 256
MAX_JPEG = 128 * 1024
REPEAT_SECONDS = 45


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
        self.queue = queue.Queue(4)
        self.closed = threading.Event()
        self.thread = None
        self.recent = OrderedDict()

    def offer(self, frame, revision, starts):
        if self.root is None or self.closed.is_set() or not starts or not revision:
            return
        # Mirror the history's sliding filter before decoding or writing any JPEG.
        accepted = []
        for rule_id, condition, kinds in starts[:8]:
            key = (frame.camera, revision, rule_id, condition, kinds)
            previous = self.recent.pop(key, None)
            self.recent[key] = frame.captured
            if previous is None or not 0 <= frame.captured - previous < REPEAT_SECONDS:
                accepted.append((rule_id, condition, kinds))
        while len(self.recent) > 512:
            self.recent.popitem(last=False)
        if not accepted:
            return
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="ai-event-evidence", daemon=True)
            self.thread.start()
        try:
            self.queue.put_nowait((frame, revision, accepted))
        except queue.Full:
            pass   # Evidence can be dropped under pressure; recording is never blocked.

    def _run(self):
        while not self.closed.is_set() or not self.queue.empty():
            try:
                frame, revision, starts = self.queue.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with os.scandir(self.root) as entries:
                    remaining = MAX_PENDING - sum(item.name.endswith(".evt") for item in entries)
                if remaining <= 0:
                    continue
                try:
                    jpeg = isolated_thumbnail(frame)
                except Exception:
                    jpeg = b""  # Detection remains useful without a still.
                for rule_id, condition, kinds in starts[:remaining]:
                    event_id = uuid.uuid4().hex
                    path = self.root / (event_id + ".evt")
                    temporary = self.root / (event_id + ".part")
                    header = json.dumps({"v": 1, "camera_id": frame.camera, "revision": revision,
                                         "captured": frame.captured, "rule_id": rule_id, "condition": condition,
                                         "objects": kinds}, separators=(",", ":"), allow_nan=False).encode()
                    try:
                        with temporary.open("xb") as writer:
                            writer.write(header + b"\n")
                            writer.write(jpeg)
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue

    def stop(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(timeout=1)


if __name__ == "__main__" and len(sys.argv) == 2:
    _thumbnail_worker(sys.argv[1])
