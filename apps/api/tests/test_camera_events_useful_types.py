"""Useful event semantics, never vendor-specific topic allowlists or camera writes."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import uuid

from lxml import etree
import pytest

from app.models.audit_event import AuditEvent
from app.routers import camera_events as routes
from app.services import camera_events as events
from app.services import onvif_events as ov
from test_camera_events_stage172 import notification, group
from test_stage11_retention_automation import db


USEFUL = {"motion", "person", "vehicle", "animal", "intrusion", "line_crossing",
          "loitering", "tamper", "audio", "wrong_direction", "fire"}
TOPICS = [
    ("RuleEngine/CellMotionDetector/Motion", "motion"),
    ("RuleEngine/PeopleDetector/People", "person"),
    ("RuleEngine/VehicleDetector/Vehicle", "vehicle"),
    ("RuleEngine/AnimalDetector/Animal", "animal"),
    ("RuleEngine/FieldDetector/ObjectsInside", "intrusion"),
    ("RuleEngine/IntrusionDetector/Intrusion", "intrusion"),
    ("RuleEngine/LineDetector/Crossed", "line_crossing"),
    ("RuleEngine/LineCrossDetector/LineCross", "line_crossing"),
    ("RuleEngine/LoiteringDetector/ObjectIsLoitering", "loitering"),
    ("RuleEngine/FieldDetector/StayDetection", "loitering"),
    ("RuleEngine/TamperDetector/Tamper", "tamper"),
    ("AudioAnalytics/Audio/DetectedSound", "audio"),
    ("RuleEngine/RetrogradeMotionDetector/ObjectIsRetrogradeMotioning", "wrong_direction"),
    ("RuleEngine/VideoSource/FireAlarm", "fire"),
    ("RuleEngine/SmokeDetector/SmokeAlarm", "fire"),
]


def data_items(message, **values):
    data = message["Message"]["_value_1"].find("{http://www.onvif.org/ver10/schema}Data")
    for name, value in values.items():
        etree.SubElement(data, "{http://www.onvif.org/ver10/schema}SimpleItem", Name=name, Value=value)
    return message


@pytest.mark.parametrize("prefix", ["tns1", "differentVendor", "anotherCamera"])
@pytest.mark.parametrize("topic,expected", TOPICS)
def test_all_eleven_useful_types_are_understood_independently_of_vendor(prefix, topic, expected):
    signal = ov.parse_notification(notification(topic=f"{prefix}:{topic}"))
    assert signal is not None and signal.kind == expected


@pytest.mark.parametrize("topic", ["Monitoring/OperatingTime/LastClockSynchronization",
    "tns1:Monitoring/OperatingTime/LastClockSynchronization", "Media/ProfileChanged",
    "Device/Heartbeat", "Media/ConfigurationChanged", "RuleEngine/AbandonedObjectDetector/ObjectLeft",
    "RuleEngine/RemovedObjectDetector/ObjectRemoved"])
def test_known_service_or_explicitly_excluded_messages_stop_before_database(topic):
    message = notification(topic=topic)
    assert ov.ignored_notification(message)
    assert ov.parse_notification(message) is None


def test_rule_and_object_class_survive_together(db):
    signal = ov.parse_notification(data_items(notification(topic="RuleEngine/LineDetector/Crossed"), ClassTypes="Human"))
    assert signal.kind == "line_crossing" and signal.object_kind == "person"
    identifier = events.record_signal(db, group(), group()["cameras"][0], signal)
    item = events.list_camera_events(db, 1)["items"][0]
    assert item["id"] == identifier and item["object_kind"] == "person" and item["rule_kind"] == "line_crossing"
    for kind in ("person", "line_crossing"):
        assert events.list_camera_events(db, 1, kind=kind)["total"] == 1
        assert events.notifications_allowed({"kinds": [kind]}, item, SimpleNamespace())
    assert not events.notifications_allowed({"kinds": ["audio"]}, item, SimpleNamespace())


def test_rule_end_without_class_flag_closes_existing_episode(db):
    start = ov.parse_notification(data_items(notification(topic="RuleEngine/CellMotionDetector/Motion"), IsHuman="true"))
    identifier = events.record_signal(db, group(), group()["cameras"][0], start)
    end = ov.parse_notification(notification(topic="RuleEngine/CellMotionDetector/Motion", active="false"))
    assert start.kind == "person" and end.kind == "motion"
    events.record_signal(db, group(), group()["cameras"][0], end)
    item = events.serialize(db.get(AuditEvent, identifier))
    assert not item["active"] and item["ended_at"] and events.event_query(db).count() == 1


def test_mixed_objects_and_late_classification_preserve_all_useful_information(db):
    identifier = events.record_signal(db, group(), group()["cameras"][0], ov.parse_notification(notification()))
    message = data_items(notification(), ClassTypes="Human Vehicle Animal")
    signal = ov.parse_notification(message)
    assert signal.object_kinds == ("person", "vehicle", "animal")
    assert events.record_signal(db, group(), group()["cameras"][0], signal) is None
    item = events.serialize(db.get(AuditEvent, identifier))
    assert item["object_kinds"] == ["person", "vehicle", "animal"]
    for kind in ("motion", "person", "vehicle", "animal"):
        assert events.list_camera_events(db, 1, kind=kind)["total"] == 1
        assert events.notifications_allowed({"kinds": [kind]}, item, SimpleNamespace())


@pytest.mark.parametrize("field,kind", [("IsPeople", "person"), ("IsVehicle", "vehicle"),
    ("IsAnimal", "animal"), ("IsFire", "fire"), ("IsSmokeDetected", "fire"),
    ("IsInside", "intrusion"), ("IsLineCrossed", "line_crossing"),
    ("IsLoitering", "loitering"), ("IsWrongDirection", "wrong_direction")])
def test_data_only_smart_event_start_and_end_are_recognized(db, field, kind):
    start = ov.parse_notification(data_items(notification(topic="RuleEngine/SmartEvent/Alarm", active=None), **{field: "true"}))
    end = ov.parse_notification(data_items(notification(topic="RuleEngine/SmartEvent/Alarm", active=None), **{field: "false"}))
    assert start.kind == end.kind == kind and start.active is True and end.active is False
    identifier = events.record_signal(db, group(), group()["cameras"][0], start)
    events.record_signal(db, group(), group()["cameras"][0], end)
    assert events.serialize(db.get(AuditEvent, identifier))["ended_at"]


def test_substrings_do_not_invent_a_vehicle_or_discard_an_unknown_media_alarm():
    assert ov.kind_for("RuleEngine/CarriedObjectDetector/Alarm") == "other"
    message = notification(topic="Media/CustomDetector/Alarm", active=None)
    assert not ov.ignored_notification(message)
    assert ov.parse_notification(message).kind == "other"


def test_false_unrelated_rule_flags_do_not_hide_a_real_object_alarm():
    signal = ov.parse_notification(data_items(notification(topic="RuleEngine/SmartEvent/Alarm", active=None),
        IsVehicle="true", IsFire="false", IsInside="false"))
    assert signal.kind == "vehicle" and signal.active is True


def test_excluding_missing_objects_does_not_discard_a_zone_exit_with_a_similar_word():
    message = data_items(notification(topic="RuleEngine/FieldDetector/ObjectRemovedFromZone", active=None), IsInside="false")
    assert not ov.ignored_notification(message)
    signal = ov.parse_notification(message)
    assert signal.kind == "intrusion" and signal.active is False


@pytest.mark.parametrize("kind", sorted(USEFUL))
def test_all_new_types_round_trip_through_preferences_without_changing_camera_receiving(db, kind):
    value = routes.Preferences(kinds=[kind], camera_ids=[1], notifications=True).model_dump()
    assert events.save_preferences(db, 17, value)["kinds"] == [kind]
    assert events.preferences(db, 17)[0]["kinds"] == [kind]


def test_full_eleven_kind_selection_can_be_saved_and_null_and_empty_stay_distinct(db):
    value = routes.Preferences(kinds=list(ov.EVENT_KINDS)).model_dump()
    assert events.save_preferences(db, 17, value)["kinds"] == list(ov.EVENT_KINDS)
    for kinds in (None, [], ["other"]):
        expected = [] if kinds == ["other"] else kinds
        saved = events.save_preferences(db, 17, routes.Preferences(kinds=kinds).model_dump())
        assert saved["kinds"] == expected
        assert events.preferences(db, 17)[0]["kinds"] == expected


def test_unknown_alarm_is_not_clock_noise_but_does_not_become_generic_history(db):
    message = notification(topic="RuleEngine/FutureDetector/FutureAlarm", active=None)
    assert not ov.ignored_notification(message)
    signal = ov.parse_notification(message)
    assert signal is not None and signal.kind == "other"
    assert events.record_signal(db, group(), group()["cameras"][0], signal) is None
    assert events.event_query(db).count() == 0


def test_cleanup_removes_only_old_clock_noise_not_unknown_real_history(db):
    identifiers = {}
    for name, category, topic in (("clock", events.CATEGORY, "Monitoring/OperatingTime/LastClockSynchronization"),
        ("unknown", events.CATEGORY, "RuleEngine/FutureDetector/FutureAlarm"),
        ("admin", "system", "Monitoring/OperatingTime/LastClockSynchronization")):
        identifier = identifiers[name] = str(uuid.uuid4())
        db.add(AuditEvent(id=identifier, created_at=datetime.utcnow(), category=category,
            event_type="camera.other", severity="info", message_ru="", message_en="", event_metadata={"topic": topic}))
    db.commit()
    events.mark_read(db, 1, [identifiers["clock"]])
    assert events.cleanup_history(db) == 1
    assert db.get(AuditEvent, identifiers["clock"]) is None
    assert db.get(AuditEvent, identifiers["unknown"]) is not None
    assert db.get(AuditEvent, identifiers["admin"]) is not None
    assert db.query(AuditEvent).filter(AuditEvent.category == events.READ_CATEGORY).count() == 0


def test_legacy_useful_rule_history_stays_visible_under_the_new_filters(db):
    for topic, kind in ov.LEGACY_TOPIC_KINDS.items():
        identifier = str(uuid.uuid4())
        db.add(AuditEvent(id=identifier, created_at=events.now_utc(), category=events.CATEGORY,
            event_type="camera.other", severity="info", message_ru="", message_en="", event_metadata={"topic": topic}))
        db.commit()
        assert events.serialize(db.get(AuditEvent, identifier))["kind"] == kind
        assert identifier in {item["id"] for item in events.list_camera_events(db, 1, kind=kind)["items"]}
    assert events.cleanup_history(db) == 0
