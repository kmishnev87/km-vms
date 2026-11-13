"""Wire-format regression and explicit receiving, without physical camera writes."""
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from lxml import etree
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from zeep import Client, Settings
from zeep.transports import Transport
from requests import Response

from app.models.camera import Camera
from app.models.system_settings import SystemSettings
from app.schemas.camera import CameraCreate, CameraUpdate
from app.services import camera_event_settings as choices
from app.services import camera_events_worker as worker
from app.services import onvif_events as ov
from app.services.schema_migrations import _stage172_event_receiving_apply, STAGE172_EVENT_RECEIVING_MIGRATION
from test_camera_events_stage172 import notification
from test_stage11_retention_automation import db


def wire_response(topic="vendor:RuleEngine/CellMotionDetector/Motion", *, active="true", initialized=False, count=1):
    envelope = etree.Element("{http://www.w3.org/2003/05/soap-envelope}Envelope")
    body = etree.SubElement(envelope, "{http://www.w3.org/2003/05/soap-envelope}Body")
    response = etree.SubElement(body, f"{{{ov.EVENT_NS}}}PullMessagesResponse")
    for name in ("CurrentTime", "TerminationTime"):
        etree.SubElement(response, f"{{{ov.EVENT_NS}}}{name}").text = datetime.now(timezone.utc).isoformat()
    for _ in range(count):
        item = etree.SubElement(response, f"{{{ov.NOTIFICATION_NS}}}NotificationMessage")
        etree.SubElement(item, f"{{{ov.NOTIFICATION_NS}}}Topic", Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet").text = topic
        message = etree.SubElement(item, f"{{{ov.NOTIFICATION_NS}}}Message")
        message.append(notification(active=active, initialized=initialized)["Message"]["_value_1"])
    return envelope


def capture(envelope):
    plugin = ov.PullMessagesEnvelope()
    plugin.ingress(envelope, {}, SimpleNamespace(name="PullMessages"))
    return plugin.notifications


@pytest.mark.parametrize("prefix", ["tns1", "otherManufacturer", "unfamiliarCamera"])
def test_mixed_text_topics_are_preserved_without_any_vendor_switch(prefix):
    item = capture(wire_response(f"{prefix}:RuleEngine/CellMotionDetector/Motion"))[0]
    parsed = ov.parse_notification(item)
    assert parsed.kind == "motion" and parsed.active is True
    assert parsed.topic == "RuleEngine/CellMotionDetector/Motion"
    assert parsed.source == {"VideoSourceConfigurationToken": "lens1"}


def test_real_zeep_wsdl_loses_mixed_text_but_request_plugin_keeps_it(monkeypatch):
    payload = etree.tostring(wire_response())
    transport = Transport()
    def reply(*args, **kwargs):
        response = Response(); response.status_code = 200
        response.headers["Content-Type"] = "application/soap+xml; charset=utf-8"
        response._content = payload
        return response
    monkeypatch.setattr(transport, "post", reply)
    recorder = ov.PullMessagesEnvelope()
    client = Client(str(Path(ov.sdk.wsdl_dir()) / "events.wsdl"), transport=transport,
                    settings=Settings(strict=False, forbid_dtd=True, forbid_entities=True, forbid_external=True), plugins=[recorder])
    proxy = client.create_service(f"{{{ov.EVENT_NS}}}PullPointSubscriptionBinding", "http://192.0.2.1/onvif/pull")
    result = proxy.PullMessages(Timeout="PT1S", MessageLimit=10)
    assert ov.attr(ov.attr(result.NotificationMessage[0], "Topic"), "_value_1") is None
    assert ov.parse_notification(recorder.notifications[0]).kind == "motion"


def test_device_client_consumes_preserved_envelope_not_lossy_typed_topic():
    client = object.__new__(ov.DeviceClient)
    client.subscription = "http://192.0.2.1/onvif/pull"
    client.references = []; client.renew_at = float("inf")
    client.transport = SimpleNamespace(settings=lambda **kwargs: nullcontext())
    class Proxy:
        _client = SimpleNamespace(plugins=[])
        def PullMessages(self, **kwargs):
            for plugin in self._client.plugins:
                plugin.ingress(wire_response(), {}, SimpleNamespace(name="PullMessages"))
            return SimpleNamespace(NotificationMessage=[{"Topic": {"_value_1": None}}])
    client.service = lambda *args, **kwargs: Proxy()
    assert ov.parse_notification(client.pull()[0]).active is True


def test_empty_pull_bounded_messages_and_initialization_are_not_fake_alarms():
    assert capture(wire_response(count=0)) == []
    assert len(capture(wire_response(count=ov.MAX_MESSAGES + 1))) == ov.MAX_MESSAGES
    parsed = ov.parse_notification(capture(wire_response(initialized=True))[0])
    assert parsed.initialized and parsed.active is True


def test_false_human_flag_does_not_hide_a_true_vehicle_alarm():
    message = notification(active=None)
    data = message["Message"]["_value_1"].find("{http://www.onvif.org/ver10/schema}Data")
    for key, value in (("IsHuman", "false"), ("IsVehicle", "true")):
        etree.SubElement(data, "{http://www.onvif.org/ver10/schema}SimpleItem", Name=key, Value=value)
    result = ov.parse_notification(message)
    assert result.kind == "vehicle" and result.active is True


def test_topics_are_relative_to_topic_set_not_soap_envelope():
    root = etree.fromstring(b'<Envelope><Body><TopicSet xmlns:w="http://docs.oasis-open.org/wsn/t-1"><RuleEngine><CellMotionDetector><Motion w:topic="true"/></CellMotionDetector></RuleEngine></TopicSet></Body></Envelope>')
    client = object.__new__(ov.DeviceClient); client._topics = None
    client.cam = SimpleNamespace(xaddrs={ov.EVENT_NS: "http://192.0.2.1/events"})
    client.service = lambda *args: SimpleNamespace(GetEventProperties=lambda: {"TopicSet": list(root.find(".//TopicSet"))})
    assert client.topics() == ["RuleEngine/CellMotionDetector/Motion"]


@pytest.mark.parametrize("support", [True, False, None])
def test_capability_read_never_subscribes_or_guesses_by_manufacturer(support):
    client = object.__new__(ov.DeviceClient)
    client.topics = lambda: ["RuleEngine/CellMotionDetector/Motion"]
    client.service = lambda *args: SimpleNamespace(GetServiceCapabilities=lambda: {"Capabilities": {"WSPullPointSupport": support}})
    client.cam = SimpleNamespace(devicemgmt=SimpleNamespace(GetCapabilities=lambda _: {"Events": {"WSPullPointSupport": support}}))
    client.subscribe = Mock(side_effect=AssertionError("must not subscribe"))
    assert client.event_capabilities()["supported"] is support
    client.subscribe.assert_not_called()


def test_timeout_is_unknown_not_a_permanent_unsupported_verdict(monkeypatch):
    monkeypatch.setattr(choices, "DeviceClient", Mock(side_effect=TimeoutError))
    assert choices.read_event_capabilities(host="192.0.2.1", port=80, username="test", password="test")["supported"] is None
    with pytest.raises(Exception) as error:
        choices.validate_event_receiving(True, {"protocol": "onvif"})
    assert error.value.status_code == 409


@pytest.mark.parametrize("bad", [None, "false", 0, 1])
def test_receiving_choice_requires_an_explicit_boolean(bad):
    with pytest.raises(ValidationError):
        CameraUpdate(events_enabled=bad)


def test_new_cameras_default_off_and_partial_update_does_not_reset_it():
    assert not CameraCreate(name="Front", host="192.0.2.1", port=80, protocol="onvif").events_enabled
    assert "events_enabled" not in CameraUpdate(name="Renamed").model_dump(exclude_unset=True)


def test_additive_migration_preserves_existing_rows_and_defaults_off():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE cameras (id INTEGER PRIMARY KEY, name VARCHAR(255))"))
            connection.execute(text("INSERT INTO cameras VALUES (1,'Preserved')"))
        from sqlalchemy.orm import Session
        with Session(engine) as session:
            assert _stage172_event_receiving_apply(session)["column_added"]
            assert not _stage172_event_receiving_apply(session)["column_added"]
            assert session.execute(text("SELECT id,name,events_enabled FROM cameras")).one() == (1, "Preserved", 0)
        assert STAGE172_EVENT_RECEIVING_MIGRATION.runtime_compatibility_floor == 10
    finally:
        engine.dispose()


def test_groups_default_off_share_subscription_and_keep_disabled_lens_identity(db, monkeypatch):
    db.add(SystemSettings(system_initialized=True))
    for identifier in (1, 2):
        db.add(Camera(id=identifier, name=f"Lens {identifier}", storage_folder_name=f"lens{identifier}",
                      host="192.0.2.1", port=80, protocol="onvif", username="test", password_encrypted="cipher",
                      onvif_profile_token=f"p{identifier}", enabled=False))
    db.commit()
    class Scope:
        def __enter__(self): return db
        def __exit__(self, *args): pass
    monkeypatch.setattr(worker, "SessionLocal", Scope)
    monkeypatch.setattr(worker, "decrypt_text", lambda _: "test-only")
    assert worker.connection_groups() == {}
    db.get(Camera, 1).events_enabled = True; db.commit()
    group = next(iter(worker.connection_groups().values()))
    assert len(group["cameras"]) == 2
    assert worker.group_is_current(db, group)
    identities = [{"profile": f"p{i}", "source": f"s{i}", "configuration": f"s{i}", "analytics": ""} for i in (1, 2)]
    for source, allowed in (("s1", True), ("s2", False), ("unknown", False)):
        signal = ov.parse_notification(notification(source=source))
        assert worker.signal_camera(group, identities, signal)[1] is allowed
    db.get(Camera, 1).events_enabled = False; db.commit()
    assert not worker.group_is_current(db, group)
    assert worker.connection_groups() == {}
