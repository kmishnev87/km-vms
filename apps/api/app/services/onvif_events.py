"""Bounded, vendor-neutral ONVIF Events and Analytics. No database or implicit subscription.

Events, Rules and AnalyticsModules are separate SOAP bindings, even when the
device advertises one URL. Camera credentials and raw XML never leave this layer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from lxml import etree
from zeep import Plugin, Settings
from zeep.transports import Transport
from zeep.wsa import WsAddressingPlugin

from app.services import onvif_service as sdk

EVENT_NS = "http://www.onvif.org/ver10/events/wsdl"
NOTIFICATION_NS = "http://docs.oasis-open.org/wsn/b-2"
ANALYTICS_NS = "http://www.onvif.org/ver20/analytics/wsdl"
MAX_XML_BYTES = 512 * 1024
MAX_MESSAGES = 64
PULL_SECONDS = 60
EVENT_KINDS = ("motion", "person", "vehicle", "animal", "intrusion", "line_crossing",
               "loitering", "tamper", "audio", "wrong_direction", "fire")
# 'other' is only a legacy/diagnostic value, not a new history subscription kind.
KINDS = frozenset((*EVENT_KINDS, "other"))
SERVICE_HISTORY_TOPICS = ("Monitoring/OperatingTime/LastClockSynchronization",)
# Exact standard topics also make already stored legacy 'other' rule events
# searchable without rewriting or deleting that history.
LEGACY_TOPIC_KINDS = {"RuleEngine/FieldDetector/ObjectsInside": "intrusion",
    "RuleEngine/IntrusionDetector/Intrusion": "intrusion",
    "RuleEngine/LineDetector/Crossed": "line_crossing", "RuleEngine/LineCrossDetector/LineCross": "line_crossing",
    "RuleEngine/LoiteringDetector/ObjectIsLoitering": "loitering", "RuleEngine/FieldDetector/StayDetection": "loitering",
    "RuleEngine/RetrogradeMotionDetector/ObjectIsRetrogradeMotioning": "wrong_direction",
    "RuleEngine/VideoSource/FireAlarm": "fire", "RuleEngine/SmokeDetector/SmokeAlarm": "fire"}
_detector_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class EventDeviceError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def attr(value, key, default=None):
    return sdk._safe_attr(value, key, default)


def text(value, limit=255):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:limit]


def boolean(value):
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1"}:
        return True
    if str(value).lower() in {"false", "0"}:
        return False
    return None


def xml_elements(value, depth=0):
    if depth > 6:
        return
    if isinstance(value, etree._Element):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value[:MAX_MESSAGES]:
            yield from xml_elements(item, depth + 1)
    elif isinstance(value, dict):
        for item in list(value.values())[:MAX_MESSAGES]:
            yield from xml_elements(item, depth + 1)
    elif hasattr(value, "__values__"):
        yield from xml_elements(value.__values__, depth + 1)


def words_for(value):
    # Match semantic words, not substrings such as 'car' in 'CarriedObject'.
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", str(value))
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def object_kinds_for(topic, data=None):
    """Preserve every explicit class; a mixed scene need not contain just one."""
    labels = [str(k) for k, v in (data or {}).items() if boolean(v) is True]
    labels += [str(v) for k, v in (data or {}).items() if str(k).lower() in {"objecttype", "classtype", "classtypes", "objectclass"}]
    words = words_for(str(topic) + " " + " ".join(labels))
    return tuple(kind for kind, keys in (("person", {"human", "person", "people", "humanbody", "isperson", "ispeople", "ishuman"}),
        ("vehicle", {"vehicle", "car", "motor", "motorcycle", "bike", "bicycle", "isvehicle", "iscar"}),
        ("animal", {"animal", "pet", "isanimal"})) if words.intersection(keys))


def object_kind_for(topic, data=None):
    return next(iter(object_kinds_for(topic, data)), None)


def rule_kind_for(topic, data=None):
    words = str(topic).lower()
    fields = {str(k).lower(): boolean(v) for k, v in (data or {}).items() if boolean(v) is not None}
    for kind, keys in (("wrong_direction", ("retrograde", "wrongdirection", "wrongway")),
                       ("loitering", ("loiter", "staydetection", "staydetector")),
                       ("line_crossing", ("linedetector/crossed", "linecross", "crossline")),
                       ("intrusion", ("intrusion", "objectsinside", "objectinzone")),
                       ("fire", ("firealarm", "firedetector", "smokealarm", "smokedetector"))):
        if any(key in words for key in keys):
            return kind
    for state in (True, False):
        if state is False and any(fields.get(name) is True for name in (
            "ispeople", "ishuman", "isperson", "isvehicle", "iscar", "isanimal",
            "ismotion", "motion", "ismovement", "istamper", "issounddetected")):
            continue  # A false flag for another feature cannot hide a real alarm.
        for kind, names in (("wrong_direction", ("isretrograde", "iswrongdirection")),
            ("loitering", ("isloitering", "isstaying")), ("line_crossing", ("iscrossed", "islinecrossed")),
            ("intrusion", ("isinside", "isintrusion")),
            ("fire", ("isfire", "isfiredetected", "issmoke", "issmokedetected"))):
            if any(fields.get(name) is state for name in names):
                return kind
    if words_for(topic).intersection({"motion", "movement"}) or fields.keys() & {"ismotion", "motion", "ismovement"}:
        return "motion"
    return None


def kind_for(topic, data=None):
    rule = rule_kind_for(topic, data)
    # A person crossing a line is still a line event; do not discard its rule.
    if rule and rule != "motion":
        return rule
    kind = object_kind_for(topic, data)
    if kind:
        return kind
    words = (str(topic) + " " + " ".join(str(k) for k, v in (data or {}).items() if boolean(v) is True)).lower()
    for kind, keys in (("tamper", ("tamper", "sabotage")), ("audio", ("audio", "sound"))):
        if any(key in words for key in keys):
            return kind
    if rule:
        return rule
    # Some smart-event topics carry their only semantic label in a boolean
    # field. Its false/end message must remain recognizable too.
    fields = {str(k).lower() for k, v in (data or {}).items() if boolean(v) is not None}
    for kind, names in (("person", {"ispeople", "ishuman", "isperson"}),
                        ("vehicle", {"isvehicle", "iscar"}), ("animal", {"isanimal"}),
                        ("tamper", {"istamper"}), ("audio", {"issounddetected"})):
        if fields.intersection(names):
            return kind
    return "other"


def notification_topic(notification):
    value = attr(notification, "Topic")
    return re.sub(r"(^|/)[A-Za-z_][\w.-]*:", r"\1", text(attr(value, "_value_1", value), 512))


def ignored_notification(notification):
    topic = notification_topic(notification)
    lower = topic.lower()
    excluded = {"abandonedobjectdetector", "abandonedobject", "objectleft", "leftobjectdetector",
                "objectleftdetector", "removedobjectdetector", "objectremovaldetector", "missingobjectdetector",
                "removedobject", "objectremoved"}
    return bool(topic) and (topic in SERVICE_HISTORY_TOPICS
        or lower in {"media/profilechanged", "media/configurationchanged"}
        or any(word in lower for word in ("heartbeat", "keepalive", "configurationchanged"))
        or bool(set(lower.split("/")) & excluded))


class PullMessagesEnvelope(Plugin):
    """Preserve WSN mixed-text topics before Zeep's typed conversion loses them.

    Zeep already applies the strict XML security settings and the bounded transport.
    This is request-local, contains no vendor names and never persists raw XML.
    """
    def __init__(self):
        self.notifications = None

    def ingress(self, envelope, http_headers, operation):
        if operation.name != "PullMessages":
            return envelope, http_headers
        response = next((node for node in envelope.iter()
                         if isinstance(node.tag, str) and node.tag == f"{{{EVENT_NS}}}PullMessagesResponse"), None)
        if response is not None:
            self.notifications = []
            for node in response:
                if not isinstance(node.tag, str) or etree.QName(node).localname != "NotificationMessage" or etree.QName(node).namespace not in {EVENT_NS, NOTIFICATION_NS}:
                    continue
                topic = node.find(f"{{{NOTIFICATION_NS}}}Topic")
                message = node.find(f"{{{NOTIFICATION_NS}}}Message")
                self.notifications.append({"Topic": {"_value_1": topic.text if topic is not None else None},
                                           "Message": list(message) if message is not None else None})
                if len(self.notifications) >= MAX_MESSAGES:
                    break
        return envelope, http_headers


@dataclass(frozen=True)
class Signal:
    topic: str
    kind: str
    active: bool | None
    initialized: bool
    occurred_at: datetime
    camera_at: datetime | None
    clock_uncertain: bool
    source: dict = field(default_factory=dict)
    rule_kind: str | None = None
    object_kind: str | None = None
    data_keys: tuple[str, ...] = ()
    object_kinds: tuple[str, ...] = ()


def parse_notification(notification, received_at=None):
    received_at = received_at or datetime.now(timezone.utc).replace(tzinfo=None)
    topic = notification_topic(notification)
    if not re.fullmatch(r"[A-Za-z_][\w.:-]*(?:/[A-Za-z_][\w.:-]*)*", topic) or ignored_notification(notification):
        return None
    elements = list(xml_elements(attr(notification, "Message")))
    root = next((n for e in elements for n in e.iter() if isinstance(n.tag, str) and etree.QName(n).localname == "Message"), None)
    if root is None or len(etree.tostring(root)) > MAX_XML_BYTES:
        return None
    camera_at = None
    try:
        stamp = datetime.fromisoformat(root.get("UtcTime", "").replace("Z", "+00:00"))
        camera_at = stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else None
    except (ValueError, OverflowError):
        pass
    uncertain = camera_at is None or abs((camera_at - received_at).total_seconds()) > 300
    source, data = {}, {}
    for section in root:
        if not isinstance(section.tag, str):
            continue
        target = source if etree.QName(section).localname == "Source" else data if etree.QName(section).localname == "Data" else None
        if target is not None:
            for item in list(section)[:32]:
                if isinstance(item.tag, str) and etree.QName(item).localname == "SimpleItem":
                    target[text(item.get("Name"), 80)] = text(item.get("Value"), 255)
    rule = rule_kind_for(topic, data)
    lowered = {k.lower(): v for k, v in data.items()}
    # Explicit state wins over class flags, including an end with no class data.
    active = boolean(lowered.get("state"))
    state_names = {"intrusion": ("isinside", "isintrusion"),
                   "line_crossing": ("iscrossed", "islinecrossed"),
                   "loitering": ("isloitering", "isstaying"),
                   "wrong_direction": ("isretrograde", "iswrongdirection"),
                   "fire": ("isfire", "isfiredetected", "issmoke", "issmokedetected")}.get(rule, ())
    states = [boolean(lowered[name]) for name in state_names if name in lowered]
    if active is None:
        known = [v for v in states if v is not None]
        active = any(known) if known else None
    if active is None:
        states = [boolean(lowered[name]) for name in ("ismotion", "motion", "ismovement", "istamper", "issounddetected") if name in lowered]
        known = [v for v in states if v is not None]
        active = any(known) if known else None
    if active is None:
        classes = [boolean(v) for k, v in data.items() if k.lower() in {
            "ispeople", "ishuman", "isperson", "isvehicle", "iscar", "isanimal"}]
        known = [v for v in classes if v is not None]
        active = any(known) if known else None
    return Signal(topic, kind_for(topic, data), active, root.get("PropertyOperation") == "Initialized",
                  received_at if uncertain else camera_at, camera_at, uncertain, source,
                  rule, object_kind_for(topic, data), tuple(data)[:32], object_kinds_for(topic, data))


def safe_endpoint(address, host, port, *, fallback="/onvif/event_service"):
    parsed = urlsplit(str(address or ""))
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.fragment or re.search(r"[\x00-\x20\x7f]", str(address)):
        raise EventDeviceError("invalid_event_endpoint")
    # Keep subscription query parameters, but never follow a camera into another host.
    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if str(parsed.hostname).lower() != str(host).lower():
        target_port = int(port)
    target_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return urlunsplit((parsed.scheme, f"{target_host}:{target_port}", parsed.path or fallback, parsed.query, ""))


class BoundedTransport(Transport):
    def post(self, address, message, headers):
        response = self.session.post(address, data=message, headers=headers, timeout=self.operation_timeout, stream=True, allow_redirects=False)
        try:
            chunks, total, deadline = [], 0, time.monotonic() + 75
            for chunk in response.iter_content(8192):
                total += len(chunk)
                if total > MAX_XML_BYTES or time.monotonic() > deadline:
                    raise EventDeviceError("event_response_too_large")
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True
            return response
        finally:
            response.close()


class DeviceClient:
    def __init__(self, host, port, username, password, onvif_path=None):
        self.host, self.port = str(host), int(port)
        self.cam, addresses = sdk._prepare_camera(host, port, username, password, onvif_path)
        self.addresses = addresses
        self.transport = BoundedTransport(session=self.cam.transport.session, timeout=5, operation_timeout=8)
        self.subscription = None
        self.references = []
        self.renew_at = 0
        self._closed = False
        self._topics = None
        self._close_lock = threading.Lock()

    def service(self, name, binding=None, address=None, long_poll=False):
        sdk_service = self.cam.create_onvif_service(name)
        client = sdk_service.zeep_client
        client.transport = self.transport
        client.settings = Settings(strict=False, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if not any(isinstance(p, WsAddressingPlugin) for p in client.plugins):
            client.plugins.append(WsAddressingPlugin())
        original, _, original_binding = self.cam.get_definition(name)
        return client.create_service(binding or original_binding, address or original)

    def profiles(self):
        try:
            profiles = self.cam.create_media_service().GetProfiles()
            if profiles:
                return profiles
        except Exception:
            pass
        media, _ = sdk._get_media_service(self.cam, self.addresses)
        return media.GetProfiles()

    def topics(self):
        if self._topics is not None:
            return self._topics
        if EVENT_NS not in self.cam.xaddrs:
            # Network failures must not become a permanent 'unsupported' verdict.
            caps = self.cam.devicemgmt.GetCapabilities({"Category": "Events"})
            address = attr(attr(caps, "Events"), "XAddr")
            if not address:
                raise EventDeviceError("events_unsupported")
            self.cam.xaddrs[EVENT_NS] = safe_endpoint(address, self.host, self.port)
        result = self.service("events").GetEventProperties()
        topics = []
        for root in xml_elements(attr(result, "TopicSet")):
            for node in root.iter():
                if any(etree.QName(key).localname == "topic" and boolean(value) is True for key, value in node.attrib.items()):
                    parts = [etree.QName(node).localname]
                    for parent in node.iterancestors():
                        if etree.QName(parent).localname == "TopicSet":
                            break
                        parts.insert(0, etree.QName(parent).localname)
                    path = "/".join(parts)
                    topics.append(text(path, 512))
        self._topics = sorted(set(topics))[:128]
        return self._topics

    def event_capabilities(self):
        topics = self.topics()
        supported = None
        try:
            capabilities = self.service("events").GetServiceCapabilities()
            value = attr(capabilities, "Capabilities", capabilities)
            supported = boolean(attr(value, "WSPullPointSupport"))
        except Exception:
            pass  # Older Events implementations expose this through Device instead.
        if supported is None:
            caps = self.cam.devicemgmt.GetCapabilities({"Category": "Events"})
            supported = boolean(attr(attr(caps, "Events"), "WSPullPointSupport"))
        return {"supported": supported, "status": "supported" if supported is True else "unsupported" if supported is False else "unknown",
                "kinds": sorted({kind_for(topic) for topic in topics}) if supported is True else []}

    def subscribe(self):
        self.topics()
        value = self.service("events").CreatePullPointSubscription(InitialTerminationTime="PT5M")
        reference = attr(value, "SubscriptionReference")
        address = attr(reference, "Address")
        self.subscription = safe_endpoint(attr(address, "_value_1", address), self.host, self.port)
        self.references = [copy.deepcopy(e) for e in xml_elements(attr(reference, "ReferenceParameters"))]
        for element in self.references:
            element.set("{http://www.w3.org/2005/08/addressing}IsReferenceParameter", "true")
        self.renew_at = time.monotonic() + self._lifetime(value) * .65

    @staticmethod
    def _lifetime(value):
        try:
            return max(30, min(3600, (attr(value, "TerminationTime") - attr(value, "CurrentTime")).total_seconds()))
        except (TypeError, AttributeError):
            return 120

    def pull(self):
        if not self.subscription:
            self.subscribe()
        if time.monotonic() >= self.renew_at:
            value = self.service("events", f"{{{EVENT_NS}}}SubscriptionManagerBinding", self.subscription).Renew(
                TerminationTime="PT5M", _soapheaders=self.references)
            self.renew_at = time.monotonic() + self._lifetime(value) * .65
        proxy = self.service("pullpoint", address=self.subscription)
        envelope = PullMessagesEnvelope()
        proxy._client.plugins.append(envelope)
        with self.transport.settings(timeout=PULL_SECONDS + 10):
            value = proxy.PullMessages(Timeout=timedelta(seconds=PULL_SECONDS), MessageLimit=MAX_MESSAGES, _soapheaders=self.references)
        return envelope.notifications if envelope.notifications is not None else list(attr(value, "NotificationMessage", []) or [])[:MAX_MESSAGES]

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self.subscription:
            try:
                # A separate short-timeout transport can release a blocked PullMessages.
                proxy = self.service("events", f"{{{EVENT_NS}}}SubscriptionManagerBinding", self.subscription)
                proxy._binding_options["address"] = self.subscription
                with self.transport.settings(timeout=5):
                    proxy.Unsubscribe(_soapheaders=self.references)
            except Exception:
                pass


def _profile_identity(profile):
    source = attr(profile, "VideoSourceConfiguration")
    return {"profile": text(attr(profile, "token")), "source": text(attr(source, "SourceToken")),
            "configuration": text(attr(source, "token")),
            "analytics": text(attr(attr(profile, "VideoAnalyticsConfiguration"), "token"))}


def profile_identities(profiles):
    return [_profile_identity(p) for p in profiles[:128]]


def resolve_source(cameras, identities, source):
    # Prefer exact source/configuration identity. An event for a whole device is
    # not arbitrarily attributed to the first lens.
    priorities = (("videosource", "videosourcetoken", "videosourceconfigurationtoken", "source"),
                  ("videoanalyticsconfigurationtoken", "analyticsconfiguration", "videoanalyticsconfiguration"))
    lowered = {k.lower(): str(v) for k, v in source.items()}
    identified_source = False
    for keys in priorities:
        values = {lowered[k] for k in keys if lowered.get(k)}
        if not values:
            continue
        identified_source = True
        allowed = {p["profile"] for p in identities if values & {p["source"], p["configuration"], p["analytics"]}}
        matches = [c for c in cameras if str(c.get("profile_token") or "") in allowed]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    # A single configured lens is not the whole physical device. An explicit
    # event for an unknown/other lens must not be relabelled as this camera.
    return cameras[0] if len(cameras) == 1 and not identified_source else None


def _rule_services(client):
    if ANALYTICS_NS not in client.cam.xaddrs:
        raise EventDeviceError("analytics_unsupported")
    return client.service("analytics", f"{{{ANALYTICS_NS}}}RuleEngineBinding"), client.service("analytics")


def _rule_boolean_slots(rule, description):
    parameters = attr(rule, "Parameters")
    described = attr(description, "Parameters")
    boolean_names = {str(attr(p, "Name")) for p in attr(described, "SimpleItemDescription", []) or []
                     if str(attr(p, "Type", "")).split("}")[-1].split(":")[-1] == "boolean"}
    for index, part in enumerate(attr(parameters, "SimpleItem", []) or []):
        name = str(attr(part, "Name"))
        if name in {"Armed", "Enabled", "Enable"} and name in boolean_names and boolean(attr(part, "Value")) is not None:
            yield ("simple", index, name, boolean(attr(part, "Value")))


def _canonical(value):
    if isinstance(value, etree._Element):
        return {"tag": value.tag, "attributes": dict(value.attrib), "text": (value.text or "").strip(),
                "children": [_canonical(child) for child in value]}
    if hasattr(value, "__values__"):
        return _canonical(value.__values__)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


def _set_boolean_part(rule, index, value):
    part = attr(attr(rule, "Parameters"), "SimpleItem")[index]
    if isinstance(part, dict):
        part["Value"] = value
    else:
        part.Value = value


def detector_state(client, profile_token):
    profiles = client.profiles()
    profile = next((p for p in profiles if str(attr(p, "token")) == str(profile_token)), None)
    token = attr(attr(profile, "VideoAnalyticsConfiguration"), "token")
    if not token:
        raise EventDeviceError("analytics_profile_unavailable")
    rules_proxy, modules_proxy = _rule_services(client)
    supported = rules_proxy.GetSupportedRules(ConfigurationToken=token)
    descriptions = {str(attr(d, "Name")): d for d in attr(supported, "RuleDescription", []) or []}
    rules = rules_proxy.GetRules(ConfigurationToken=token) or []
    result, private = [], {}
    for rule in list(rules)[:128]:
        name, rule_type = str(attr(rule, "Name")), str(attr(rule, "Type"))
        revision = hashlib.sha256(json.dumps(_canonical(rule), sort_keys=True, default=str).encode()).hexdigest()
        identifier = hashlib.sha256(json.dumps([str(token), name, rule_type]).encode()).hexdigest()[:32]
        slots = list(_rule_boolean_slots(rule, descriptions.get(rule_type)))
        state = slots[0][3] if len(slots) == 1 else None
        result.append({"id": identifier, "name": text(name, 100), "kind": kind_for(rule_type),
                       "enabled": state, "can_change": len(slots) == 1, "revision": revision,
                       "reason": None if len(slots) == 1 else "detector_switch_not_exposed"})
        private[identifier] = (rule, slots, token, rules_proxy)
    return {"supported": True, "rules": result, "identities": profile_identities(profiles)}, private


def set_detector(client, profile_token, rule_id, enabled, revision):
    if type(enabled) is not bool:
        raise EventDeviceError("invalid_detector_state")
    state, private = detector_state(client, profile_token)
    current = next((r for r in state["rules"] if r["id"] == rule_id), None)
    if current is None or not current["can_change"]:
        raise EventDeviceError("detector_switch_not_exposed")
    if current["revision"] != revision:
        raise EventDeviceError("detector_state_changed")
    rule, slots, token, proxy = private[rule_id]
    updated = copy.deepcopy(rule)
    _, index, _, _ = slots[0]
    _set_boolean_part(updated, index, "true" if enabled else "false")
    proxy.ModifyRules(ConfigurationToken=token, Rule=[updated])
    after, after_private = detector_state(client, profile_token)
    confirmed = next((r for r in after["rules"] if r["id"] == rule_id), None)
    if confirmed is None or confirmed["enabled"] is not enabled:
        raise EventDeviceError("detector_change_unconfirmed")
    before_other, after_other = copy.deepcopy(rule), copy.deepcopy(after_private[rule_id][0])
    _set_boolean_part(before_other, index, "selected_state")
    _set_boolean_part(after_other, index, "selected_state")
    if _canonical(before_other) != _canonical(after_other):
        raise EventDeviceError("detector_change_unconfirmed")
    return after


def detector_lock(identity):
    with _locks_guard:
        if identity not in _detector_locks and len(_detector_locks) >= 256:
            raise EventDeviceError("detector_busy")
        return _detector_locks.setdefault(identity, threading.RLock())
