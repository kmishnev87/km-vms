import contextlib
from unittest.mock import Mock

import pytest
from app.services import archive_migration as migration


@pytest.mark.parametrize("failure", ["acquire", "work", "release"])
def test_worker_survives_database_failures_and_returns_to_work(monkeypatch, failure):
    class Stop:
        stopped = False
        def wait(self, delay):
            delays.append(delay)
            return self.stopped
    stop, delays = Stop(), []
    monkeypatch.setattr(migration, "_worker_stop", stop)
    monkeypatch.setattr(migration, "SessionLocal", lambda: contextlib.nullcontext(Mock()))
    acquired, worked, released = [], [], []
    def acquire(*args, **kwargs):
        acquired.append(1)
        if failure == "acquire" and len(acquired) == 1:
            raise ConnectionError("test outage")
        return object()
    def work():
        worked.append(1)
        if failure == "work" and len(worked) == 1:
            raise ConnectionError("test outage")
        if failure != "release" or len(worked) > 1:
            stop.stopped = True
        return True
    def release(*args):
        released.append(1)
        if failure == "release" and len(released) == 1:
            raise ConnectionError("test outage")
    monkeypatch.setattr(migration, "acquire_worker_lease", acquire)
    monkeypatch.setattr(migration, "release_worker_lease", release)
    monkeypatch.setattr(migration, "_WorkerLeaseHeartbeat", lambda handle: Mock())
    monkeypatch.setattr(migration, "_recover_terminal_audit_once", work)
    for name in ["_expire_one_ready_plan", "_prepare_one_plan", "_run_one_operation"]:
        monkeypatch.setattr(migration, name, lambda *args: False)
    migration._worker_loop()
    assert len(acquired) == 2
    assert worked
    assert max(delays) <= 30
    if failure != "release":
        assert max(delays) >= max(migration.WORKER_POLL_SECONDS, 2)
