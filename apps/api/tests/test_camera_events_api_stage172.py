import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from app.db.session import Base, get_db
from app.models.camera import Camera
from app.models.user import User
from app.models.audit_event import AuditEvent
from app.routers.deps import get_current_user
from app.routers import camera_events as routes
from app.services import camera_events as domain
from app.services.onvif_events import Signal
from app.routers import cameras as camera_routes
from app.routers import camera_onvif_routes as onvif_routes
from app.services import camera_event_settings
from app.core.security import encrypt_text


@pytest.fixture
def api():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as db:
        db.add(User(id=1,username="test-viewer",password_hash="test-only",role="viewer",is_active=True))
        db.add(Camera(id=1,name="Front",storage_folder_name="front",host="192.0.2.1",port=554,protocol="rtsp",enabled=True))
        db.commit()
        group={"device_id":"device","cameras":[{"id":1,"name":"Front","profile_token":"p"}]}
        identifier=domain.record_signal(db,group,group["cameras"][0],Signal("Motion","motion",True,False,datetime.now(timezone.utc).replace(tzinfo=None),None,True,{}))
    application=FastAPI();application.include_router(routes.router)
    def database():
        with factory() as db:
            yield db
    def actor():
        with factory() as db:
            return db.get(User,1)
    application.dependency_overrides[get_db]=database
    application.dependency_overrides[get_current_user]=actor
    client=TestClient(application)
    yield client,factory,identifier,application
    client.close();engine.dispose()


def test_viewer_gets_history_but_not_camera_configuration(api):
    client,_,identifier,_=api
    response=client.get("/camera-events")
    assert response.status_code==200 and response.json()["items"][0]["id"]==identifier
    assert client.get("/camera-events/cameras/1/detectors").status_code==403
    assert client.put("/camera-events/cameras/1/detectors/"+"a"*32,json={"enabled":True,"revision":"a"*64}).status_code==403
    assert client.get(f"/camera-events/{identifier}/target?mode=archive").status_code==403
    assert client.get(f"/camera-events/{identifier}/target?mode=live").json()["camera_id"]==1


def test_admin_can_read_unsupported_camera_without_network(api):
    client,factory,_,_=api
    with factory() as db:
        db.get(User,1).role="admin";db.commit()
    response=client.get("/camera-events/cameras/1/detectors")
    assert response.status_code==200 and response.json()["supported"] is False


@pytest.mark.parametrize("query",["limit=201","offset=-1","kind=unknown","since=bad","after=bad"])
def test_untrusted_event_filters_are_bounded(api,query):
    client,_,_,_=api
    path="/camera-events/updates?wait=0&" if "after=" in query else "/camera-events?"
    assert client.get(path+query).status_code==422


def test_read_and_preferences_are_idempotent_and_separate(api):
    client,_,identifier,_=api
    assert client.post("/camera-events/read",json={"ids":[identifier]}).status_code==200
    assert client.post("/camera-events/read",json={"ids":[identifier]}).status_code==200
    assert client.get("/camera-events").json()["unread_count"]==0
    assert client.put("/camera-events/preferences",json={"notifications":False}).status_code==200
    assert client.get("/camera-events/preferences").json()["notifications"] is False
    assert client.get("/camera-events").json()["total"]==1
    assert client.put("/camera-events/preferences",json={"notifications":False,"recording_mode":"off"}).status_code==422


def test_read_all_spans_database_batches_without_touching_newer_events_or_other_users(api):
    client, factory, _, _ = api
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory() as db:
        db.add(User(id=2, username="another-viewer", password_hash="test-only", role="viewer", is_active=True))
        db.add_all([AuditEvent(id=str(uuid.uuid4()), created_at=now-timedelta(minutes=1), category=domain.CATEGORY,
            event_type="camera.other", severity="info", message_ru="", message_en="", target_type="camera", target_id="1",
            target_name="Front", event_metadata={"topic":"RuleEngine/FieldDetector/ObjectsInside"}) for _ in range(1005)])
        db.commit()
    snapshot = client.get("/camera-events?limit=1").json()
    assert snapshot["total"] == 1006 and snapshot["read_cursor"]
    with factory() as db:
        latest_id = str(uuid.uuid4())
        db.add(AuditEvent(id=latest_id, created_at=now+timedelta(seconds=1), category=domain.CATEGORY,
            event_type="camera.motion", severity="info", message_ru="", message_en="", target_type="camera", target_id="1",
            target_name="Front", event_metadata={}))
        db.commit()
    result = client.post("/camera-events/read-all", json={"through": snapshot["read_cursor"]})
    assert result.status_code == 200 and result.json()["marked"] == 1006
    remaining = client.get("/camera-events?unread=true").json()
    assert remaining["unread_count"] == 1 and [item["id"] for item in remaining["items"]] == [latest_id]
    assert client.post("/camera-events/read-all", json={"through": snapshot["read_cursor"]}).json()["marked"] == 0
    with factory() as db:
        assert domain.list_camera_events(db, 2)["unread_count"] == 1007
        assert domain.event_query(db).count() == 1007
        assert db.get(Camera, 1).enabled is True


@pytest.mark.parametrize("cursor", ["invalid", "wrong|"+str(uuid.uuid4()), "2026-11-13T12:00:00Z|"+str(uuid.uuid4()),
    "2026-11-13T12:00:00|not-a-valid-uuid-padding", "2026-11-13T12:00:00|"+"f"*100])
def test_read_all_rejects_invalid_boundary_without_marking_any_events(api, cursor):
    client, _, _, _ = api
    assert client.post("/camera-events/read-all", json={"through":cursor}).status_code == 422
    assert client.get("/camera-events").json()["unread_count"] == 1


def test_unrelated_or_deleted_events_never_open_arbitrary_cameras(api):
    client,factory,identifier,_=api
    assert client.get(f"/camera-events/{uuid.uuid4()}/target?mode=live").status_code==404
    with factory() as db:
        db.get(Camera,1).deleted_at=datetime.now();db.commit()
    assert client.get(f"/camera-events/{identifier}/target?mode=live").status_code==404


def test_recording_off_does_not_block_event_live_target(api):
    client,factory,identifier,_=api
    with factory() as db:
        db.get(Camera,1).enabled=False;db.get(Camera,1).status="disabled";db.commit()
    assert client.get(f"/camera-events/{identifier}/target?mode=live").status_code==200


def test_marker_permission_matches_chronology(api):
    client,factory,_,_=api
    query="/camera-events/markers?camera_ids=1&from=2026-11-12T00:00:00&to=2026-11-13T00:00:00"
    assert client.get(query).status_code==403
    with factory() as db:
        db.get(User,1).role="operator";db.commit()
    assert client.get(query).status_code==200
    assert client.get(query.replace("camera_ids=1","camera_ids=not-an-id")).status_code==422


def test_marker_preserves_the_safe_standard_topic_for_the_same_rule_label_as_history(api):
    client, factory, identifier, _ = api
    with factory() as db:
        db.get(User, 1).role = "operator"
        row = db.get(AuditEvent, identifier)
        row.created_at = datetime(2026, 11, 12, 12)
        row.event_type = "camera.other"
        row.event_metadata = {"topic":"RuleEngine/FieldDetector/ObjectsInside", "occurred_at":"2026-11-12T12:00:00Z"}
        db.commit()
    value = client.get("/camera-events/markers?camera_ids=1&from=2026-11-12T00:00:00Z&to=2026-11-13T00:00:00Z").json()
    assert len(value["items"]) == 1
    assert value["items"][0]["topic"] == "RuleEngine/FieldDetector/ObjectsInside"
    assert not {"metadata", "host", "username", "password", "source_token"}.intersection(value["items"][0])


def test_updates_cursor_and_preferences_do_not_expose_internal_records(api):
    client,_,identifier,_=api
    result=client.get("/camera-events/updates?wait=0").json()
    assert result["items"]==[] and result["cursor"] and result["timezone"]
    assert client.get("/camera-events/updates",params={"wait":0,"after":result["cursor"]}).json()["items"]==[]
    row=client.get("/camera-events").json()["items"][0]
    for field in ("episode","metadata","actor_username","password","username","host","source_token"):
        assert field not in row


def test_async_listeners_are_bounded_and_wake_without_wait_threads():
    async def scenario():
        listeners=[domain.listen(1) for _ in range(8)]
        assert all(listeners) and domain.listen(1) is None
        other=domain.listen(2)
        assert other is not None
        try:
            domain.notify_change()
            await asyncio.wait_for(asyncio.gather(*(value[1].wait() for value in listeners+[other])),1)
        finally:
            for value in listeners+[other]:domain.unlisten(value[0])
    asyncio.run(scenario())
    assert not domain._listeners


def test_opt_in_is_default_off_and_stale_receiver_state_cannot_show_it_as_on(api, monkeypatch):
    client, factory, _, _ = api
    with factory() as db:
        camera = db.get(Camera, 1); camera.protocol = "onvif"; db.commit()
    monkeypatch.setattr(routes, "receiver_states", lambda: {"1": {"status": "connected"}})
    item = client.get("/camera-events/cameras").json()["items"][0]
    assert item["events_enabled"] is False and item["receiver"]["status"] == "disabled"


def test_capability_read_and_opt_in_writes_require_camera_management(api, monkeypatch):
    client, factory, _, app = api
    app.include_router(camera_routes.router)
    reader = []
    def read(**kwargs):
        reader.append(True)
        return {"supported": True, "status": "supported", "kinds": ["motion"]}
    monkeypatch.setattr(onvif_routes, "read_event_capabilities", read)
    payload = {"camera_id": 1, "protocol": "onvif"}
    assert client.post("/cameras/onvif/events/capabilities", json=payload).status_code == 403
    assert client.put("/cameras/1", json={"events_enabled": True}).status_code == 403
    assert not reader
    with factory() as db:
        db.get(User, 1).role = "admin"; db.commit()
    response = client.post("/cameras/onvif/events/capabilities", json=payload)
    assert response.status_code == 200 and response.json()["supported"] is True
    assert reader == [True]


def test_saved_receiving_choice_can_toggle_without_changing_recording_and_without_network_when_off(api, monkeypatch):
    client, factory, _, app = api
    app.include_router(camera_routes.router)
    with factory() as db:
        db.get(User, 1).role = "admin"
        camera = db.get(Camera, 1)
        camera.protocol = "onvif"; camera.port = 80; camera.username = "synthetic"
        camera.password_encrypted = encrypt_text("synthetic-camera-password")
        camera.onvif_profile_token = "profile1"; camera.rtsp_main_url = "rtsp://synthetic:synthetic-camera-password@192.0.2.1:554/main"
        camera.rtsp_host = camera.host; camera.rtsp_port = 554
        db.commit()
    calls = []
    monkeypatch.setattr(camera_event_settings, "read_event_capabilities", lambda **kwargs: calls.append(True) or {"supported": True})
    on = client.put("/cameras/1", json={"events_enabled": True})
    assert on.status_code == 200 and on.json()["events_enabled"] is True
    assert on.json()["enabled"] is True
    assert calls == [True]
    renamed = client.put("/cameras/1", json={"name": "Renamed"})
    assert renamed.status_code == 200 and renamed.json()["events_enabled"] is True
    off = client.put("/cameras/1", json={"events_enabled": False})
    assert off.status_code == 200 and off.json()["events_enabled"] is False
    assert off.json()["enabled"] is True and calls == [True]
    assert client.get("/camera-events").json()["total"] == 1
