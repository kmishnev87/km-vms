import base64
import json
import uuid
from datetime import datetime, timedelta

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


@pytest.mark.parametrize("value", ["2026-11-16T04:25:38+00:00", "2026-11-16T04:25:38Z", "2026-11-16T09:25:38+05:00", datetime(2026, 11, 16, 4, 25, 38)])
def test_push_time_is_android_8_compatible_utc_z(value):
    assert push.push_timestamp(value) == "2026-11-16T04:25:38Z"
    encoded = json.loads(push.event_payload({"id": str(uuid.uuid4()), "kind": "intrusion", "occurred_at": value}))
    assert encoded["occurred_at"] == "2026-11-16T04:25:38Z"


def test_push_fractional_seconds_are_preserved():
    assert push.push_timestamp("2026-11-16T09:25:38.123456+05:00") == "2026-11-16T04:25:38.123456Z"


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


def test_failed_dns_is_retryable_on_registration_and_does_not_discard_a_working_route(fixture, monkeypatch):
    client, factory, device, subscription = fixture
    identifier = register(fixture)
    current = event(factory)
    resolve = transport.socket.getaddrinfo

    def disconnected(*_, **__):
        raise transport.socket.gaierror(transport.socket.EAI_AGAIN, "test-resolver")

    monkeypatch.setattr(transport.socket, "getaddrinfo", disconnected)
    response = client.put(f"/mobile/push/devices/{device}", json={"revision": 2, "label": "Test phone",
        "enabled": True, "subscription": subscription})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "mobile_push_dns_unavailable"
    assert push.deliver_one(factory, identifier, "dns-test", sender=transport.send_push)
    with factory() as db:
        saved = store.get(db, identifier)
        assert saved.enabled and saved.subscription_encrypted
        assert saved.revision == 1 and saved.last_status == "push_endpoint_unavailable"
        assert saved.cursor_id != current and saved.retry_at > events.now_utc()
        saved.retry_at = events.now_utc() - timedelta(seconds=1)
        saved.save()
        db.commit()

    monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
    sent = []
    assert push.deliver_one(factory, identifier, "dns-test", sender=lambda sub, data: sent.append(json.loads(data)))
    assert [item["event_id"] for item in sent] == [current]
    with factory() as db:
        saved = store.get(db, identifier)
        assert saved.enabled and saved.last_status == "service_accepted"
        assert saved.failures == 0 and saved.retry_at is None


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


def test_both_sources_reach_the_same_badge_and_device_delivery(fixture, tmp_path, monkeypatch):
    from app.services import ai_event_history
    from app.services.onvif_events import Signal

    _, factory, _, _ = fixture
    device = register(fixture)
    monkeypatch.setattr(ai_event_history.settings, "storage_previews", str(tmp_path))
    captured = events.now_utc()
    with factory() as db:
        camera = db.get(Camera, 1)
        camera.protocol = "onvif"
        camera.events_enabled = False  # Explicit policy wins over the legacy column.
        camera.recording_policy = {"schema_version": 1, "revision": 1, "mode": "continuous",
                                   "source": "camera", "profile": "none"}
        db.add(Camera(id=2, name="AI", storage_folder_name="fixture_ai", host="192.0.2.2",
            port=554, enabled=True, protocol="rtsp", events_enabled=False,
            recording_policy={"schema_version": 1, "revision": 1, "mode": "continuous",
                              "source": "ai", "profile": "none", "event_kinds": ["person"]}))
        events.save_preferences(db, 2, {"notifications": False})
        lens = {"id": 1, "name": "Gate", "events_enabled": True}
        native = events.record_signal(db, {"device_id": "fixture", "cameras": [lens]}, lens,
            Signal("RuleEngine/Motion", "motion", True, False, captured, captured, False, source={}))
        assert native
        ai = uuid.uuid4()
        inbox = tmp_path / (ai.hex + ".evt")
        from datetime import timezone
        packet = {"v": 1, "camera_id": 2, "revision": 1, "captured": captured.replace(tzinfo=timezone.utc).timestamp(),
                  "rule_id": None, "condition": "presence", "objects": ["person"]}
        inbox.write_bytes(json.dumps(packet).encode() + b"\n")
        assert ai_event_history.ingest(db, inbox, received=captured)
        assert events.updates(db, 1)["unread_count"] == 2
        assert events.updates(db, 2)["unread_count"] == 0
        assert events.automatic_read_ids(db, 2, [native, str(ai)]) == {native, str(ai)}
        assert events.list_camera_events(db, 1, source="camera")["total"] == 1
        assert events.list_camera_events(db, 1, source="ai")["total"] == 1
    sent = []
    for _ in range(2):
        assert push.deliver_one(factory, device, "test-worker", sender=lambda _, payload: sent.append(json.loads(payload)))
    assert {payload["event_id"]: payload.get("ai_condition") for payload in sent} == {native: None, str(ai): "presence"}


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


def test_legacy_type_filters_do_not_block_push_and_metadata_update_does_not_repeat_it(fixture):
    _, factory, _, _ = fixture
    identifier = register(fixture)
    current = event(factory)
    with factory() as db:
        preferences = events.preferences(db, 1)[0]
        events.save_preferences(db, 1, {**preferences, "kinds": ["person"]})
    sent = []
    assert push.deliver_one(factory, identifier, "first-worker", sender=lambda _s, payload: sent.append(json.loads(payload)))
    with factory() as db:
        row = db.get(AuditEvent, current)
        row.event_metadata = {**row.event_metadata, "object_kinds": ["person"]}
        db.commit()
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
        assert len(calls) == (1 if scenario in {"permanent_error", "logout_during_send", "kind_filter"} else 0)
        if scenario in {"disabled_user", "no_permission", "permanent_error", "logout_during_send"}: assert not saved.enabled
        else: assert saved.cursor_id == current
        assert events.event_query(db).count() == 1
