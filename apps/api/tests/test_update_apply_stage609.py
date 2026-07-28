import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import fcntl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER, ROLE_VIEWER
from app.core.security import create_access_token, hash_password
from app.db.session import Base, get_db
from app.main import app
from app.models.audit_event import AuditEvent
from app.models.schema_migration_control import SchemaMigrationControl
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services.update_apply import UpdateApplyBlocked, _validate_latest_for_apply, reject_forbidden_apply_fields
from app.services.update_check import reset_update_check_cache_for_tests, run_update_check


def sqlite_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage609.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.merge(SystemSettings(id=1, system_initialized=True, system_name="Stage 6.0.9", created_at=datetime(2026, 6, 19, 3, 0, 0)))
    db.add_all(
        [
            User(username="stage609_owner", full_name="Stage 609 Owner", password_hash=hash_password("stage609-password"), role=ROLE_OWNER, is_active=True),
            User(username="stage609_admin", full_name="Stage 609 Admin", password_hash=hash_password("stage609-password"), role=ROLE_ADMIN, is_active=True),
            User(username="stage609_operator", full_name="Stage 609 Operator", password_hash=hash_password("stage609-password"), role=ROLE_OPERATOR, is_active=True),
            User(username="stage609_viewer", full_name="Stage 609 Viewer", password_hash=hash_password("stage609-password"), role=ROLE_VIEWER, is_active=True),
        ]
    )
    db.commit()
    return engine, db


def auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.username)}"}


def apply_direct(
    client,
    user,
    *,
    version="9.9.9",
    commit="c" * 40,
    confirm=True,
    submission_id=None,
):
    return client.post(
        "/system/update/apply",
        json={
            "confirm": confirm,
            "submission_id": submission_id or next_submission_id(),
            "expected_manifest_version": version,
            "expected_manifest_commit": commit,
        },
        headers=auth_headers(user),
    )


_submission_counter = 0


def next_submission_id():
    global _submission_counter
    _submission_counter += 1
    return f"00000000-0000-4000-8000-{_submission_counter:012x}"


def apply_json(**values):
    return {
        "submission_id": next_submission_id(),
        **values,
    }


def manifest(path: Path, **overrides):
    payload = {
        "schema_version": 1,
        "channel": "stable",
        "version": "9.9.9",
        "git_ref": "main",
        "commit": "cccccccccccccccccccccccccccccccccccccccc",
        "published_at": "2026-06-19T00:00:00Z",
        "title": "Stage 6.0.9 Apply Helper",
        "summary": "Adds bounded in-app apply helper.",
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


def release_identity(path: Path, **overrides):
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
def clean_env(monkeypatch, tmp_path):
    reset_update_check_cache_for_tests()
    for name in [
        "KMVMS_UPDATE_MANIFEST_PATH",
        "KMVMS_UPDATE_MANIFEST_FORCE_LOCAL",
        "KMVMS_PUBLIC_RELEASE_MANIFEST_PATH",
        "KMVMS_PUBLIC_RELEASE_MANIFEST_URL",
        "KMVMS_PUBLIC_RELEASE_PROVIDER_MODE",
        "KMVMS_PUBLIC_RELEASE_PROVIDER",
        "KMVMS_PUBLIC_RELEASE_TIMEOUT_SECONDS",
        "KMVMS_APP_ROOT",
        "KMVMS_BUILD_ID",
        "KMVMS_GIT_COMMIT",
        "KMVMS_BUILD_METADATA_FILE",
        "KM_VMS_GITHUB_TOKEN",
        "KM_VMS_GITHUB_TOKEN_FILE",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_FORCE_LOCAL", "1")
    monkeypatch.setenv("KMVMS_PUBLIC_RELEASE_PROVIDER", "0")
    app_root = tmp_path / "app-root"
    app_root.mkdir()
    release_identity(app_root / ".km-vms-release.json")
    monkeypatch.setenv("KMVMS_APP_ROOT", str(app_root))
    monkeypatch.setattr(settings, "update_control_root", str(tmp_path / "update-control"))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    monkeypatch.setattr(settings, "kmvms_update_source_private", False)
    monkeypatch.setattr(settings, "kmvms_update_token_configured", False)
    yield
    reset_update_check_cache_for_tests()


@pytest.fixture
def client_db(tmp_path):
    engine, db = sqlite_session(tmp_path)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_apply_requires_auth_permission_and_confirmation(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    admin = db.query(User).filter(User.role == ROLE_ADMIN).one()
    operator = db.query(User).filter(User.role == ROLE_OPERATOR).one()
    viewer = db.query(User).filter(User.role == ROLE_VIEWER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))

    assert client.post("/system/update/apply", json=apply_json(confirm=True)).status_code in {401, 403}
    assert client.post("/system/update/apply", json=apply_json(confirm=True), headers=auth_headers(operator)).status_code == 403
    assert client.post("/system/update/apply", json=apply_json(confirm=True), headers=auth_headers(viewer)).status_code == 403
    assert client.post("/system/update/apply", json=apply_json(confirm=False), headers=auth_headers(owner)).status_code == 409

    accepted = apply_direct(client, admin)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "queued"


def test_apply_rejects_browser_controlled_source_fields(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    response = client.post(
        "/system/update/apply",
        json={
            "confirm": True,
            "submission_id": next_submission_id(),
            "expected_manifest_version": "9.9.9",
            "expected_manifest_commit": "c" * 40,
            "repo": "attacker/example",
        },
        headers=auth_headers(owner),
    )

    assert response.status_code == 422
    assert not (Path(settings.update_control_root) / "update-request.json").exists()
    assert db.query(AuditEvent).filter(AuditEvent.event_type == "system.update_apply_requested").count() == 0


def test_apply_writes_bounded_request_and_sanitized_status(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))

    response = apply_direct(client, owner)
    assert response.status_code == 200
    request_file = Path(settings.update_control_root) / "update-request.json"
    status_file = Path(settings.update_control_root) / "update-status.json"
    request = json.loads(request_file.read_text(encoding="utf-8"))
    status = response.json()["apply_status"]
    rendered = json.dumps({"request": request, "status": status}, ensure_ascii=False)

    assert request["schema_version"] == 3
    assert request["document_type"] == "update_apply_request"
    assert request["submission_id"] == response.json()["submission_id"]
    assert request["state"] == "admitted"
    admission_request = request
    assert admission_request["intent"] == "apply_update"
    assert admission_request["confirmed"] is True
    assert admission_request["source"]["kind"] == "trusted_manifest"
    assert admission_request["source"]["repo"] == "kmishnev87/km-vms"
    assert admission_request["source"]["ref"] == "main"
    assert admission_request["source"]["commit"] == "cccccccccccccccccccccccccccccccccccccccc"
    assert admission_request["source"]["apply_ref"] == "cccccccccccccccccccccccccccccccccccccccc"
    assert admission_request["apply_candidate"]["source"] == "live_check"
    assert admission_request["status_path"] == "data/update-control/update-status.json"
    assert not status_file.exists(), "API admission must not create a second queued status handoff"
    assert status["admission"]["authority"] == "active"
    assert status["admission"]["state"] == "admitted"
    assert status["expected_commit"] == "cccccccccccccccccccccccccccccccccccccccc"
    assert status["source"]["apply_ref"] == "cccccccccccccccccccccccccccccccccccccccc"
    assert status["apply_candidate"]["source"] == "live_check"
    assert status["side_effects"]["api_docker_socket"] is False
    assert status["side_effects"]["api_shell_execution"] is False
    assert status["side_effects"]["request_controlled_source"] is False
    for forbidden in ("github_pat_", "Authorization", "Bearer ", ".env", "DATABASE_URL", "rtsp://"):
        assert forbidden not in rendered


def test_repeated_submission_is_one_admission_and_one_audit(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    submission_id = next_submission_id()

    first = apply_direct(client, owner, submission_id=submission_id)
    second = apply_direct(client, owner, submission_id=submission_id)

    assert first.status_code == second.status_code == 200
    assert first.json()["request_id"] == second.json()["request_id"]
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert (
        db.query(AuditEvent)
        .filter(AuditEvent.event_type == "system.update_apply_requested")
        .count()
        == 1
    )


def test_terminal_or_unknown_old_artifact_does_not_block_next_apply(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    control = Path(settings.update_control_root)
    control.mkdir(parents=True, exist_ok=True)
    (control / "update-request.json").write_text(
        json.dumps({"schema_version": 1, "unknown_old_field": "inert"}),
        encoding="utf-8",
    )
    (control / "update-status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": "update-" + ("a" * 32),
                "status": "completed",
                "phase": "completed",
                "expected_commit": "a" * 40,
                "installed_commit": "a" * 40,
                "commit_verified": True,
            }
        ),
        encoding="utf-8",
    )

    response = apply_direct(client, owner)

    assert response.status_code == 200
    request = json.loads((control / "update-request.json").read_text(encoding="utf-8"))
    assert request["schema_version"] == 3
    assert request["state"] == "admitted"


def test_live_helper_or_schema_mutation_blocks_second_apply(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    control = Path(settings.update_control_root)
    control.mkdir(parents=True, exist_ok=True)
    lease_path = control / "update-helper-claim.lock"

    with lease_path.open("a+", encoding="utf-8") as lease:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX)
        blocked = apply_direct(client, owner)
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "update_already_running"
        fcntl.flock(lease.fileno(), fcntl.LOCK_UN)

    now = datetime.utcnow()
    db.add(
        SchemaMigrationControl(
            id="current",
            fencing_generation=1,
            owner_attempt_id="migration-attempt-" + ("b" * 32),
            request_id="update-" + ("b" * 32),
            installed_version="0.7.18",
            installed_commit="b" * 40,
            source_schema_version=6,
            target_commit="c" * 40,
            target_release="9.9.9",
            target_schema_version=8,
            registry_fingerprint="d" * 64,
            plan_fingerprint="e" * 64,
            source_shape_fingerprint="f" * 64,
            control_definition_fingerprint="1" * 64,
            state="migrating",
            lease_expires_at=now + timedelta(minutes=5),
            updated_at=now,
        )
    )
    db.commit()
    blocked = apply_direct(client, owner)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "schema_mutation_active"


def test_forbidden_fields_blockers_running_and_private_token_preconditions(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))

    assert client.post("/system/update/apply", json=apply_json(confirm=True, url="https://example.invalid/repo.tgz"), headers=auth_headers(owner)).status_code == 422
    with pytest.raises(Exception):
        reject_forbidden_apply_fields({"token": "ghp_secret"})

    first = apply_direct(client, owner)
    assert first.status_code == 200
    second = apply_direct(client, owner)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "update_already_running"

    reset_update_check_cache_for_tests()
    monkeypatch.setattr(settings, "update_control_root", str(tmp_path / "private-control"))
    monkeypatch.setattr(settings, "kmvms_update_source_private", True)
    private = apply_direct(client, owner)
    assert private.status_code == 409
    assert private.json()["detail"]["code"] == "token_not_configured"


def test_manifest_blockers_current_and_missing_helper_prevent_apply(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()

    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", False)
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    assert client.post("/system/update/apply", json=apply_json(confirm=True), headers=auth_headers(owner)).json()["detail"]["code"] == "helper_not_configured"

    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "blocked.json", requires_backup=True, requires_manual_action=True, requires_migration=True)))
    reset_update_check_cache_for_tests()
    blocked = apply_direct(client, owner)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] in {"requires_backup", "unsupported_release_requirements"}

    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "current.json", version="0.7.0", commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")))
    reset_update_check_cache_for_tests()
    current = apply_direct(client, owner, commit="a" * 40)
    assert current.status_code == 409
    assert current.json()["detail"]["code"] == "no_update_available"

    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "bad-commit.json", version="9.9.10", commit="main")))
    reset_update_check_cache_for_tests()
    bad_commit = apply_direct(client, owner, version="9.9.10", commit="d" * 40)
    assert bad_commit.status_code == 409
    assert bad_commit.json()["detail"]["code"] == "manifest_check_failed"


def test_apply_precondition_requires_full_trusted_commit():
    with pytest.raises(UpdateApplyBlocked) as exc:
        _validate_latest_for_apply(
            {
                "status": "update_available",
                "blockers": [],
                "latest": {
                    "version": "9.9.10",
                    "commit": "main",
                    "source_type": "github_tarball",
                    "source_repo": "kmishnev87/km-vms",
                    "source_ref": "main",
                },
            }
        )
    assert exc.value.code == "trusted_commit_missing"


def test_status_and_cancel_are_sanitized_and_registered(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))

    queued = apply_direct(client, owner).json()
    status_response = client.get("/system/update/apply/status", headers=auth_headers(owner))
    cancel_response = client.post("/system/update/apply/cancel", headers=auth_headers(owner))
    cancelled_status = client.get("/system/update/apply/status", headers=auth_headers(owner)).json()

    assert status_response.status_code == 200
    assert status_response.json()["request_id"] == queued["request_id"]
    assert status_response.json()["expected_commit"] == "cccccccccccccccccccccccccccccccccccccccc"
    assert status_response.json()["apply_candidate"]["source"] == "live_check"
    assert status_response.json()["commit_verified"] is False
    assert cancel_response.json()["status"] == "cancelled"
    assert cancelled_status["status"] == "cancelled"
    rows = {(item.method, item.path, item.decision, item.allowed_roles) for item in ENDPOINT_PERMISSIONS}
    for method, path in (
        ("POST", "/system/update/apply"),
        ("GET", "/system/update/apply/status"),
        ("POST", "/system/update/apply/cancel"),
    ):
        assert (method, path, "manage_settings", (ROLE_OWNER, ROLE_ADMIN)) in rows


def test_apply_uses_fresh_trusted_snapshot_when_live_provider_fails(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    checked = run_update_check(db)
    assert checked["status"] == "update_available"

    from app.services import update_apply as update_apply_module

    def fail_live_check(*_args, **_kwargs):
        raise AssertionError("fresh trusted snapshot should avoid live provider during apply")

    monkeypatch.setattr(update_apply_module, "run_update_check", fail_live_check)

    response = apply_direct(client, owner)

    assert response.status_code == 200
    request = json.loads((Path(settings.update_control_root) / "update-request.json").read_text(encoding="utf-8"))
    assert request["apply_candidate"]["source"] == "trusted_snapshot"
    assert request["source"]["commit"] == "cccccccccccccccccccccccccccccccccccccccc"


def test_apply_uses_snapshot_after_transient_failed_recheck(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    good_manifest = manifest(tmp_path / "release.json")
    bad_manifest = tmp_path / "bad-release.json"
    bad_manifest.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(good_manifest))
    assert run_update_check(db)["status"] == "update_available"
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(bad_manifest))
    assert run_update_check(db)["status"] == "check_failed"

    response = apply_direct(client, owner)

    assert response.status_code == 200
    request = json.loads((Path(settings.update_control_root) / "update-request.json").read_text(encoding="utf-8"))
    assert request["apply_candidate"]["source"] == "trusted_snapshot"
    assert request["source"]["commit"] == "cccccccccccccccccccccccccccccccccccccccc"


def test_apply_blocks_stale_trusted_snapshot_without_writing_request(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    assert run_update_check(db)["status"] == "update_available"

    from app.services import update_apply as update_apply_module

    monkeypatch.setattr(update_apply_module, "trusted_apply_snapshot_status", lambda: {"available": True, "fresh": False, "age_seconds": 901, "fresh_for_seconds": 900})

    response = apply_direct(client, owner)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "trusted_snapshot_stale"
    assert not (Path(settings.update_control_root) / "update-request.json").exists()


def test_apply_blocks_snapshot_expected_mismatches_without_writing_request(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    assert run_update_check(db)["status"] == "update_available"

    wrong_version = apply_direct(client, owner, version="9.9.8")
    wrong_commit = apply_direct(client, owner, commit="d" * 40)

    assert wrong_version.status_code == 409
    assert wrong_version.json()["detail"]["code"] == "manifest_version_changed"
    assert wrong_commit.status_code == 409
    assert wrong_commit.json()["detail"]["code"] == "manifest_commit_changed"
    assert not (Path(settings.update_control_root) / "update-request.json").exists()


def test_apply_blocks_if_expected_version_or_commit_missing(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    assert run_update_check(db)["status"] == "update_available"

    response = client.post(
        "/system/update/apply",
        json=apply_json(confirm=True, expected_manifest_version="9.9.9"),
        headers=auth_headers(owner),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "update_check_required"
    assert not (Path(settings.update_control_root) / "update-request.json").exists()


def test_apply_blocks_if_installed_identity_changed_after_snapshot(client_db, tmp_path, monkeypatch):
    client, db = client_db
    owner = db.query(User).filter(User.role == ROLE_OWNER).one()
    monkeypatch.setenv("KMVMS_UPDATE_MANIFEST_PATH", str(manifest(tmp_path / "release.json")))
    assert run_update_check(db)["status"] == "update_available"
    root = Path(os.environ["KMVMS_APP_ROOT"])
    release_identity(root / ".km-vms-release.json", version="0.7.0", commit_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    response = apply_direct(client, owner)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "trusted_snapshot_invalidated"
    assert not (Path(settings.update_control_root) / "update-request.json").exists()
