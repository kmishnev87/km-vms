import json
from datetime import timedelta
from types import SimpleNamespace

from test_mobile_push import fixture, event, register
from app.services import camera_events as events, mobile_push as push, mobile_push_store as store, mobile_badges as badges
from app.services.mobile_push_transport import PushDeliveryError


def opt_in(fixture, *, enabled=False, revision=1, badge=True):
    client, factory, device_id, subscription = fixture
    result = client.put(f"/mobile/push/devices/{device_id}", json={"revision": revision,
        "label": "Widget fixture", "enabled": enabled, "badge_enabled": badge,
        "subscription": subscription if enabled or badge else None})
    assert result.status_code == 200, result.text
    with factory() as db:
        return store.by_user(db, 1, device_id).id


def current(factory, user=1):
    with factory() as db:
        value = badges.snapshot(db, user)
        db.commit()
        return value


def changed(factory):
    with factory() as db:
        badges.touch(db)
        db.commit()


def test_opt_in_snapshot_uses_the_same_account_unread_query(fixture):
    client, factory, *_ = fixture
    item = event(factory)
    before = current(factory)
    other = current(factory, 2)
    assert before["count"] == other["count"] == 1
    assert before["epoch"] != other["epoch"]
    with factory() as db:
        events.mark_read(db, 1, [item])
    after = current(factory)
    assert after["count"] == 0 and after["revision"] > before["revision"]
    assert current(factory, 2) == other
    result = client.get('/mobile/push/config?badge=true')
    assert result.status_code == 200
    assert result.json()["widget_badge_v1"] is True
    assert result.json()["unread_badge"] == after


def test_zero_is_delivered_once_without_enabling_phone_alerts(fixture):
    _, factory, *_ = fixture
    device = opt_in(fixture)
    packets = []
    assert badges.deliver_one(factory, device, "counter", sender=lambda _s, value: packets.append(json.loads(value)))
    assert packets[0]["type"] == "unread_badge" and packets[0]["count"] == 0
    assert not badges.deliver_one(factory, device, "counter", sender=lambda *_: packets.append(None))
    assert not push.deliver_one(factory, device, "visible", sender=lambda *_: packets.append(None))
    with factory() as db:
        row = store.get(db, device)
        assert not row.enabled and row.badge_enabled
    assert len(packets) == 1


def test_read_and_delete_push_absolute_counts_instead_of_adding_push_messages(fixture):
    _, factory, *_ = fixture
    device = opt_in(fixture)
    one, two = event(factory), event(factory)
    changed(factory)
    packets = []
    sender = lambda _s, value: packets.append(json.loads(value))
    assert badges.deliver_one(factory, device, "counter", sender=sender)
    with factory() as db:
        events.mark_read(db, 1, [one])
    assert badges.deliver_one(factory, device, "counter", sender=sender)
    with factory() as db:
        assert events.delete_camera_events(db, ids=[two]) == 1
    assert badges.deliver_one(factory, device, "counter", sender=sender)
    assert [value["count"] for value in packets] == [2, 1, 0]
    assert packets[0]["revision"] < packets[1]["revision"] < packets[2]["revision"]
    assert all(set(value) == {"v", "type", "epoch", "revision", "count"} for value in packets)


def test_unchanged_counts_are_coalesced_and_rolled_back_changes_do_not_escape(fixture):
    _, factory, *_ = fixture
    device = opt_in(fixture)
    packets = []
    sender = lambda _s, value: packets.append(json.loads(value))
    badges.deliver_one(factory, device, "counter", sender=sender)
    first = current(factory)
    with factory() as db:
        badges.touch(db, 1)
        db.rollback()
    assert current(factory) == first
    assert not badges.deliver_one(factory, device, "counter", sender=sender)
    for _ in range(3):
        changed(factory)
    assert badges.deliver_one(factory, device, "counter", sender=sender)
    assert not badges.deliver_one(factory, device, "counter", sender=sender)
    assert len(packets) == 1


def test_a_failed_counter_delivery_does_not_delay_visible_event_delivery(fixture):
    _, factory, *_ = fixture
    device = opt_in(fixture, enabled=True)
    event(factory)
    changed(factory)
    def failed(*_):
        raise PushDeliveryError("temporary")
    assert badges.deliver_one(factory, device, "counter", sender=failed)
    with factory() as db:
        row = store.get(db, device)
        assert row.badge_retry_at and row.retry_at is None
    sent = []
    assert push.deliver_one(factory, device, "visible", sender=lambda _s, value: sent.append(json.loads(value)))
    assert sent[0]["type"] == "camera_event"


def test_registration_rotation_wins_over_an_in_flight_counter_result(fixture):
    _, factory, *_ = fixture
    device = opt_in(fixture)
    def rotated(*_):
        opt_in(fixture, revision=2, badge=False)
    assert badges.deliver_one(factory, device, "counter", sender=rotated)
    with factory() as db:
        row = store.get(db, device)
        assert row.revision == 2 and not row.badge_enabled and not row.enabled
        assert row.subscription_encrypted is None and row.badge_revision == -1


def test_existing_client_same_revision_remains_idempotent_and_receives_no_counter(fixture):
    client, factory, device_id, *_ = fixture
    device = register(fixture)
    assert register(fixture) == device
    assert not badges.deliver_one(factory, device, "counter", sender=lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert client.get(f"/mobile/push/devices/{device_id}").json()["badge_enabled"] is False


def test_expired_counter_only_route_stops(fixture):
    _, factory, *_ = fixture
    device = opt_in(fixture)
    with factory() as db:
        row = store.get(db, device)
        row.registered_at = events.now_utc() - timedelta(days=91)
        row.save(); db.commit()
    assert badges.deliver_one(factory, device, "counter", sender=lambda *_: (_ for _ in ()).throw(AssertionError()))
    with factory() as db:
        assert not store.get(db, device).badge_enabled


def test_real_ingestion_and_muted_new_events_preserve_the_old_unread_backlog(fixture):
    _, factory, *_ = fixture
    current(factory); current(factory, 2)
    event(factory)
    with factory() as db:
        events.save_preferences(db, 2, {"notifications": False})
    signal = SimpleNamespace(kind="intrusion", topic="RuleEngine/FieldDetector/ObjectsInside",
        source="panorama", key="object1", active=True, occurred_at=events.now_utc(), camera_at=None,
        initialized=False, clock_uncertain=False, object_kind="person", object_kinds=("person",), rule_kind="intrusion")
    group = {"device_id": "owned-fixture", "cameras": [{"id": 1, "name": "Gate"}]}
    with factory() as db:
        identifier = events.record_signal(db, group, group["cameras"][0], signal)
        assert identifier
    assert current(factory)["count"] == 2
    assert current(factory, 2)["count"] == 1


def test_snapshot_selection_deletion_and_retention_refresh_both_accounts(fixture):
    from app.services import camera_event_selections as selections
    _, factory, *_ = fixture
    one, two = event(factory), event(factory)
    before, other = current(factory), current(factory, 2)
    with factory() as db:
        latest = events.event_query(db).order_by(events.AuditEvent.created_at.desc(), events.AuditEvent.id.desc()).first()
        selection = selections.create(db, 1, through=(latest.created_at, latest.id), filters={})
        assert selections.apply(db, 1, selection["id"], delete=True) == 2
    assert current(factory)["count"] == current(factory, 2)["count"] == 0
    assert current(factory)["revision"] > before["revision"]
    assert current(factory, 2)["revision"] > other["revision"]
    old = event(factory)
    with factory() as db:
        row = events.event_query(db).filter(events.AuditEvent.id == old).one()
        row.created_at -= timedelta(days=31)
        db.commit()
        assert events._cleanup_batch(db) == 1
    assert current(factory)["count"] == 0
