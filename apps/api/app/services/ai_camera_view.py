"""Resolve spatial AI's lens type using the existing ONVIF PTZ discovery.

The private, derived fact is shared with Recorder over the existing AI event
volume. No camera commands, credentials on disk, or per-frame discovery.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from app.core.config import settings
from app.core.security import decrypt_text
from app.db.session import SessionLocal
from app.models.camera import Camera
from app.services import onvif_ptz

_KEYS = ("id", "protocol", "host", "port", "onvif_path", "onvif_channel_id",
         "onvif_profile_token", "onvif_sub_profile_token", "rtsp_main_url", "rtsp_sub_url")
_FIXED_REASONS = {"selected_profile_has_no_ptz", "ptz_service_unavailable"}
_RETRY_SECONDS = (20, 120)
_wake = threading.Event()
_stop = threading.Event()
_thread = None


def has_spatial_rules(policy):
    rules = policy.get("ai_rules") if isinstance(policy, dict) else None
    return bool(isinstance(rules, dict) and (rules.get("exclusions")
                or any(rule.get("points") for rule in (rules.get("rules") or []) if isinstance(rule, dict))))


def spatial_ai(camera):
    policy = camera.recording_policy or {}
    return (camera.protocol == "onvif" and isinstance(policy, dict) and policy.get("schema_version") == 1
            and policy.get("source") == "ai" and has_spatial_rules(policy))


def binding(camera):
    # Keep this private fact's input contract in sync with Recorder's reader.
    values = [str(getattr(camera, key, None) or "") for key in _KEYS]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def _path(camera):
    identifier = int(camera.id)
    if identifier <= 0:
        raise ValueError("invalid_camera")
    return Path(settings.storage_previews) / ".ai-events" / "view-facts" / f"{identifier}.json"


def view_kind(camera):
    # RTSP-only definitions have no KM VMS PTZ profile. A real ONVIF discovery
    # failure is distinct from a definitive non-PTZ result.
    if getattr(camera, "protocol", None) == "rtsp":
        return "fixed"
    try:
        path = _path(camera)
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            return "unknown"
        with path.open("rb") as source:
            data = source.read(513)
        if len(data) <= 512:
            fact = json.loads(data)
            if (fact.get("v") == 1 and fact.get("binding") == binding(camera)
                    and fact.get("kind") in ("fixed", "ptz")):
                return fact["kind"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return "unknown"


def remember(camera, capabilities):
    """Persist only a definitive response for the configured logical lens."""
    if getattr(camera, "protocol", None) != "onvif" or not isinstance(capabilities, dict):
        return False
    reasons = set(capabilities.get("unsupported_reasons") or [])
    token = getattr(camera, "onvif_profile_token", None)
    selected = capabilities.get("profile_token")
    if token and selected != token:
        return False
    if capabilities.get("supported") is True and selected:
        kind = "ptz"
    elif capabilities.get("supported") is False and reasons & _FIXED_REASONS:
        kind = "fixed"
    else:
        return False   # Don't overwrite the last known type on a partial answer.
    if view_kind(camera) == kind:
        return True
    temporary = None
    try:
        path = _path(camera)
        if path.parent.is_symlink() or path.is_symlink():
            return False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=path.parent,
                                         prefix=".fact-", delete=False) as target:
            temporary = Path(target.name)
            json.dump({"v": 1, "binding": binding(camera), "kind": kind}, target, separators=(",", ":"))
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _discover(camera):
    try:
        password = decrypt_text(camera.password_encrypted)
        if not camera.host or not camera.port or not camera.username or not password:
            return False
        capabilities = onvif_ptz.capabilities(
            host=camera.host, port=camera.port, username=camera.username,
            password=password, onvif_path=camera.onvif_path, profile_token=camera.onvif_profile_token,
            include_area_zoom=False,
        )
        return remember(camera, capabilities)
    except Exception:
        return False  # No endpoint details or credentials in background logs.


def _candidates():
    with SessionLocal() as db:
        cameras = db.query(Camera).filter(Camera.deleted_at.is_(None), Camera.status != "deleting",
            Camera.protocol == "onvif", Camera.recording_policy.is_not(None)).order_by(Camera.id)
        return [camera for camera in cameras if spatial_ai(camera)]
    # The session is closed before any device IO; no row lock is held.


def _scan(attempts, *, clock=time.monotonic):
    cameras = _candidates()
    active = {(camera.id, binding(camera)) for camera in cameras}
    for stale in attempts.keys() - active:
        del attempts[stale]
    next_at = None
    for camera in cameras:
        if _stop.is_set():
            break
        key = camera.id, binding(camera)
        if view_kind(camera) != "unknown":
            attempts.pop(key, None)
            continue
        count, due = attempts.get(key, (0, 0))
        if due is None:  # Exhausted. An explicit PTZ UI read can supply the fact.
            continue
        if clock() >= due:
            if _discover(camera):
                attempts.pop(key, None)
                continue
            due = clock() + _RETRY_SECONDS[count] if count < len(_RETRY_SECONDS) else None
            attempts[key] = count + 1, due
        if due is not None:
            next_at = due if next_at is None else min(next_at, due)
    return max(0, next_at - clock()) if next_at is not None else None


def _run():
    attempts, delay = {}, None
    while not _stop.is_set():
        _wake.wait(delay)
        _wake.clear()
        if _stop.is_set():
            return
        try:
            delay = _scan(attempts)
        except Exception:
            delay = 30.0  # Recover from transient database/storage startup errors.


def wake():
    _wake.set()


def start():
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        wake()
        _thread = threading.Thread(target=_run, name="ai-camera-view-discovery", daemon=True)
        _thread.start()


def stop():
    _stop.set()
    wake()
    if _thread is not None:
        _thread.join(timeout=1.0)
