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
    monkeypatch.setattr(settings, "create_backup_before_upgrade", lambda *args, **kwargs: pytest.fail("backup must not run"))

    with pytest.raises(HTTPException) as exc:
        system_backup_create(
            BackupCreateRequest(source="manual_admin"),
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
        "create_backup_before_upgrade",
        lambda *args, **kwargs: {
            "backup_id": "backup-123",
            "status": "verified",
            "db_backend": "sqlite",
            "source": "manual_admin",
            "backup_file_label": "km-vms-db-backup.sqlite",
            "metadata_file_label": "km-vms-db-backup.json",
            "file_size": 4096,
            "checksum_sha256": "a" * 64,
            "restore_validation_status": "verified",
        },
    )

    result = system_backup_create(
        BackupCreateRequest(source="manual_admin", confirm=True),
        fake_request(),
        db=SimpleNamespace(),
        current_user=fake_user(),
    )

    assert result["backup_id"] == "backup-123"
    assert result["video_archive_files_included"] is False
    audit = events[-1]
    assert audit["event_type"] == "system.backup_create_completed"
    assert audit["target_id"] == "backup-123"
    assert audit["metadata"] == {
        "status": "verified",
        "db_backend": "sqlite",
        "source": "manual_admin",
        "file_size": 4096,
        "restore_validation_status": "verified",
        "video_archive_files_included": False,
    }


def test_stage630_backup_create_safety_block_is_audited(monkeypatch):
    events = []
    monkeypatch.setattr(settings, "create_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(settings, "build_migration_plan", lambda db: {"status": "current"})

    def block_backup(*args, **kwargs):
        raise BackupSafetyBlocked("blocked", {"reason": "unsafe_root", "summary": "Unsafe backup root /secret/path"})

    monkeypatch.setattr(settings, "create_backup_before_upgrade", block_backup)

    with pytest.raises(HTTPException) as exc:
        system_backup_create(
            BackupCreateRequest(source="manual_admin", confirm=True),
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
