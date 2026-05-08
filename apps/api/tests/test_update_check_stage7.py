import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER, ROLE_VIEWER, user_has_permission
from app.core.version import installed_build_metadata
from app.db.session import Base
from app.models.schema_version import SchemaVersionState
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers import settings as settings_router
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION, CURRENT_STATE_ID
from app.services.update_check import (
    compare_versions,
    read_trusted_local_manifest,
    reset_update_check_cache_for_tests,
    run_startup_due_check,
    run_update_check,
)
from test_schema_migration_runner_stage3 import seed_state


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


def sqlite_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage7.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def _seed(db):
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    row = SystemSettings(id=1, system_initialized=True, system_name="Stage 7", created_at=datetime(2026, 5, 7, 3, 0, 0))
    db.merge(row)
    db.add(User(username="stage7_owner", full_name="Stage 7 Owner", password_hash="hash-value-not-exported", role="owner", is_active=True))
    db.commit()


def _manifest(path: Path, **overrides):
    payload = {
        "latest_version": "1.1.0",
        "release_id": "release-1.1.0",
        "build_id": "build-110",
        "release_date": "2026-05-08",
        "severity": "critical",
        "release_notes": "Schema and storage compatibility update.",
        "affected_domains": ["db", "migrations", "storage", "cameras", "settings", "metadata", "archive"],
        "required_schema_version": CURRENT_SCHEMA_VERSION,
        "migration_risk": "risky_requires_backup",
        "backup_required": True,
        "restore_validation_required": True,
        "recording_stop_required": True,
        "manual_steps": ["Review backup and maintenance window"],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _assert_invalid_manifest(result):
    rendered = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "invalid_manifest"
    assert result["classification"]["classification"] == "invalid_manifest"
    assert result["latest_release"] is None
    assert result["raw_manifest_exposed"] is False
    assert result["errors"][0]["error_category"] == "manifest_schema_invalid"
    assert "abc" not in rendered
    assert "not-a-number" not in rendered
    assert "password" not in rendered.lower()
    assert "rtsp://" not in rendered.lower()


@pytest.fixture(autouse=True)
def clean_update_env(monkeypatch):
    reset_update_check_cache_for_tests()
    for name in [
        "KMVMS_UPDATE_MANIFEST_PATH",
        "KMVMS_UPDATE_CHANNEL_ID",
        "KMVMS_BUILD_METADATA_FILE",
        "KMVMS_BUILD_ID",
        "KMVMS_GIT_COMMIT",
        "KMVMS_BUILD_TIME",
        "KMVMS_INSTALL_SOURCE",
        "KMVMS_SOURCE_CHANNEL_ID",
    ]:
        monkeypatch.delenv(name, raising=False)
    yield
    reset_update_check_cache_for_tests()


def test_installed_build_metadata_is_separate_from_schema_version(tmp_path, monkeypatch):
    build_file = tmp_path / "build-info.json"
    build_file.write_text(
        json.dumps(
            {
                "app_version": "1.2.3",
                "build_id": "build-123",
                "git_commit": "abcdef123456",
                "install_source": "release",
                "source_channel_id": "stable",
                "supported_schema_version": CURRENT_SCHEMA_VERSION,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KMVMS_BUILD_METADATA_FILE", str(build_file))

    installed = installed_build_metadata()

    assert installed["app_version"] == "1.2.3"
    assert installed["build_id"] == "build-123"
    assert installed["status"] == "installed_build_known"
    assert installed["supported_schema_version"] == CURRENT_SCHEMA_VERSION
    assert installed["app_version"] != str(CURRENT_SCHEMA_VERSION)


def test_development_fallback_is_not_final_release_identity(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)

    status = settings_router.system_update_status(db=db, current_user=object())

    assert status["installed_build"]["status"] == "development_build"
    assert status["installed_build"]["metadata_source"] == "development_fallback"
    assert status["installed_build"]["limitation"]
    assert status["source_channel"]["status"] == "source_channel_not_configured"


def test_source_not_configured_is_safe_and_public_status_excludes_update(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)

    result = run_update_check(db, manual=False)
    public_status = settings_router.system_status(db=db)

    assert result["status"] == "not_configured"
    assert result["source_channel"]["arbitrary_url_supported"] is False
    assert result["raw_manifest_exposed"] is False
    assert "update" not in public_status


def test_local_trusted_manifest_normalizes_without_echoing_raw_manifest(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    manifest = _manifest(tmp_path / "release.json")
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))
    monkeypatch.setenv("KMVMS_BUILD_ID", "build-100")

    result = run_update_check(db, manual=False)
    rendered = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "blocked"
    assert result["classification"]["classification"] == "risky"
    assert result["latest_release"]["latest_version"] == "1.1.0"
    assert result["latest_release"]["release_notes_summary"] == "Schema and storage compatibility update."
    assert result["source_channel"]["trusted_source_type"] == "local_static_manifest"
    assert result["raw_manifest_exposed"] is False
    assert "Schema and storage compatibility update." in rendered
    assert "password" not in rendered.lower()
    assert "rtsp://" not in rendered.lower()


def test_invalid_manifest_is_rejected_with_sanitized_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")

    with pytest.raises(Exception) as exc:
        read_trusted_local_manifest(bad)

    assert "not-json" not in str(exc.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("required_schema_version", "abc"),
        ("target_schema_version", "not-a-number"),
        ("schema_compatibility_min", "abc"),
        ("schema_compatibility_max", "not-a-number"),
        ("required_schema_version", True),
    ],
)
def test_malformed_manifest_schema_fields_return_invalid_manifest(tmp_path, monkeypatch, field, value):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    manifest = _manifest(tmp_path / "release.json", **{field: value})
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))

    result = run_update_check(db, manual=False)

    _assert_invalid_manifest(result)
    assert result["side_effects"]["update_applied"] is False
    assert result["side_effects"]["artifact_downloaded"] is False
    assert result["preflight"]["side_effects"]["migration_executed"] is False


@pytest.mark.parametrize(
    "compatibility",
    [
        ["schema_min"],
        {"schema_min": "abc"},
        {"schema_max": ["not-supported"]},
    ],
)
def test_malformed_manifest_compatibility_section_is_invalid_manifest(tmp_path, monkeypatch, compatibility):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    manifest = _manifest(tmp_path / "release.json", compatibility=compatibility)
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))

    result = run_update_check(db, manual=False)

    _assert_invalid_manifest(result)


def test_manifest_known_list_and_bool_shapes_are_strict(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    manifest = _manifest(tmp_path / "release.json", affected_domains={"db": True}, backup_required="true")
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))

    result = run_update_check(db, manual=False)

    _assert_invalid_manifest(result)


def test_release_notes_are_bounded_without_echoing_raw_secret_content(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    secret_tail = " password=secret rtsp://user:pass@camera"
    manifest = _manifest(tmp_path / "release.json", release_notes=("A" * 2000) + secret_tail)
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))

    result = run_update_check(db, manual=False)
    rendered = json.dumps(result, ensure_ascii=False)

    assert result["latest_release"]["release_notes_summary"] is not None
    assert len(result["latest_release"]["release_notes_summary"]) <= 800
    assert "password=secret" not in rendered
    assert "rtsp://user:pass@camera" not in rendered


def test_manual_endpoint_handles_malformed_manifest_without_500(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    manifest = _manifest(tmp_path / "release.json", required_schema_version="abc")
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))
    owner = db.query(User).filter(User.role == "owner").first()

    result = settings_router.system_update_check(request=FakeRequest(), db=db, current_user=owner)
    status = settings_router.system_update_status(db=db, current_user=owner)

    _assert_invalid_manifest(result)
    assert status["last_update_check"]["status"] == "invalid_manifest"


def test_diagnostic_report_preserves_invalid_manifest_without_raw_manifest_echo(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    manifest = _manifest(
        tmp_path / "release.json",
        required_schema_version="abc",
        release_notes="password=secret rtsp://user:pass@camera",
    )
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))
    run_update_check(db, manual=False)
    monkeypatch.setattr(settings_router, "storage_diagnostics", lambda: {"status": "ok"})
    monkeypatch.setattr(settings_router, "build_storage_monitoring_summary", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "reconciliation_diagnostics", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "retention_diagnostics", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "build_recorder_archive_payloads", lambda db: {})
    monkeypatch.setattr(settings_router, "get_hardware_capabilities", lambda: {"available_backends": []})
    monkeypatch.setattr(settings_router, "camera_diagnostics", lambda db: [])
    monkeypatch.setattr(settings_router.live_manager, "status", lambda: [])
    monkeypatch.setattr(settings_router.live_manager, "debug", lambda: {})
    monkeypatch.setattr(settings_router, "recordings_diagnostics", lambda: {"count": 0})
    monkeypatch.setattr(settings_router, "chronology_diagnostics", lambda db: {"items": []})

    archive = settings_router.build_log_archive(db, mode="normal", include_logs=False)
    import zipfile

    with zipfile.ZipFile(archive) as bundle:
        update = json.loads(bundle.read("update/status.json").decode("utf-8"))
        upgrade = json.loads(bundle.read("upgrade/report.json").decode("utf-8"))
        rendered = json.dumps(
            {
                "update": update,
                "upgrade_update_check": upgrade.get("update_check"),
            },
            ensure_ascii=False,
        )

    assert update["last_update_check"]["status"] == "invalid_manifest"
    assert upgrade["update_check"]["last_update_check"]["status"] == "invalid_manifest"
    assert "password=secret" not in rendered
    assert "rtsp://user:pass@camera" not in rendered
    assert str(manifest) not in rendered


def test_version_compare_handles_semver_and_unknown_ordering():
    assert compare_versions("1.0.0", "1.0.1")["ordering"] == "newer_available"
    assert compare_versions("1.0.0", "1.0.0")["ordering"] == "same_version"
    assert compare_versions("1.0.1", "1.0.0")["ordering"] == "installed_newer_than_channel"
    assert compare_versions("development", "1.0.0")["ordering"] == "unknown_ordering"


def test_risky_release_preflight_reports_schema_backup_restore_manual_steps(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    manifest = _manifest(tmp_path / "release.json", severity="security")
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))

    result = run_update_check(db)

    assert result["preflight"]["migration_plan"]["mutates_database"] is False
    assert result["preflight"]["backup_requirement"]["required"] is True
    assert result["preflight"]["backup_requirement"]["status"] == "blocked"
    assert result["preflight"]["restore_validation_requirement"]["status"] == "restore_status_source_unavailable"
    assert "verified_backup_required_before_apply" in result["preflight"]["blockers"]
    assert "restore_validation_required_before_apply" in result["preflight"]["blockers"]
    assert result["preflight"]["recording_stop_required"] is True
    assert result["preflight"]["side_effects"] == {
        "update_applied": False,
        "artifact_downloaded": False,
        "containers_restarted": False,
        "migration_executed": False,
        "backup_created": False,
        "restore_executed": False,
    }


def test_schedule_uses_stable_anchor_jitter_and_manual_rate_limit(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    now = datetime(2026, 5, 8, 4, 0, 0)

    first = run_update_check(db, manual=True, now=now)
    with pytest.raises(Exception) as exc:
        run_update_check(db, manual=True, now=now + timedelta(minutes=1))
    status = settings_router.system_update_status(db=db, current_user=object())

    assert first["schedule"]["schedule_source"] == "system_settings_created_at_plus_deterministic_jitter"
    assert first["schedule"]["failed_retry_policy"] == "next_planned_daily_slot_except_manual_owner_admin_check"
    assert "rate" in str(exc.value).lower()
    assert status["cache"]["cache_persistence"] == "in_memory_last_result_only"


def test_startup_due_check_does_not_apply_update_or_touch_schema(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    before = db.get(SchemaVersionState, CURRENT_STATE_ID).updated_at

    result = run_startup_due_check(db)

    assert result["side_effects"]["update_applied"] is False
    assert result["side_effects"]["migration_executed"] is False
    assert result["side_effects"]["backup_created"] is False
    assert db.get(SchemaVersionState, CURRENT_STATE_ID).updated_at == before


def test_update_endpoints_are_protected_and_registered(tmp_path):
    rows = {(item.method, item.path, item.decision, item.allowed_roles) for item in ENDPOINT_PERMISSIONS}
    assert ("GET", "/system/update/status", "manage_settings", (ROLE_OWNER, ROLE_ADMIN)) in rows
    assert ("POST", "/system/update/check", "manage_settings", (ROLE_OWNER, ROLE_ADMIN)) in rows
    for role in (ROLE_OPERATOR, ROLE_VIEWER):
        assert user_has_permission(role, "manage_settings") is False


def test_diagnostic_archive_includes_update_status_without_running_manual_check(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    monkeypatch.setattr(settings_router, "storage_diagnostics", lambda: {"status": "ok"})
    monkeypatch.setattr(settings_router, "build_storage_monitoring_summary", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "reconciliation_diagnostics", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "retention_diagnostics", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "build_recorder_archive_payloads", lambda db: {})
    monkeypatch.setattr(settings_router, "get_hardware_capabilities", lambda: {"available_backends": []})
    monkeypatch.setattr(settings_router, "camera_diagnostics", lambda db: [])
    monkeypatch.setattr(settings_router.live_manager, "status", lambda: [])
    monkeypatch.setattr(settings_router.live_manager, "debug", lambda: {})
    monkeypatch.setattr(settings_router, "recordings_diagnostics", lambda: {"count": 0})
    monkeypatch.setattr(settings_router, "chronology_diagnostics", lambda db: {"items": []})

    archive = settings_router.build_log_archive(db, mode="normal", include_logs=False)
    import zipfile

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        upgrade = json.loads(bundle.read("upgrade/report.json").decode("utf-8"))
        update = json.loads(bundle.read("update/status.json").decode("utf-8"))
        summary = bundle.read("upgrade/summary.txt").decode("utf-8")

    assert "update/status.json" in names
    assert update["status"] == "not_configured"
    assert upgrade["update_check"]["status"] == "not_configured"
    assert "update_check_status: not_configured" in summary
    assert update["side_effects"]["artifact_downloaded"] is False
    assert update["side_effects"]["containers_restarted"] is False

