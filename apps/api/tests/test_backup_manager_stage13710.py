import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.db.session import Base
from app.models.user import User
from app.routers import maintenance as maintenance_router
from app.services.backup_before_upgrade import BackupExecutionConfig, create_backup_before_upgrade
from app.services.restore_maintenance import RestoreMaintenanceBlocked, delete_backup_artifact, inspect_restore_maintenance, list_restore_artifacts
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from test_schema_migration_runner_stage3 import seed_state


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage13710_backup_manager.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    db.add(User(username="stage13710_owner", full_name="Stage 13.7 Owner", password_hash="hash", role="owner", is_active=True))
    db.commit()
    return engine, db


def _backup(tmp_path):
    engine, db = _db(tmp_path)
    try:
        backup = create_backup_before_upgrade(
            db,
            config=BackupExecutionConfig(backup_root=tmp_path / "safe-db-backups", allow_tmp_for_tests=True, source="stage13710_test"),
        )
        return engine, db, backup
    except Exception:
        db.close()
        engine.dispose()
        raise


def test_backup_artifact_delete_requires_confirm_and_keeps_files(tmp_path):
    engine, db, backup = _backup(tmp_path)
    root = tmp_path / "safe-db-backups"

    with pytest.raises(RestoreMaintenanceBlocked) as blocked:
        delete_backup_artifact(artifact_id=backup["backup_id"], confirm=False, backup_root=str(root))

    assert blocked.value.status == "confirmation_required"
    assert Path(backup["manifest_path"]).exists()
    assert Path(backup["backup_file_path"]).exists()
    assert Path(backup["metadata_path"]).exists()
    db.close()
    engine.dispose()


def test_backup_artifact_delete_removes_only_product_owned_files(tmp_path):
    engine, db, backup = _backup(tmp_path)
    root = tmp_path / "safe-db-backups"
    unrelated = root / "operator-note.txt"
    unrelated.write_text("keep", encoding="utf-8")

    result = delete_backup_artifact(artifact_id=backup["backup_id"], confirm=True, backup_root=str(root))

    assert result["status"] == "deleted"
    assert result["deleted"] is True
    assert result["artifact_id"] == backup["backup_id"]
    assert result["deleted_count"] == 3
    assert result["video_archive_files_deleted"] is False
    assert not Path(backup["manifest_path"]).exists()
    assert not Path(backup["backup_file_path"]).exists()
    assert not Path(backup["metadata_path"]).exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert "raw_path" not in json.dumps(result, ensure_ascii=False).lower()
    db.close()
    engine.dispose()


def test_backup_artifact_delete_rejects_path_like_and_tampered_labels(tmp_path):
    engine, db, backup = _backup(tmp_path)
    root = tmp_path / "safe-db-backups"

    with pytest.raises(RestoreMaintenanceBlocked):
        delete_backup_artifact(artifact_id=f"../{backup['backup_id']}", confirm=True, backup_root=str(root))

    manifest_path = Path(backup["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata_file_label"] = "configured_backup_root/other.metadata.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RestoreMaintenanceBlocked) as blocked:
        delete_backup_artifact(artifact_id=backup["backup_id"], confirm=True, backup_root=str(root))

    assert blocked.value.status == "artifact_unsafe"
    assert Path(backup["manifest_path"]).exists()
    assert Path(backup["backup_file_path"]).exists()
    assert Path(backup["metadata_path"]).exists()
    db.close()
    engine.dispose()


def test_backup_artifact_delete_blocks_component_symlink_to_another_file_inside_root(tmp_path):
    engine, db, backup = _backup(tmp_path)
    root = tmp_path / "safe-db-backups"
    manifest_path = Path(backup["manifest_path"])
    metadata_path = Path(backup["metadata_path"])
    dump_path = Path(backup["backup_file_path"])
    foreign_target = root / "other-backup.dump"
    foreign_target.write_bytes(b"foreign-backup")
    dump_path.unlink()
    dump_path.symlink_to(foreign_target.name)

    with pytest.raises(RestoreMaintenanceBlocked) as blocked:
        delete_backup_artifact(
            artifact_id=backup["backup_id"],
            confirm=True,
            backup_root=str(root),
        )

    assert blocked.value.status == "artifact_unsafe"
    assert dump_path.is_symlink()
    assert foreign_target.read_bytes() == b"foreign-backup"
    assert manifest_path.exists()
    assert metadata_path.exists()
    db.close()
    engine.dispose()


def test_backup_artifact_delete_blocks_component_symlink_outside_root(tmp_path):
    engine, db, backup = _backup(tmp_path)
    root = tmp_path / "safe-db-backups"
    manifest_path = Path(backup["manifest_path"])
    dump_path = Path(backup["backup_file_path"])
    external_target = tmp_path / "external-backup.dump"
    external_target.write_bytes(b"external-backup")
    dump_path.unlink()
    dump_path.symlink_to(external_target)

    with pytest.raises(RestoreMaintenanceBlocked) as blocked:
        delete_backup_artifact(
            artifact_id=backup["backup_id"],
            confirm=True,
            backup_root=str(root),
        )

    assert blocked.value.status == "artifact_unsafe"
    assert dump_path.is_symlink()
    assert external_target.read_bytes() == b"external-backup"
    assert manifest_path.exists()
    db.close()
    engine.dispose()


def test_backup_artifact_delete_blocks_configured_root_symlink(tmp_path):
    engine, db, backup = _backup(tmp_path)
    root = tmp_path / "safe-db-backups"
    root_alias = tmp_path / "backup-root-link"
    root_alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(RestoreMaintenanceBlocked) as blocked:
        delete_backup_artifact(
            artifact_id=backup["backup_id"],
            confirm=True,
            backup_root=str(root_alias),
        )

    assert blocked.value.status == "artifact_unsafe"
    assert root_alias.is_symlink()
    assert Path(backup["manifest_path"]).exists()
    assert Path(backup["backup_file_path"]).exists()
    assert Path(backup["metadata_path"]).exists()
    db.close()
    engine.dispose()


def test_backup_manager_status_is_sanitized_and_exposes_delete_capability(tmp_path):
    engine, db, backup = _backup(tmp_path)
    payload = inspect_restore_maintenance(backup_root=str(tmp_path / "safe-db-backups"), db_backend="sqlite")
    artifact = payload["artifacts"][0]

    assert artifact["artifact_id"] == backup["backup_id"]
    assert artifact["integrity_status"] == "not_checked"
    assert artifact["compatibility_status"] == "compatible"
    assert artifact["delete_status"] == "allowed"
    assert artifact["delete_supported"] is True
    assert "checksum_sha256" not in artifact
    assert "backup_file_label" not in artifact
    rendered = json.dumps(payload, ensure_ascii=False).lower()
    assert "sqlite:///" not in rendered
    assert "safe-db-backups" not in rendered
    assert "raw_path" not in rendered
    db.close()
    engine.dispose()


def test_backup_artifact_listing_returns_newest_before_limit(tmp_path):
    root = tmp_path / "safe-db-backups"
    root.mkdir()
    base = datetime(2026, 7, 5, 10, 0, 0)
    expected_ids = []

    for index in range(25):
        created_at = base + timedelta(minutes=index)
        backup_id = f"kmvms-db-{created_at.strftime('%Y%m%dT%H%M%S')}Z-{index:012x}"
        manifest = {
            "backup_id": backup_id,
            "created_at": created_at.isoformat() + "Z",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "db_backend": "sqlite",
            "backup_file_label": f"{backup_id}.sqlite3",
            "metadata_file_label": f"{backup_id}.metadata.json",
            "file_size": 1,
            "restore_validation_status": "verified",
        }
        (root / f"{backup_id}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        if index >= 5:
            expected_ids.append(backup_id)

    result = list_restore_artifacts(backup_root=str(root), limit=20)

    assert [item["artifact_id"] for item in result] == list(reversed(expected_ids))
    assert len(result) == 20
    assert all(item["integrity_status"] == "not_checked" for item in result)
    assert all(item["availability_status"] == "missing" for item in result)


def test_backup_delete_endpoint_is_registered_for_manage_settings():
    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("POST", "/system/restore/artifacts/{artifact_id}/delete", "manage_settings") in rows


def test_backup_validation_cleanup_retry_is_audited_as_pending(monkeypatch):
    events = []
    result = {
        "operation_id": "backup-validation-operation-123",
        "submission_id": "11111111-1111-4111-8111-111111111111",
        "state": "running",
        "phase": "cleanup_retry",
        "reason_code": "temporary_validation_cleanup_failed",
        "replayed": False,
    }
    monkeypatch.setattr(
        maintenance_router,
        "run_backup_validation_operation",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        maintenance_router,
        "create_event",
        lambda **kwargs: events.append(kwargs),
    )

    response = maintenance_router.restore_apply(
        maintenance_router.RestoreApplyRequest(
            confirm=True,
            artifact_id="kmvms-db-20260729T010203Z-abcdef123456",
            submission_id="11111111-1111-4111-8111-111111111111",
        ),
        SimpleNamespace(
            headers={"user-agent": "stage137101-test"},
            client=SimpleNamespace(host="127.0.0.1"),
        ),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=1, username="owner"),
    )

    assert response == result
    assert len(events) == 1
    audit = events[0]
    assert audit["event_type"] == "system.restore_apply_cleanup_pending"
    assert audit["severity"] == "info"
    assert audit["message_ru"] == "Результат проверки резервной копии записан; очистка временной базы ожидается."
    assert audit["message_en"] == "Backup validation result was recorded; temporary database cleanup is pending."
    assert audit["metadata"]["state"] == "running"
    assert audit["metadata"]["phase"] == "cleanup_retry"
    assert audit["metadata"]["reason_code"] == "temporary_validation_cleanup_failed"
    assert audit["event_type"] not in {
        "system.restore_apply_completed",
        "system.restore_apply_failed",
    }
