"""Regressions for receiver pacing, lease deadlines and the retry budget."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import camera_events_worker as worker, onvif_events as ov
from app.services.camera_event_health import RETRY_DELAYS, Recovery, pull_delay


@pytest.mark.parametrize("elapsed,expected", [(0, 30), (.5, 29.5), (1, 29), (1.1, 28.9), (29, 1), (60, 0)])
def test_all_early_empty_responses_are_spaced_by_30_seconds(elapsed, expected):
    assert pull_delay(elapsed, False) == pytest.approx(expected)
    assert pull_delay(elapsed, True) == max(0, 1 - elapsed)


def test_short_subscription_limits_soap_and_transport_and_does_not_accumulate_plugins(monkeypatch):
    now = [10.]
    monkeypatch.setattr(ov.time, "monotonic", lambda: now[0])
    client = object.__new__(ov.DeviceClient)
    client.subscription = "http://192.0.2.1/events"
    client.references = []
    timing = []
    plugins = []
    captured = []
    wall = datetime(2026, 11, 18, tzinfo=timezone.utc)
    short = {"CurrentTime": wall, "TerminationTime": wall + timedelta(seconds=30)}
    client._set_lifetime(short, now[0])

    @contextmanager
    def settings(*, timeout):
        timing.append(timeout)
        yield

    def pull(**kwargs):
        assert len(plugins) == 1
        captured.append(kwargs["Timeout"].total_seconds())
        return {"NotificationMessage": []}

    renew = Mock(return_value=short)
    proxy = SimpleNamespace(_client=SimpleNamespace(plugins=plugins), PullMessages=pull, Renew=renew)
    client.service = lambda *a, **k: proxy
    client.transport = SimpleNamespace(settings=settings)
    assert client.pull() == []
    assert 0 < captured[-1] < 30 and sum(timing[-1]) < 30
    assert not plugins
    now[0] += 20
    assert client.pull() == []
    renew.assert_called_once()
    assert client.expires_at == 60
    assert 0 < captured[-1] < 30 and not plugins

    normal = {"CurrentTime": wall, "TerminationTime": wall + timedelta(minutes=5)}
    client._set_lifetime(normal, now[0])
    client.pull()
    assert captured[-1] == 60 and not plugins
    proxy.PullMessages = Mock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        client.pull()
    assert not plugins


def test_quiet_receiver_renews_short_lease_during_the_30_second_spacing(monkeypatch):
    clock = [1.1]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    receiver = worker.Receiver({})
    receiver.stop = SimpleNamespace(is_set=lambda: False, wait=lambda delay: clock.__setitem__(0, clock[0] + delay))
    client = SimpleNamespace(renew_at=19.5)
    client.renew_if_due = Mock(side_effect=lambda: setattr(client, "renew_at", clock[0] + 19.5))
    receiver.client = client
    receiver._wait_between_pulls(0, False)
    client.renew_if_due.assert_called_once()
    assert clock[0] == pytest.approx(30)


def test_successful_subscribe_and_failing_pull_exhaust_once_on_one_receiver(monkeypatch):
    group = {"host": "192.0.2.1", "port": 80, "username": "fixture", "password": "fixture",
             "onvif_path": None, "device_id": "fixture", "cameras": []}
    receiver = worker.Receiver(group)
    subscribe = Mock()
    pull = Mock(side_effect=TimeoutError)
    close = Mock()
    client = SimpleNamespace(topics=lambda: [], profiles=lambda: [], subscribe=subscribe, pull=pull, close=close)
    monkeypatch.setattr(worker, "DeviceClient", lambda *a: client)
    monkeypatch.setattr(worker, "renew_worker_lease", lambda *a, **k: None)
    monkeypatch.setattr(worker, "SessionLocal", lambda: nullcontext(None))
    snapshots = []
    monkeypatch.setattr(receiver, "_publish", lambda lease, phase, **kw: snapshots.append(
        (phase, receiver.recovery.incident_id, kw.get("transitions", ()))))
    waits = []

    def wait(lease, delay):
        waits.append(delay)
        if delay is None:
            receiver.cancel()

    monkeypatch.setattr(receiver, "_wait_to_retry", wait)
    receiver._serve(SimpleNamespace())
    assert waits == [*RETRY_DELAYS, None]
    assert subscribe.call_count == pull.call_count == close.call_count == len(RETRY_DELAYS) + 1
    assert len({identity for _, identity, _ in snapshots if identity}) == 1
    assert [x for _, _, transitions in snapshots for x in transitions] == ["failed", "exhausted"]


def test_recovery_stability_and_coalesced_manual_retry(monkeypatch):
    at = datetime(2026, 11, 18)
    recovery = Recovery()
    assert recovery.failed("test_disconnect", at) == (30, ["failed"])
    assert recovery.received(60) is None
    assert recovery.received(179) is None
    assert recovery.failed("test_repeat", at) == (120, [])
    assert recovery.received(300) is None
    assert recovery.received(420)["failed_attempts"] == 2
    assert recovery.details() is None

    receiver = worker.Receiver({})
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.)
    assert receiver.request_retry() == "receiver_not_exhausted"
    receiver.manual_ready_at = 110
    assert receiver.request_retry() == "retry_cooldown"
    receiver.manual_ready_at = 100
    assert receiver.request_retry() == receiver.request_retry() == "queued"
    assert receiver.retry_requested.is_set()
