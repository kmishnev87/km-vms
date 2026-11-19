"""Explicit camera calibration, with the Web editor's reference frame stored privately."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.core.config import settings

WIDTH, HEIGHT = 20, 12
MAX_JPEG = 256 * 1024
EDITOR_WIDTH, EDITOR_HEIGHT = 1920, 1080
EDITOR_MAX_JPEG = 1024 * 1024
EDITOR_MAX_FILE = ((EDITOR_MAX_JPEG + 2) // 3) * 4 + 4096
FRAME_LIFETIME = 20 * 60
_capture_slot = threading.BoundedSemaphore(1)
_SAVED_NAME = re.compile(r"[a-f0-9]{64}\.json\Z")


def b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign(data):
    return hmac.new(settings.jwt_secret.encode("utf-8"), b"kmvms-ai-frame:v1\0" + data, hashlib.sha256).digest()


def binding(camera):
    fields = (camera.id, camera.rtsp_main_url, camera.rtsp_sub_url, camera.rtsp_transport,
              camera.default_live_stream, camera.default_record_stream)
    return b64(sign(json.dumps(fields, ensure_ascii=True).encode("ascii")))


def capture_input(camera, profile):
    record_track = "sub" if str(camera.default_record_stream or "").lower() == "sub" else "main"
    record = camera.rtsp_sub_url if record_track == "sub" else camera.rtsp_main_url
    if profile == "balanced" and record:
        live_track = "main" if camera.default_live_stream == "main" else "sub"
        return (camera.rtsp_main_url if live_track == "main" else camera.rtsp_sub_url), "live", live_track
    return record, "record", record_track


def jpeg_size(data, max_width=800, max_height=600):
    if data[:2] != b"\xff\xd8":
        return None
    cursor = 2
    while cursor + 8 <= len(data) and data[cursor] == 0xff:
        marker = data[cursor+1]
        length = int.from_bytes(data[cursor+2:cursor+4], "big")
        if length < 2 or cursor + 2 + length > len(data):
            break
        if marker in (0xc0, 0xc1, 0xc2) and length >= 8:
            height = int.from_bytes(data[cursor+5:cursor+7], "big")
            width = int.from_bytes(data[cursor+7:cursor+9], "big")
            return (width, height) if 0 < width <= max_width and 0 < height <= max_height else None
        cursor += length + 2
    return None


def edges(luma):
    # Only strong signed gradients, not image pixels, are persisted as a
    # viewpoint sketch. Keep this 20×12 contract in sync with Recorder.
    differences = [luma[i+1] - luma[i] for i in range(len(luma)) if i % WIDTH < WIDTH-1]
    differences += [luma[i+WIDTH] - luma[i] for i in range(WIDTH * (HEIGHT-1))]
    mask = [1 if delta >= 12 else 2 if delta <= -12 else 0 for delta in differences]
    return bytes(sum(value << (2*j) for j, value in enumerate(mask[i:i+4])) for i in range(0, len(mask), 4))


def make_reference(camera, width, height, luma, *, now=None, editor=False):
    value = {"v": 1, "cfg": binding(camera), "at": int(time.time() if now is None else now),
             "ratio": round(width / height * 1000), "edges": b64(edges(luma))}
    if editor:
        value["editor"] = secrets.token_hex(8)
    payload = json.dumps(value, separators=(",", ":")).encode("ascii")
    return f"{b64(payload)}.{b64(sign(payload))}"


def verified_reference(camera, token):
    try:
        encoded, proof = token.split(".")
        payload = unb64(encoded)
        if not hmac.compare_digest(sign(payload), unb64(proof)):
            return None
        obj = json.loads(payload)
        return obj if obj["v"] == 1 and hmac.compare_digest(obj["cfg"], binding(camera)) else None
    except (AttributeError, TypeError, ValueError, KeyError):
        return None


def reference_is_fresh(camera, token, *, now=None):
    obj = verified_reference(camera, token)
    return bool(obj and -5 <= (time.time() if now is None else now) - obj["at"] <= FRAME_LIFETIME)


def saved_reference(camera):
    rules = (getattr(camera, "recording_policy", None) or {}).get("ai_rules") or {}
    return rules.get("reference")


def editor_directory(camera_id):
    return Path(settings.storage_previews) / ".ai-rule-frames" / str(int(camera_id))


def editor_path(camera_id, reference):
    return editor_directory(camera_id) / (hashlib.sha256(reference.encode("ascii")).hexdigest() + ".json")


def read_editor_image(camera, reference):
    claims = verified_reference(camera, reference)
    if not claims or not claims.get("editor"):
        return None
    try:
        with editor_path(camera.id, reference).open("rb") as source:
            content = source.read(EDITOR_MAX_FILE + 1)
        if len(content) > EDITOR_MAX_FILE:
            return None
        snapshot = json.loads(content)
        if snapshot["reference"] != reference:
            return None
        return snapshot
    except (OSError, ValueError, TypeError, KeyError):
        return None


def prune_editor_images(camera_id, keep=()):
    directory = editor_directory(camera_id)
    retained = {editor_path(camera_id, reference).name for reference in keep if reference}
    if directory.is_dir():
        for path in directory.iterdir():
            if _SAVED_NAME.fullmatch(path.name) and path.name not in retained:
                path.unlink(missing_ok=True)
        if not retained and not any(directory.iterdir()):
            directory.rmdir()


def store_editor_image(camera, snapshot):
    reference = snapshot["reference"]
    target = editor_path(camera.id, reference)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > EDITOR_MAX_FILE:
        raise ValueError("calibration frame too large")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".capture-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    # A camera lock is held by the caller: keep the committed frame and the newest draft only.
    prune_editor_images(camera.id, (saved_reference(camera), reference))


def matching_references(before, after):
    try:
        old, new = [json.loads(unb64(reference.split(".")[0])) for reference in (before, after)]
        a, b = unb64(old["edges"]), unb64(new["edges"])
        ratio = old["ratio"]
        if len(a) != 112 or len(b) != 112 or abs(ratio-new["ratio"]) > max(16, ratio*.018):
            return False
        positions = [((x >> shift) & 3, (y >> shift) & 3) for x, y in zip(a, b) for shift in (0, 2, 4, 6)]
        known = [(x, y) for x, y in positions if x]
        return len(known) >= 24 and sum(x == y for x, y in known) / len(known) >= .68
    except (AttributeError, TypeError, ValueError, KeyError):
        return False


def run_ffmpeg(args, *, supplied=None, timeout):
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "1", *args],
                            input=supplied, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=timeout, check=False)
    return result.stdout if result.returncode == 0 else b""


def frame(camera, profile, *, editor=False):
    url, role, track = capture_input(camera, profile)
    try:
        usable = bool(url and urlsplit(url).scheme.lower() in {"rtsp", "rtsps"})
    except ValueError:
        usable = False
    if not usable:
        raise HTTPException(409, detail={"code": "ai_frame_input_missing"})
    if not _capture_slot.acquire(blocking=False):
        raise HTTPException(429, detail={"code": "ai_frame_busy"})
    try:
        transport = "udp" if camera.rtsp_transport == "udp" else "tcp"
        width, height = (EDITOR_WIDTH, EDITOR_HEIGHT) if editor else (800, 600)
        limit = EDITOR_MAX_JPEG if editor else MAX_JPEG
        image = run_ffmpeg(["-rtsp_transport", transport, "-threads", "1", "-noautorotate", "-i", url,
                            "-map", "0:v:0", "-an", "-sn", "-vf",
                            f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                            "-frames:v", "1", "-threads", "1", "-q:v", "4", "-f", "mjpeg", "pipe:1"], timeout=12)
        if editor and len(image) > limit:
            # Recompress locally instead of connecting to the camera twice.
            image = run_ffmpeg(["-f", "mjpeg", "-threads", "1", "-i", "pipe:0", "-frames:v", "1",
                                "-threads", "1", "-q:v", "9", "-f", "mjpeg", "pipe:1"], supplied=image, timeout=3)
        size = jpeg_size(image, width, height) if len(image) <= limit else None
        if size:
            luma = run_ffmpeg(["-f", "mjpeg", "-threads", "1", "-i", "pipe:0", "-vf",
                               "scale=20:12:flags=bilinear,format=gray", "-frames:v", "1",
                               "-threads", "1", "-f", "rawvideo", "pipe:1"], supplied=image, timeout=3)
            if len(luma) == WIDTH * HEIGHT:
                reference = make_reference(camera, *size, luma, editor=editor)
                saved = saved_reference(camera)
                response = {"image": base64.b64encode(image).decode("ascii"), "width": size[0], "height": size[1],
                            "reference": reference, "matching_saved": not saved or matching_references(saved, reference)}
                if editor:
                    response.update(role=role, track=track)
                return response
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    finally:
        _capture_slot.release()
    raise HTTPException(409, detail={"code": "ai_frame_unavailable"})
