"""Offline, replaceable detector adapter. Import only inside the isolated worker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MODEL_ROOT = Path("/app/ai-model")
SIZE = 416
MAX_PIXELS = 12_000_000
PACKET_LIMIT = 2 * 1024 * 1024
EXTRA_LIMIT = 64 * 1024
CODECS = frozenset({"h264", "hevc", "mjpeg", "mpeg4", "av1"})


class YoloXTiny:
    def __init__(self, requested_device, root=MODEL_ROOT):
        import numpy as np

        self.np = np
        manifest = json.loads((root / "model.json").read_text(encoding="utf-8"))
        model = root / "yolox_tiny.onnx"
        if manifest["id"] != "yolox-tiny-416" or model.stat().st_size != manifest["bytes"]:
            raise ValueError("model_contract")
        with model.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != manifest["sha256"]:
                raise ValueError("model_checksum")
        self.version = manifest["version"]
        self.classes = {int(key): kind for key, kind in manifest["class_map"].items()}
        grids, strides = [], []
        for stride in (8, 16, 32):
            y, x = np.mgrid[:SIZE // stride, :SIZE // stride]
            grids.append(np.stack((x, y), axis=-1).reshape(-1, 2))
            strides.append(np.full((x.size, 1), stride))
        self.grid, self.stride = np.concatenate(grids), np.concatenate(strides)
        self.backend = "cpu"
        self.device = "CPU"
        self.fallback = False
        if requested_device == "auto":
            try:
                self._open_gpu(model)
            except Exception:
                # Device not usable. Start CPU in a clean process; no second
                # model or retry of a wedged driver in this worker.
                raise GpuUnavailable from None
        else:
            self._open_cpu(model)
        # One verified execution, not just a driver/device name.
        pred = self.infer(np.zeros((1, 3, SIZE, SIZE), dtype=np.float32))
        if pred.shape != (1, len(self.grid), 85) or not np.isfinite(pred).all():
            raise ValueError("model_output_contract")

    def _open_gpu(self, path):
        import openvino as ov

        core = ov.Core()
        device = next((name for name in core.available_devices if name.startswith("GPU")), None)
        if device is None:
            raise GpuUnavailable
        session = core.compile_model(str(path), device, {
            "PERFORMANCE_HINT": "LATENCY", "GPU_QUEUE_THROTTLE": "LOW",
        })
        execution = session.get_property("EXECUTION_DEVICES")
        if not execution or any(not name.startswith("GPU") for name in execution):
            raise GpuUnavailable
        self.infer = lambda value: session(value)[session.output(0)]
        self.backend = "gpu"
        self.device = str(execution[0])
        self.runtime = "OpenVINO " + ov.__version__.split("-")[0]

    def _open_cpu(self, path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        options.log_severity_level = 3
        session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        name = session.get_inputs()[0].name
        self.infer = lambda value: session.run(None, {name: value})[0]
        self.runtime = "ONNX Runtime " + ort.__version__

    def detect(self, frame, minimum=.50):
        np = self.np
        ratio = min(SIZE / frame.width, SIZE / frame.height)
        width = max(1, int(frame.width * ratio))
        height = max(1, int(frame.height * ratio))
        bgr = frame.reformat(width=width, height=height, format="bgr24").to_ndarray()
        tensor = np.full((1, 3, SIZE, SIZE), 114, dtype=np.float32)
        tensor[0, :, :height, :width] = bgr.transpose(2, 0, 1)
        return self.objects(self.infer(tensor)[0], width, height, minimum)

    def objects(self, pred, width, height, minimum=.50):
        np = self.np
        scores = pred[:, 4:5] * pred[:, 5:]
        ids = scores.argmax(axis=1)
        confidence = scores.max(axis=1)
        selected = np.flatnonzero((confidence >= minimum) & np.isfinite(pred).all(axis=1))
        selected = sorted((i for i in selected if int(ids[i]) in self.classes), key=lambda i: -confidence[i])[:128]
        if not selected:
            return []
        idx = np.array(selected)
        xy = (pred[idx, :2] + self.grid[idx]) * self.stride[idx]
        wh = np.exp(np.clip(pred[idx, 2:4], -10, 10)) * self.stride[idx]
        scale = np.array((width, height))
        boxes = np.clip(np.hstack(((xy - wh / 2) / scale, (xy + wh / 2) / scale)), 0, 1)
        kinds = [self.classes[int(ids[i])] for i in idx]
        small = 1e-8
        result = []
        for box, kind, i in zip(boxes, kinds, idx):
            area = float(np.prod(box[2:] - box[:2]))
            if area < small:
                continue
            overlap = False
            for item in result:
                if item["kind"] != kind:
                    continue
                other = np.asarray(item["box"])
                intersection = float(np.prod(np.maximum(0, np.minimum(box[2:], other[2:]) - np.maximum(box[:2], other[:2]))))
                if intersection / max(small, area + float(np.prod(other[2:] - other[:2])) - intersection) >= .50:
                    overlap = True
                    break
            if not overlap:
                result.append({"kind": kind, "confidence": min(1.0, float(confidence[i])), "box": box.tolist()})
                if len(result) == 32:
                    break
        return result


class GpuUnavailable(Exception):
    pass


def decode_keyframe(codec, extra, payload):
    import av

    if codec not in CODECS or not 0 < len(payload) <= PACKET_LIMIT or len(extra) > EXTRA_LIMIT:
        return None
    try:
        ctx = av.CodecContext.create(codec, "r")
        ctx.thread_count = 1
        ctx.options = {"max_pixels": str(MAX_PIXELS)}
        if extra:
            ctx.extradata = extra
        frames = ctx.decode(av.Packet(payload))
        if not frames:
            frames = ctx.decode(None)
        if frames and 0 < frames[0].width * frames[0].height <= MAX_PIXELS:
            return frames[0]
    except (ValueError, av.error.FFmpegError):
        pass
    return None


def scene_thumb(frame):
    # Low-resolution luma, no full-sized frame copies. Source changes reset it.
    return frame.reformat(width=20, height=12, format="gray").to_ndarray().astype("int16")


def scene_changed(before, after):
    import numpy as np

    difference = after - before
    # Remove a uniform exposure change. A large spatial change is a separate
    # opt-in event, not a substitute for object recognition.
    return float(np.mean(np.abs(difference - np.median(difference)) > 28)) >= .40
