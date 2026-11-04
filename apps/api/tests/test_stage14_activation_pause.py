from copy import deepcopy

import pytest

from app.models.storage_operation import StorageOperation
from app.services import archive_root_activation as activation
from test_storage_recording_contract import db, add_camera


def setup_pause(db):
    camera = add_camera(db)
    camera.enabled = True
    operation = StorageOperation(
        id="archive-root-activation-stage14", operation_type="archive_root_activation",
        actor_kind="system", actor_key="system:stage14", system_owner="stage14",
        idempotency_key="stage14", request_fingerprint="a" * 64,
        status="running", scope={}, progress={},
    )
    db.add(operation)
    db.commit()
    return camera, {
        "operation_id": operation.id,
        "camera_snapshots": [{"camera_id": camera.id, "enabled_intent": True, "changed_by_operation": False}],
        "paused_camera_ids": [],
    }


def test_lost_json_projection_recovers_committed_pause_and_restores(db, monkeypatch):
    monkeypatch.setattr(activation, "create_event", lambda **kwargs: None)
    camera, state = setup_pause(db)
    stale_json = deepcopy(state)
    ids, snapshots = activation._pause_operation_cameras(db, state)
    assert ids == [camera.id] and not camera.enabled
    # The process dies here: the original JSON still contains no pause proof.
    db.expire_all()
    ids, snapshots = activation._pause_operation_cameras(db, stale_json)
    assert ids == [camera.id] and snapshots[0]["changed_by_operation"]
    result = activation._restore_operation_cameras(db, stale_json, reason="stage14")
    db.refresh(camera)
    assert result["restored"] == [camera.id] and camera.enabled
    assert activation._restore_operation_cameras(db, stale_json, reason="replay")["restored"] == [camera.id]


def test_user_disable_is_not_undone_by_activation_recovery(db, monkeypatch):
    monkeypatch.setattr(activation, "create_event", lambda **kwargs: None)
    camera, state = setup_pause(db)
    activation._pause_operation_cameras(db, state)
    activation.preserve_manual_camera_disable(db, camera.id)
    camera.enabled = False
    db.commit()
    assert activation._restore_operation_cameras(db, state, reason="stage14")["restored"] == []
    db.refresh(camera)
    assert camera.enabled is False


def test_pause_and_proof_roll_back_together_before_commit(db, monkeypatch):
    camera, state = setup_pause(db)
    commit = db.commit
    def failure():
        db.rollback()
        raise ConnectionError("before commit")
    monkeypatch.setattr(db, "commit", failure)
    with pytest.raises(ConnectionError):
        activation._pause_operation_cameras(db, state)
    monkeypatch.setattr(db, "commit", commit)
    db.refresh(camera)
    assert camera.enabled is True
    operation = db.get(StorageOperation, state["operation_id"])
    assert not operation.progress.get("activation_paused_camera_ids")
