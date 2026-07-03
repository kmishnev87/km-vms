import json
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers import settings as settings_router
from app.services.backup_before_upgrade import BackupExecutionConfig, create_backup_before_upgrade
from app.services.schema_versioning import CURRENT_BASELINE_ID, CURRENT_SCHEMA_VERSION, CURRENT_STATE_ID
from app.services import upgrade_report as upgrade_report_module
from app.services.upgrade_report import build_upgrade_report, upgrade_report_text_summary
from test_schema_migration_runner_stage3 import seed_state


def sqlite_session(tmp_path):
    db_path = tmp_path / "stage6.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def _add_failed_history(db):
    credential_url = "rtsp://" + "user:pass@example/live"
    synthetic_token = "secret" + "-token"
    db.add(
        SchemaMigrationHistory(
            migration_id="stage6_failed_safe_summary",
            previous_version=0,
            target_version=1,
            schema_version=0,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            applied_at=datetime.utcnow(),
            status="failed",
            source="migration_runner",
            service_name="api_bootstrap",
            details={"safe": True},
            error_summary=f"password={synthetic_token} {credential_url} failed",
        )
    )
    db.commit()


def _seed_basic(db):
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    db.add(SystemSettings(id=1, system_initialized=True))
    db.add(User(username="stage6_owner", full_name="Stage 6 Owner", password_hash="hash-value-not-exported", role="owner", is_active=True))
    db.add(
        AuditEvent(
            id="stage6-audit-event",
            actor_username="stage6_owner",
            actor_role="owner",
            category="diagnostics",
            event_type="diagnostics.stage6.seed",
            severity="info",
            message_ru="stage6",
            message_en="stage6",
            event_metadata={"Authorization": "Bearer " + "secret" + "-token"},
        )
    )
    db.commit()


def test_upgrade_report_is_read_only_and_has_stable_core_schema(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed_basic(db)
    before_state = db.get(SchemaVersionState, CURRENT_STATE_ID).updated_at
    before_history = db.query(SchemaMigrationHistory).count()

    report = build_upgrade_report(db)

    assert report["report_version"] == "stage6.upgrade_report.v1"
    assert report["generated_at"]
    assert report["status"] == "complete"
    assert report["data_freshness"]["status"] == "current_read_only_snapshot"
    assert set(["data_sources", "limitations", "versions", "migration_runner", "backup", "warnings", "redaction"]).issubset(report)
    assert report["backup"]["backup_status_source"] == "source_unavailable"
    assert report["backup"]["status"] == "backup_status_source_unavailable"
    assert report["backup"]["status_semantics"] == "source_unavailable"
    assert report["restore_validation"]["status_source"] == "source_unavailable"
    assert report["restore_validation"]["status"] == "restore_status_source_unavailable"
    assert report["restore_validation"]["status_semantics"] == "source_unavailable"
    assert db.query(SchemaMigrationHistory).count() == before_history
    assert db.get(SchemaVersionState, CURRENT_STATE_ID).updated_at == before_state
    assert report["side_effects"] == {
        "db_mutated": False,
        "filesystem_write_probe_performed": False,
        "backup_created": False,
        "restore_executed": False,
        "migration_executed": False,
        "production_adoption_written": False,
    }


def test_upgrade_report_versions_migrations_and_warnings_are_safe(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed_basic(db)
    _add_failed_history(db)

    report = build_upgrade_report(db)
    rendered = json.dumps(report, ensure_ascii=False)
    warning_codes = {item["code"] for item in report["warnings"]}

    assert report["versions"]["app_version"] == APP_VERSION
    assert report["versions"]["app_build_version"] == APP_BUILD_VERSION
    assert report["versions"]["schema_version_is_app_version"] is False
    assert report["versions"]["app_build_version_source"] == "development_fallback"
    assert "development-only" in report["versions"]["app_build_version_limitation"]
    assert report["versions"]["installed_build"]["status"] == "development_build"
    assert report["versions"]["current_schema_version"] == CURRENT_SCHEMA_VERSION
    assert report["versions"]["target_schema_version"] == CURRENT_SCHEMA_VERSION
    assert report["schema_migration_history"]["counts"]["failed"] == 1
    assert report["migration_runner"]["mutates_database"] is False
    assert report["production"]["production_adoption_deferred"] is True
    assert report["production"]["production_not_touched"] is True
    assert report["production"]["production_not_touched_source"] == "report_generation_read_only"
    assert report["production"]["production_read_only_inspected_only"] is False
    assert report["production"]["production_read_only_inspected_only_source"] == "unknown"
    assert report["redaction"]["redaction_status"] == "scoped_check_passed"
    assert report["redaction"]["redaction_scope"] == "upgrade_report_fields_only"
    assert "upgrade_report_json_fields" in report["redaction"]["checked_outputs"]
    assert "installed_build_development_fallback" in warning_codes
    assert "production_adoption_deferred" in warning_codes
    assert "backup_status_source_unavailable" in warning_codes
    assert "restore_validation_status_source_unavailable" in warning_codes
    assert "video_archive_restore_not_covered" in warning_codes
    forbidden_values = [
        "secret" + "-token",
        "pass@example",
        "hash-value-not-exported",
        "rtsp://" + "user:pass",
        "sqlite:///",
        "postgresql://" + "user:pass",
        "Authorization: " + "Bearer",
        "BEGIN " + "PRIVATE KEY",
        "." + "env",
    ]
    for forbidden in forbidden_values:
        assert forbidden not in rendered


def test_upgrade_report_sanitizes_update_check_release_text_before_secret_gate(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed_basic(db)

    def fake_update_status(_db):
        return {
            "status": "current",
            "installed_release": {
                "version": "0.7.5",
                "title": "Login UX polish",
                "summary": "Remembers the last username without storing passwords.",
            },
            "available_release": {
                "version": "0.7.5",
                "title": "Login UX polish",
                "summary": "Remembers the last username without storing passwords.",
            },
        }

    monkeypatch.setattr(upgrade_report_module, "build_update_status", fake_update_status)

    report = build_upgrade_report(db)

    rendered = json.dumps(report["update_check"], ensure_ascii=False)
    assert "passwords" not in rendered
    assert "redacted=***" in rendered
    assert report["redaction"]["redaction_status"] == "scoped_check_passed"


def test_upgrade_report_backup_manifest_without_restore_source_does_not_claim_not_performed(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed_basic(db)
    backup = create_backup_before_upgrade(
        db,
        config=BackupExecutionConfig(backup_root=tmp_path / "stage6-backups", allow_tmp_for_tests=True, source="test"),
    )

    report = build_upgrade_report(db, backup_manifest_path=backup["manifest_path"])

    assert report["backup"]["backup_status_source"] == "provided_manifest_path_for_test_only"
    assert report["backup"]["test_only_source"] is True
    assert report["backup"]["status"] == "backup_available"
    assert report["backup"]["manifest_restore_validation_status"] == "not_performed_stage5_deferred"
    assert report["restore_validation"]["status_source"] == "source_unavailable"
    assert report["restore_validation"]["status"] == "restore_status_source_unavailable"
    assert report["restore_validation"]["status_semantics"] == "source_unavailable"
    assert report["restore_validation"]["status"] != "not_performed_stage5_deferred"


def test_upgrade_report_backup_missing_is_distinct_from_source_unavailable(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed_basic(db)
    backup = create_backup_before_upgrade(
        db,
        config=BackupExecutionConfig(backup_root=tmp_path / "stage6-backups", allow_tmp_for_tests=True, source="test"),
    )
    Path(backup["backup_file_path"]).unlink()

    report = build_upgrade_report(db, backup_manifest_path=backup["manifest_path"])

    assert report["backup"]["backup_status_source"] == "provided_manifest_path_for_test_only"
    assert report["backup"]["status"] == "backup_missing"
    assert report["backup"]["status_semantics"] == "backup_missing"
    assert report["backup"]["status"] != "backup_status_source_unavailable"


def test_upgrade_report_backup_restore_validation_link_and_root_evidence(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed_basic(db)
    backup = create_backup_before_upgrade(
        db,
        config=BackupExecutionConfig(backup_root=tmp_path / "stage6-backups", allow_tmp_for_tests=True, source="test"),
    )
    restore_manifest = tmp_path / "stage6.restore-validation.json"
    restore_manifest.write_text(
        json.dumps(
            {
                "status": "restore_validated",
                "backup_restore_validated": True,
                "backup_id": backup["backup_id"],
                "backup_checksum_sha256": backup["checksum_sha256"],
                "video_archive_restore_status": "not_covered_metadata_only",
            }
        ),
        encoding="utf-8",
    )

    report = build_upgrade_report(db, backup_manifest_path=backup["manifest_path"], restore_validation_manifest_path=restore_manifest)

    assert report["backup"]["status"] == "restore_validated"
    assert report["backup"]["backup_status_source"] == "provided_manifest_path_for_test_only"
    assert report["restore_validation"]["status_source"] == "provided_manifest_path_for_test_only"
    assert report["restore_validation"]["status_semantics"] == "restore_validated"
    assert report["backup"]["backup_available"] is True
    assert report["backup"]["checksum_status"] == "matched"
    assert report["backup"]["backup_path_label"] == "configured_backup_root/backup_artifact"
    assert report["restore_validation"]["backup_restore_validated"] is True
    assert report["restore_validation"]["production_rollback_automated"] is False
    assert report["restore_validation"]["video_archive_restore_covered"] is False
    assert report["backup"]["root_evidence"]["write_probe_performed"] is False
    assert report["backup"]["root_evidence"]["configured_contract_status"] in {
        "configured_persistent_contract",
        "configured_disposable_test_contract",
    }
    assert report["backup"]["root_evidence"]["persistence_evidence_status"] == "unknown_safe_check_unavailable"
    assert report["backup"]["root_evidence"]["host_mount_proven"] is False


def test_system_upgrade_report_endpoint_is_protected_in_registry(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    _seed_basic(db)

    report = settings_router.system_upgrade_report(db=db, current_user=object())
    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}

    assert report["side_effects"]["backup_created"] is False
    assert ("GET", "/system/upgrade/report", "run_diagnostics") in rows
    assert not any(item.path == "/system/upgrade/report" and item.decision in {"public route", "viewer", "operator"} for item in ENDPOINT_PERMISSIONS)
    public_status = settings_router.system_status(db=db)
    assert "upgrade" not in public_status
    assert "backup" not in public_status


def test_diagnostic_archive_includes_upgrade_report_and_excludes_forbidden_artifacts(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    _seed_basic(db)

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
    monkeypatch.setattr(upgrade_report_module, "build_update_status", lambda db: {"status": "not_configured"})

    archive = settings_router.build_log_archive(db, mode="normal", include_logs=False)
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        report = json.loads(bundle.read("upgrade/report.json").decode("utf-8"))
        summary = bundle.read("upgrade/summary.txt").decode("utf-8")
        rendered = "\n".join(bundle.read(name).decode("utf-8", errors="replace") for name in names)

    assert "upgrade/report.json" in names
    assert "upgrade/summary.txt" in names
    assert report["diagnostic_archive"]["included_in_existing_diagnostic_archive"] is True
    assert report["backup"]["backup_status_source"] == "source_unavailable"
    assert report["backup"]["status"] == "backup_status_source_unavailable"
    assert report["restore_validation"]["status_source"] == "source_unavailable"
    assert report["restore_validation"]["status"] == "restore_status_source_unavailable"
    assert report["redaction"]["redaction_scope"] == "upgrade_report_fields_only"
    assert report["diagnostic_archive"]["redaction_scope"] == "upgrade_report_fields_and_diagnostic_archive_upgrade_summary"
    assert report["update_check"]["status"] == "not_configured"
    assert "production_restore_executed: false" in summary
    assert "update_check_status: not_configured" in summary
    assert "backup_status_source: source_unavailable" in summary
    assert "restore_validation_status_source: source_unavailable" in summary
    for forbidden_name in names:
        assert not forbidden_name.endswith((".dump", ".sqlite3", ".db", ".env"))
    forbidden_values = ["secret" + "-token", "hash-value-not-exported", "rtsp://", "postgresql://", "sqlite:///"]
    for forbidden in forbidden_values:
        assert forbidden not in rendered
