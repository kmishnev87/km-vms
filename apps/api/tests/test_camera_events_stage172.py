from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import copy
import uuid

from lxml import etree
import pytest
from fastapi import HTTPException

from app.services import camera_events as domain
from app.services import onvif_events as ov
from app.services.audit_log import list_events
from app.models.audit_event import AuditEvent
from app.routers import camera_events as routes
from test_stage11_retention_automation import db


def notification(*, topic="tns1:RuleEngine/CellMotionDetector/Motion", active="true", initialized=False, source="lens1", stamp=None):
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    root = etree.Element("{http://www.onvif.org/ver10/schema}Message", UtcTime=stamp,
                         PropertyOperation="Initialized" if initialized else "Changed")
    src = etree.SubElement(root, "{http://www.onvif.org/ver10/schema}Source")
    etree.SubElement(src, "{http://www.onvif.org/ver10/schema}SimpleItem", Name="VideoSourceConfigurationToken", Value=source)
    data = etree.SubElement(root, "{http://www.onvif.org/ver10/schema}Data")
    if active is not None:
        etree.SubElement(data, "{http://www.onvif.org/ver10/schema}SimpleItem", Name="IsMotion", Value=active)
    return {"Topic": {"_value_1": topic}, "Message": {"_value_1": root}}


@pytest.mark.parametrize("value,expected", [(True,True),(False,False),("true",True),("1",True),("0",False),("false",False),(None,None),("maybe",None)])
def test_boolean_contract(value, expected):
    assert ov.boolean(value) is expected


@pytest.mark.parametrize("topic,expected", [("CellMotionDetector/Motion","motion"),("IsPeople","person"),("HumanBody","person"),
    ("Vehicle/Detected","vehicle"),("Animal/Detected","animal"),("Tamper/Alarm","tamper"),("Audio/DetectedSound","audio"),("Other/Alarm","other")])
def test_kind_does_not_invent_classification(topic, expected):
    assert ov.kind_for(topic) == expected


def test_parser_namespaced_message_and_source():
    result = ov.parse_notification(notification())
    assert result.kind == "motion" and result.active is True
    assert result.source == {"VideoSourceConfigurationToken":"lens1"}
    assert result.topic == "RuleEngine/CellMotionDetector/Motion"
    assert not result.clock_uncertain and not result.initialized


@pytest.mark.parametrize("stamp", ["invalid","2020-01-01T00:00:00Z","2026-01-01T00:00:00"])
def test_untrusted_camera_time_uses_receipt_without_losing_evidence(stamp):
    now=datetime(2026,11,13,0,0)
    result=ov.parse_notification(notification(stamp=stamp), now)
    assert result.clock_uncertain and result.occurred_at==now


@pytest.mark.parametrize("topic", ["Device/Heartbeat","Device/KeepAlive","VideoSource/ConfigurationChanged"])
def test_non_events_do_not_fill_history(topic):
    assert ov.parse_notification(notification(topic=topic)) is None


def test_missing_xml_is_not_an_event():
    assert ov.parse_notification({"Topic":"Motion","Message":{}}) is None


def test_xml_comments_do_not_disconnect_the_receiver():
    message=notification()
    root=message["Message"]["_value_1"]
    root.insert(0,etree.Comment("device metadata"))
    assert ov.parse_notification(message).kind=="motion"


@pytest.mark.parametrize("topic", ["https://camera.invalid/path?credential=value", "Motion/<script>", "Motion/field=value"])
def test_invalid_topic_payload_is_not_stored(topic):
    assert ov.parse_notification(notification(topic=topic)) is None


def test_two_lenses_map_only_by_exact_source():
    cameras=[{"id":1,"profile_token":"profile-a"},{"id":2,"profile_token":"profile-b"}]
    identities=[{"profile":"profile-a","source":"a","configuration":"ca","analytics":"aa"},
                {"profile":"profile-b","source":"b","configuration":"cb","analytics":"ab"}]
    assert ov.resolve_source(cameras,identities,{"VideoSourceConfigurationToken":"cb"})["id"]==2
    assert ov.resolve_source(cameras,identities,{"Source":"a"})["id"]==1
    assert ov.resolve_source(cameras,identities,{}) is None
    assert ov.resolve_source(cameras,identities,{"Source":"unrecognized"}) is None
    assert ov.resolve_source(cameras[:1],identities,{"Source":"b"}) is None
    assert ov.resolve_source(cameras[:1],identities,{"Source":"unrecognized"}) is None
    assert ov.resolve_source(cameras[:1],identities,{})["id"]==1


@pytest.mark.parametrize("value", ["file:///etc/passwd","http://user:password@camera/path","https://camera/path#fragment","http://camera/a\npath"])
def test_subscription_endpoint_rejects_unsafe_forms(value):
    with pytest.raises(ov.EventDeviceError):
        ov.safe_endpoint(value,"camera",80)


def test_nat_subscription_preserves_reference_query_without_cross_host():
    value=ov.safe_endpoint("http://other-host:1234/onvif/sub?id=abc","camera",2020)
    assert value=="http://camera:2020/onvif/sub?id=abc"


def _rule(enabled="true"):
    return {"Name":"Motion", "Type":"tt:MotionRegionDetector", "Parameters":{"SimpleItem":[{"Name":"Armed","Value":enabled},{"Name":"Sensitivity","Value":"0.43"}]}}


def test_unsupported_or_undescribed_switch_is_not_guessed():
    description={"Parameters":{"SimpleItemDescription":[{"Name":"Armed","Type":"xs:boolean"}]}}
    assert list(ov._rule_boolean_slots(_rule(),description))==[("simple",0,"Armed",True)]
    assert not list(ov._rule_boolean_slots(_rule(),None))
    assert not list(ov._rule_boolean_slots({"Parameters":{"SimpleItem":[{"Name":"ActiveCells","Value":"AAAA"}]}},description))


def test_detector_change_is_exact_and_read_back(monkeypatch):
    original=_rule()
    rules=[{"id":"id","can_change":True,"revision":"revision","enabled":True}]
    calls=[]
    class Proxy:
        def ModifyRules(self, **kwargs):
            calls.append(kwargs)
    count=[0]
    def state(*args):
        count[0]+=1
        return ({"rules":rules if count[0]==1 else [{**rules[0],"enabled":False}]},
                {"id":(original,[("simple",0,"Armed",True)],"config",Proxy())})
    monkeypatch.setattr(ov,"detector_state",state)
    result=ov.set_detector(object(),"profile","id",False,"revision")
    assert result["rules"][0]["enabled"] is False
    assert calls[0]["Rule"][0]["Parameters"]["SimpleItem"]==[{"Name":"Armed","Value":"false"},{"Name":"Sensitivity","Value":"0.43"}]
    assert original==_rule()


def test_changed_detector_revision_never_sends_write(monkeypatch):
    monkeypatch.setattr(ov,"detector_state",lambda *_:({"rules":[{"id":"id","can_change":True,"revision":"new"}]},{}))
    with pytest.raises(ov.EventDeviceError,match="detector_state_changed"):
        ov.set_detector(object(),"profile","id",True,"old")


def group():
    return {"device_id":"device","cameras":[{"id":1,"name":"Front","profile_token":"p"}]}


def test_episode_start_repeat_end_and_initialization(db):
    camera=group()["cameras"][0]
    initial=ov.parse_notification(notification(initialized=True))
    assert domain.record_signal(db,group(),camera,initial) is None
    assert domain.event_query(db).count()==0
    first=domain.record_signal(db,group(),camera,ov.parse_notification(notification()))
    assert first
    assert domain.record_signal(db,group(),camera,ov.parse_notification(notification())) is None
    assert domain.event_query(db).count()==1
    assert domain.event_query(db).first().event_metadata["occurrences"]==2
    domain.record_signal(db,group(),camera,ov.parse_notification(notification(active="false")))
    assert domain.event_query(db).first().event_metadata["active"] is False
    second=domain.record_signal(db,group(),camera,ov.parse_notification(notification()))
    assert second and second!=first and domain.event_query(db).count()==2
    assert list_events(db)==[]  # Camera history never spams the administrative journal.


def test_ambiguous_device_is_not_attributed_to_first_lens(db):
    identifier=domain.record_signal(db,group(),None,ov.parse_notification(notification()))
    value=domain.serialize(domain.event_query(db).filter_by(id=identifier).one())
    assert value["camera_id"] is None and value["source_ambiguous"]


def test_lost_source_does_not_leave_a_false_ongoing_alarm(db):
    identifier=domain.record_signal(db,group(),group()["cameras"][0],ov.parse_notification(notification()))
    domain.source_disconnected(db,group())
    value=domain.event_query(db).filter_by(id=identifier).one().event_metadata
    assert value["active"] is None and value["state_unconfirmed"] is True and not value.get("ended_at")
    assert domain.serialize(domain.event_query(db).filter_by(id=identifier).one())["state_unconfirmed"]


def test_read_markers_are_per_user_idempotent_and_do_not_change_events(db):
    identifier=domain.record_signal(db,group(),group()["cameras"][0],ov.parse_notification(notification()))
    assert domain.list_camera_events(db,1)["unread_count"]==1
    assert domain.mark_read(db,1,[identifier,identifier])==1
    assert domain.mark_read(db,1,[identifier])==1
    assert domain.list_camera_events(db,1)["items"][0]["read"]
    assert domain.list_camera_events(db,1)["unread_count"]==0
    assert domain.list_camera_events(db,2)["unread_count"]==1
    assert domain.event_query(db).count()==1
    assert list_events(db)==[]


def test_first_event_after_empty_cursor_is_delivered(db):
    cursor=domain.updates(db,1)["cursor"]
    identifier=domain.record_signal(db,group(),group()["cameras"][0],ov.parse_notification(notification()))
    update=domain.updates(db,1,cursor)
    assert [r["id"] for r in update["items"]]==[identifier]
    assert domain.updates(db,1,update["cursor"])["items"]==[]
    assert domain.updates(db,1,update["cursor"])["cursor"]==update["cursor"]


def test_history_filters_pages_and_retention_do_not_touch_other_events(db, monkeypatch):
    for n in range(4):
        signal=ov.parse_notification(notification(source=str(n)))
        domain.record_signal(db,group(),group()["cameras"][0],signal)
    admin=AuditEvent(id=str(uuid.uuid4()),category="system",event_type="test",severity="info",message_ru="test",message_en="test",event_metadata={})
    db.add(admin);db.commit()
    assert domain.list_camera_events(db,1,limit=2,offset=2,kind="motion")["total"]==4
    monkeypatch.setattr(domain,"MAX_EVENTS",2)
    assert domain.cleanup_history(db)==2
    assert domain.event_query(db).count()==2 and db.get(AuditEvent,admin.id) is not None


@pytest.mark.parametrize("notifications,kinds,cameras,expected",[(False,None,None,False),(True,["person"],None,False),
    (True,["motion"],[1],True),(True,None,[],False),(True,None,None,True)])
def test_preferences_filter_notifications_not_recording(notifications,kinds,cameras,expected):
    preference={"notifications":notifications,"kinds":kinds,"camera_ids":cameras}
    assert domain.notifications_allowed(preference,{"kind":"motion","camera_id":1},SimpleNamespace(zone=timezone.utc)) is expected


def test_preferences_cannot_write_unrelated_layout(db):
    value=routes.Preferences(notifications=False,kinds=["person"],camera_ids=[1]).model_dump()
    assert domain.save_preferences(db,42,value)==value
    assert domain.preferences(db,42)[0]["notifications"] is False
    assert domain.preferences(db,43)[0]["notifications"] is True
    # A repeated first-save uses the existing unique user/key row.
    domain.save_preferences(db,42,{**value,"notifications":True})
    assert domain.preferences(db,42)[0]["notifications"] is True


def test_cleanup_drains_bounded_backlog_instead_of_falling_behind(monkeypatch):
    counts=iter([2000,2000,11,0])
    monkeypatch.setattr(domain,"_cleanup_batch",lambda _:next(counts))
    assert domain.cleanup_history(object())==4011


@pytest.mark.parametrize("minute,expected", [(23*60,True),(2*60,True),(12*60,False)])
def test_overnight_notification_window(monkeypatch,minute,expected):
    class Clock:
        @staticmethod
        def now(zone):
            return datetime(2026,11,13,minute//60,minute%60,tzinfo=zone)
    monkeypatch.setattr(domain,"datetime",Clock)
    preference={"notifications":True,"hours_enabled":True,"start_minute":22*60,"end_minute":6*60}
    assert domain.notifications_allowed(preference,{"kind":"motion","camera_id":1},SimpleNamespace(zone=timezone.utc)) is expected


def test_receiver_groups_lenses_and_ignores_recording_switch(monkeypatch):
    from app.services import camera_events_worker as worker
    from app.models.system_settings import SystemSettings
    cameras=[SimpleNamespace(id=n,host="camera",port=80,username="test",password_encrypted="cipher",
             onvif_profile_token=f"profile-{n}",onvif_path="/onvif/device_service",name=f"Lens {n}",enabled=False,events_enabled=True) for n in (1,2)]
    class Query:
        def __init__(self,model): self.model=model
        def first(self): return SimpleNamespace(system_initialized=True)
        def filter(self,*args): return self
        def all(self): return cameras
    class Database:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def query(self,model): return Query(model)
    monkeypatch.setattr(worker,"SessionLocal",Database)
    monkeypatch.setattr(worker,"decrypt_text",lambda _:"test-only")
    groups=worker.connection_groups()
    assert len(groups)==1 and len(next(iter(groups.values()))["cameras"])==2


@pytest.mark.parametrize("payload",[{"notifications":"false"},{"notifications":True,"password":"x"},{"camera_ids":[True]},{"start_minute":1500},{"kinds":["invented"]}])
def test_preferences_reject_ambiguous_or_unknown_values(payload):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        routes.Preferences(**payload)
