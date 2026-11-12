import asyncio
from datetime import datetime, timezone
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
from app.routers.deps import get_current_user
from app.routers import camera_events as routes
from app.services import camera_events as domain
from app.services.onvif_events import Signal


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
