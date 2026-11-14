import base64
import json
import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.models import User, AuditEvent, Camera
from app.routers.deps import get_current_user
from app.routers import mobile_push as routes
from app.services import camera_events as events, mobile_push as push, mobile_push_store as store
from app.services import mobile_push_transport as transport


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *_a, **_kw: [(2, 1, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(push, "wake_local", lambda: None)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([User(id=i, username=f"viewer{i}", password_hash="test-only", role="viewer", is_active=True) for i in (1, 2)])
        db.add(Camera(id=1, name="Gate", storage_folder_name="gate", host="192.0.2.1", port=554, protocol="rtsp", enabled=False))
        db.commit()
    application = FastAPI(); application.include_router(routes.router)
    def database():
        with factory() as db: yield db
    def actor():
        with factory() as db: return db.get(User, 1)
    application.dependency_overrides[get_db] = database
    application.dependency_overrides[get_current_user] = actor
    client = TestClient(application)
    raw = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    subscription = {"endpoint": "https://push.example.test/opaque-capability", "keys": {"p256dh": encode(raw), "auth": encode(b"a" * 16)}}
    yield client, factory, str(uuid.uuid4()), subscription
    client.close(); engine.dispose()


def register(fixture, revision=1, enabled=True):
    client, factory, device_id, subscription = fixture
    response = client.put(f"/mobile/push/devices/{device_id}", json={"revision": revision, "label": "Test phone", "enabled": enabled,
        "subscription": subscription if enabled else None})
    assert response.status_code == 200, response.text
    with factory() as db: identifier = store.by_user(db, 1, device_id).id
    return identifier


def event(factory, **metadata):
    with factory() as db:
        row = AuditEvent(id=str(uuid.uuid4()), created_at=events.now_utc(), category=events.CATEGORY, event_type="camera.intrusion",
            severity="info", message_ru="", message_en="", target_type="camera", target_id="1", target_name="Gate",
            event_metadata={"occurred_at": events.now_utc().isoformat() + "Z", **metadata})
        db.add(row); db.commit()
        return row.id


def test_registration_is_encrypted_actor_bound_and_uses_existing_schema(fixture):
    client, factory, device_id, subscription = fixture
    with factory() as db: before = set(inspect(db.connection()).get_table_names())
    identifier = register(fixture)
    payload = client.get(f"/mobile/push/devices/{device_id}").json()
    assert payload["enabled"] and "opaque-capability" not in json.dumps(payload)
    with factory() as db:
        saved = store.get(db, identifier)
        assert "opaque-capability" not in json.dumps(saved.row.layout)
        assert store.by_user(db, 2, device_id) is None
        assert set(inspect(db.connection()).get_table_names()) == before
        assert db.get(Camera, 1).enabled is False
    assert transport.vapid_public_key() == transport.vapid_public_key()
    assert len(transport.decode_key(transport.vapid_public_key(), 65)) == 65


def test_late_registration_cannot_revive_disabled_device(fixture):
    client, factory, device_id, subscription = fixture
    register(fixture)
    register(fixture, revision=3, enabled=False)
    assert client.put(f"/mobile/push/devices/{device_id}", json={"revision": 2, "label": "Test phone", "enabled": True,
        "subscription": subscription}).status_code == 409
    with factory() as db: assert not store.by_user(db, 1, device_id).enabled


@pytest.mark.parametrize("target", ["http://push.example.test/a", "https://user@push.example.test/a", "https://push.example.test:444/a", "https://localhost/a", "https://push.example.test/a#b"])
def test_invalid_destinations_are_not_registered(fixture, target):
    client, _, device, subscription = fixture
    subscription["endpoint"] = target
    assert client.put(f"/mobile/push/devices/{device}", json={"revision": 1, "label": "Test", "enabled": True, "subscription": subscription}).status_code == 422


def test_dns_with_any_private_answer_is_rejected(fixture, monkeypatch):
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *_a, **_kw: [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    client, _, device, subscription = fixture
    assert client.put(f"/mobile/push/devices/{device}", json={"revision": 1, "label": "Test", "enabled": True, "subscription": subscription}).status_code == 422


def test_real_webpush_encryption_and_pinned_tls_do_not_follow_redirects(fixture, monkeypatch):
    from http_ece import decrypt
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    subscription = {"endpoint": "https://push.example.test/opaque-token", "keys": {"p256dh": encode(public), "auth": encode(b"a" * 16)}}
    observed = {}
    class Pool:
        def __init__(self, host, port, **kwargs):
            observed.update(host=host, port=port, options=kwargs)
        def urlopen(self, method, path, **kwargs):
            observed.update(method=method, path=path, request=kwargs)
            return type("Response", (), {"status": 201, "close": lambda self: None})()
        def close(self): observed["closed"] = True
    monkeypatch.setattr(transport, "HTTPSConnectionPool", Pool)
    try:
        transport.send_push(subscription, '{"v":1,"test":true}')
    except transport.PushDeliveryError as exc:
        pytest.fail(type(exc.__context__).__name__ + ": " + str(exc.__context__))
    assert observed["host"] == "8.8.8.8" and observed["options"]["server_hostname"] == "push.example.test"
    assert observed["options"]["assert_hostname"] == "push.example.test" and observed["request"]["redirect"] is False
    assert {key.lower(): value for key, value in observed["request"]["headers"].items()}["authorization"].startswith("vapid ")
    assert decrypt(observed["request"]["body"], private_key=private, auth_secret=b"a" * 16, version="aes128gcm") == b'{"v":1,"test":true}'
    assert observed["closed"]


def test_notification_detail_rejects_non_camera_audit_entries(fixture):
    client, factory, _, _ = fixture
    current = event(factory)
    assert client.get(f"/mobile/push/events/{current}").json()["event"]["id"] == current
    with factory() as db:
        row = AuditEvent(id=str(uuid.uuid4()), category="settings", event_type="secret.audit", severity="info", message_ru="", message_en="", event_metadata={})
        db.add(row); db.commit(); other = row.id
    assert client.get(f"/mobile/push/events/{other}").status_code == 404
    assert client.post(f"/mobile/push/devices/{uuid.uuid4()}/test").status_code == 404


def test_delivery_advances_durable_cursor_without_changing_history(fixture):
    _, factory, _, _ = fixture
    old = event(factory)
    identifier = register(fixture)
    current = event(factory)
    sent = []
    assert push.deliver_one(factory, identifier, "test-worker", sender=lambda subscription, payload: sent.append(json.loads(payload)))
    assert [item["event_id"] for item in sent] == [current]
    assert push.deliver_one(factory, identifier, "test-worker", sender=lambda *_: pytest.fail("duplicate")) is False
    with factory() as db:
        assert store.get(db, identifier).cursor_id == current
        assert events.event_query(db).count() == 2
        assert store.get(db, identifier).last_status == "service_accepted"


def test_transient_failure_retries_same_event_after_backoff(fixture):
    _, factory, _, _ = fixture
    identifier = register(fixture); current = event(factory)
    def fail(*_): raise transport.PushDeliveryError("push_delivery_unavailable")
    assert push.deliver_one(factory, identifier, "test-worker", sender=fail)
    with factory() as db:
        saved = store.get(db, identifier)
        assert saved.cursor_id != current and saved.retry_at > events.now_utc()
    assert push.deliver_one(factory, identifier, "test-worker", sender=lambda *_: pytest.fail("early retry")) is False
    with factory() as db:
        saved = store.get(db, identifier); saved.retry_at = events.now_utc() - timedelta(seconds=1); saved.save(); db.commit()
    assert push.deliver_one(factory, identifier, "another-process", sender=lambda *_: None)
    with factory() as db:
        saved = store.get(db, identifier)
        assert saved.cursor_id == current and saved.failures == 0 and saved.retry_at is None


def test_orphaned_other_process_lease_schedules_one_wake_instead_of_losing_backlog(fixture):
    from app.services.storage_operations_foundation import acquire_worker_lease
    _, factory, _, _ = fixture
    identifier = register(fixture); event(factory)
    with factory() as db:
        lease = acquire_worker_lease(db, worker_key="mobile.push." + str(identifier), owner_instance_id="other-process", lease_seconds=45)
    deferred = []
    assert push.deliver_one(factory, identifier, "new-process", sender=lambda *_: pytest.fail("leased"), defer=deferred.append) is False
    assert len(deferred) == 1 and deferred[0] > events.now_utc()


def test_later_person_classification_is_not_lost_after_filtered_start(fixture):
    _, factory, _, _ = fixture
    identifier = register(fixture)
    current = event(factory)
    with factory() as db:
        preferences = events.preferences(db, 1)[0]
        events.save_preferences(db, 1, {**preferences, "kinds": ["person"]})
    assert push.deliver_one(factory, identifier, "first-worker", sender=lambda *_: pytest.fail("not a person yet"))
    with factory() as db:
        row = db.get(AuditEvent, current)
        row.event_metadata = {**row.event_metadata, "object_kinds": ["person"], "mobile_notification_at": events.now_utc().strftime("%Y-%m-%d %H:%M:%S.%f")}
        db.commit()
    sent = []
    assert push.deliver_one(factory, identifier, "next-worker", sender=lambda _s, payload: sent.append(json.loads(payload)))
    assert [item["event_id"] for item in sent] == [current]
    assert push.deliver_one(factory, identifier, "next-worker", sender=lambda *_: pytest.fail("duplicate")) is False


@pytest.mark.parametrize("scenario", ["disabled_user", "no_permission", "muted", "kind_filter", "camera_filter", "permanent_error", "logout_during_send"])
def test_delivery_obeys_permissions_preferences_and_concurrent_logout(fixture, scenario):
    _, factory, _, _ = fixture
    identifier = register(fixture); current = event(factory)
    with factory() as db:
        if scenario == "disabled_user": db.get(User, 1).is_active = False
        if scenario == "no_permission": db.get(User, 1).role = "unknown"
        if scenario in {"muted", "kind_filter", "camera_filter"}:
            value = events.preferences(db, 1)[0]
            value.update({"notifications": False} if scenario == "muted" else {"kinds": ["animal"]} if scenario == "kind_filter" else {"camera_ids": [99]})
            events.save_preferences(db, 1, value)
        db.commit()
    calls = []
    def sender(*_):
        calls.append(1)
        if scenario == "permanent_error": raise transport.PushDeliveryError("push_registration_expired", permanent=True)
        if scenario == "logout_during_send":
            with factory() as db:
                saved = store.get(db, identifier); saved.enabled = False; saved.revision += 1; saved.save(); db.commit()
    push.deliver_one(factory, identifier, "test-worker", sender=sender)
    with factory() as db:
        saved = store.get(db, identifier)
        assert len(calls) == (1 if scenario in {"permanent_error", "logout_during_send"} else 0)
        if scenario in {"disabled_user", "no_permission", "permanent_error", "logout_during_send"}: assert not saved.enabled
        else: assert saved.cursor_id == current
        assert events.event_query(db).count() == 1
