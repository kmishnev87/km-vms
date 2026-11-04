from contextlib import nullcontext
from datetime import datetime

import pytest
from app.services import update_apply as updates


@pytest.mark.parametrize('status', ['completed', 'failed', 'failed_rolled_back', 'cancelled', 'blocked'])
def test_finished_duration_is_stable_after_restart(monkeypatch, status):
    monkeypatch.setattr(updates, '_utcnow', lambda: datetime(2027, 1, 1))
    payload = dict(status=status, started_at='2026-11-04T10:31:17.516212Z',
                   finished_at='2026-11-04T10:32:27.183111Z', elapsed_seconds=None)
    monkeypatch.setattr(updates, '_admission_guard', nullcontext)
    monkeypatch.setattr(updates, '_read_update_apply_status_unlocked', lambda: dict(payload))
    assert updates.read_update_apply_status()['elapsed_seconds'] == 69


def test_history_gets_its_own_saved_interval():
    payload = dict(status='idle', last_apply_summary=dict(status='completed',
                   started_at='2026-11-04T10:00:00Z', finished_at='2026-11-04T10:02:15Z'))
    updates._attach_elapsed_seconds(payload)
    assert payload['elapsed_seconds'] is None
    assert payload['last_apply_summary']['elapsed_seconds'] == 135


def test_missing_timestamps_are_not_zero():
    payload = dict(status='completed', elapsed_seconds=0)
    updates._attach_elapsed_seconds(payload)
    assert payload['elapsed_seconds'] is None
