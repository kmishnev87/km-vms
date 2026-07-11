import io
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.permissions import ROLE_OWNER, ROLE_VIEWER
from app.db.session import Base
from app.main import health
from app.models.audit_event import AuditEvent
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers.deps import get_current_user, require_permission
from app.routers.settings import BugReportRequest, SetupRequest, create_bug_report, download_log_archive, setup
from app.services import bootstrap
from app.services.audit_log import events_as_text, serialize_event
from app.services.setup_storage import CONTAINER_ARCHIVE_PATH, SELECTION_FILE
from app.services.media_tokens import create_media_token, validate_media_token
from app.services.security_audit import reset_security_audit_throttle


class FakeRequest:
    method = "GET"
    headers = {
        "user-agent": "stage3-test",
        "authorization": "Bearer should-not-be-logged",
        "cookie": "session=stage3-cookie-secret",
    }
    client = SimpleNamespace(host="127.0.0.1")
    url = SimpleNamespace(path="/settings")
    scope = {"route": SimpleNamespace(path="/settings")}


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage3_audit_events_")
    original_storage_root = settings.storage_root
    original_control = settings.storage_install_control
    settings.storage_root = str(Path(tmp.name) / "archive")
    settings.storage_install_control = str(Path(tmp.name) / "install-control")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    reset_security_audit_throttle()
    try:
        yield session
    finally:
        reset_security_audit_throttle()
        session.close()
        settings.storage_root = original_storage_root
        settings.storage_install_control = original_control
        tmp.cleanup()


def add_user(db, username="stage3_user", role=ROLE_OWNER, active=True):
    user = User(username=username, full_name=username, password_hash="hash", role=role, is_active=active)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def write_setup_storage_selection() -> None:
    selected = str(Path(settings.storage_install_control).parent / "host-archive")
    control = Path(settings.storage_install_control)
    control.mkdir(parents=True, exist_ok=True)
    (control / SELECTION_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selected_host_path": selected,
                "selected_mount_path": str(Path(selected).parent),
                "folder_name": Path(selected).name,
                "container_archive_path": CONTAINER_ARCHIVE_PATH,
                "candidate_id": "stage3-audit",
                "selected_at": "2026-05-07T00:00:00Z",
                "apply_status": "active",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def audit_events(db):
    return db.query(AuditEvent).order_by(AuditEvent.created_at.asc()).all()


def rendered_events(events):
    return json.dumps([serialize_event(event) for event in events], ensure_ascii=False) + events_as_text(events)


def test_permission_denied_and_unauthenticated_denied_are_audited_and_throttled(db):
    viewer = add_user(db, role=ROLE_VIEWER)
    dependency = require_permission("manage_settings")

    with pytest.raises(HTTPException) as exc:
        dependency(viewer, request=FakeRequest(), db=db)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        dependency(viewer, request=FakeRequest(), db=db)

    with pytest.raises(HTTPException) as exc:
        get_current_user(request=FakeRequest(), credentials=None, db=db)
    assert exc.value.status_code == 401

    events = audit_events(db)
    assert [event.event_type for event in events] == [
        "security.permission_denied",
        "security.unauthenticated_access_denied",
    ]
    assert all(event.category == "security" for event in events)
    assert events[0].event_metadata["required_permission"] == "manage_settings"
    assert events[0].event_metadata["route"] == "/settings"
    rendered = rendered_events(events)
    assert "should-not-be-logged" not in rendered
    assert "Bearer should-not-be-logged" not in rendered
    assert "stage3-cookie-secret" not in rendered
    assert "raw_body" not in rendered


def test_low_value_health_and_successful_media_token_paths_do_not_emit_security_audit(db):
    user = add_user(db, role=ROLE_OWNER)
    token, _expires = create_media_token(user=user, scope="live", resource={"camera_id": 1, "stream": "main"})

    assert health() == {"status": "ok"}
    assert validate_media_token(
        db,
        token=token,
        scope="live",
        resource={"camera_id": 1, "stream": "main"},
        permission="view_live",
        request=FakeRequest(),
        media_area="live",
    ) == user
    assert audit_events(db) == []


def test_successful_permission_check_does_not_emit_audit_event(db):
    owner = add_user(db, role=ROLE_OWNER)
    dependency = require_permission("manage_settings")

    assert dependency(owner, request=FakeRequest(), db=db) == owner
    assert audit_events(db) == []


def test_media_token_denials_are_audited_without_token_values(db):
    user = add_user(db, role=ROLE_OWNER)
    viewer = add_user(db, username="stage3_viewer", role=ROLE_VIEWER)
    token, _expires = create_media_token(user=user, scope="live", resource={"camera_id": 1, "stream": "main"})
    viewer_token, _ = create_media_token(user=viewer, scope="live", resource={"camera_id": 1, "stream": "main"})
    expired = jwt.encode(
        {
            "typ": "media",
            "sub": user.username,
            "scope": "live",
            "resource": {"camera_id": 1, "stream": "main"},
            "exp": int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    invalid_signature = jwt.encode(
        {
            "typ": "media",
            "sub": user.username,
            "scope": "live",
            "resource": {"camera_id": 1, "stream": "main"},
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
        },
        "wrong-stage3-secret",
        algorithm="HS256",
    )
    ghost_user_token = jwt.encode(
        {
            "typ": "media",
            "sub": "missing_stage3_user",
            "scope": "live",
            "resource": {"camera_id": 1, "stream": "main"},
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    cases = [
        (None, "missing", 401, "live", {"camera_id": 1, "stream": "main"}, "view_live"),
        (expired, "expired", 401, "live", {"camera_id": 1, "stream": "main"}, "view_live"),
        (token, "wrong_scope", 403, "recording", {"camera_id": 1, "stream": "main"}, "view_live"),
        (token, "forbidden_target", 403, "live", {"camera_id": 2, "stream": "main"}, "view_live"),
        ("not-a-jwt", "malformed", 401, "live", {"camera_id": 1, "stream": "main"}, "view_live"),
        (invalid_signature, "invalid_signature", 401, "live", {"camera_id": 1, "stream": "main"}, "view_live"),
        (ghost_user_token, "user_not_found_or_inactive", 401, "live", {"camera_id": 1, "stream": "main"}, "view_live"),
        (viewer_token, "missing_permission", 403, "live", {"camera_id": 1, "stream": "main"}, "view_recordings"),
    ]
    for value, reason, status_code, scope, resource, permission in cases:
        with pytest.raises(HTTPException) as exc:
            validate_media_token(
                db,
                token=value,
                scope=scope,
                resource=resource,
                permission=permission,
                request=FakeRequest(),
                media_area="live",
            )
        assert exc.value.status_code == status_code

    events = audit_events(db)
    reasons = [event.event_metadata["reason"] for event in events]
    assert reasons == [
        "missing",
        "expired",
        "wrong_scope",
        "forbidden_target",
        "malformed",
        "invalid_signature",
        "user_not_found_or_inactive",
        "missing_permission",
    ]
    assert all(event.event_type == "security.media_token_denied" for event in events)
    assert all(event.category == "security" for event in events)
    assert all("resource_keys" in event.event_metadata for event in events)
    rendered = rendered_events(events)
    for forbidden in (token, expired, invalid_signature, ghost_user_token, viewer_token, "not-a-jwt"):
        assert forbidden not in rendered
    assert "stage3-cookie-secret" not in rendered
    assert "Bearer should-not-be-logged" not in rendered


def test_setup_completed_and_failed_events_are_safe(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    db.commit()
    payload = SetupRequest(
        username="owner",
        password="plain-password-secret",
        password_confirm="plain-password-secret",
        timezone="UTC",
        language="ru",
        storage_path="/requested",
        recording_format="mkv",
    )
    with pytest.raises(HTTPException):
        setup(payload, db=db, request=FakeRequest())
    failed = audit_events(db)[0]
    assert failed.event_type == "system.setup_failed"
    assert failed.event_metadata["reason"] == "already_initialized"

    db.query(AuditEvent).delete()
    db.query(SystemSettings).delete()
    db.commit()
    write_setup_storage_selection()
    response = setup(payload, db=db, request=FakeRequest())
    assert response["ok"] is True
    completed = audit_events(db)[0]
    assert completed.event_type == "system.setup_completed"
    assert completed.category == "system"
    assert completed.event_metadata["current_state"]["system_initialized"] is True
    rendered = rendered_events([completed])
    assert "plain-password-secret" not in rendered
    assert "password_hash" not in rendered


def test_owner_fallback_promotion_emits_system_audit_event(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    user = add_user(db, username="fallback_user", role=ROLE_VIEWER)

    bootstrap.ensure_owner_migration(db)

    db.refresh(user)
    assert user.role == ROLE_OWNER
    event = db.query(AuditEvent).filter(AuditEvent.event_type == "system.owner_fallback_promoted").one()
    assert event.category == "system"
    assert event.severity == "security"
    assert event.target_id == str(user.id)
    assert event.event_metadata["previous_role"] == ROLE_VIEWER
    assert event.event_metadata["new_role"] == ROLE_OWNER


def test_bug_report_events_are_separate_and_redact_failures(db, monkeypatch):
    owner = add_user(db, role=ROLE_OWNER)
    payload = BugReportRequest(text="problem with Authorization: Bearer raw-token", include_logs=False)

    monkeypatch.setattr("app.routers.settings.build_log_archive", lambda **kwargs: io.BytesIO(b"zip"))
    response = create_bug_report(payload, request=FakeRequest(), db=db, current_user=owner)
    assert response.media_type == "application/zip"
    assert [event.event_type for event in audit_events(db)] == [
        "diagnostics.bug_report_requested",
        "diagnostics.bug_report_created",
    ]
    assert all(event.category == "diagnostics" for event in audit_events(db))
    rendered = rendered_events(audit_events(db))
    assert "raw-token" not in rendered

    db.query(AuditEvent).delete()
    db.commit()

    def fail_archive(**_kwargs):
        raise RuntimeError("Authorization: Bearer diagnostic-secret")

    monkeypatch.setattr("app.routers.settings.build_log_archive", fail_archive)
    with pytest.raises(RuntimeError):
        create_bug_report(payload, request=FakeRequest(), db=db, current_user=owner)
    events = audit_events(db)
    assert [event.event_type for event in events] == [
        "diagnostics.bug_report_requested",
        "diagnostics.bug_report_failed",
    ]
    rendered = rendered_events(events)
    assert "diagnostic-secret" not in rendered


def test_diagnostic_archive_events_cover_requested_created_and_failed_without_secret_leakage(db, monkeypatch):
    owner = add_user(db, role=ROLE_OWNER)

    monkeypatch.setattr("app.routers.settings.build_log_archive", lambda **kwargs: io.BytesIO(b"zip"))
    response = download_log_archive(request=FakeRequest(), mode="normal", db=db, current_user=owner)
    assert response.media_type == "application/zip"
    events = audit_events(db)
    assert [event.event_type for event in events] == [
        "diagnostics.archive_requested",
        "diagnostics.archive_created",
    ]
    assert all(event.category == "diagnostics" for event in events)
    assert events[0].event_metadata["mode"] == "normal"
    assert events[1].event_metadata["mode"] == "normal"
    rendered = rendered_events(events)
    assert "stage3-cookie-secret" not in rendered
    assert "Bearer should-not-be-logged" not in rendered

    db.query(AuditEvent).delete()
    db.commit()

    def fail_archive(**_kwargs):
        raise RuntimeError("Authorization: Bearer archive-secret")

    monkeypatch.setattr("app.routers.settings.build_log_archive", fail_archive)
    with pytest.raises(RuntimeError):
        download_log_archive(request=FakeRequest(), mode="extended", db=db, current_user=owner)
    events = audit_events(db)
    assert [event.event_type for event in events] == [
        "diagnostics.archive_requested",
        "diagnostics.archive_failed",
    ]
    assert events[1].event_metadata["mode"] == "extended"
    assert events[1].event_metadata["error_type"] == "RuntimeError"
    rendered = rendered_events(events)
    assert "archive-secret" not in rendered
    assert "Bearer archive-secret" not in rendered
