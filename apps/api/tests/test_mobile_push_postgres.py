"""Real LISTEN/NOTIFY delivery in an owned, disposable PostgreSQL database."""
import base64
import os
import threading
import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, event as sql_event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models import User, AuditEvent
from app.services import camera_events as events, mobile_push as push, mobile_push_store as store
from app.services import mobile_push_transport as transport, mobile_push_worker as worker
from app.services import mobile_badges as badges
import json

TEST_URL = os.getenv("KMVMS_MOBILE_POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="owned mobile PostgreSQL test URL required")


def test_cross_connection_notification_durable_backlog_and_no_idle_polling(monkeypatch):
    address = make_url(TEST_URL)
    name = "mobile_push_" + uuid.uuid4().hex
    admin = create_engine(address, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection: connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(address.set(database=name))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sent, done, queries = [], threading.Event(), []
    try:
        Base.metadata.create_all(engine)
        with factory() as db:
            db.add(User(id=1, username="isolated-viewer", password_hash="test-only", role="viewer", is_active=True)); db.commit()
        monkeypatch.setattr(settings, "database_url", address.set(database=name).render_as_string(hide_password=False))
        monkeypatch.setattr(worker, "SessionLocal", factory)
        def delivered(_subscription, payload): sent.append(payload); done.set()
        monkeypatch.setattr(worker, "deliver_one", lambda factory, identifier, owner, **kwargs: push.deliver_one(factory, identifier, owner, sender=delivered, **kwargs))
        def observed(_conn, _cursor, statement, *_args): queries.append(statement)
        sql_event.listen(engine, "before_cursor_execute", observed)
        raw = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        device = str(uuid.uuid4())
        with monkeypatch.context() as dns:
            dns.setattr(transport.socket, "getaddrinfo", lambda *_a, **_kw: [(2, 1, 6, "", ("8.8.8.8", 443))])
            with factory() as db:
                push.register_device(db, 1, device, 1, "Isolated phone", True, {"endpoint": "https://push.example.test/a",
                    "keys": {"p256dh": encode(raw), "auth": encode(b"a" * 16)}})
        worker.start_mobile_push_worker()
        assert worker._ready.wait(5), "notification listener must connect to the owned test DB"
        # Wait until the initial database reads settle, then prove no idle scan.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            count = len(queries); time.sleep(.2)
            if count > 0 and len(queries) == count: break
        count = len(queries); time.sleep(.7)
        assert len(queries) == count, "worker must sleep, not poll the DB while idle"
        identifier = str(uuid.uuid4())
        with factory() as db:
            db.add(AuditEvent(id=identifier, created_at=events.now_utc(), category=events.CATEGORY, event_type="camera.person",
                severity="info", message_ru="", message_en="", target_type="camera", target_id="1", target_name="Gate", event_metadata={}))
            push.signal_change(db); db.commit()  # No local wake: simulates another API process.
        assert done.wait(5), "cross-connection NOTIFY must wake the sleeping worker"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with factory() as db: saved = store.by_user(db, 1, device)
            if saved.cursor_id == identifier: break
            time.sleep(.05)
        assert saved.cursor_id == identifier and len(sent) == 1
        worker.stop_mobile_push_worker()
        assert worker._thread is None
        # Restart preserves cursor: no second notification for the same event.
        worker.start_mobile_push_worker(); time.sleep(.5); worker.stop_mobile_push_worker()
        assert len(sent) == 1
    finally:
        worker.stop_mobile_push_worker()
        engine.dispose()
        with admin.connect() as connection: connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


def test_silent_unread_channel_wakes_on_committed_reads_without_idle_polling(monkeypatch):
    address = make_url(TEST_URL)
    name = "mobile_badge_" + uuid.uuid4().hex
    admin = create_engine(address, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(address.set(database=name))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sent, queries = [], []
    def until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.05)
        assert predicate()
    try:
        Base.metadata.create_all(engine)
        with factory() as db:
            db.add(User(id=1, username="isolated-counter", password_hash="test-only", role="viewer", is_active=True)); db.commit()
        monkeypatch.setattr(settings, "database_url", address.set(database=name).render_as_string(hide_password=False))
        monkeypatch.setattr(worker, "SessionLocal", factory)
        def delivered(_subscription, payload):
            sent.append(json.loads(payload))
        monkeypatch.setattr(worker, "deliver_badge", lambda factory, identifier, owner, **kwargs:
            badges.deliver_one(factory, identifier, owner, sender=delivered, **kwargs))
        sql_event.listen(engine, "before_cursor_execute", lambda _c, _cu, statement, *_args: queries.append(statement))
        raw = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        device = str(uuid.uuid4())
        with monkeypatch.context() as dns:
            dns.setattr(transport.socket, "getaddrinfo", lambda *_a, **_kw: [(2, 1, 6, "", ("8.8.8.8", 443))])
            with factory() as db:
                push.register_device(db, 1, device, 1, "Counter only", False, {"endpoint": "https://push.example.test/counter",
                    "keys": {"p256dh": encode(raw), "auth": encode(b"a" * 16)}}, badge_enabled=True)
        worker.start_mobile_push_worker()
        assert worker._ready.wait(5)
        until(lambda: len(sent) == 1)
        assert sent[0]["count"] == 0
        identifier = str(uuid.uuid4())
        with factory() as db:
            db.add(AuditEvent(id=identifier, created_at=events.now_utc(), category=events.CATEGORY, event_type="camera.person",
                severity="info", message_ru="", message_en="", target_type="camera", target_id="1", target_name="Gate", event_metadata={}))
            db.flush(); badges.touch(db); db.commit()
        until(lambda: len(sent) == 2)
        assert sent[1]["count"] == 1
        with factory() as db:
            events.mark_read(db, 1, [identifier])
        until(lambda: len(sent) == 3)
        assert sent[2]["count"] == 0
        with factory() as db:
            badges.touch(db); db.rollback()
        time.sleep(.3)
        count = len(queries)
        time.sleep(.8)
        assert len(queries) == count and len(sent) == 3, "idle means no DB polling or extra push"
        assert all(value["type"] == "unread_badge" for value in sent)
        assert sent[0]["revision"] < sent[1]["revision"] < sent[2]["revision"]
    finally:
        worker.stop_mobile_push_worker()
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
