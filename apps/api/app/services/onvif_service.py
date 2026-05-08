from pathlib import Path
import hashlib
from urllib.parse import quote, urlparse, urlsplit, urlunparse, urlunsplit

from onvif import ONVIFCamera

MEDIA_XADDR_KEYS = (
    "media",
    "media2",
    "http://www.onvif.org/ver10/media/wsdl",
    "http://www.onvif.org/ver20/media/wsdl",
)


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
    return getattr(obj, name, default) if obj is not None else default


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
    return next((p for p in profiles if getattr(p, "token", None) == profile_token), None)


def _get_video_encoder_config_with_state(media, profile):
    vec = _safe_attr(profile, "VideoEncoderConfiguration")
    if not vec:
        return None, "missing_video_encoder_configuration"

    token = _safe_attr(vec, "token")
    if not token:
        return vec, None

    try:
        return media.GetVideoEncoderConfiguration({"ConfigurationToken": token}), None
    except Exception as e:
        return vec, f"video_encoder_configuration_read_failed: {e.__class__.__name__}"


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
    main_hint = any(value in name for value in ("main", "primary", "stream1", "profile1", "high"))
    sub_hint = any(value in name for value in ("sub", "secondary", "stream2", "profile2", "low"))

    if prefer_sub:
        return (1 if sub_hint else 0, -pixels if pixels else 0, -fps if fps else 0, item.get("name") or "")
    return (1 if main_hint else 0, pixels, fps, item.get("name") or "")


def _suggest_profiles(profiles: list[dict]) -> tuple[str | None, str | None]:
    if not profiles:
        return None, None

    main = max(profiles, key=lambda item: _profile_score(item, prefer_sub=False))
    if len(profiles) == 1:
        return main.get("token"), main.get("token")

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


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


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


def _encoding_options(options, encoding):
    if not options or not encoding:
        return None
    candidates = [str(encoding), str(encoding).upper(), str(encoding).lower()]
    for candidate in candidates:
        direct = _safe_attr(options, candidate)
        if direct is not None:
            return direct
    return None


def _resolution_options(encoding_options):
    result = []
    for item in _as_list(_safe_attr(encoding_options, "ResolutionsAvailable")):
        width = _safe_attr(item, "Width")
        height = _safe_attr(item, "Height")
        if width and height:
            value = f"{width}x{height}"
            if value not in result:
                result.append(value)
    return result


def _field_meta(name: str, value, readable: bool = True, writable: bool = False, options=None, value_range=None):
    return {
        "name": name,
        "value": value,
        "readable": bool(readable),
        "writable": bool(writable),
        "options": options or [],
        "range": value_range,
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


def _supported_video_fields(media, cfg) -> dict:
    rc = _safe_attr(cfg, "RateControl")
    res = _safe_attr(cfg, "Resolution")
    resolution = f"{_safe_attr(res, 'Width')}x{_safe_attr(res, 'Height')}" if res else None
    options = _encoder_options(media, cfg)
    encoding = _safe_attr(cfg, "Encoding")
    encoding_options = _encoding_options(options, encoding)
    codec_options = [
        codec
        for codec in ("H264", "H265", "JPEG", "MPEG4")
        if _encoding_options(options, codec) is not None
    ]

    resolution_options = _resolution_options(encoding_options)
    fps_range = _range_meta(_safe_attr(encoding_options, "FrameRateRange"))
    bitrate_range = _range_meta(_safe_attr(encoding_options, "BitrateRange"))
    iframe_range = (
        _range_meta(_safe_attr(encoding_options, "GovLengthRange"))
        or _range_meta(_safe_attr(encoding_options, "EncodingIntervalRange"))
    )
    quality_range = _range_meta(_safe_attr(options, "QualityRange"))

    return {
        "codec": _field_meta("codec", encoding, bool(encoding), bool(codec_options), codec_options),
        "resolution": _field_meta("resolution", resolution, bool(resolution), bool(resolution_options), resolution_options),
        "fps": _field_meta("fps", _safe_attr(rc, "FrameRateLimit"), _safe_attr(rc, "FrameRateLimit") is not None, bool(fps_range), value_range=fps_range),
        "bitrate": _field_meta("bitrate", _safe_attr(rc, "BitrateLimit"), _safe_attr(rc, "BitrateLimit") is not None, bool(bitrate_range), value_range=bitrate_range),
        "iframe_interval": _field_meta("iframe_interval", _safe_attr(rc, "EncodingInterval"), _safe_attr(rc, "EncodingInterval") is not None, bool(iframe_range), value_range=iframe_range),
        "quality": _field_meta("quality", _safe_attr(cfg, "Quality"), _safe_attr(cfg, "Quality") is not None, bool(quality_range), value_range=quality_range),
    }


def _encoder_options(media, cfg):
    token = _safe_attr(cfg, "token")
    if not token:
        return None
    try:
        return media.GetVideoEncoderConfigurationOptions({"ConfigurationToken": token})
    except Exception:
        return None


def get_onvif_profile_config(host, port, username, password, profile_token):
    cam, media_candidates = _prepare_camera(host, port, username, password)
    media, selected_media_xaddr = _get_media_service(cam, media_candidates)

    profile = _find_profile(media, profile_token)
    if not profile:
        raise Exception("Профиль не найден")

    cfg = _get_video_encoder_config_from_profile(media, profile)

    if not cfg:
        return {
            "profile_token": profile_token,
            "name": _safe_attr(profile, "Name"),
            "media_service_xaddr": selected_media_xaddr,
            "config": {
                "codec": None,
                "width": None,
                "height": None,
                "resolution": None,
                "fps": None,
                "bitrate": None,
                "iframe_interval": None,
                "quality": None,
            },
            "supported": {},
        }

    rc = _safe_attr(cfg, "RateControl")
    res = _safe_attr(cfg, "Resolution")
    resolution = f"{_safe_attr(res, 'Width')}x{_safe_attr(res, 'Height')}" if res else None
    supported = _supported_video_fields(media, cfg)

    return {
        "profile_token": profile_token,
        "name": _safe_attr(profile, "Name"),
        "media_service_xaddr": selected_media_xaddr,
        "config": {
            "codec": _safe_attr(cfg, "Encoding"),
            "width": _safe_attr(res, "Width"),
            "height": _safe_attr(res, "Height"),
            "resolution": resolution,
            "fps": _safe_attr(rc, "FrameRateLimit"),
            "bitrate": _safe_attr(rc, "BitrateLimit"),
            "iframe_interval": _safe_attr(rc, "EncodingInterval"),
            "quality": _safe_attr(cfg, "Quality"),
        },
        "supported": supported,
    }


def update_onvif_profile(host, port, username, password, profile_token, config):
    cam, media_candidates = _prepare_camera(host, port, username, password)
    media, selected_media_xaddr = _get_media_service(cam, media_candidates)

    profile = _find_profile(media, profile_token)
    if not profile:
        raise Exception("Профиль не найден")

    cfg = _get_video_encoder_config_from_profile(media, profile)
    if not cfg:
        raise Exception("Не удалось получить VideoEncoderConfiguration")

    supported = _supported_video_fields(media, cfg)
    config = _validate_profile_config_request(config or {}, supported)

    if config.get("codec"):
        cfg.Encoding = str(config["codec"])

    resolution = config.get("resolution")
    if resolution and "x" in str(resolution).lower():
        width_value, height_value = str(resolution).lower().split("x", 1)
        config["width"] = int(width_value.strip())
        config["height"] = int(height_value.strip())

    if config.get("width") and config.get("height"):
        if not getattr(cfg, "Resolution", None):
            raise Exception("Камера не отдала Resolution в ONVIF-конфиге")
        cfg.Resolution.Width = int(config["width"])
        cfg.Resolution.Height = int(config["height"])

    rate_control_fields = {"fps", "bitrate", "iframe_interval"}
    if rate_control_fields.intersection(config) and not getattr(cfg, "RateControl", None):
        raise Exception("Камера не отдала RateControl в ONVIF-конфиге")

    if config.get("fps") and getattr(cfg, "RateControl", None):
        cfg.RateControl.FrameRateLimit = int(config["fps"])

    if config.get("bitrate") and getattr(cfg, "RateControl", None):
        cfg.RateControl.BitrateLimit = int(config["bitrate"])

    if config.get("iframe_interval") and getattr(cfg, "RateControl", None):
        cfg.RateControl.EncodingInterval = int(config["iframe_interval"])

    if config.get("quality") not in (None, ""):
        cfg.Quality = float(config["quality"])

    media.SetVideoEncoderConfiguration({
        "Configuration": cfg,
        "ForcePersistence": True
    })

    return {
        "ok": True,
        "media_service_xaddr": selected_media_xaddr,
    }
