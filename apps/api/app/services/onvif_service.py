from pathlib import Path
from collections.abc import Mapping
import copy
import hashlib
import re
import time
from urllib.parse import quote, urlparse, urlsplit, urlunparse, urlunsplit

from onvif import ONVIFCamera

MEDIA_XADDR_KEYS = (
    "media",
    "media2",
    "http://www.onvif.org/ver10/media/wsdl",
    "http://www.onvif.org/ver20/media/wsdl",
)

PTZ_MAX_DURATION_SECONDS = 2.0
PTZ_MIN_DURATION_SECONDS = 0.1
PTZ_MAX_SPEED = 0.35
PTZ_MIN_SPEED = 0.01
PTZ_DIRECTIONS = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
    "up_left": (-1.0, 1.0),
    "up_right": (1.0, 1.0),
    "down_left": (-1.0, -1.0),
    "down_right": (1.0, -1.0),
}
PTZ_ZOOM_DIRECTIONS = {"in": 1.0, "out": -1.0}
COMPATIBILITY_DOMAINS = (
    "onvif_service",
    "media_profiles",
    "stream_uri",
    "rtsp_reachable_override",
    "main_sub_assignment",
    "profile_config_options",
    "ptz",
    "events",
    "recorder_contract",
    "redaction",
)

ONVIF_VIDEO_CONFIG_FIELDS = (
    "codec",
    "resolution",
    "fps",
    "bitrate",
    "iframe_interval",
    "quality",
)


class OnvifConfigurationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def wsdl_dir():
    import onvif
    base = Path(onvif.__file__).resolve().parent
    candidates = [
        base / "wsdl",
        base.parent / "wsdl",
        Path("/etc/onvif/wsdl"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    raise RuntimeError("Не найден WSDL каталог ONVIF")


def _safe_attr(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        if name in obj:
            return obj[name]
        lowered = str(name).lower()
        for key, value in obj.items():
            if str(key).lower() == lowered:
                return value
    try:
        value = getattr(obj, name)
    except (AttributeError, TypeError):
        value = default
    if value is not default:
        return value
    values = getattr(obj, "__values__", None)
    if isinstance(values, Mapping):
        return _safe_attr(values, name, default)
    return default


def _safe_text(value, max_length=160):
    text = str(value or "").strip()
    text = text.replace("\r", " ").replace("\n", " ")
    return text[:max_length] or None


def _safe_number(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _safe_bool(value) -> bool:
    return bool(value) if value is not None else False


def _candidate_id(host: str | None, port: int | None, source: str) -> str:
    raw = f"{source}:{host or ''}:{port or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def inject_auth_to_rtsp(
    uri: str | None,
    username: str | None,
    password: str | None,
    override_host: str | None = None,
    override_port: int | None = None,
) -> str | None:
    if not uri:
        return None

    parts = urlsplit(uri)
    if not parts.scheme.lower().startswith("rtsp"):
        return uri

    host = override_host or parts.hostname or ""
    port_value = override_port if override_port else parts.port
    port = f":{port_value}" if port_value else ""

    if username and password:
        userinfo = f"{quote(str(username), safe='')}:{quote(str(password), safe='')}@"
    elif username:
        userinfo = f"{quote(str(username), safe='')}@"
    else:
        userinfo = ""

    netloc = f"{userinfo}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def rtsp_path_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    parts = urlsplit(str(uri))
    if not parts.scheme.lower().startswith("rtsp"):
        return None
    query = f"?{parts.query}" if parts.query else ""
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    value = f"{parts.path or ''}{query}{fragment}"
    return value or None


def rtsp_display_uri(
    uri: str | None,
    override_host: str | None = None,
    override_port: int | None = None,
) -> str | None:
    if not uri:
        return None
    parts = urlsplit(str(uri))
    if not parts.scheme.lower().startswith("rtsp"):
        return None
    host = override_host or parts.hostname or ""
    port_value = override_port if override_port is not None else parts.port
    port = f":{int(port_value)}" if port_value else ""
    return urlunsplit((parts.scheme, f"{host}{port}", parts.path, parts.query, parts.fragment))


def _rewrite_xaddr(xaddr: str | None, host: str, port: int, fallback_path: str):
    if not xaddr:
        return f"http://{host}:{port}{fallback_path}"

    parsed = urlparse(str(xaddr))
    scheme = parsed.scheme or "http"
    path = parsed.path or fallback_path
    return urlunparse((scheme, f"{host}:{port}", path, "", "", ""))


def _try_force_service_address(service, address: str):
    errors = []

    try:
        binding_options = getattr(service, "_binding_options", None)
        if isinstance(binding_options, dict):
            binding_options["address"] = address
            return True, errors
    except Exception as e:
        errors.append(f"_binding_options: {e}")

    try:
        inner = getattr(service, "service", None)
        inner_binding = getattr(inner, "_binding_options", None)
        if isinstance(inner_binding, dict):
            inner_binding["address"] = address
            return True, errors
    except Exception as e:
        errors.append(f"service._binding_options: {e}")

    try:
        ws_client = getattr(service, "ws_client", None)
        if ws_client and hasattr(ws_client, "set_options"):
            ws_client.set_options(location=address)
            return True, errors
    except Exception as e:
        errors.append(f"ws_client.set_options: {e}")

    try:
        zeep_client = getattr(service, "zeep_client", None)
        transport = getattr(zeep_client, "service", None)
        binding = getattr(transport, "_binding_options", None)
        if isinstance(binding, dict):
            binding["address"] = address
            return True, errors
    except Exception as e:
        errors.append(f"zeep_client.service._binding_options: {e}")

    return False, errors


def _set_media_xaddr(cam, address: str):
    if hasattr(cam, "xaddrs") and isinstance(cam.xaddrs, dict):
        for key in MEDIA_XADDR_KEYS:
            cam.xaddrs[key] = address


def _prepare_camera(host: str, port: int, username: str, password: str):
    cam = ONVIFCamera(host, int(port), username, password, wsdl_dir())

    services = []
    try:
        services = cam.devicemgmt.GetServices({"IncludeCapability": False})
    except Exception:
        services = []

    media_candidates = []

    for s in services:
        ns = (getattr(s, "Namespace", "") or "").lower()
        if "media" in ns:
            media_candidates.append(_rewrite_xaddr(getattr(s, "XAddr", None), host, int(port), "/onvif/media_service"))

    try:
        caps = cam.devicemgmt.GetCapabilities({"Category": "All"})
        media_caps = _safe_attr(caps, "Media")
        if media_caps:
            media_candidates.append(_rewrite_xaddr(_safe_attr(media_caps, "XAddr"), host, int(port), "/onvif/media_service"))
    except Exception:
        pass

    media_candidates.extend([
        f"http://{host}:{int(port)}/onvif/media_service",
        f"http://{host}:{int(port)}/onvif/Media",
        f"http://{host}:{int(port)}/onvif/media",
    ])

    uniq = []
    for item in media_candidates:
        if item and item not in uniq:
            uniq.append(item)

    if uniq:
        _set_media_xaddr(cam, uniq[0])

    return cam, uniq


def _get_media_service(cam, candidates: list[str]):
    last_error = None

    for address in candidates:
        try:
            _set_media_xaddr(cam, address)

            service = cam.create_media_service()
            _try_force_service_address(service, address)

            service.GetProfiles()
            return service, address
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"Не удалось подключиться к ONVIF Media service. Последняя ошибка: {last_error}")


def _find_profile(media, profile_token):
    profiles = media.GetProfiles()
    return next((p for p in profiles if _safe_attr(p, "token") == profile_token), None)


def _get_video_encoder_config_with_state(media, profile):
    vec = _normalize_encoder_configuration(
        _safe_attr(profile, "VideoEncoderConfiguration")
    )
    if not vec:
        return None, "missing_video_encoder_configuration"

    token = _safe_attr(vec, "token")
    if not token:
        return vec, None

    try:
        value = _normalize_encoder_configuration(
            media.GetVideoEncoderConfiguration(
                {"ConfigurationToken": token}
            )
        )
        if value is None:
            return vec, "video_encoder_configuration_unavailable"
        return value, None
    except Exception:
        return vec, "video_encoder_configuration_read_failed"


def _get_video_encoder_config_from_profile(media, profile):
    cfg, _ = _get_video_encoder_config_with_state(media, profile)
    return cfg


def _profile_score(item: dict, prefer_sub: bool = False) -> tuple:
    name = f"{item.get('name') or ''} {item.get('token') or ''}".lower()
    video = item.get("video") or {}
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = int(video.get("fps") or 0)
    pixels = width * height
    compact_name = re.sub(r"[^a-z0-9]+", "", name)
    sub_hint = any(value in compact_name for value in ("sub", "secondary", "low", "stream2", "profile2"))
    main_hint = not sub_hint and any(
        value in compact_name for value in ("main", "primary", "high", "stream1", "profile1")
    )

    if prefer_sub:
        return (
            1 if pixels else 0,
            -pixels if pixels else 0,
            -fps if fps else 0,
            1 if sub_hint else 0,
            -1 if main_hint else 0,
            item.get("name") or "",
        )
    return (
        1 if pixels else 0,
        pixels,
        fps,
        1 if main_hint else 0,
        -1 if sub_hint else 0,
        item.get("name") or "",
    )


def _suggest_profiles(profiles: list[dict]) -> tuple[str | None, str | None]:
    if not profiles:
        return None, None

    main = max(profiles, key=lambda item: _profile_score(item, prefer_sub=False))
    if len(profiles) == 1:
        return main.get("token"), None

    sub_candidates = [item for item in profiles if item.get("token") != main.get("token")]
    sub = max(sub_candidates, key=lambda item: _profile_score(item, prefer_sub=True)) if sub_candidates else main
    return main.get("token"), sub.get("token")


def _profile_to_dict(profile, media, username, password, host, port, rtsp_host=None, rtsp_port=None):
    cfg, cfg_warning = _get_video_encoder_config_with_state(media, profile)
    rc = _safe_attr(cfg, "RateControl")
    res = _safe_attr(cfg, "Resolution")
    audio_cfg = _safe_attr(profile, "AudioEncoderConfiguration")

    raw_uri = None
    fixed_uri = None
    stream_path = None
    rtsp_ready = False
    warnings = []
    if cfg_warning:
        warnings.append(cfg_warning)
    try:
        stream_resp = media.GetStreamUri({
            "StreamSetup": {
                "Stream": "RTP-Unicast",
                "Transport": {"Protocol": "RTSP"},
            },
            "ProfileToken": profile.token,
        })
        raw_uri = _safe_attr(stream_resp, "Uri")
        stream_path = rtsp_path_from_uri(raw_uri)
        fixed_uri = rtsp_display_uri(
            raw_uri,
            override_host=rtsp_host or host,
            override_port=rtsp_port,
        )
        rtsp_ready = bool(stream_path and (rtsp_host or host))
        parsed = urlsplit(str(raw_uri)) if raw_uri else None
        if parsed and parsed.hostname and rtsp_host and parsed.hostname != rtsp_host:
            warnings.append("camera_returned_different_rtsp_host")
    except Exception:
        raw_uri = None
        fixed_uri = None

    codec = _safe_text(_safe_attr(cfg, "Encoding"), 40)
    width = _safe_number(_safe_attr(res, "Width"))
    height = _safe_number(_safe_attr(res, "Height"))
    fps = _safe_number(_safe_attr(rc, "FrameRateLimit"))
    bitrate = _safe_number(_safe_attr(rc, "BitrateLimit"))
    encoding_interval = _safe_number(_safe_attr(rc, "EncodingInterval"))
    quality = _safe_number(_safe_attr(cfg, "Quality"))

    return {
        "token": _safe_text(_safe_attr(profile, "token"), 255),
        "name": _safe_text(_safe_attr(profile, "Name"), 255),
        "suggested_role": "unknown",
        "assigned_role": "unknown",
        "assigned_roles": [],
        "video": {
            "codec": codec,
            "encoding": codec,
            "width": width,
            "height": height,
            "fps": fps,
            "quality": quality,
            "bitrate": bitrate,
            "bitrate_limit": bitrate,
            "max_bitrate": bitrate,
            "bitrate_type": None,
            "encoding_interval": encoding_interval,
            "iframe_interval": encoding_interval,
            "gop": encoding_interval,
            "gov_length": encoding_interval,
            "codec_profile": _safe_text(_safe_attr(cfg, "H264Profile") or _safe_attr(cfg, "Profile"), 80),
            "encode_strategy": None,
        },
        "audio": {
            "codec": _safe_text(_safe_attr(audio_cfg, "Encoding"), 40),
            "sample_rate": _safe_number(_safe_attr(audio_cfg, "SampleRate")),
            "channels": _safe_number(_safe_attr(audio_cfg, "Channels")),
        },
        "stream_uri": fixed_uri,
        "display_endpoint": fixed_uri,
        "stream_path": stream_path,
        "rtsp_ready": rtsp_ready,
        "rtsp_host_source": "user_reachable" if rtsp_host else "onvif_host_fallback",
        "rtsp_reachable": {
            "host": _safe_text(rtsp_host or host, 255),
            "port": int(rtsp_port) if rtsp_port else None,
            "source": "user_reachable" if rtsp_host else "onvif_host_fallback",
        },
        "video_config_state": "ok" if cfg and not cfg_warning else "unavailable",
        "warnings": warnings,
    }


def fetch_onvif_profiles(host, port, username, password, rtsp_host=None, rtsp_port=None):
    cam, media_candidates = _prepare_camera(host, port, username, password)
    dev = cam.devicemgmt
    media, selected_media_xaddr = _get_media_service(cam, media_candidates)

    info = dev.GetDeviceInformation()
    profiles = media.GetProfiles()

    result = []
    for p in profiles:
        item = _profile_to_dict(p, media, username, password, host, port, rtsp_host=rtsp_host, rtsp_port=rtsp_port)
        item["media_service_xaddr"] = selected_media_xaddr
        result.append(item)

    suggested_main, suggested_sub = _suggest_profiles(result)
    for item in result:
        token = item.get("token")
        if token and token == suggested_main:
            item["suggested_role"] = "main"
        elif token and token == suggested_sub:
            item["suggested_role"] = "sub"

    return {
        "device": {
            "manufacturer": _safe_attr(info, "Manufacturer"),
            "model": _safe_attr(info, "Model"),
            "firmware": _safe_attr(info, "FirmwareVersion"),
            "serial_number": _safe_attr(info, "SerialNumber"),
        },
        "profiles": result,
        "suggested_main_profile_token": suggested_main,
        "suggested_sub_profile_token": suggested_sub,
        "rtsp_reachable": {
            "host": rtsp_host or host,
            "port": rtsp_port,
            "source": "user_reachable" if rtsp_host else "onvif_host_fallback",
        },
        "media_service_xaddr": selected_media_xaddr,
    }


def discover_onvif_devices(timeout_seconds: int = 5) -> dict:
    timeout = max(1, min(int(timeout_seconds or 5), 10))
    try:
        from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery
    except Exception:
        return {
            "ok": True,
            "discovery_supported": False,
            "code": "discovery_not_supported",
            "message": "ONVIF WS-Discovery is not available in this runtime.",
            "candidates": [],
            "warnings": [],
            "limitations": ["manual_onvif_probe_available", "no_broad_subnet_scan"],
            "timeout_seconds": timeout,
        }

    candidates = []
    warnings = []
    wsd = None
    try:
        wsd = WSDiscovery()
        wsd.start()
        services = wsd.searchServices(timeout=timeout) or []
        seen = set()
        for service in services[:20]:
            xaddrs = list(getattr(service, "getXAddrs", lambda: [])() or [])
            scopes = list(getattr(service, "getScopes", lambda: [])() or [])
            types = list(getattr(service, "getTypes", lambda: [])() or [])
            for xaddr in xaddrs[:3]:
                parsed = urlparse(str(xaddr))
                host = parsed.hostname
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                key = (host, port, parsed.path)
                if not host or key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "id": _candidate_id(host, port, "ws_discovery"),
                        "source": "ws_discovery",
                        "host": _safe_text(host, 120),
                        "port": int(port),
                        "xaddr_path": _safe_text(parsed.path or "/onvif/device_service", 160),
                        "scopes": [_safe_text(item, 120) for item in scopes[:10] if _safe_text(item, 120)],
                        "types": [_safe_text(item, 120) for item in types[:10] if _safe_text(item, 120)],
                        "warnings": [],
                    }
                )
    except TimeoutError:
        return {
            "ok": False,
            "discovery_supported": True,
            "code": "timeout",
            "message": "ONVIF discovery timed out.",
            "candidates": [],
            "warnings": [],
            "limitations": ["manual_onvif_probe_available", "no_broad_subnet_scan"],
            "timeout_seconds": timeout,
        }
    except Exception:
        return {
            "ok": False,
            "discovery_supported": True,
            "code": "runtime_network_limited",
            "message": "ONVIF discovery is unavailable in the current network/runtime.",
            "candidates": [],
            "warnings": ["manual_onvif_probe_available"],
            "limitations": ["no_broad_subnet_scan"],
            "timeout_seconds": timeout,
        }
    finally:
        if wsd is not None:
            try:
                wsd.stop()
            except Exception:
                pass

    return {
        "ok": True,
        "discovery_supported": True,
        "code": "ok" if candidates else "no_devices_found",
        "message": "ONVIF discovery completed." if candidates else "No ONVIF devices were found.",
        "candidates": candidates,
        "warnings": warnings,
        "limitations": ["no_broad_subnet_scan"],
        "timeout_seconds": timeout,
    }


def probe_onvif_device(host, port=80, username=None, password=None, rtsp_host=None, rtsp_port=None, timeout_seconds=5) -> dict:
    host = str(host or "").strip()
    if not host:
        raise ValueError("host_required")
    port = int(port or 80)
    rtsp_host = str(rtsp_host or host).strip()
    rtsp_port = int(rtsp_port or 554)

    cam, media_candidates = _prepare_camera(host, port, username or "", password or "")
    info = cam.devicemgmt.GetDeviceInformation()
    media, selected_media_xaddr = _get_media_service(cam, media_candidates)
    profiles = media.GetProfiles()
    profile_items = [
        _profile_to_dict(profile, media, username, password, host, port, rtsp_host=rtsp_host, rtsp_port=rtsp_port)
        for profile in profiles
    ]
    suggested_main, suggested_sub = _suggest_profiles(profile_items)
    stream_ready = any(item.get("rtsp_ready") for item in profile_items)
    if not profile_items:
        raise RuntimeError("profiles_unavailable")
    if not stream_ready:
        raise RuntimeError("stream_uri_unavailable")
    return {
        "ok": True,
        "code": "ok",
        "message": "ONVIF probe completed.",
        "device": {
            "manufacturer": _safe_text(_safe_attr(info, "Manufacturer"), 120),
            "model": _safe_text(_safe_attr(info, "Model"), 120),
            "firmware": _safe_text(_safe_attr(info, "FirmwareVersion"), 120),
            "serial_number": _safe_text(_safe_attr(info, "SerialNumber"), 120),
        },
        "onvif": {"host": host, "port": port, "status": "reachable"},
        "rtsp_reachable": {
            "host": rtsp_host,
            "port": rtsp_port,
            "source": "user_override" if (rtsp_host != host or rtsp_port != 554) else "onvif_host_fallback",
        },
        "media": {
            "status": "ok" if profile_items else "profiles_unavailable",
            "profile_count": len(profile_items),
            "stream_uri_status": "ok" if stream_ready else "stream_uri_unavailable",
            "media_service_xaddr_path": urlparse(selected_media_xaddr).path if selected_media_xaddr else None,
        },
        "profiles": profile_items[:20],
        "suggested_main_profile_token": suggested_main,
        "suggested_sub_profile_token": suggested_sub,
        "raw_secret_exposed": False,
    }


_MISSING = object()
_OPTIONS_UNSET = object()


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _unwrap_known_container(value, names: tuple[str, ...], *, require_single=False):
    current = value
    for _depth in range(4):
        if isinstance(current, (list, tuple)):
            if len(current) != 1:
                return None if require_single else list(current)
            current = current[0]
            continue
        nested = _MISSING
        for name in names:
            candidate = _safe_attr(current, name, _MISSING)
            if candidate is not _MISSING and candidate is not current:
                nested = candidate
                break
        if nested is _MISSING:
            return current
        current = nested
    return current


def _normalize_encoder_configuration(value):
    return _unwrap_known_container(
        value,
        (
            "Configuration",
            "VideoEncoderConfiguration",
            "GetVideoEncoderConfigurationResponse",
        ),
        require_single=True,
    )


def _normalize_encoder_options(value):
    return _unwrap_known_container(
        value,
        (
            "Options",
            "VideoEncoderConfigurationOptions",
            "GetVideoEncoderConfigurationOptionsResponse",
        ),
    )


def _safe_set(obj, name: str, value) -> None:
    if isinstance(obj, dict):
        obj[name] = value
        return
    values = getattr(obj, "__values__", None)
    if isinstance(values, dict) and name in values:
        values[name] = value
        return
    try:
        setattr(obj, name, value)
    except (AttributeError, TypeError) as exc:
        raise OnvifConfigurationError(
            "video_encoder_configuration_shape_unsupported",
            "The camera returned an unsupported ONVIF encoder configuration shape.",
        ) from exc


def _range_meta(value):
    if value is None:
        return None
    minimum = _safe_attr(value, "Min")
    maximum = _safe_attr(value, "Max")
    step = _safe_attr(value, "Step")
    if minimum is None and maximum is None and step is None:
        return None
    result = {"min": minimum, "max": maximum}
    if step is not None:
        result["step"] = step
    return result


def _option_roots(options):
    roots = []
    for item in _as_list(_normalize_encoder_options(options)):
        if item is None:
            continue
        roots.append(item)
        extension = _safe_attr(item, "Extension")
        if extension is not None:
            roots.extend(_as_list(extension))
    return roots


def _encoding_options(options, encoding):
    if not options or not encoding:
        return None
    matches = []
    candidates = {str(encoding).lower()}
    for root in _option_roots(options):
        for key in ("H264", "H265", "JPEG", "MPEG4"):
            if key.lower() not in candidates:
                continue
            direct = _safe_attr(root, key)
            if direct is not None:
                matches.extend(_as_list(direct))
    if not matches:
        return None
    return matches[0] if len(matches) == 1 else matches


def _resolution_options(encoding_options):
    result = []
    for option in _as_list(encoding_options):
        for item in _as_list(_safe_attr(option, "ResolutionsAvailable")):
            width = _safe_number(_safe_attr(item, "Width"))
            height = _safe_number(_safe_attr(item, "Height"))
            if width and height:
                value = f"{int(width)}x{int(height)}"
                if value not in result:
                    result.append(value)
    return result


def _single_range(encoding_options, *names: str):
    options = _as_list(encoding_options)
    if len(options) != 1:
        return None
    for name in names:
        value_range = _range_meta(_safe_attr(options[0], name))
        if value_range is not None:
            return value_range
    return None


def _top_level_range(options, name: str):
    roots = [item for item in _as_list(_normalize_encoder_options(options)) if item is not None]
    if len(roots) != 1:
        return None
    return _range_meta(_safe_attr(roots[0], name))


def _field_meta(
    name: str,
    value,
    readable: bool = True,
    writable: bool = False,
    options=None,
    value_range=None,
    *,
    state: str | None = None,
    reason_codes=None,
):
    if state is None:
        state = "writable" if writable else "read_only" if readable else "unavailable"
    return {
        "name": name,
        "value": value,
        "readable": bool(readable),
        "writable": bool(writable),
        "options": options or [],
        "range": value_range,
        "state": state,
        "reason_codes": sorted(set(reason_codes or [])),
    }


def _value_in_range(name: str, value, value_range: dict | None) -> None:
    if not value_range:
        return
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric")
    minimum = value_range.get("min")
    maximum = value_range.get("max")
    if minimum is not None and numeric < float(minimum):
        raise ValueError(f"{name} is below supported range")
    if maximum is not None and numeric > float(maximum):
        raise ValueError(f"{name} is above supported range")
    step = value_range.get("step")
    if step not in (None, 0, "0"):
        base = float(minimum or 0)
        step_value = float(step)
        offset = (numeric - base) / step_value
        if abs(offset - round(offset)) > 1e-9:
            raise ValueError(f"{name} does not match supported step")


def _validate_profile_config_request(config: dict, supported: dict) -> dict:
    allowed = {"codec", "resolution", "fps", "bitrate", "iframe_interval", "quality"}
    requested = {key: value for key, value in (config or {}).items() if value not in (None, "")}
    unknown = sorted(set(requested) - allowed)
    if unknown:
        raise ValueError(f"Unsupported ONVIF setting requested: {', '.join(unknown)}")

    validated = {}
    for key, value in requested.items():
        meta = supported.get(key) or {}
        if not meta.get("writable"):
            raise ValueError(f"ONVIF setting is not writable: {key}")
        options = [str(item) for item in (meta.get("options") or [])]
        if options and str(value) not in options:
            raise ValueError(f"ONVIF setting value is not supported: {key}")
        _value_in_range(key, value, meta.get("range"))
        validated[key] = value
    if not validated:
        raise ValueError("No writable ONVIF settings were requested")
    return validated


def _configuration_token(cfg, profile=None):
    for candidate in (
        _safe_attr(cfg, "token"),
        _safe_attr(cfg, "_token"),
        _safe_attr(cfg, "ConfigurationToken"),
        _safe_attr(_safe_attr(profile, "VideoEncoderConfiguration"), "token"),
        _safe_attr(_safe_attr(profile, "VideoEncoderConfiguration"), "_token"),
    ):
        value = _safe_text(candidate, 255)
        if value:
            return value
    return None


def _codec_specific_config(cfg):
    encoding = str(_safe_attr(cfg, "Encoding") or "").upper()
    for name in (encoding, "H264", "H265", "MPEG4"):
        if not name:
            continue
        value = _safe_attr(cfg, name)
        if value is not None:
            return value
    return None


def _config_values(cfg) -> dict:
    rc = _safe_attr(cfg, "RateControl")
    res = _safe_attr(cfg, "Resolution")
    codec_specific = _codec_specific_config(cfg)
    width = _safe_number(_safe_attr(res, "Width"))
    height = _safe_number(_safe_attr(res, "Height"))
    resolution = (
        f"{int(width)}x{int(height)}"
        if width is not None and height is not None
        else None
    )
    iframe_interval = _safe_number(_safe_attr(codec_specific, "GovLength"))
    if iframe_interval is None:
        iframe_interval = _safe_number(_safe_attr(rc, "EncodingInterval"))
    return {
        "codec": _safe_text(_safe_attr(cfg, "Encoding"), 40),
        "width": width,
        "height": height,
        "resolution": resolution,
        "fps": _safe_number(_safe_attr(rc, "FrameRateLimit")),
        "bitrate": _safe_number(_safe_attr(rc, "BitrateLimit")),
        "iframe_interval": iframe_interval,
        "quality": _safe_number(_safe_attr(cfg, "Quality")),
    }


def _empty_config_values() -> dict:
    return {
        "codec": None,
        "width": None,
        "height": None,
        "resolution": None,
        "fps": None,
        "bitrate": None,
        "iframe_interval": None,
        "quality": None,
    }


def _encoder_options_with_state(media, cfg, profile_token=None):
    token = _configuration_token(cfg)
    if media is None:
        return None, "video_encoder_options_unavailable"
    if not token and not profile_token:
        return None, "video_encoder_configuration_token_unavailable"
    payload = {}
    if token:
        payload["ConfigurationToken"] = token
    if profile_token:
        payload["ProfileToken"] = profile_token
    try:
        options = _normalize_encoder_options(
            media.GetVideoEncoderConfigurationOptions(payload)
        )
    except Exception:
        return None, "video_encoder_options_read_failed"
    if options is None:
        return None, "video_encoder_options_unavailable"
    return options, None


def _encoder_options(media, cfg, profile_token=None):
    options, _reason = _encoder_options_with_state(media, cfg, profile_token)
    return options


def _supported_video_fields(
    media,
    cfg,
    *,
    profile_token=None,
    config_token=None,
    current_reason=None,
    options=_OPTIONS_UNSET,
    options_reason=None,
) -> dict:
    values = _config_values(cfg) if cfg is not None else _empty_config_values()
    token = config_token or _configuration_token(cfg)
    if options is _OPTIONS_UNSET:
        options, options_reason = _encoder_options_with_state(
            media,
            cfg,
            profile_token,
        )
    encoding = values["codec"]
    encoding_options = _encoding_options(options, encoding)
    codec_options = [
        codec
        for codec in ("H264", "H265", "JPEG", "MPEG4")
        if _encoding_options(options, codec) is not None
    ]
    resolution_options = _resolution_options(encoding_options)
    fps_range = _single_range(encoding_options, "FrameRateRange")
    bitrate_range = _single_range(encoding_options, "BitrateRange")
    iframe_range = _single_range(
        encoding_options,
        "GovLengthRange",
        "EncodingIntervalRange",
    )
    quality_range = _top_level_range(options, "QualityRange")
    capabilities = {
        "codec": (codec_options, None),
        "resolution": (resolution_options, None),
        "fps": ([], fps_range),
        "bitrate": ([], bitrate_range),
        "iframe_interval": ([], iframe_range),
        "quality": ([], quality_range),
    }

    result = {}
    for name in ONVIF_VIDEO_CONFIG_FIELDS:
        value = values[name]
        readable = current_reason is None and value not in (None, "")
        field_options, value_range = capabilities[name]
        capability = bool(field_options or value_range)
        reasons = []
        if current_reason:
            reasons.append(current_reason)
            state = "error" if current_reason.endswith("read_failed") else "unavailable"
        elif not readable:
            reasons.append(f"video_encoder_{name}_current_unavailable")
            state = "unavailable"
        elif not token:
            reasons.append("video_encoder_configuration_token_unavailable")
            state = "unavailable"
        elif options_reason:
            reasons.append(options_reason)
            state = "error" if options_reason.endswith("read_failed") else "unavailable"
        elif not capability:
            reasons.append(f"video_encoder_{name}_unsupported")
            state = "unsupported"
        else:
            state = "writable"
        writable = bool(readable and token and capability and not options_reason)
        result[name] = _field_meta(
            name,
            value,
            readable,
            writable,
            field_options,
            value_range,
            state=state,
            reason_codes=reasons,
        )
    return result


def _get_video_encoder_config_truth(media, profile):
    embedded = _normalize_encoder_configuration(
        _safe_attr(profile, "VideoEncoderConfiguration")
    )
    if embedded is None:
        return None, None, "video_encoder_configuration_unavailable"
    token = _configuration_token(embedded, profile)
    if not token:
        return embedded, None, None
    try:
        cfg = _normalize_encoder_configuration(
            media.GetVideoEncoderConfiguration(
                {"ConfigurationToken": token}
            )
        )
    except Exception:
        return None, token, "video_encoder_configuration_read_failed"
    if cfg is None:
        return None, token, "video_encoder_configuration_unavailable"
    returned_token = _configuration_token(cfg)
    if returned_token and returned_token != token:
        return None, token, "video_encoder_configuration_token_mismatch"
    return cfg, token, None


def _profile_config_payload(
    media,
    profile,
    profile_token,
    selected_media_xaddr,
    *,
    cfg=None,
    config_token=None,
    current_reason=None,
):
    if cfg is None and current_reason is None:
        cfg, config_token, current_reason = _get_video_encoder_config_truth(
            media,
            profile,
        )
    options = None
    options_reason = None
    if current_reason is None:
        options, options_reason = _encoder_options_with_state(
            media,
            cfg,
            profile_token,
        )
    supported = _supported_video_fields(
        media,
        cfg,
        profile_token=profile_token,
        config_token=config_token,
        current_reason=current_reason,
        options=options,
        options_reason=options_reason,
    )
    reason_codes = sorted(
        {
            reason
            for meta in supported.values()
            for reason in meta.get("reason_codes", [])
        }
    )
    if current_reason:
        status = "error" if current_reason.endswith("read_failed") or current_reason.endswith("mismatch") else "unavailable"
    elif not config_token:
        status = "unavailable"
    elif options_reason:
        status = "error" if options_reason.endswith("read_failed") else "unavailable"
    elif not any(meta["readable"] for meta in supported.values()):
        status = "unavailable"
    elif any(meta["writable"] for meta in supported.values()):
        status = "ok"
    else:
        status = "unsupported"
    return {
        "profile_token": profile_token,
        "name": _safe_text(_safe_attr(profile, "Name"), 255),
        "media_service_xaddr": selected_media_xaddr,
        "status": status,
        "reason_codes": reason_codes,
        "current_read": current_reason is None and cfg is not None,
        "options_read": options_reason is None and options is not None,
        "config": _config_values(cfg) if cfg is not None else _empty_config_values(),
        "supported": supported,
    }


def get_onvif_profile_config(host, port, username, password, profile_token):
    cam, media_candidates = _prepare_camera(host, port, username, password)
    media, selected_media_xaddr = _get_media_service(cam, media_candidates)
    profile = _find_profile(media, profile_token)
    if not profile:
        raise OnvifConfigurationError(
            "onvif_profile_not_found",
            "The selected ONVIF profile is unavailable.",
        )
    return _profile_config_payload(
        media,
        profile,
        profile_token,
        selected_media_xaddr,
    )


def _normalize_requested_value(name: str, value):
    if name in {"fps", "bitrate", "iframe_interval"}:
        return int(value)
    if name == "quality":
        return float(value)
    return str(value)


def _config_values_equal(name: str, left, right) -> bool:
    if left is None or right is None:
        return left is right
    if name in {"fps", "bitrate", "iframe_interval", "quality"}:
        try:
            return abs(float(left) - float(right)) < 1e-9
        except (TypeError, ValueError):
            return False
    return str(left).strip().lower() == str(right).strip().lower()


def _apply_video_config_diff(cfg, requested: dict) -> None:
    if "codec" in requested:
        _safe_set(cfg, "Encoding", str(requested["codec"]))
    if "resolution" in requested:
        resolution = str(requested["resolution"])
        if "x" not in resolution.lower():
            raise OnvifConfigurationError(
                "video_encoder_resolution_invalid",
                "The requested ONVIF resolution is invalid.",
            )
        width, height = resolution.lower().split("x", 1)
        target = _safe_attr(cfg, "Resolution")
        if target is None:
            raise OnvifConfigurationError(
                "video_encoder_configuration_shape_unsupported",
                "The camera did not return a writable resolution object.",
            )
        _safe_set(target, "Width", int(width.strip()))
        _safe_set(target, "Height", int(height.strip()))
    rate_control = _safe_attr(cfg, "RateControl")
    if {"fps", "bitrate"}.intersection(requested) and rate_control is None:
        raise OnvifConfigurationError(
            "video_encoder_configuration_shape_unsupported",
            "The camera did not return a writable rate-control object.",
        )
    if "fps" in requested:
        _safe_set(rate_control, "FrameRateLimit", int(requested["fps"]))
    if "bitrate" in requested:
        _safe_set(rate_control, "BitrateLimit", int(requested["bitrate"]))
    if "iframe_interval" in requested:
        codec_specific = _codec_specific_config(cfg)
        if codec_specific is not None and _safe_attr(codec_specific, "GovLength") is not None:
            _safe_set(codec_specific, "GovLength", int(requested["iframe_interval"]))
        elif rate_control is not None and _safe_attr(rate_control, "EncodingInterval") is not None:
            _safe_set(rate_control, "EncodingInterval", int(requested["iframe_interval"]))
        else:
            raise OnvifConfigurationError(
                "video_encoder_configuration_shape_unsupported",
                "The camera did not return a writable I-frame interval.",
            )
    if "quality" in requested:
        _safe_set(cfg, "Quality", float(requested["quality"]))


def update_onvif_profile(host, port, username, password, profile_token, config):
    cam, media_candidates = _prepare_camera(host, port, username, password)
    media, selected_media_xaddr = _get_media_service(cam, media_candidates)
    profile = _find_profile(media, profile_token)
    if not profile:
        raise OnvifConfigurationError(
            "onvif_profile_not_found",
            "The selected ONVIF profile is unavailable.",
        )
    cfg, config_token, current_reason = _get_video_encoder_config_truth(
        media,
        profile,
    )
    if current_reason or cfg is None or not config_token:
        raise OnvifConfigurationError(
            current_reason or "video_encoder_configuration_token_unavailable",
            "The current ONVIF encoder configuration cannot be read safely.",
        )
    supported = _supported_video_fields(
        media,
        cfg,
        profile_token=profile_token,
        config_token=config_token,
    )
    requested = _validate_profile_config_request(config or {}, supported)
    current_values = _config_values(cfg)
    changed = {
        name: _normalize_requested_value(name, value)
        for name, value in requested.items()
        if not _config_values_equal(name, current_values.get(name), value)
    }
    if not changed:
        payload = _profile_config_payload(
            media,
            profile,
            profile_token,
            selected_media_xaddr,
            cfg=cfg,
            config_token=config_token,
        )
        payload.update(
            {
                "ok": True,
                "changed": False,
                "changed_fields": [],
                "verification": {"status": "not_needed", "matched": True},
            }
        )
        return payload

    try:
        vendor_configuration = copy.deepcopy(cfg)
    except Exception:
        vendor_configuration = cfg
    _apply_video_config_diff(vendor_configuration, changed)
    try:
        media.SetVideoEncoderConfiguration(
            {
                "Configuration": vendor_configuration,
                "ForcePersistence": True,
            }
        )
    except Exception as exc:
        raise OnvifConfigurationError(
            "video_encoder_configuration_set_failed",
            "The camera refused the ONVIF encoder configuration update.",
        ) from exc

    verified_cfg, verified_token, verified_reason = _get_video_encoder_config_truth(
        media,
        profile,
    )
    if verified_reason or verified_cfg is None:
        raise OnvifConfigurationError(
            verified_reason or "video_encoder_configuration_verify_failed",
            "The updated ONVIF encoder configuration could not be verified.",
        )
    verified_values = _config_values(verified_cfg)
    mismatched = [
        name
        for name, value in changed.items()
        if not _config_values_equal(name, verified_values.get(name), value)
    ]
    if mismatched:
        raise OnvifConfigurationError(
            "video_encoder_configuration_mismatch",
            "The camera returned different encoder values after the update.",
        )
    payload = _profile_config_payload(
        media,
        profile,
        profile_token,
        selected_media_xaddr,
        cfg=verified_cfg,
        config_token=verified_token or config_token,
    )
    payload.update(
        {
            "ok": True,
            "changed": True,
            "changed_fields": sorted(changed),
            "verification": {"status": "matched", "matched": True},
        }
    )
    return payload


def _ptz_limits() -> dict:
    return {
        "actions": ["stop", "move", "zoom"],
        "directions": sorted(PTZ_DIRECTIONS),
        "zoom_directions": sorted(PTZ_ZOOM_DIRECTIONS),
        "duration_seconds": {
            "min": PTZ_MIN_DURATION_SECONDS,
            "max": PTZ_MAX_DURATION_SECONDS,
            "required_for": ["move", "zoom"],
        },
        "speed": {
            "min": PTZ_MIN_SPEED,
            "max": PTZ_MAX_SPEED,
            "default": 0.1,
        },
        "real_execution_requires": ["dry_run_false", "supported_ptz_camera", "bounded_duration", "automatic_stop"],
    }


def ptz_command_limits() -> dict:
    return _ptz_limits()


def _safe_range_axis(axis) -> dict | None:
    value_range = _safe_attr(axis, "XRange") or _safe_attr(axis, "YRange")
    return _range_meta(value_range)


def _ptz_space_items(spaces, name: str) -> list[dict]:
    result = []
    for item in _as_list(_safe_attr(spaces, name)):
        entry = {"uri": _safe_text(_safe_attr(item, "URI"), 180)}
        x_range = _range_meta(_safe_attr(item, "XRange"))
        y_range = _range_meta(_safe_attr(item, "YRange"))
        if x_range:
            entry["x"] = x_range
        if y_range:
            entry["y"] = y_range
        result.append(entry)
    return result


def _normalize_ptz_node(node) -> dict:
    spaces = _safe_attr(node, "SupportedPTZSpaces")
    pan_tilt_spaces = (
        _ptz_space_items(spaces, "ContinuousPanTiltVelocitySpace")
        + _ptz_space_items(spaces, "RelativePanTiltTranslationSpace")
        + _ptz_space_items(spaces, "AbsolutePanTiltPositionSpace")
    )
    zoom_spaces = (
        _ptz_space_items(spaces, "ContinuousZoomVelocitySpace")
        + _ptz_space_items(spaces, "RelativeZoomTranslationSpace")
        + _ptz_space_items(spaces, "AbsoluteZoomPositionSpace")
    )
    return {
        "token": _safe_text(_safe_attr(node, "token"), 120),
        "name": _safe_text(_safe_attr(node, "Name"), 120),
        "can_pan_tilt": bool(pan_tilt_spaces),
        "can_zoom": bool(zoom_spaces),
        "spaces": {
            "pan_tilt": pan_tilt_spaces[:8],
            "zoom": zoom_spaces[:8],
        },
    }


def _prepare_ptz_context(host, port, username, password):
    cam, media_candidates = _prepare_camera(host, port, username or "", password or "")
    ptz = cam.create_ptz_service()
    media, _selected_media_xaddr = _get_media_service(cam, media_candidates)
    profiles = media.GetProfiles()
    profile_token = _safe_attr(profiles[0], "token") if profiles else None
    return cam, ptz, profile_token


def get_onvif_ptz_capabilities(host, port, username, password) -> dict:
    try:
        _cam, ptz, profile_token = _prepare_ptz_context(host, port, username, password)
    except Exception as exc:
        text = str(exc).lower()
        if "ptz" in text or "service" in text or "wsdl" in text:
            return {
                "ok": True,
                "supported": False,
                "source": "unsupported",
                "can_pan_tilt": False,
                "can_zoom": False,
                "can_stop": False,
                "can_presets": False,
                "limits": _ptz_limits(),
                "warnings": ["ptz_service_unavailable"],
                "unsupported_reasons": ["ptz_service_unavailable"],
                "raw_secret_exposed": False,
            }
        raise

    nodes = []
    warnings = []
    try:
        nodes = [_normalize_ptz_node(node) for node in _as_list(ptz.GetNodes())]
    except Exception:
        warnings.append("ptz_nodes_unavailable")

    can_pan_tilt = any(item.get("can_pan_tilt") for item in nodes)
    can_zoom = any(item.get("can_zoom") for item in nodes)
    supported = bool(can_pan_tilt or can_zoom or profile_token)
    return {
        "ok": True,
        "supported": supported,
        "source": "onvif_ptz_service" if supported else "unsupported",
        "can_pan_tilt": can_pan_tilt,
        "can_zoom": can_zoom,
        "can_stop": supported,
        "can_presets": False,
        "profile_token_available": bool(profile_token),
        "nodes": nodes[:8],
        "limits": _ptz_limits(),
        "warnings": warnings,
        "unsupported_reasons": [] if supported else ["ptz_capabilities_not_reported"],
        "raw_secret_exposed": False,
    }


def _bounded_float(value, *, name: str, minimum: float, maximum: float, required: bool = True) -> float | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{name}_required")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}_must_be_numeric")
    if number < minimum or number > maximum:
        raise ValueError(f"{name}_out_of_bounds")
    return number


def validate_ptz_command_payload(payload: dict | None) -> dict:
    payload = payload or {}
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"stop", "move", "zoom"}:
        raise ValueError("unsupported_ptz_action")

    validation_only = bool(payload.get("validation_only"))
    dry_run = bool(payload.get("dry_run", True))
    speed = _bounded_float(
        payload.get("speed", _ptz_limits()["speed"]["default"]),
        name="speed",
        minimum=PTZ_MIN_SPEED,
        maximum=PTZ_MAX_SPEED,
        required=False,
    )

    command = {
        "action": action,
        "execution_mode": "validation_only" if validation_only else "dry_run" if dry_run else "execute_requested",
        "validation_only": validation_only,
        "dry_run": dry_run,
        "speed": speed or _ptz_limits()["speed"]["default"],
        "duration_seconds": None,
        "direction": None,
        "pan": 0.0,
        "tilt": 0.0,
        "zoom": 0.0,
        "limits": _ptz_limits(),
    }

    if action == "stop":
        return command

    duration = _bounded_float(
        payload.get("duration_seconds"),
        name="duration_seconds",
        minimum=PTZ_MIN_DURATION_SECONDS,
        maximum=PTZ_MAX_DURATION_SECONDS,
        required=True,
    )
    command["duration_seconds"] = duration

    direction = str(payload.get("direction") or "").strip().lower()
    if action == "move":
        if direction not in PTZ_DIRECTIONS:
            raise ValueError("unsupported_ptz_direction")
        pan, tilt = PTZ_DIRECTIONS[direction]
        command.update({
            "direction": direction,
            "pan": pan * command["speed"],
            "tilt": tilt * command["speed"],
        })
    elif action == "zoom":
        if direction not in PTZ_ZOOM_DIRECTIONS:
            raise ValueError("unsupported_ptz_zoom_direction")
        command.update({
            "direction": direction,
            "zoom": PTZ_ZOOM_DIRECTIONS[direction] * command["speed"],
        })

    return command


def ptz_validation_response(command: dict, message: str = "PTZ command validated.") -> dict:
    return {
        "ok": True,
        "action": command["action"],
        "execution_mode": command["execution_mode"],
        "payload_valid": True,
        "camera_capability_checked": False,
        "camera_supported": None,
        "command_executable": False,
        "executed": False,
        "physical_camera_mutated": False,
        "camera_stopped": False,
        "duration_seconds": command.get("duration_seconds"),
        "warnings": ["physical_execution_not_requested"],
        "message": message,
        "raw_secret_exposed": False,
    }


def compatibility_domain(status: str, reason_codes=None, evidence_level: str = "not_checked", **extra) -> dict:
    return {
        "status": status,
        "reason_codes": list(reason_codes or []),
        "evidence_level": evidence_level,
        **extra,
    }


def camera_static_onvif_state(camera) -> dict:
    protocol = str(getattr(camera, "protocol", "") or "").lower()
    configured = bool(
        protocol == "onvif"
        and getattr(camera, "host", None)
        and getattr(camera, "port", None)
        and getattr(camera, "username", None)
        and getattr(camera, "password_encrypted", None)
    )
    return {
        "protocol": protocol,
        "enabled": bool(getattr(camera, "enabled", False)),
        "deleted": bool(getattr(camera, "deleted_at", None)),
        "configured": configured,
        "misconfigured": protocol == "onvif" and not configured,
        "reason_codes": [] if configured else (["not_onvif"] if protocol != "onvif" else ["onvif_credentials_required"]),
    }


def summarize_main_sub_assignment(camera, profiles: list[dict] | None = None) -> dict:
    profiles = profiles or []
    main_path = rtsp_path_from_uri(getattr(camera, "rtsp_main_url", None)) or getattr(camera, "rtsp_main_url", None)
    sub_path = rtsp_path_from_uri(getattr(camera, "rtsp_sub_url", None)) or getattr(camera, "rtsp_sub_url", None)
    main_token = getattr(camera, "onvif_profile_token", None)
    path_counts: dict[str, int] = {}
    token_counts: dict[str, int] = {}
    for profile in profiles:
        token = str(profile.get("token") or "")
        if token:
            token_counts[token] = token_counts.get(token, 0) + 1
        path = profile.get("stream_path") or rtsp_path_from_uri(profile.get("stream_uri"))
        if path:
            path_counts[path] = path_counts.get(path, 0) + 1

    reason_codes = []
    if main_token and token_counts.get(str(main_token), 0) == 1:
        main_confidence = "token_exact"
    elif main_path and path_counts.get(str(main_path), 0) == 1:
        main_confidence = "path_unique"
    elif main_path and path_counts.get(str(main_path), 0) > 1:
        main_confidence = "path_ambiguous"
        reason_codes.append("main_stream_path_not_unique")
    elif main_token:
        main_confidence = "token_unverified"
        reason_codes.append("main_profile_token_not_seen")
    else:
        main_confidence = "not_configured"
        reason_codes.append("main_stream_not_configured")

    if sub_path and path_counts.get(str(sub_path), 0) == 1:
        sub_confidence = "path_unique"
    elif sub_path and path_counts.get(str(sub_path), 0) > 1:
        sub_confidence = "path_ambiguous"
        reason_codes.append("sub_stream_path_not_unique")
    elif sub_path:
        sub_confidence = "path_unverified"
        reason_codes.append("sub_stream_path_not_seen")
    else:
        sub_confidence = "not_configured"
        reason_codes.append("sub_stream_not_configured")

    if main_path and sub_path and str(main_path) == str(sub_path):
        reason_codes.append("main_sub_paths_identical")

    status = "ok"
    if any("ambiguous" in value for value in (main_confidence, sub_confidence)) or "main_sub_paths_identical" in reason_codes:
        status = "warning"
    elif reason_codes:
        status = "unknown"

    return {
        "status": status,
        "main_confidence": main_confidence,
        "sub_confidence": sub_confidence,
        "reason_codes": reason_codes,
        "profile_count": len(profiles),
    }


def normalize_ptz_compatibility(ptz_result: dict | None) -> dict:
    if not ptz_result:
        return compatibility_domain("unknown", ["ptz_not_checked"])
    if ptz_result.get("source") == "not_onvif":
        return compatibility_domain("not_checked", ["not_onvif"], "static", supported=False)
    if not ptz_result.get("supported"):
        return compatibility_domain("unsupported", ptz_result.get("unsupported_reasons") or ["ptz_unsupported"], "real_runtime", supported=False)
    nodes = ptz_result.get("nodes") or []
    incomplete = bool(ptz_result.get("profile_token_available") and not nodes)
    can_move = bool(ptz_result.get("can_pan_tilt") or ptz_result.get("can_zoom"))
    if incomplete or not can_move:
        return compatibility_domain(
            "warning",
            ["ptz_capability_incomplete"],
            "real_runtime",
            supported="partial",
            can_pan_tilt=bool(ptz_result.get("can_pan_tilt")),
            can_zoom=bool(ptz_result.get("can_zoom")),
        )
    return compatibility_domain(
        "ok",
        [],
        "real_runtime",
        supported=True,
        can_pan_tilt=bool(ptz_result.get("can_pan_tilt")),
        can_zoom=bool(ptz_result.get("can_zoom")),
    )


def check_onvif_events_feasibility(host, port, username, password) -> dict:
    cam = ONVIFCamera(host, int(port), username or "", password or "", wsdl_dir())
    services = []
    try:
        services = cam.devicemgmt.GetServices({"IncludeCapability": True}) or []
    except Exception:
        services = []

    event_services = []
    for service in services:
        namespace = str(getattr(service, "Namespace", "") or "").lower()
        xaddr = str(getattr(service, "XAddr", "") or "")
        if "event" in namespace or "event" in xaddr.lower():
            event_services.append(service)

    if not event_services:
        try:
            caps = cam.devicemgmt.GetCapabilities({"Category": "Events"})
            events = _safe_attr(caps, "Events")
            if events:
                event_services.append(events)
        except Exception:
            pass

    if event_services:
        return {
            "events_supported": True,
            "events_status": "supported",
            "reason_codes": [],
            "limitations": ["feasibility_only_no_subscription_started"],
            "raw_secret_exposed": False,
        }
    return {
        "events_supported": False,
        "events_status": "unsupported",
        "reason_codes": ["event_service_not_reported"],
        "limitations": ["feasibility_only_no_subscription_started"],
        "raw_secret_exposed": False,
    }


def select_profile_config_health_target(camera, profiles: list[dict]) -> dict:
    assigned = _safe_text(getattr(camera, "onvif_profile_token", None), 255)
    tokens = {
        _safe_text(item.get("token"), 255)
        for item in profiles
        if isinstance(item, dict) and _safe_text(item.get("token"), 255)
    }
    if assigned:
        if assigned in tokens:
            return {
                "profile_token": assigned,
                "source": "assigned_main",
                "reason_codes": [],
            }
        return {
            "profile_token": None,
            "source": "assigned_main",
            "reason_codes": ["assigned_main_profile_not_reported"],
        }
    if len(tokens) == 1:
        return {
            "profile_token": next(iter(tokens)),
            "source": "single_profile_fallback",
            "reason_codes": ["main_assignment_missing_single_profile_fallback"],
        }
    return {
        "profile_token": None,
        "source": "none",
        "reason_codes": ["profile_config_exact_profile_unavailable"],
    }


def build_onvif_health_contract(
    camera,
    *,
    password: str | None = None,
    profiles_result: dict | None = None,
    ptz_result: dict | None = None,
    events_result: dict | None = None,
    profile_config_result: dict | None = None,
    profile_config_checked: bool = False,
    check_performed: bool = False,
    checked_at: str | None = None,
) -> dict:
    static = camera_static_onvif_state(camera)
    protocol = static["protocol"]
    effective_misconfigured = bool(static["misconfigured"] or (check_performed and protocol == "onvif" and password is None))
    profiles = (profiles_result or {}).get("profiles") or []
    assignment = summarize_main_sub_assignment(camera, profiles)
    ptz_domain = normalize_ptz_compatibility(ptz_result)

    if protocol != "onvif":
        onvif_domain = compatibility_domain("unsupported", ["not_onvif"], "static")
    elif effective_misconfigured:
        onvif_domain = compatibility_domain("error", ["onvif_credentials_required"], "static")
    elif check_performed and profiles_result:
        onvif_domain = compatibility_domain("ok", [], "real_runtime")
    elif check_performed:
        onvif_domain = compatibility_domain("error", ["onvif_check_failed"], "real_runtime")
    else:
        onvif_domain = compatibility_domain("not_checked", ["explicit_check_required"])

    if profiles_result:
        media_domain = compatibility_domain("ok" if profiles else "warning", [] if profiles else ["profiles_empty"], "real_runtime", profile_count=len(profiles))
        stream_status = "ok" if any(item.get("stream_path") for item in profiles) else "warning"
        stream_domain = compatibility_domain(stream_status, [] if stream_status == "ok" else ["stream_uri_path_unavailable"], "real_runtime")
        if profile_config_checked and profile_config_result:
            config_status = str(profile_config_result.get("status") or "error")
            if config_status == "ok":
                domain_status = "ok"
            elif config_status == "unsupported":
                domain_status = "unsupported"
            else:
                domain_status = "error"
            config_domain = compatibility_domain(
                domain_status,
                profile_config_result.get("reason_codes") or [],
                "real_runtime",
                profile_token=profile_config_result.get("profile_token"),
                profile_source=profile_config_result.get("profile_source"),
                current_read=bool(profile_config_result.get("current_read")),
                options_read=bool(profile_config_result.get("options_read")),
                writable_fields=sorted(
                    name
                    for name, meta in (profile_config_result.get("supported") or {}).items()
                    if isinstance(meta, dict) and meta.get("writable") is True
                ),
            )
        elif profile_config_checked:
            config_domain = compatibility_domain(
                "error",
                ["profile_config_check_failed"],
                "real_runtime",
            )
        else:
            config_domain = compatibility_domain(
                "not_checked",
                ["profile_config_not_checked"],
                "not_checked",
            )
    elif protocol == "onvif" and check_performed:
        media_domain = compatibility_domain("error", ["profiles_unavailable"], "real_runtime", profile_count=0)
        stream_domain = compatibility_domain("unknown", ["stream_uri_not_checked"], "real_runtime")
        config_domain = compatibility_domain("unknown", ["profile_config_not_checked"], "not_checked")
    else:
        media_domain = compatibility_domain("not_checked", ["explicit_check_required"])
        stream_domain = compatibility_domain("not_checked", ["explicit_check_required"])
        config_domain = compatibility_domain("not_checked", ["explicit_check_required"])

    if getattr(camera, "rtsp_host", None) or getattr(camera, "rtsp_port", None):
        rtsp_domain = compatibility_domain("ok", [], "static", source="user_reachable_override")
    elif getattr(camera, "rtsp_main_url", None) or getattr(camera, "rtsp_sub_url", None):
        rtsp_domain = compatibility_domain("unknown", ["rtsp_url_configured_reachability_not_checked"], "static")
    else:
        rtsp_domain = compatibility_domain("not_checked", ["rtsp_not_configured_or_not_checked"])

    if events_result:
        events_status = events_result.get("events_status") or "unknown"
        events_domain = compatibility_domain(
            "ok" if events_status == "supported" else "unsupported",
            events_result.get("reason_codes") or [],
            "real_runtime",
            events_supported=bool(events_result.get("events_supported")),
            limitations=events_result.get("limitations") or [],
        )
    else:
        events_domain = compatibility_domain("not_checked", ["events_feasibility_not_checked"])

    recorder_domain = compatibility_domain(
        "unknown",
        ["recorder_runtime_not_queried_by_onvif_health"],
        "static",
        recording_mode=getattr(camera, "recording_mode", None),
        default_record_stream=getattr(camera, "default_record_stream", None),
    )
    redaction_domain = compatibility_domain("ok", [], "static", raw_secret_exposed=False)

    assignment_details = {key: value for key, value in assignment.items() if key not in {"status", "reason_codes"}}
    domains = {
        "onvif_service": onvif_domain,
        "media_profiles": media_domain,
        "stream_uri": stream_domain,
        "rtsp_reachable_override": rtsp_domain,
        "main_sub_assignment": compatibility_domain(
            assignment["status"],
            assignment["reason_codes"],
            "real_runtime" if profiles_result else "static",
            **assignment_details,
        ),
        "profile_config_options": config_domain,
        "ptz": ptz_domain,
        "events": events_domain,
        "recorder_contract": recorder_domain,
        "redaction": redaction_domain,
    }

    onvif_status = onvif_domain["status"]
    if onvif_status == "ok":
        onvif_availability = "reachable"
    elif protocol != "onvif":
        onvif_availability = "unsupported"
    elif effective_misconfigured:
        onvif_availability = "misconfigured"
    elif check_performed:
        onvif_availability = "unreachable"
    else:
        onvif_availability = "unknown"

    return {
        "ok": True,
        "camera": {
            "id": getattr(camera, "id", None),
            "name": getattr(camera, "name", None),
            "enabled": bool(getattr(camera, "enabled", False)),
            "deleted": bool(getattr(camera, "deleted_at", None)),
            "protocol": protocol,
        },
        "checked_at": checked_at,
        "persisted_last_check": False,
        "check_performed": bool(check_performed),
        "availability": {
            "onvif_status": onvif_availability,
            "rtsp_status": "not_checked",
            "recorder_status": "not_checked",
            "live_status": "not_checked",
        },
        "onvif_configured": bool(static["configured"] and not effective_misconfigured),
        "onvif_misconfigured": bool(effective_misconfigured),
        "ptz_status": domains["ptz"]["status"],
        "profile_summary": {
            "profile_count": len(profiles),
            "main_sub_assignment": assignment,
        },
        "event_service": {
            "status": domains["events"]["status"],
            "supported": domains["events"].get("events_supported"),
            "limitations": domains["events"].get("limitations", []),
        },
        "compatibility_matrix": domains,
        "warnings": sorted({reason for domain in domains.values() for reason in domain.get("reason_codes", []) if reason}),
        "limitations": [
            "no_background_polling",
            "no_physical_camera_mutation",
            "rtsp_and_recorder_status_are_separate_and_not_checked_here",
        ],
        "raw_secret_exposed": False,
    }


def execute_onvif_ptz_command(host, port, username, password, payload: dict) -> dict:
    command = validate_ptz_command_payload(payload)
    if command["validation_only"] or command["dry_run"]:
        return ptz_validation_response(command)

    _cam, ptz, profile_token = _prepare_ptz_context(host, port, username, password)
    if not profile_token:
        raise RuntimeError("ptz_profile_token_unavailable")

    stopped = False
    try:
        if command["action"] == "stop":
            ptz.Stop({"ProfileToken": profile_token, "PanTilt": True, "Zoom": True})
            stopped = True
        else:
            velocity = {}
            if command["action"] == "move":
                velocity["PanTilt"] = {"x": command["pan"], "y": command["tilt"]}
            if command["action"] == "zoom":
                velocity["Zoom"] = {"x": command["zoom"]}
            ptz.ContinuousMove({"ProfileToken": profile_token, "Velocity": velocity})
            time.sleep(float(command["duration_seconds"]))
            ptz.Stop({"ProfileToken": profile_token, "PanTilt": True, "Zoom": True})
            stopped = True
    finally:
        if command["action"] != "stop" and not stopped:
            try:
                ptz.Stop({"ProfileToken": profile_token, "PanTilt": True, "Zoom": True})
                stopped = True
            except Exception:
                stopped = False

    return {
        "ok": True,
        "action": command["action"],
        "execution_mode": "executed",
        "executed": True,
        "physical_camera_mutated": command["action"] != "stop",
        "camera_stopped": stopped,
        "duration_seconds": command.get("duration_seconds"),
        "warnings": [] if stopped else ["stop_not_verified"],
        "message": "PTZ command executed with bounded safety stop." if stopped else "PTZ command execution finished but stop could not be verified.",
        "raw_secret_exposed": False,
    }
