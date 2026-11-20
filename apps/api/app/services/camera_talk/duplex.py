"""Read the configured audio profile; a backchannel alone is not duplex evidence."""
from app.services.onvif_service import (
    MEDIA1_NS, ONVIFCamera, _rewrite_xaddr, _safe_attr, _set_media_xaddr, wsdl_dir,
)


def profile_duplex(profile, options=None):
    output = _safe_attr(profile, "AudioOutputConfiguration")
    current = str(_safe_attr(output, "SendPrimacy") or "").lower()
    values = [str(value).lower() for value in (_safe_attr(options, "SendPrimacyOptions") or [])]
    if any("/halfduplex/" in value for value in [current, *values]):
        return "push_to_talk"
    output_token = _safe_attr(output, "OutputToken")
    outputs = _safe_attr(options, "OutputTokensAvailable") or []
    # A usable mic, decoder and confirmed output in this same profile matter.
    # Missing/unsupported options on a camera remain unknown.
    if (not current and not values and output_token and output_token in outputs
            and _safe_attr(profile, "AudioSourceConfiguration")
            and _safe_attr(profile, "AudioEncoderConfiguration")
            and _safe_attr(profile, "AudioDecoderConfiguration")):
        return "full"
    return "unknown"


def onvif_duplex(source):
    if source.protocol != "onvif" or not source.profile:
        return "unknown"
    client = None
    try:
        camera = ONVIFCamera(source.host, source.onvif_port, source.username, source.password,
                             wsdl_dir(), onvif_path=source.onvif_path)
        transport = camera.devicemgmt.zeep_client.transport
        transport.operation_timeout = 0.8
        client = transport.session
        client.max_redirects = 0
        services = camera.devicemgmt.GetServices({"IncludeCapability": False})
        endpoint = next((_safe_attr(item, "XAddr") for item in services
                         if str(_safe_attr(item, "Namespace") or "").lower() == MEDIA1_NS), None)
        if not endpoint:
            return "unknown"
        _set_media_xaddr(camera, _rewrite_xaddr(endpoint, source.host, source.onvif_port, "/onvif/media_service"))
        media = camera.create_media_service()
        profile = media.GetProfile({"ProfileToken": source.profile})
        duplex = profile_duplex(profile)
        if duplex == "push_to_talk":
            return duplex
        config = _safe_attr(profile, "AudioOutputConfiguration")
        token = _safe_attr(config, "token")
        if not token:
            return "unknown"
        options = media.GetAudioOutputConfigurationOptions({"ConfigurationToken": token, "ProfileToken": source.profile})
        return profile_duplex(profile, options)
    except Exception:
        return "unknown"
    finally:
        if client is not None:
            client.close()
