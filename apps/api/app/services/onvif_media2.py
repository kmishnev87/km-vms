"""Small ONVIF Media2 adapter; device/PTZ services continue using onvif-zeep.

The installed Media1 WSDL has no VideoEncoder2Configuration. Keep Media2's wire
shape (notably attributes and repeated codec options) and the complete original
encoder XML, instead of silently losing H.265/vendor fields through that WSDL.
Operation contracts: ONVIF ver20/media/wsdl and ver10/schema.
"""
from copy import deepcopy
from uuid import uuid4

import requests
from lxml import etree
from requests.auth import HTTPDigestAuth


MEDIA1_NS = "http://www.onvif.org/ver10/media/wsdl"
MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
ADDRESS_NS = "http://www.w3.org/2005/08/addressing"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class Media2Error(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class MediaObject(dict):
    """Mapping with the attribute access used by legacy ONVIF callers."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def xml_object(node):
    """Read ONVIF elements/attributes; never collapse repeated elements."""
    values = MediaObject({etree.QName(key).localname: value for key, value in node.attrib.items()
                          if not key.startswith("{")})
    repeated = set()
    for child in node:
        if not isinstance(child.tag, str) or etree.QName(child).namespace not in (MEDIA2_NS, SCHEMA_NS):
            continue
        name = etree.QName(child).localname
        value = xml_object(child) if len(child) or child.attrib else child.text
        if name in values:
            if name not in repeated:
                values[name] = [values[name]]
                repeated.add(name)
            values[name].append(value)
        else:
            values[name] = value
    return values


class EncoderConfiguration(MediaObject):
    """The XML stays server-side and is copied intact when applying a diff."""
    def __init__(self, node):
        super().__init__(xml_object(node))
        self.xml = deepcopy(node)

    def render(self):
        node = deepcopy(self.xml)
        node.tag = etree.QName(MEDIA2_NS, "Configuration")
        # These are the only encoder fields our public API can modify. Everything
        # else, including unknown extensions, attributes and multicast, is retained.
        for path in (
            ("Encoding",), ("Resolution", "Width"), ("Resolution", "Height"),
            ("RateControl", "FrameRateLimit"), ("RateControl", "BitrateLimit"),
            ("RateControl", "AverageBitRate"), ("Quality",),
        ):
            value = self
            target = node
            for part in path:
                value = value.get(part) if isinstance(value, dict) else None
                target = target.find(etree.QName(SCHEMA_NS, part)) if target is not None else None
            if value is not None and target is not None:
                target.text = str(value)
        for name in ("GovLength", "Profile"):
            if self.get(name) is not None:
                node.set(name, str(self[name]))
        control = self.get("RateControl") or {}
        rate_node = node.find(etree.QName(SCHEMA_NS, "RateControl"))
        if rate_node is not None and control.get("ConstantBitRate") is not None:
            value = control["ConstantBitRate"]
            rate_node.set("ConstantBitRate", "true" if value is True or str(value).lower() in ("1", "true") else "false")
        return node


class Media2Service:
    protocol = "media2"

    def __init__(self, address, username, password, *, wsse, session=None):
        self.address = address
        self._wsse = wsse
        self._session = session if session is not None else requests.Session()
        self._auth = HTTPDigestAuth(username, password)
        self._profiles = None

    def _call(self, operation, params=None, configuration=None):
        action = f"{MEDIA2_NS}/{operation}"
        envelope = etree.Element(etree.QName(SOAP_NS, "Envelope"), nsmap={
            "s": SOAP_NS, "a": ADDRESS_NS, "tr2": MEDIA2_NS, "tt": SCHEMA_NS,
        })
        header = etree.SubElement(envelope, etree.QName(SOAP_NS, "Header"))
        for name, value in (("Action", action), ("MessageID", f"urn:uuid:{uuid4()}"), ("To", self.address)):
            etree.SubElement(header, etree.QName(ADDRESS_NS, name)).text = value
        reply = etree.SubElement(header, etree.QName(ADDRESS_NS, "ReplyTo"))
        etree.SubElement(reply, etree.QName(ADDRESS_NS, "Address")).text = f"{ADDRESS_NS}/anonymous"
        body = etree.SubElement(envelope, etree.QName(SOAP_NS, "Body"))
        command = etree.SubElement(body, etree.QName(MEDIA2_NS, operation))
        for key, value in (params or {}).items():
            for item in value if isinstance(value, (list, tuple)) else [value]:
                if item is not None:
                    etree.SubElement(command, etree.QName(MEDIA2_NS, key)).text = str(item)
        if configuration is not None:
            command.append(configuration.render())
        headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
                   "SOAPAction": f'"{action}"'}
        self._wsse.apply(envelope, headers)
        try:
            with self._session.post(
                self.address, data=etree.tostring(envelope, encoding="utf-8", xml_declaration=True),
                headers=headers, auth=self._auth, timeout=(3, 6), allow_redirects=False, stream=True,
            ) as response:
                if response.status_code in (401, 403):
                    raise Media2Error("onvif_authentication_failed", "ONVIF authentication or permission failed.")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise Media2Error("onvif_media_response_invalid", "ONVIF response exceeds the supported size.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                status = response.status_code
        except requests.Timeout:
            raise Media2Error("onvif_media_timeout", "ONVIF service did not respond in time.") from None
        except requests.RequestException:
            raise Media2Error("onvif_media_unreachable", "ONVIF service could not be reached.") from None
        try:
            root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
            if root.getroottree().docinfo.doctype:
                raise ValueError("DTD is not an ONVIF response")
        except (etree.XMLSyntaxError, ValueError):
            raise Media2Error("onvif_media_response_invalid", "Camera returned an invalid ONVIF response.") from None
        response_body = root.find(etree.QName(SOAP_NS, "Body"))
        if response_body is None:
            raise Media2Error("onvif_media_response_invalid", "Camera returned no ONVIF response body.")
        fault = response_body.find(etree.QName(SOAP_NS, "Fault"))
        if fault is not None:
            # Never expose the SOAP fault text: vendors can echo credentials/URLs.
            codes = " ".join(fault.xpath('.//*[local-name()="Value"]/text()'))
            code = "onvif_authentication_failed" if any(word in codes for word in ("NotAuthorized", "FailedAuthentication")) else "onvif_media_fault"
            raise Media2Error(code, "Camera refused the ONVIF operation.")
        if not 200 <= status < 300:
            raise Media2Error("onvif_media_http_error", "Camera returned an unsuccessful ONVIF HTTP response.")
        result = response_body.find(etree.QName(MEDIA2_NS, f"{operation}Response"))
        if result is None:
            raise Media2Error("onvif_media_response_invalid", "Camera returned a different ONVIF operation response.")
        return result

    def GetProfiles(self):
        if self._profiles is None:
            result = self._call("GetProfiles", {"Type": "All"})
            profiles = []
            names = {"VideoSource": "VideoSourceConfiguration", "VideoEncoder": "VideoEncoderConfiguration",
                     "AudioSource": "AudioSourceConfiguration", "AudioEncoder": "AudioEncoderConfiguration",
                     "PTZ": "PTZConfiguration", "Analytics": "VideoAnalyticsConfiguration"}
            for node in result.findall(etree.QName(MEDIA2_NS, "Profiles")):
                profile = MediaObject(token=node.get("token"), Name=node.findtext(etree.QName(MEDIA2_NS, "Name")))
                configurations = node.find(etree.QName(MEDIA2_NS, "Configurations"))
                if configurations is not None:
                    for element in configurations:
                        name = names.get(etree.QName(element).localname)
                        if name and etree.QName(element).namespace == MEDIA2_NS:
                            profile[name] = EncoderConfiguration(element) if name == "VideoEncoderConfiguration" else xml_object(element)
                profiles.append(profile)
            self._profiles = profiles
        return deepcopy(self._profiles)

    def GetVideoEncoderConfiguration(self, params):
        token = str(params["ConfigurationToken"])
        result = self._call("GetVideoEncoderConfigurations", {"ConfigurationToken": token})
        matches = [node for node in result.findall(etree.QName(MEDIA2_NS, "Configurations")) if node.get("token") == token]
        if len(matches) != 1:
            raise Media2Error("video_encoder_configuration_token_mismatch", "Camera did not return the exact requested encoder.")
        return EncoderConfiguration(matches[0])

    def GetVideoEncoderConfigurationOptions(self, params):
        result = self._call("GetVideoEncoderConfigurationOptions", params)
        return [xml_object(node) for node in result.findall(etree.QName(MEDIA2_NS, "Options"))]

    def SetVideoEncoderConfiguration(self, params):
        configuration = params["Configuration"]
        if not isinstance(configuration, EncoderConfiguration):
            raise Media2Error("video_encoder_configuration_shape_unsupported", "No original Media2 encoder configuration is available.")
        result = self._call("SetVideoEncoderConfiguration", configuration=configuration)
        self._profiles = None
        return xml_object(result)

    def GetStreamUri(self, params):
        # Present the common legacy interface to callers; use Media2 on the wire.
        result = self._call("GetStreamUri", {"Protocol": "RtspUnicast", "ProfileToken": params["ProfileToken"]})
        return xml_object(result)
