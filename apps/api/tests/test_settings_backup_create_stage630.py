from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import settings
from app.routers.settings import BackupCreateRequest, system_backup_create
from app.services.backup_before_upgrade import BackupSafetyBlocked


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def fake_request():
    return SimpleNamespace(headers=FakeHeaders({"user-agent": "stage630-test"}), client=SimpleNamespace(host="127.0.0.1"))


def fake_user():
    return SimpleNamespace(id=1, username="admin")


def test_stage630_backup_create_requires_explicit_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "create_event", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(settings, "run_backup_create_operation", lambda *args, **kwargs: pytest.fail("backup must not run"))

    with pytest.raises(HTTPException) as exc:
        system_backup_create(
            BackupCreateRequest(confirm=False, submission_id="11111111-1111-4111-8111-111111111111"),
            fake_request(),
            db=SimpleNamespace(),
            current_user=fake_user(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason"] == "confirmation_required"
    assert calls[-1]["event_type"] == "system.backup_create_blocked"
    assert calls[-1]["metadata"] == {"status": "blocked", "reason": "confirmation_required", "source": "manual_admin"}


def test_stage630_backup_create_success_audits_sanitized_result(monkeypatch):
    events = []
    monkeypatch.setattr(settings, "create_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(settings, "build_migration_plan", lambda db: {"status": "current"})
    monkeypatch.setattr(
        settings,
        "run_backup_create_operation",
        lambda *args, **kwargs: {
            "operation_id": "backup-operation-123",
            "submission_id": "11111111-1111-4111-8111-111111111111",
            "kind": "create",
            "artifact_id": "kmvms-db-20260729T010203Z-abcdef123456",
            "state": "completed",
            "phase": "completed",
            "replayed": False,
            "result": {
                "status": "verified",
                "file_size": 4096,
                "integrity_status": "verified",
                "restore_validation_status": "not_performed",
            },
        },
    )

    result = system_backup_create(
        BackupCreateRequest(confirm=True, submission_id="11111111-1111-4111-8111-111111111111"),
        fake_request(),
        db=SimpleNamespace(),
        current_user=fake_user(),
    )

    assert result["state"] == "completed"
    assert result["artifact_id"] == "kmvms-db-20260729T010203Z-abcdef123456"
    audit = events[-1]
    assert audit["event_type"] == "system.backup_create_completed"
    assert audit["target_id"] == "kmvms-db-20260729T010203Z-abcdef123456"
    assert audit["metadata"] == {
        "operation_id": "backup-operation-123",
        "state": "completed",
        "phase": "completed",
        "replayed": False,
        "source": "manual_admin",
        "video_archive_files_included": False,
    }


def test_stage630_backup_create_safety_block_is_audited(monkeypatch):
    events = []
    monkeypatch.setattr(settings, "create_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(settings, "build_migration_plan", lambda db: {"status": "current"})

    def block_backup(*args, **kwargs):
        raise BackupSafetyBlocked("blocked", {"reason": "unsafe_root", "summary": "Unsafe backup root /secret/path"})

    monkeypatch.setattr(settings, "run_backup_create_operation", block_backup)

    with pytest.raises(HTTPException) as exc:
        system_backup_create(
            BackupCreateRequest(confirm=True, submission_id="11111111-1111-4111-8111-111111111111"),
            fake_request(),
            db=SimpleNamespace(),
            current_user=fake_user(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason"] == "unsafe_root"
    audit = events[-1]
    assert audit["event_type"] == "system.backup_create_blocked"
    assert audit["metadata"]["status"] == "blocked"
    assert audit["metadata"]["reason"] == "unsafe_root"
