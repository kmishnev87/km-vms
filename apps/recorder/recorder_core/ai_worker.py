"""Binary stdin/stdout only: one low-priority, disposable local inference process."""
from __future__ import annotations

import json
import os
import resource
import signal
import struct
import sys
from collections import OrderedDict

from recorder_core.ai_engine import EXTRA_LIMIT, PACKET_LIMIT, GpuUnavailable, YoloXTiny, decode_keyframe, scene_changed, scene_thumb


def read_exact(stream, count):
    data = stream.read(count)
    if len(data) != count:
        raise EOFError
    return data


def report(value):
    sys.stdout.buffer.write(json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n")
    sys.stdout.buffer.flush()


def run(device):
    os.nice(12)
    if hasattr(os, "sched_getaffinity"):
        cores = sorted(os.sched_getaffinity(0))
        if cores:
            os.sched_setaffinity(0, set(cores[-2:]))
    # Most native GPU allocations are virtual mappings. Bound the address
    # space and separately enforce a smaller resident budget in the parent.
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        detector = YoloXTiny(device)
    except GpuUnavailable:
        report({"phase": "gpu_unavailable"})
        return
    except Exception:
        report({"phase": "startup_failed"})
        return
    report({"phase": "ready", "model": "yolox-tiny-416", "version": detector.version,
            "backend": detector.backend, "device": detector.device, "runtime": detector.runtime})
    stream = sys.stdin.buffer
    thumbs = OrderedDict()
    while True:
        try:
            header_size, extra_size, payload_size = struct.unpack("!III", read_exact(stream, 12))
            if not 0 < header_size <= 512 or extra_size > EXTRA_LIMIT or not 0 < payload_size <= PACKET_LIMIT:
                return
            header = json.loads(read_exact(stream, header_size))
            frame = decode_keyframe(header["codec"], read_exact(stream, extra_size), read_exact(stream, payload_size))
        except (EOFError, ValueError, KeyError):
            return
        if frame is None:
            report({"phase": "frame_unusable"})
            continue
        try:
            thumb = scene_thumb(frame)
            camera = int(header["camera"])
            prior = thumbs.pop(camera, None)
            source = header["stream"]
            changed = bool(prior is not None and prior[0] == source and scene_changed(prior[1], thumb))
            thumbs[camera] = source, thumb
            while len(thumbs) > 16:
                thumbs.popitem(last=False)
            advanced = header.get("rules") is True
            result = {"phase": "sample", "objects": detector.detect(frame, .35 if advanced else .50), "scene_change": changed}
            if advanced:
                result.update(luma=thumb.astype("uint8").tobytes().hex(), size=[frame.width, frame.height])
            report(result)
        except Exception:
            report({"phase": "inference_failed"})
            return


if __name__ == "__main__":
    run("auto" if len(sys.argv) == 2 and sys.argv[1] == "auto" else "cpu")
