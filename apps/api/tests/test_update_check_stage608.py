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
from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER, ROLE_VIEWER, user_has_permission
from app.db.session import Base
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers import settings as settings_router
from app.services.update_check import (
    UpdateCheckBlocked,
    compare_versions,
    read_installed_update_state,
    read_trusted_local_manifest,
    reset_update_check_cache_for_tests,
    run_startup_due_check,
    run_update_check,
)


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


def sqlite_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage608.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def _seed(db):
    row = SystemSettings(id=1, system_initialized=True, system_name="Stage 6.0.8", created_at=datetime(2026, 6, 18, 3, 0, 0))
    db.merge(row)
    db.add(User(username="stage608_owner", full_name="Stage 608 Owner", password_hash="hash-value-not-exported", role=ROLE_OWNER, is_active=True))
    db.commit()


def _manifest(path: Path, **overrides):
    payload = {
        "schema_version": 1,
        "channel": "stable",
        "version": "6.0.8",
        "git_ref": "main",
        "commit": "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "published_at": "2026-06-18T00:00:00Z",
        "title": "Update Status and Release Manifest API",
        "summary": "Adds read-only update status and trusted manifest check.",
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


def _release_identity(path: Path, **overrides):
    payload = {
        "schema_version": 1,
        "product": "KM VMS",
        "version": "0.7.0",
        "title": "Installed release",
        "summary": "Installed release identity.",
        "source_kind": "github-release",
        "source_repo": "kmishnev87/km-vms",
        "source_ref": "main",
        "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "installed_by": "test",
        "metadata_source": "official_update",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_update_env(monkeypatch):
    reset_update_check_cache_for_tests()
    for name in [
        "KMVMS_UPDATE_MANIFEST_PATH",
        "KMVMS_UPDATE_MANIFEST_FORCE_LOCAL",
        "KMVMS_PUBLIC_RELEASE_MANIFEST_PATH",
        "KMVMS_PUBLIC_RELEASE_MANIFEST_URL",
        "KMVMS_PUBLIC_RELEASE_PROVIDER",
        "KMVMS_PUBLIC_RELEASE_TIMEOUT_SECONDS",
        "KMVMS_UPDATE_CHANNEL_ID",
        "KMVMS_BUILD_METADATA_FILE",
        "KMVMS_BUILD_ID",
        "KMVMS_GIT_COMMIT",
        "KMVMS_BUILD_TIME",
        "KMVMS_INSTALL_SOURCE",
        "KMVMS_SOURCE_CHANNEL_ID",
        "KMVMS_APP_ROOT",
        "KM_VMS_APP_DIR",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", "1")
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_PROVIDER", "0")
    yield
    reset_update_check_cache_for_tests()


def test_missing_metadata_degrades_without_manifest_configuration(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    monkeypatch.setenv("KMVMS_APP_ROOT", str(tmp_path / "missing-root"))
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_MANIFEST_PATH", str(tmp_path / "missing-public-release.json"))

    status = settings_router.system_update_status(db=db, current_user=object())

    assert status["status"] == "not_configured"
    assert status["installed"]["metadata_validity"] == "missing"
    assert status["can_apply_from_ui"] is False
    assert status["source_channel"]["arbitrary_url_supported"] is False
    assert status["side_effects"]["containers_restarted"] is False


def test_metadata_reader_normalizes_success_and_redacts_token_like_fields(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / ".km-vms-source.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_kind": "github-tarball",
                "github_repo": "kmishnev87/km-vms",
                "ref": "main",
                "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "github_token": "token_should_not_echo",
            }
        ),
        encoding="utf-8",
    )
    (app_root / ".km-vms-update.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "success",
                "finished_at": "2026-06-18T01:02:03Z",
                "failed_phase": None,
                "source_kind": "github-tarball",
                "github_repo": "kmishnev87/km-vms",
                "ref": "main",
                "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))

    installed = read_installed_update_state()
    rendered = json.dumps(installed.__dict__, default=str)

    assert installed.metadata_validity == "valid"
    assert installed.status == "identity_incomplete"
    assert installed.installed_commit == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert installed.last_update_status == "success"
    assert "token_should_not_echo" not in rendered


def test_malformed_and_unsupported_metadata_are_sanitized(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / ".km-vms-source.json").write_text("{not-json", encoding="utf-8")
    (app_root / ".km-vms-update.json").write_text(json.dumps({"schema_version": 999, "status": {"bad": True}, "failed_phase": "/tmp/secret"}), encoding="utf-8")
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))

    installed = read_installed_update_state()

    assert installed.metadata_validity == "invalid"
    assert any(item.code == "source_metadata_invalid_json" for item in installed.warnings)
    assert any(item.code == "update_metadata_unsupported_schema" for item in installed.warnings)


def test_valid_manifest_schema_normalizes_stage608_fields(tmp_path):
    manifest = read_trusted_local_manifest(_manifest(tmp_path / "release.json"))

    assert manifest.schema_version == 1
    assert manifest.version == "6.0.8"
    assert manifest.source_type == "github_tarball"
    assert manifest.source_repo == "kmishnev87/km-vms"
    assert manifest.requires_backup is False


@pytest.mark.parametrize(
    "override",
    [
        {"schema_version": 2},
        {"commit": "not-a-sha"},
        {"git_ref": "../main"},
        {"published_at": "not-a-date"},
        {"requires_backup": "false"},
        {"minimum_current_version": "not-semver"},
        {"artifacts": {"source": {"repo": "bad repo", "ref": "main"}}},
    ],
)
def test_invalid_manifest_returns_check_failed_safely(tmp_path, monkeypatch, override):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    manifest = _manifest(tmp_path / "release.json", **override)
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest))

    result = run_update_check(db, manual=False)
    rendered = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "check_failed"
    assert result["latest"] is None
    assert result["raw_manifest_exposed"] is False
    assert "github_pat_" not in rendered
    assert "Traceback" not in rendered


def test_same_commit_is_current_and_no_false_update(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    app_root = tmp_path / "app"
    app_root.mkdir()
    commit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    _release_identity(app_root / ".km-vms-release.json", version="9.9.9", commit_sha=commit)
    (app_root / ".km-vms-source.json").write_text(json.dumps({"schema_version": 1, "source_kind": "github-tarball", "commit_sha": commit}), encoding="utf-8")
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(_manifest(tmp_path / "release.json", commit=commit, version="9.9.9")))

    result = run_update_check(db)

    assert result["status"] == "current"
    assert result["can_apply_from_ui"] is False


def test_public_release_provider_reads_remote_descriptor_without_token(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    app_root = tmp_path / "app"
    app_root.mkdir()
    _release_identity(app_root / ".km-vms-release.json", version="0.7.1", commit_sha="a" * 40)
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))
    monkeypatch.delenv("KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", raising=False)
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_PROVIDER", "1")
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_MANIFEST_URL", "https://raw.githubusercontent.com/kmishnev87/km-vms/main/release/km-vms-release.json")

    from app.services import update_check as update_check_module

    monkeypatch.setattr(
        update_check_module,
        "_read_public_release_payload",
        lambda url: {
            "schema_version": 1,
            "version": "0.7.2",
            "title": "Public provider",
            "summary": "Public provider summary.",
            "release_channel": "public-github",
            "source_kind": "github-release",
            "source_repo": "kmishnev87/km-vms",
            "source_ref": "main",
            "commit_sha": "b" * 40,
            "published_at": "2026-06-26T00:00:00Z",
        },
    )

    result = run_update_check(db)

    assert result["status"] == "update_available"
    assert result["available_release"]["version"] == "0.7.2"
    assert result["available_release"]["provider"] == "public_github_release"
    assert result["can_apply_from_ui"] is False


def test_public_release_provider_failure_is_sanitized(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    app_root = tmp_path / "app"
    app_root.mkdir()
    _release_identity(app_root / ".km-vms-release.json", version="0.7.1", commit_sha="a" * 40)
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))
    monkeypatch.delenv("KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", raising=False)
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_PROVIDER", "1")
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_MANIFEST_URL", "https://raw.githubusercontent.com/kmishnev87/km-vms/main/release/km-vms-release.json")

    from app.services import update_check as update_check_module

    def raise_unavailable(_url):
        raise UpdateCheckBlocked(
            "provider_unavailable",
            {"summary": "Public release metadata is temporarily unavailable.", "error_category": "public_provider_unavailable"},
        )

    monkeypatch.setattr(update_check_module, "_read_public_release_payload", raise_unavailable)

    result = run_update_check(db)

    assert result["status"] == "check_failed"
    assert result["available_release"] is None
    assert result["can_apply_from_ui"] is False
    assert result["errors"] == [
        {
            "code": "provider_unavailable",
            "summary": "Public release metadata is temporarily unavailable.",
            "error_category": "public_provider_unavailable",
        }
    ]


def test_same_version_without_commit_is_current_or_unknown(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    app_root = tmp_path / "app"
    app_root.mkdir()
    _release_identity(app_root / ".km-vms-release.json", version="1.0.0", commit_sha="a" * 40)
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(_manifest(tmp_path / "release.json", version="1.0.0", commit=None)))

    result = run_update_check(db)

    assert result["status"] == "current_or_unknown"
    assert any(item["code"] == "commit_evidence_missing" for item in result["warnings"])


def test_update_available_and_blockers_are_conservative(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    app_root = tmp_path / "app"
    app_root.mkdir()
    _release_identity(app_root / ".km-vms-release.json", version="0.7.0", commit_sha="a" * 40)
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(_manifest(tmp_path / "release.json", version="9.0.0", commit="b" * 40)))

    available = run_update_check(db)
    assert available["status"] == "update_available"
    assert available["can_apply_from_ui"] is False

    reset_update_check_cache_for_tests()
    blocked_manifest = _manifest(tmp_path / "blocked.json", version="9.0.0", commit="c" * 40, requires_backup=True, requires_manual_action=True, requires_migration=True)
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(blocked_manifest))
    blocked = run_update_check(db)

    assert blocked["status"] == "blocked"
    assert {item["code"] for item in blocked["blockers"]} == {"requires_backup", "requires_manual_action", "requires_migration"}
    assert blocked["side_effects"]["update_applied"] is False


def test_minimum_current_version_blocks_incompatible_release(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    app_root = tmp_path / "app"
    app_root.mkdir()
    _release_identity(app_root / ".km-vms-release.json", version="0.7.0", commit_sha="a" * 40)
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(_manifest(tmp_path / "release.json", version="9.0.0", commit=None, minimum_current_version="2.0.0")))

    result = run_update_check(db)

    assert result["status"] == "blocked"
    assert result["blockers"][0]["code"] == "minimum_current_version_not_satisfied"


def test_request_body_cannot_supply_arbitrary_source_and_endpoint_is_registered(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    owner = db.query(User).filter(User.role == ROLE_OWNER).first()
    monkeypatch.setenv("KMVMS_APP_ROOT", str(tmp_path / "missing-root"))
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_MANIFEST_PATH", str(tmp_path / "missing-public-release.json"))

    result = settings_router.system_update_check(request=FakeRequest(), db=db, current_user=owner)

    assert result["status"] == "not_configured"
    assert result["source_channel"]["arbitrary_url_supported"] is False
    rows = {(item.method, item.path, item.decision, item.allowed_roles) for item in ENDPOINT_PERMISSIONS}
    assert ("GET", "/system/update/status", "manage_settings", (ROLE_OWNER, ROLE_ADMIN)) in rows
    assert ("POST", "/system/update/check", "manage_settings", (ROLE_OWNER, ROLE_ADMIN)) in rows
    for role in (ROLE_OPERATOR, ROLE_VIEWER):
        assert user_has_permission(role, "manage_settings") is False


def test_manual_rate_limit_and_startup_are_read_only(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    now = datetime(2026, 6, 18, 4, 0, 0)

    first = run_update_check(db, manual=True, now=now)
    with pytest.raises(Exception) as exc:
        run_update_check(db, manual=True, now=now + timedelta(minutes=1))
    startup = run_startup_due_check(db)

    assert first["side_effects"]["artifact_downloaded"] is False
    assert "rate" in str(exc.value).lower()
    assert startup["side_effects"]["containers_restarted"] is False


def test_diagnostic_archive_includes_update_status_without_running_apply(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed(db)
    monkeypatch.setenv("KMVMS_APP_ROOT", str(tmp_path / "missing-root"))
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_MANIFEST_PATH", str(tmp_path / "missing-public-release.json"))
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

    assert update["status"] == "not_configured"
    assert upgrade["update_check"]["status"] == "not_configured"
    assert update["can_apply_from_ui"] is False
    assert update["side_effects"]["update_applied"] is False


def test_version_compare_avoids_lexicographic_bug():
    assert compare_versions("1.0.9", "1.0.10")["ordering"] == "newer_available"
    assert compare_versions("1.0.10", "1.0.9")["ordering"] == "installed_newer_than_channel"
    assert compare_versions("development", "9.0.0")["ordering"] == "unknown_ordering"
