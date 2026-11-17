from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.models import Camera, SystemSettings, User
from app.routers import deps, live, settings as routes
from app.services.system_settings import get_system_settings, serialize_settings, validate_settings_payload


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    owner = User(id=1, username="mode-owner", password_hash="test-only", role="owner", is_active=True)
    db.add(owner)
    db.add(Camera(id=1, name="Mode test", host="192.0.2.1", port=554, protocol="rtsp", storage_folder_name="mode-test", enabled=False))
    db.commit()
    state = SimpleNamespace(actor=owner)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(live.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_current_user] = lambda: state.actor
    monkeypatch.setattr(routes, "format_change_status", lambda *_: {"state": "idle"})
    with TestClient(app) as client:
        yield client, state, db
    db.close()
    engine.dispose()


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_save_shared_mode_without_changing_recording(api, role):
    client, state, db = api
    state.actor.role = role
    original = serialize_settings(get_system_settings(db))
    saved = client.patch("/settings", json={"online_mode": "fast"})
    assert saved.status_code == 200
    assert saved.json()["online_mode"] == "fast"
    db.expire_all()
    assert client.get("/settings").json()["online_mode"] == "fast"
    assert client.get("/settings").json()["recording_format"] == original["recording_format"]
    assert client.get("/settings").json()["timezone"] == original["timezone"]
    assert client.patch("/settings", json={"online_mode": "stable"}).json()["online_mode"] == "stable"


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_viewing_roles_can_read_mode_with_media_authority_but_cannot_change_it(api, role):
    client, state, db = api
    get_system_settings(db).online_mode = "fast"
    db.commit()
    state.actor.role = role
    assert client.patch("/settings", json={"online_mode": "stable"}).status_code == 403
    assert client.get("/settings").status_code == 403
    response = client.post("/live/media-token", json={"camera_id": 1, "stream": "main"})
    assert response.status_code == 200
    assert response.json()["online_mode"] == "fast"
    assert "storage_path" not in response.json()
    assert "no-store" in response.headers["cache-control"]


def test_existing_default_is_stable(api):
    client, _state, db = api
    assert serialize_settings(get_system_settings(db))["online_mode"] == "stable"
    assert client.post("/live/media-token", json={"camera_id": 1, "stream": "sub"}).json()["online_mode"] == "stable"


def test_mode_is_not_disclosed_to_an_unauthenticated_client():
    app = FastAPI(); app.include_router(live.router)
    with TestClient(app) as client:
        assert client.post("/live/media-token", json={"camera_id": 1, "stream": "sub"}).status_code == 401


@pytest.mark.parametrize("value", [None, "", "auto", "FAST", True, 1])
def test_invalid_modes_are_rejected(api, value):
    client, _state, db = api
    assert client.patch("/settings", json={"online_mode": value}).status_code == 422
    assert get_system_settings(db).online_mode == "stable"
