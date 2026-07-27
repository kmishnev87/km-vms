import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.version import APP_VERSION
from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.permissions import ROLE_OPERATOR, ROLE_OWNER
from app.core.security import create_access_token, hash_password
from app.db.session import Base, get_db
from app.main import app
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from app.services.update_check import reset_update_check_cache_for_tests
from app.services.update_maintenance import (
    UpdateMaintenanceBlocked,
    apply_update_maintenance,
    assert_update_report_secret_safe,
    dry_run_update_maintenance,
    inspect_update_maintenance,
)
from app.services.upgrade_report import build_upgrade_report
from test_schema_migration_runner_stage3 import seed_state


UPDATE_RELEASE_ID = "cccccccccccccccccccccccccccccccccccccccc"


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


def sqlite_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage13_update.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    settings = SystemSettings(id=1, system_initialized=True, system_name="Stage 13 Update", created_at=datetime(2026, 5, 12, 3, 0, 0), storage_path="/storage/archive")
    owner = User(username="stage13_update_owner", full_name="Stage 13 Update Owner", password_hash=hash_password("stage13-test-password"), role=ROLE_OWNER, is_active=True)
    camera = Camera(
        id=13001,
        name="Stage 13 Update Camera",
        storage_folder_name="stage13_update_camera",
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
    )
    root = ArchiveRoot(
        id="stage13_update_root",
        label="primary",
        root_path="/storage/archive",
        storage_namespace="kmvms/recordings",
        is_active=True,
    )
    job = RecordingJob(id="stage13_update_job", camera_id=camera.id, state="stopped", started_at=datetime(2026, 5, 12, 3, 1, 0))
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id=job.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path="kmvms/recordings/stage13/segment.mkv",
        relative_path="kmvms/recordings/stage13/segment.mkv",
        started_at=datetime(2026, 5, 12, 3, 1, 0),
        ended_at=datetime(2026, 5, 12, 3, 2, 0),
        duration_sec=60,
        size_bytes=1024,
        status="ready",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=root.id,
        storage_namespace=root.storage_namespace,
        container_format="mkv",
        file_extension=".mkv",
    )
    db.add_all([settings, owner, camera, root, job, segment])
    db.commit()
    return engine, db


def add_user(db, *, role=ROLE_OWNER, username=None):
    user = User(username=username or f"stage13_update_{role}", full_name=f"stage13 update {role}", password_hash="hash", role=role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.username)}"}


def manifest(path: Path, **overrides):
    payload = {
        "schema_version": 1,
        "channel": "stable",
        "version": "9.9.9",
        "git_ref": "main",
        "commit": UPDATE_RELEASE_ID,
        "published_at": "2026-06-18T00:00:00Z",
        "title": "Stage 13 Update Apply Contract Regression",
        "summary": "Safe update apply contract test.",
        "release_notes_url": None,
        "breaking_changes": [],
        "requires_backup": False,
        "requires_manual_action": False,
        "requires_migration": False,
        "minimum_current_version": None,
        "artifacts": {"source": {"type": "github_tarball", "repo": "kmishnev87/km-vms", "ref": "main"}},
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    reset_update_check_cache_for_tests()
    for name in ["KMVMS_UPDATE_MANIFEST_PATH", "KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", "KMVMS_PUBLIC_RELEASE_PROVIDER", "KMVMS_UPDATE_CHANNEL_ID", "KMVMS_BUILD_METADATA_FILE", "KMVMS_BUILD_ID", "KMVMS_INSTALL_SOURCE", "KMVMS_SOURCE_CHANNEL_ID", "KMVMS_APP_ROOT"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_PROVIDER", "0")
    yield
    reset_update_check_cache_for_tests()


@pytest.fixture
def client_db(tmp_path):
    engine, db = sqlite_session(tmp_path)
    owner = db.query(User).filter(User.role == ROLE_OWNER).first()
    operator = add_user(db, role=ROLE_OPERATOR, username="stage13_update_operator")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), db, owner, operator
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_update_dry_run_without_source_is_read_only_and_honest(tmp_path):
    engine, db = sqlite_session(tmp_path)

    payload = dry_run_update_maintenance(db)

    assert payload["status"] == "blocked"
    assert payload["reason"] == "trusted_update_package_not_configured"
    assert payload["can_apply"] is False
    assert payload["dry_run"] is True
    assert payload["mutates_database"] is False
    assert payload["creates_backup"] is False
    assert payload["artifact_downloaded"] is False
    assert payload["containers_restarted"] is False
    assert db.query(User).count() >= 1
    engine.dispose()


def test_update_candidate_preflight_is_sanitized_and_apply_blocked(tmp_path, monkeypatch):
    engine, db = sqlite_session(tmp_path)
    release = manifest(tmp_path / "release.json")
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(release))
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", "1")
    monkeypatch.setenv("KMVMS_UPDATE_CHANNEL_ID", "stable")
    monkeypatch.setenv("KMVMS_BUILD_ID", "stage13-current-build")
    installed_root = tmp_path / "installed"
    installed_root.mkdir()
    (installed_root / ".km-vms-release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "KM VMS",
                "version": APP_VERSION,
                "title": "Hermetic installed test identity",
                "summary": "Hermetic installed test identity.",
                "release_channel": "public-github",
                "source_kind": "github-release",
                "source_repo": "kmishnev87/km-vms",
                "source_ref": f"v{APP_VERSION}",
                "commit_sha": "b" * 40,
                "metadata_status": "complete",
                "metadata_source": "pytest",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KMVMS_APP_ROOT", str(installed_root))

    payload = dry_run_update_maintenance(db)
    rendered = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "blocked"
    assert payload["reason"] == "update_apply_not_available_for_release"
    assert payload["release_id"] == UPDATE_RELEASE_ID
    assert payload["release_validated"] is True
    assert payload["report"]["compose_plan"]["manual_intervention_required"] is True
    assert payload["report"]["preservation"]["camera_credentials"]["raw_values_included"] is False
    assert payload["report"]["side_effects"]["update_applied"] is False
    assert_update_report_secret_safe(payload["report"])
    assert str(release) not in rendered
    assert "docker-compose" not in rendered
    assert "password" not in rendered.lower()

    with pytest.raises(UpdateMaintenanceBlocked) as exc:
        apply_update_maintenance(db, confirm=True, release_id=UPDATE_RELEASE_ID)
    assert exc.value.status == "update_apply_not_available_for_release"
    assert exc.value.diagnostics["can_apply"] is False
    engine.dispose()


def test_update_apply_confirmation_and_release_reference_are_checked(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    release = manifest(tmp_path / "release.json")
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(release))
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", "1")

    with pytest.raises(UpdateMaintenanceBlocked) as confirm:
        apply_update_maintenance(db, confirm=False, release_id=UPDATE_RELEASE_ID)
    assert confirm.value.status == "confirmation_required"

    with pytest.raises(UpdateMaintenanceBlocked) as mismatch:
        apply_update_maintenance(db, confirm=True, release_id="wrong-release")
    assert mismatch.value.status == "release_reference_mismatch"


def test_update_simulated_apply_requires_backup_before_any_apply_step(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    release = manifest(tmp_path / "release.json")
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(release))
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", "1")
    order = []

    def fake_backup(*_args, **_kwargs):
        order.append("backup")
        return {"status": "verified", "backup_id": "stage13-update-backup"}

    monkeypatch.setattr("app.services.update_maintenance.create_backup_before_upgrade", fake_backup)

    result = apply_update_maintenance(db, confirm=True, release_id=UPDATE_RELEASE_ID, allow_simulated_apply_for_tests=True)

    assert order == ["backup"]
    assert result["backup_status"] == "verified"
    assert result["update_apply_executed"] is False
    assert result["containers_restarted"] is False
    assert result["migration_auto_apply"] is False
    assert result["restore_auto_run"] is False


def test_update_apply_public_payload_contract_and_permissions(client_db):
    client, _db, owner, operator = client_db

    assert client.post("/system/update/dry-run", json={}).status_code == 404
    assert client.post("/system/update/dry-run", json={}, headers=auth_headers(operator)).status_code == 404
    assert client.post("/system/update/dry-run", json={}, headers=auth_headers(owner)).status_code == 404
    assert client.post("/system/update/apply", json={"confirm": True}, headers=auth_headers(operator)).status_code == 403
    assert client.post("/system/update/apply", json={"confirm": True}, headers=auth_headers(owner)).status_code == 422

    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("POST", "/system/update/dry-run", "manage_settings") not in rows
    assert ("POST", "/system/update/apply", "manage_settings") in rows


def test_update_maintenance_report_and_upgrade_report_are_read_only(tmp_path):
    _engine, db = sqlite_session(tmp_path)

    status = inspect_update_maintenance(db)
    report = build_upgrade_report(db)
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    update_source = (Path(__file__).resolve().parents[1] / "app" / "services" / "update_maintenance.py").read_text(encoding="utf-8")

    assert status["read_only"] is True
    assert report["update_maintenance"]["read_only"] is True
    assert report["update_maintenance"]["side_effects"]["update_applied"] is False
    assert "apply_update_maintenance" not in source
    assert "apply_migration_maintenance" not in update_source
    assert "apply_restore_maintenance" not in update_source
