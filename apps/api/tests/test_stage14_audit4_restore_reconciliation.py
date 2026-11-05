from concurrent.futures import ThreadPoolExecutor
import threading
import json

import pytest

from app.services import current_db_restore as restore
from app.services.maintenance_admission import write_bounded_json_atomic
from test_current_db_restore_stage1378 import (
    _actor, _evidence, _bind_control_roots, ARTIFACT_A, ARTIFACT_B, SUBMISSION,
)


def setup(tmp_path, monkeypatch):
    _bind_control_roots(tmp_path, monkeypatch)
    monkeypatch.setattr(restore, 'current_restore_preflight', lambda *a, **kw: {'can_restore': True})
    monkeypatch.setattr(restore, '_artifact_evidence', lambda artifact_id, **kw: _evidence(artifact_id))
    monkeypatch.setattr(restore, 'assert_no_maintenance_conflicts', lambda *a, **kw: {})


def admit():
    return restore.request_current_restore(object(), artifact_id=ARTIFACT_A, submission_id=SUBMISSION,
        confirm=True, confirmation_phrase=restore.RESTORE_CONFIRMATION_PHRASE,
        actor=_actor(), before_admit=lambda candidate: True)


def reconcile(**overrides):
    return restore.reconcile_current_restore(**{'submission_id': SUBMISSION,
        'artifact_id': ARTIFACT_A, 'actor': _actor(), **overrides})


def test_missing_submission_is_retired_and_cannot_arrive_later(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    assert restore.read_current_restore_status(actor=_actor())['status'] == 'idle'
    assert reconcile()['admission'] == 'not_admitted'
    assert reconcile()['admission'] == 'not_admitted'
    assert not restore.restore_request_path().exists()
    with pytest.raises(restore.CurrentRestoreBlocked, match='restore_submission_not_admitted'):
        admit()
    assert not restore.restore_request_path().exists()


def test_retirement_serializes_with_slow_preflight_and_prevents_late_admission(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    started, released = threading.Event(), threading.Event()
    def slow_preflight(*a, **kw):
        started.set()
        assert released.wait(5)
        return {'can_restore': True}
    monkeypatch.setattr(restore, 'current_restore_preflight', slow_preflight)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(admit)
        try:
            assert started.wait(5)
            assert reconcile()['admission'] == 'not_admitted'
        finally:
            released.set()
        with pytest.raises(restore.CurrentRestoreBlocked, match='restore_submission_not_admitted'):
            pending.result(timeout=5)
    assert not restore.restore_request_path().exists()


def test_durable_request_without_public_projection_is_still_admitted(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    receipt = admit()
    restore.restore_public_status_path().unlink()
    result = reconcile()
    assert result['admission'] == 'admitted'
    assert result['restore_status']['operation_id'] == receipt['operation_id']
    assert result['restore_status']['status'] == 'queued'
    assert 'actor_subject' not in result['restore_status']
    assert 'requested_by' not in result
    assert admit()['operation_id'] == receipt['operation_id']


def test_durable_request_without_secondary_receipt_is_still_admitted(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    receipt = admit()
    restore.restore_receipt_path(SUBMISSION).unlink()
    restore.restore_public_status_path().unlink()
    result = reconcile()
    assert result['admission'] == 'admitted'
    assert result['restore_status']['operation_id'] == receipt['operation_id']


@pytest.mark.parametrize('admitted', [False, True])
def test_reconciliation_cannot_rebind_another_actor_or_artifact(tmp_path, monkeypatch, admitted):
    setup(tmp_path, monkeypatch)
    if admitted:
        admit()
    else:
        reconcile()
    with pytest.raises(restore.CurrentRestoreBlocked, match='submission_binding_conflict'):
        reconcile(actor=_actor(user_id=2, username='other'))
    with pytest.raises(restore.CurrentRestoreBlocked, match='submission_binding_conflict'):
        reconcile(artifact_id=ARTIFACT_B)


def test_invalid_authority_does_not_retire_unknown_request(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    restore.restore_request_path().write_text('{invalid')
    with pytest.raises(restore.CurrentRestoreBlocked, match='restore_status_unavailable'):
        reconcile()
    assert not restore.restore_receipt_path(SUBMISSION).exists()


def test_matching_public_progress_is_used_instead_of_restarting_operation(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    receipt = admit()
    progress = json.loads(restore.restore_public_status_path().read_text())
    progress.update(status='running', phase='pre_restore_backup', started_at=restore.utc_iso())
    write_bounded_json_atomic(restore.restore_public_status_path(), progress)
    result = reconcile()
    assert result['restore_status']['phase'] == 'pre_restore_backup'
    assert result['restore_status']['operation_id'] == receipt['operation_id']
