"""Profile-bound ONVIF PTZ runtime. Request-driven; no background polling.

The existing Media1/Media2 discovery and authenticated SDK remain the only device
stack. A small bounded cache avoids discovery on every joystick renewal. Each
physical PTZ node serializes actuation and remembers cancelled gestures so late
HTTP requests cannot restart a released control.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import json
import math
import threading
import time


CACHE_SECONDS = 120.0
FOV_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"
FOV_ZOOM_SPACES = {
    "http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationSpaceFov": "factor",
    "http://www.onvif.org/ver10/ptz/ZoomSpaces/TranslationSpaceFov": "factor",
    "http://www.milestonesys.com/ver10/tptz/ZoomSpaces/TranslationSpaceFov": "ratio",
}
MAX_CONTEXTS = 128
MAX_CANCELLED = 512
CANCEL_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 5.0
SPACE_NAMES = {
    "pan_tilt": ("ContinuousPanTiltVelocitySpace", "RelativePanTiltTranslationSpace", "AbsolutePanTiltPositionSpace"),
    "zoom": ("ContinuousZoomVelocitySpace", "RelativeZoomTranslationSpace", "AbsoluteZoomPositionSpace"),
}
_guard = threading.RLock()
_contexts = OrderedDict()
_nodes = OrderedDict()
_cancelled = OrderedDict()


def _sdk():
    from app.services import onvif_service
    return onvif_service


def _attr(value, name, default=None):
    return _sdk()._safe_attr(value, name, default)


def _error(code, message):
    return _sdk().OnvifConfigurationError(code, message)


def _fingerprint(host, port, username, password, onvif_path):
    value = json.dumps([str(host).lower(), int(port), username, password, onvif_path], ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def _seconds(value):
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


@dataclass
class NodeState:
    lock: object = field(default_factory=threading.RLock)
    active: tuple | None = None
    touched: float = field(default_factory=time.monotonic)


@dataclass
class Context:
    ptz: object
    profile_token: str | None
    node_token: str | None
    configuration_token: str | None
    state_key: str
    modes: dict
    nodes: list
    supported: bool | None
    reasons: list
    can_presets: bool | None = None
    timeout: float = 1.5
    expires: float = 0.0
    presets: list | None = None
    point_space: dict | None = None
    area_zoom_space: dict | None = None
    area_zoom_driver: object | None = None
    area_zoom_probe: object | None = field(default=None, repr=False)
    area_zoom_retry_after: float = 0.0
    read_lock: object = field(default_factory=threading.RLock)


def reset_runtime_cache():
    """For isolated tests; normal invalidation is keyed by connection/profile."""
    with _guard:
        _contexts.clear()
        _nodes.clear()
        _cancelled.clear()


def _state(key):
    with _guard:
        now = time.monotonic()
        for old_key, old in list(_nodes.items()):
            if old_key != key and now - old.touched > CACHE_SECONDS + 30 and old.lock.acquire(blocking=False):
                try:
                    if old.active is None or old.active[3] < now:
                        _nodes.pop(old_key, None)
                finally:
                    old.lock.release()
        if key not in _nodes and len(_nodes) >= MAX_CONTEXTS * 4:
            raise _error("ptz_busy", "Camera controls are busy. Try again shortly.")
        state = _nodes.setdefault(key, NodeState())
        state.touched = now
        _nodes.move_to_end(key)
        return state


def _cancel(key, owner, control_id):
    if not control_id:
        return
    now = time.monotonic()
    with _guard:
        for old_key, expires in list(_cancelled.items()):
            if expires <= now:
                _cancelled.pop(old_key, None)
        _cancelled[(key, owner, control_id)] = now + CANCEL_SECONDS
        while len(_cancelled) > MAX_CANCELLED:
            _cancelled.popitem(last=False)


def _is_cancelled(key, owner, control_id):
    with _guard:
        return _cancelled.get((key, owner, control_id), 0) > time.monotonic()


def _finite_range(bounds):
    try:
        return all(math.isfinite(float(bounds[key])) for key in ("min", "max")) and bounds["min"] <= bounds["max"]
    except (TypeError, KeyError, ValueError):
        return False


def _spaces(options, node, axis):
    result = {}
    for mode, name in zip(("continuous", "relative", "absolute"), SPACE_NAMES[axis]):
        source = _attr(options, "Spaces") if options is not None else _attr(node, "SupportedPTZSpaces")
        entries = _sdk()._ptz_space_items(source, name)
        valid = []
        for entry in entries:
            ranges = [entry.get("x")] + ([entry.get("y")] if axis == "pan_tilt" else [])
            if not entry.get("uri") or not ranges[0]:
                continue
            if any(r and not _finite_range(r) for r in ranges):
                continue
            if mode != "absolute" and any(r and not r["min"] <= 0 <= r["max"] for r in ranges):
                continue
            if not any(r and r["min"] < r["max"] for r in ranges):
                continue
            valid.append(entry)
        if valid:
            valid.sort(key=lambda entry: "generic" not in entry["uri"].lower())
            result[mode] = valid[0]
    return result


def _build_context(host, port, username, password, onvif_path, profile_token, device_key):
    sdk = _sdk()
    kwargs = {"onvif_path": onvif_path} if onvif_path else {}
    if profile_token:
        kwargs["profile_token"] = profile_token
    try:
        cam, ptz, selected = sdk._prepare_ptz_context(host, port, username, password, **kwargs)
    except Exception as exc:
        if getattr(exc, "code", None) == "onvif_ptz_not_advertised":
            return Context(None, profile_token, None, None, device_key, {}, [], False, ["ptz_service_unavailable"])
        raise
    profile = getattr(cam, "_km_selected_ptz_profile", None)
    configuration = _attr(profile, "PTZConfiguration")
    if profile is not None and configuration is None:
        return Context(None, selected, None, None, device_key, {}, [], False, ["selected_profile_has_no_ptz"])
    configuration_token = _attr(configuration, "token")
    node_token = _attr(configuration, "NodeToken")
    if not selected or not configuration_token or not node_token:
        return Context(ptz, selected, node_token, configuration_token, device_key, {}, [], None, ["ptz_profile_binding_incomplete"])

    node = None
    try:
        node = ptz.GetNode({"NodeToken": node_token})
    except Exception:
        try:
            node = next((item for item in sdk._as_list(ptz.GetNodes()) if _attr(item, "token") == node_token), None)
        except Exception:
            pass
    options = None
    try:
        options = ptz.GetConfigurationOptions({"ConfigurationToken": configuration_token})
    except Exception:
        pass
    if node is None and options is None:
        return Context(ptz, selected, node_token, configuration_token, device_key, {}, [], None, ["ptz_nodes_unavailable"])
    modes = {axis: _spaces(options, node, axis) for axis in SPACE_NAMES}
    timeout_range = _attr(options, "PTZTimeout")
    timeout_min, timeout_max = _seconds(_attr(timeout_range, "Min")), _seconds(_attr(timeout_range, "Max"))
    timeout = 1.5
    if timeout_min is not None and timeout_max is not None and 0 < timeout_min <= timeout_max:
        timeout = max(timeout_min, min(timeout_max, timeout))
    else:
        # A continuous implementation must advertise its accepted Timeout range.
        for axis in modes:
            modes[axis].pop("continuous", None)
    if timeout > MAX_TIMEOUT_SECONDS:
        for axis in modes:
            modes[axis].pop("continuous", None)
    maximum_presets = _attr(node, "MaximumNumberOfPresets")
    can_presets = bool(maximum_presets) if maximum_presets is not None else None
    normalized_node = {"token": sdk._safe_text(node_token, 120),
                       "name": sdk._safe_text(_attr(node, "Name"), 120),
                       "can_pan_tilt": bool(modes["pan_tilt"]), "can_zoom": bool(modes["zoom"]),
                       "spaces": {axis: list(entries.values()) for axis, entries in modes.items()}}
    supported = bool(any(modes.values()) or can_presets)
    if not supported and options is None:
        return Context(ptz, selected, node_token, configuration_token, device_key, modes, [normalized_node],
                       None, ["ptz_options_unavailable"], can_presets)
    context = Context(ptz, selected, node_token, configuration_token, device_key, modes, [normalized_node],
                   supported, [] if supported else ["ptz_capabilities_not_reported"], can_presets,
                   timeout=min(timeout, MAX_TIMEOUT_SECONDS))
    source = _attr(options, "Spaces") if options is not None else _attr(node, "SupportedPTZSpaces")
    for space in sdk._ptz_space_items(source, "RelativePanTiltTranslationSpace"):
        if space.get("uri") == FOV_SPACE and all(
            _finite_range(space.get(axis)) and space[axis]["min"] <= 0 <= space[axis]["max"]
            and space[axis]["min"] < space[axis]["max"] for axis in ("x", "y")
        ):
            context.point_space = space
            break
    if context.point_space:
        # FOV zoom may legitimately advertise +INF. Generic finite movement
        # ranges are not a magnification scale and must never be substituted.
        for item in sdk._as_list(_attr(source, "RelativeZoomTranslationSpace")):
            uri = _attr(item, "URI")
            if uri not in FOV_ZOOM_SPACES:
                continue
            try:
                bounds = _attr(item, "XRange")
                low, high = float(_attr(bounds, "Min")), float(_attr(bounds, "Max"))
                if not math.isfinite(low) or math.isnan(high) or not 0 <= low <= 1 <= high:
                    continue
            except (TypeError, ValueError):
                continue
            context.area_zoom_space = {"uri": uri, "min": low, "max": high, "kind": FOV_ZOOM_SPACES[uri]}
            break
    if supported and not context.area_zoom_space and modes.get("pan_tilt") and modes.get("zoom"):
        from app.services.dahua_area_zoom import discover
        context.area_zoom_probe = lambda: discover(cam, selected, host, port, username, password, onvif_path)
    return context


def get_context(host, port, username, password, onvif_path=None, profile_token=None, *, allow_stale=False):
    device_key = _fingerprint(host, port, username, password, onvif_path)
    key = (device_key, profile_token)
    with _guard:
        cached = _contexts.get(key)
        if cached and (cached.expires > time.monotonic() or (allow_stale and cached.ptz is not None)):
            _contexts.move_to_end(key)
            return cached
    # Discovery is serialized per device, not globally and not under an actuation
    # lock, so a slow probe cannot queue Stop behind it.
    state = _state(device_key + ":discovery")
    with state.lock:
        with _guard:
            cached = _contexts.get(key)
            if cached and cached.expires > time.monotonic():
                return cached
        context = _build_context(host, port, username, password, onvif_path, profile_token, device_key)
        # A verified binding is valid until its connection/profile fingerprint
        # changes or a device call fails. Idle time must never add rediscovery
        # latency to the next move, renewal, preset or emergency Stop.
        context.expires = (float("inf") if context.supported is True else
                           time.monotonic() + (CACHE_SECONDS if context.supported is False else 5.0))
        with _guard:
            _contexts[key] = context
            while len(_contexts) > MAX_CONTEXTS:
                _contexts.popitem(last=False)
        return context


def _call(ctx, method, payload):
    try:
        return getattr(ctx.ptz, method)(payload)
    except Exception:
        # Do not replay a possibly accepted physical command. The next request
        # can rediscover; Stop can still use the last verified binding immediately.
        with _guard:
            ctx.expires = 0.0
        raise


def _ensure_area_zoom(ctx):
    if not ctx.area_zoom_probe or time.monotonic() < ctx.area_zoom_retry_after:
        return
    with ctx.read_lock:
        if not ctx.area_zoom_probe or time.monotonic() < ctx.area_zoom_retry_after:
            return
        ctx.area_zoom_retry_after = time.monotonic() + 10
        driver = ctx.area_zoom_probe()
        # A temporary optional read failure is not permanent "unsupported".
        # Only explicit capability/area requests retry; joystick/Stop never do.
        if driver is not False:
            ctx.area_zoom_driver = driver
            ctx.area_zoom_probe = None


def capabilities(**credentials):
    ctx = get_context(**credentials)
    _ensure_area_zoom(ctx)
    return {"ok": ctx.supported is not None, "supported": ctx.supported,
            "source": "unknown" if ctx.supported is None else "onvif_ptz_service" if ctx.supported else "unsupported",
            "can_pan_tilt": bool(ctx.modes.get("pan_tilt")), "can_zoom": bool(ctx.modes.get("zoom")),
            "can_stop": bool(ctx.supported), "can_presets": ctx.can_presets,
            "can_point": ctx.point_space is not None,
            "can_area_zoom": bool(ctx.area_zoom_space or ctx.area_zoom_driver),
            "area_zoom_source": "onvif_fov" if ctx.area_zoom_space else "dahua_cgi" if ctx.area_zoom_driver else None,
            "profile_token_available": bool(ctx.profile_token), "profile_token": ctx.profile_token,
            "configuration_token": ctx.configuration_token, "node_token": ctx.node_token,
            "nodes": ctx.nodes, "movement_modes": {axis: next(iter(modes), None) for axis, modes in ctx.modes.items()},
            "lease_seconds": ctx.timeout, "limits": _sdk().ptz_command_limits(),
            "warnings": ctx.reasons if ctx.supported is None else [],
            "unsupported_reasons": ctx.reasons if ctx.supported is False else [], "raw_secret_exposed": False}


def _presets(ctx, refresh=False):
    if ctx.supported is not True or ctx.ptz is None:
        raise _error("ptz_unavailable", "PTZ is unavailable for the selected camera profile.")
    if ctx.presets is None or refresh:
        values = _sdk()._as_list(_call(ctx, "GetPresets", {"ProfileToken": ctx.profile_token}))
        ctx.presets = [{"token": _sdk()._safe_text(_attr(p, "token"), 255),
                        "name": _sdk()._safe_text(_attr(p, "Name"), 180)} for p in values[:512] if _attr(p, "token")]
        ctx.can_presets = True
    return list(ctx.presets)


def presets(**credentials):
    ctx = get_context(**credentials)
    # Metadata reads must not take the physical node's actuation/Stop lock.
    with ctx.read_lock:
        rows = _presets(ctx, refresh=True)
    return {"ok": True, "profile_token": ctx.profile_token, "presets": rows, "raw_secret_exposed": False}


def _scaled(value, value_range):
    if value == 0:
        return 0.0
    if not value_range:
        raise _error("ptz_axis_unavailable", "The selected camera does not support this movement axis.")
    extent = value_range["max"] if value > 0 else abs(value_range["min"])
    if extent <= 0:
        raise _error("ptz_direction_unavailable", "The selected camera does not support this direction.")
    return math.copysign(min(1.0, abs(value)) * extent, value)


def _move(ctx, command):
    axis = "pan_tilt" if command["action"] == "move" else "zoom"
    choices = ctx.modes.get(axis) or {}
    if not choices:
        raise _error("ptz_axis_unavailable", "The selected camera does not support this movement axis.")
    mode, space = next(iter(choices.items()))
    values = {"x": command["pan"], "y": command["tilt"]} if axis == "pan_tilt" else {"x": command["zoom"]}
    if axis == "pan_tilt" and values["x"] and values["y"]:
        values = {key: value / math.sqrt(2) for key, value in values.items()}
    payload = {"ProfileToken": ctx.profile_token}
    name = "PanTilt" if axis == "pan_tilt" else "Zoom"
    if mode == "absolute":
        status = _call(ctx, "GetStatus", {"ProfileToken": ctx.profile_token})
        current = _attr(_attr(status, "Position"), name)
        if current is None or _attr(current, "space") not in (None, "", space["uri"]):
            raise _error("ptz_position_unavailable", "A matching camera position is required for this movement.")
        coordinates = {"space": space["uri"]}
        for key, value in values.items():
            bounds = space.get(key)
            position = _seconds(_attr(current, key))
            if bounds is None or position is None:
                if value:
                    raise _error("ptz_position_unavailable", "The camera did not report this position axis.")
                coordinates[key] = position or 0.0
                continue
            step = value * (bounds["max"] - bounds["min"]) * 0.1
            coordinates[key] = max(bounds["min"], min(bounds["max"], position + step))
        payload["Position"] = {name: coordinates}
        _call(ctx, "AbsoluteMove", payload)
    else:
        factor = 0.1 if mode == "relative" else 1.0
        coordinates = {"space": space["uri"], **{key: _scaled(value * factor, space.get(key)) for key, value in values.items()}}
        if mode == "continuous":
            payload.update(Velocity={name: coordinates}, Timeout=timedelta(seconds=ctx.timeout))
            _call(ctx, "ContinuousMove", payload)
        else:
            payload["Translation"] = {name: coordinates}
            _call(ctx, "RelativeMove", payload)
    return mode


def _stop(ctx):
    _call(ctx, "Stop", {"ProfileToken": ctx.profile_token, "PanTilt": True, "Zoom": True})


def _point(ctx, command):
    if not ctx.point_space:
        raise _error("ptz_point_unavailable", "Point positioning is not supported by this camera profile.")
    # ONVIF FOV origin is the image centre; upper-right is (1, 1).
    # These are image coordinates, NOT fractions of the camera's mechanical range.
    coordinates = {"space": FOV_SPACE}
    for axis, value in (("x", 2 * command["x"] - 1), ("y", 1 - 2 * command["y"])):
        bounds = ctx.point_space[axis]
        coordinates[axis] = max(bounds["min"], min(bounds["max"], value))
    _call(ctx, "RelativeMove", {"ProfileToken": ctx.profile_token, "Translation": {"PanTilt": coordinates}})


def _area_zoom(ctx, command):
    if ctx.area_zoom_space and ctx.point_space:
        ratio = max(command["right"] - command["left"], command["bottom"] - command["top"])
        space = ctx.area_zoom_space
        zoom = ratio if space["kind"] == "ratio" else 1 / ratio
        pan_tilt = {"space": FOV_SPACE}
        for axis, value in (("x", command["left"] + command["right"] - 1),
                            ("y", 1 - command["top"] - command["bottom"])):
            limits = ctx.point_space[axis]
            pan_tilt[axis] = max(limits["min"], min(limits["max"], value))
        _call(ctx, "RelativeMove", {"ProfileToken": ctx.profile_token, "Translation": {
            "PanTilt": pan_tilt, "Zoom": {"space": space["uri"],
                "x": max(space["min"], min(space["max"], zoom))}}})
    elif ctx.area_zoom_driver:
        ctx.area_zoom_driver.move(command)
    else:
        raise _error("ptz_area_zoom_unavailable", "Area positioning is not supported by this camera profile.")


def execute(payload, control_owner=None, **credentials):
    sdk = _sdk()
    command = sdk.validate_ptz_command_payload(payload)
    if command["validation_only"] or command["dry_run"]:
        return sdk.ptz_validation_response(command)
    device_key = _fingerprint(credentials["host"], credentials["port"], credentials["username"],
                              credentials["password"], credentials.get("onvif_path"))
    owner = str(control_owner or "legacy")
    control_id = command.get("control_id")
    if command["action"] == "stop":
        _cancel(device_key, owner, control_id)
    # A released gesture must be rejected before and after a slow cold discovery.
    cancelled = lambda: bool(control_id and _is_cancelled(device_key, owner, control_id))
    if command["action"] != "stop" and cancelled():
        return _result(command, executed=False, stopped=False, ignored=True)
    ctx = get_context(**credentials, allow_stale=command["action"] == "stop")
    if ctx.supported is not True or ctx.ptz is None:
        raise _error("ptz_unavailable", "PTZ is unavailable for the selected camera profile.")
    state = _state(ctx.state_key + ":" + str(ctx.node_token))
    if command["action"] == "area_zoom":
        _ensure_area_zoom(ctx)
    if command["action"] == "goto_preset":
        with ctx.read_lock:
            token = command["preset_token"]
            if not any(item["token"] == token for item in _presets(ctx)):
                if not any(item["token"] == token for item in _presets(ctx, refresh=True)):
                    raise _error("ptz_preset_not_found", "The saved camera position is no longer available.")
    with state.lock:
        if command["action"] == "stop":
            active = state.active
            if control_id and active and active[:2] != (owner, control_id):
                return _result(command, executed=False, stopped=False, ignored=True)
            if active:
                _cancel(device_key, active[0], active[1])
            _stop(ctx)
            state.active = None
            return _result(command, executed=True, stopped=True)
        if cancelled():
            return _result(command, executed=False, stopped=False, ignored=True)
        active = state.active
        renewal = bool(control_id and active and active[:2] == (owner, control_id))
        if renewal and command["sequence"] <= active[2]:
            return _result(command, executed=False, stopped=False, ignored=True)
        if active and not renewal:
            _cancel(device_key, active[0], active[1])
            _stop(ctx)
        if command["action"] == "goto_preset":
            _call(ctx, "GotoPreset", {"ProfileToken": ctx.profile_token, "PresetToken": command["preset_token"]})
            state.active = None
            return _result(command, executed=True, stopped=False)
        if command["action"] == "point":
            _point(ctx, command)
            state.active = None
            return _result(command, executed=True, stopped=False)
        if command["action"] == "area_zoom":
            try:
                _area_zoom(ctx, command)
                if cancelled():
                    _stop(ctx)
                    state.active = None
                    return _result(command, executed=True, stopped=True, ignored=True)
                state.active = (owner, control_id, command["sequence"], time.monotonic() + 30) if control_id else None
                return _result(command, executed=True, stopped=False)
            except Exception:
                try:
                    _stop(ctx)
                except Exception:
                    pass
                state.active = None
                raise
        try:
            mode = _move(ctx, command)
            if cancelled():
                _stop(ctx)
                state.active = None
                return _result(command, executed=True, stopped=True, ignored=True)
            if command.get("hold"):
                state.active = (owner, control_id, command["sequence"], time.monotonic() + ctx.timeout + 5)
                return {**_result(command, executed=True, stopped=False, renewal=renewal),
                        "movement_mode": mode, "lease_seconds": ctx.timeout}
            time.sleep(float(command["duration_seconds"]))
        except Exception:
            # A lost response may follow a command the device already accepted.
            # Try Stop even for failed hold starts; continuous commands also
            # carry their independent device-side expiry.
            if command.get("hold"):
                try:
                    _stop(ctx)
                except Exception:
                    pass
                state.active = None
            raise
        finally:
            if not command.get("hold"):
                _stop(ctx)
                state.active = None
        return _result(command, executed=True, stopped=True)


def _result(command, *, executed, stopped, renewal=False, ignored=False):
    return {"ok": True, "action": command["action"], "execution_mode": "executed" if executed else "cancelled",
            "executed": executed, "physical_camera_mutated": executed and command["action"] != "stop",
            "camera_stopped": stopped, "duration_seconds": command.get("duration_seconds"),
            "lease_renewal": renewal, "ignored_stale_control": ignored,
            "audit_event": not renewal and not ignored,
            "warnings": [], "raw_secret_exposed": False}
