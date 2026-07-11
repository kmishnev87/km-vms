import json
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.permissions import ROLE_OWNER
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers.audit import audit_events
from app.routers import settings as settings_router
from app.services.audit_log import create_event


def call_audit_events(db, current_user, **filters):
    params = {
        "limit": 50,
        "offset": 0,
        "category": None,
        "severity": None,
        "event_type": None,
        "actor": None,
        "target": None,
        "target_type": None,
        "target_id": None,
        "date_from": None,
        "date_to": None,
        "since_minutes": None,
        "q": None,
        "db": db,
        "current_user": current_user,
    }
    params.update(filters)
    return audit_events(**params)


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage4_audit_events_")
    original_storage_root = settings.storage_root
    settings.storage_root = str(Path(tmp.name) / "archive")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        tmp.cleanup()


def add_user(db, username="stage4_owner", role=ROLE_OWNER):
    user = User(username=username, full_name=username, password_hash="hash", role=role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def add_event(db, actor, *, event_type, category, severity="info", target_type=None, target_id=None, minutes_ago=0, metadata=None):
    event = create_event(
        db=db,
        actor=actor,
        category=category,
        event_type=event_type,
        severity=severity,
        message_ru=f"{event_type} message",
        message_en=f"{event_type} message",
        target_type=target_type,
        target_id=target_id,
        target_name=f"{target_type}-{target_id}" if target_type and target_id else None,
        metadata=metadata or {},
    )
    event.created_at = datetime.utcnow() - timedelta(minutes=minutes_ago)
    db.commit()
    db.refresh(event)
    return event


def test_audit_events_filters_are_bounded_ordered_and_secret_safe(db):
    owner = add_user(db)
    add_event(db, owner, event_type="storage.unavailable", category="storage", severity="warning", target_type="storage", target_id="root", minutes_ago=20)
    security_event = add_event(
        db,
        owner,
        event_type="security.media_token_denied",
        category="security",
        severity="warning",
        target_type="media",
        target_id="live",
        metadata={
            "Authorization": "Bearer stage4-secret-token",
            "cookie": "session=stage4-cookie-secret",
            "url": "rtsp://user:pass@example/live?media_token=stage4-media-secret",
        },
    )
    add_event(db, owner, event_type="diagnostics.archive_created", category="diagnostics", severity="info", target_type="diagnostic_archive", target_id="normal")

    result = call_audit_events(db, owner, category="security", severity="warning")
    assert [item["event_type"] for item in result["items"]] == ["security.media_token_denied"]
    rendered = json.dumps(result, ensure_ascii=False)
    assert "stage4-secret-token" not in rendered
    assert "stage4-cookie-secret" not in rendered
    assert "stage4-media-secret" not in rendered
    assert "pass@example" not in rendered
    assert result["items"][0]["metadata"]["Authorization"] == "***"

    assert result["items"][0]["id"] == security_event.id
    assert call_audit_events(db, owner, target="diagnostic_archive")["count"] == 1
    assert call_audit_events(db, owner, target_type="diagnostic_archive", target_id="normal")["count"] == 1
    assert call_audit_events(db, owner, actor="stage4_owner")["count"] == 3
    assert call_audit_events(db, owner, q="archive_created")["count"] == 1
    assert call_audit_events(db, owner, since_minutes=5)["count"] == 2
    assert call_audit_events(db, owner, limit=2, offset=1)["limit"] == 2


def test_audit_events_invalid_filters_are_safe(db):
    owner = add_user(db)
    with pytest.raises(HTTPException) as category_error:
        call_audit_events(db, owner, category="not-a-category")
    assert category_error.value.status_code == 422
    assert "not-a-category" not in str(category_error.value.detail)

    with pytest.raises(HTTPException) as date_error:
        call_audit_events(db, owner, date_from="not-a-date")
    assert date_error.value.status_code == 422


def test_diagnostic_archive_contains_audit_summary_and_redaction_proof(db, monkeypatch):
    owner = add_user(db)
    db.add(SystemSettings(id=1, system_initialized=True))
    db.commit()
    add_event(
        db,
        owner,
        event_type="security.permission_denied",
        category="security",
        severity="warning",
        metadata={
            "Authorization": "Bearer stage4-archive-secret",
            "request_body": "stage4-raw-body-secret",
            "rtsp_url": "rtsp://user:pass@example/live",
        },
    )

    monkeypatch.setattr(settings_router, "storage_diagnostics", lambda: {"status": "ok"})
    monkeypatch.setattr(settings_router, "build_storage_monitoring_summary", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "reconciliation_diagnostics", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "retention_diagnostics", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "build_recorder_archive_payloads", lambda db: {})
    monkeypatch.setattr(settings_router, "get_hardware_capabilities", lambda: {"available_backends": []})
    monkeypatch.setattr(settings_router, "camera_diagnostics", lambda db: [])
    monkeypatch.setattr(settings_router.live_manager, "status", lambda: [])
    monkeypatch.setattr(settings_router.live_manager, "debug", lambda: {})
    monkeypatch.setattr(settings_router, "recordings_diagnostics", lambda db: {"count": 0})
    monkeypatch.setattr(settings_router, "chronology_diagnostics", lambda db: {"items": []})

    archive = settings_router.build_log_archive(db, mode="extended", report_text="Authorization: Bearer stage4-report-secret", include_logs=False)
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert "audit/summary.json" in names
        assert "audit/redaction_proof.json" in names
        summary = json.loads(bundle.read("audit/summary.json").decode("utf-8"))
        proof = json.loads(bundle.read("audit/redaction_proof.json").decode("utf-8"))
        rendered = "\n".join(bundle.read(name).decode("utf-8", errors="replace") for name in names if name.startswith(("audit/", "bug-report")))

    assert summary["by_category"]["security"] == 1
    assert summary["by_severity"]["warning"] == 1
    assert summary["recent_security_warning_error"][0]["event_type"] == "security.permission_denied"
    assert proof["status"] == "PASS"
    for forbidden in ("stage4-archive-secret", "stage4-report-secret", "stage4-raw-body-secret", "pass@example"):
        assert forbidden not in rendered
    assert "Bearer ***" in rendered
