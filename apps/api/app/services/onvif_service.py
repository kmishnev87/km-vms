from pathlib import Path
from urllib.parse import quote, urlparse, urlsplit, urlunparse, urlunsplit

from onvif import ONVIFCamera


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

    if hasattr(cam, "xaddrs") and isinstance(cam.xaddrs, dict):
        if uniq:
            cam.xaddrs["media"] = uniq[0]
            cam.xaddrs["media2"] = uniq[0]

    return cam, uniq


def _get_media_service(cam, candidates: list[str]):
    last_error = None

    for address in candidates:
        try:
            if hasattr(cam, "xaddrs") and isinstance(cam.xaddrs, dict):
                cam.xaddrs["media"] = address
                cam.xaddrs["media2"] = address

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


def _get_video_encoder_config_from_profile(media, profile):
    vec = _safe_attr(profile, "VideoEncoderConfiguration")
    if not vec:
        return None

    token = _safe_attr(vec, "token")
    if not token:
        return vec

    try:
        return media.GetVideoEncoderConfiguration({"ConfigurationToken": token})
    except Exception:
        return vec


def _profile_to_dict(profile, media, username, password, host, port):
    cfg = _get_video_encoder_config_from_profile(media, profile)
    rc = _safe_attr(cfg, "RateControl")
    res = _safe_attr(cfg, "Resolution")
    audio_cfg = _safe_attr(profile, "AudioEncoderConfiguration")

    raw_uri = None
    fixed_uri = None
    try:
        stream_resp = media.GetStreamUri({
            "StreamSetup": {
                "Stream": "RTP-Unicast",
                "Transport": {"Protocol": "RTSP"},
            },
            "ProfileToken": profile.token,
        })
        raw_uri = _safe_attr(stream_resp, "Uri")
        fixed_uri = inject_auth_to_rtsp(
            raw_uri,
            username,
            password,
            override_host=host,
            override_port=port,
        )
    except Exception:
        raw_uri = None
        fixed_uri = None

    return {
        "token": _safe_attr(profile, "token"),
        "name": _safe_attr(profile, "Name"),
        "video": {
            "codec": _safe_attr(cfg, "Encoding"),
            "width": _safe_attr(res, "Width"),
            "height": _safe_attr(res, "Height"),
            "fps": _safe_attr(rc, "FrameRateLimit"),
            "quality": _safe_attr(cfg, "Quality"),
            "bitrate_limit": _safe_attr(rc, "BitrateLimit"),
            "encoding_interval": _safe_attr(rc, "EncodingInterval"),
        },
        "audio": {
            "codec": _safe_attr(audio_cfg, "Encoding"),
            "sample_rate": _safe_attr(audio_cfg, "SampleRate"),
            "channels": _safe_attr(audio_cfg, "Channels"),
        },
        "raw_stream_uri": raw_uri,
        "stream_uri": fixed_uri,
    }


def fetch_onvif_profiles(host, port, username, password):
    cam, media_candidates = _prepare_camera(host, port, username, password)
    dev = cam.devicemgmt
    media, selected_media_xaddr = _get_media_service(cam, media_candidates)

    info = dev.GetDeviceInformation()
    profiles = media.GetProfiles()

    result = []
    for p in profiles:
        item = _profile_to_dict(p, media, username, password, host, port)
        item["media_service_xaddr"] = selected_media_xaddr
        result.append(item)

    return {
        "device": {
            "manufacturer": _safe_attr(info, "Manufacturer"),
            "model": _safe_attr(info, "Model"),
            "firmware": _safe_attr(info, "FirmwareVersion"),
            "serial_number": _safe_attr(info, "SerialNumber"),
        },
        "profiles": result,
        "media_service_xaddr": selected_media_xaddr,
    }


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
                "fps": None,
                "bitrate": None,
                "iframe_interval": None,
                "quality": None,
            },
        }

    rc = _safe_attr(cfg, "RateControl")
    res = _safe_attr(cfg, "Resolution")

    return {
        "profile_token": profile_token,
        "name": _safe_attr(profile, "Name"),
        "media_service_xaddr": selected_media_xaddr,
        "config": {
            "codec": _safe_attr(cfg, "Encoding"),
            "width": _safe_attr(res, "Width"),
            "height": _safe_attr(res, "Height"),
            "fps": _safe_attr(rc, "FrameRateLimit"),
            "bitrate": _safe_attr(rc, "BitrateLimit"),
            "iframe_interval": _safe_attr(rc, "EncodingInterval"),
            "quality": _safe_attr(cfg, "Quality"),
        },
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

    if config.get("codec"):
        cfg.Encoding = str(config["codec"])

    if config.get("width") and config.get("height"):
        if not getattr(cfg, "Resolution", None):
            raise Exception("Камера не отдала Resolution в ONVIF-конфиге")
        cfg.Resolution.Width = int(config["width"])
        cfg.Resolution.Height = int(config["height"])

    if not getattr(cfg, "RateControl", None):
        raise Exception("Камера не отдала RateControl в ONVIF-конфиге")

    if config.get("fps"):
        cfg.RateControl.FrameRateLimit = int(config["fps"])

    if config.get("bitrate"):
        cfg.RateControl.BitrateLimit = int(config["bitrate"])

    if config.get("iframe_interval"):
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
