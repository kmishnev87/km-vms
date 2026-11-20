from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base, get_db
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.storage_operation import StorageWorkSignal
from app.routers import camera_archive_rules as rules
from app.routers.deps import get_current_user


@pytest.fixture
def context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rules.sqlite'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    rows = [Camera(name=name, storage_folder_name=name, enabled=False, protocol="rtsp", host="192.168.10.5", port=554,
                   rtsp_main_url="/stream", segment_minutes=5, retention_days=30, storage_quota_gb=50, events_enabled=True)
            for name in ("one", "two", "archived")]
    rows[2].deleted_at = datetime.utcnow()
    session.add_all(rows); session.commit()
    yield SimpleNamespace(db=session, rows=rows, actor=SimpleNamespace(id=7, username="owner", role="owner", is_active=True), request=SimpleNamespace(headers={}, client=None))
    session.close(); engine.dispose()


def change(row, **values):
    return {"camera_id": row.id, "expected": rules.rule_values(row), "values": values}


def save(ctx, changes):
    return rules.save_archive_rules(rules.RuleChanges(changes=changes), ctx.request, ctx.db, ctx.actor)


def test_catalog_is_policy_only_and_includes_disabled_not_deleted(context):
    catalog = rules.list_archive_rules(context.db, context.actor)
    assert len(catalog["cameras"]) == 2
    assert all(set(row) == {"camera_id", "name", "enabled", rules.ROOT_FIELD, *rules.FIELDS} for row in catalog["cameras"])
    assert not any(row["enabled"] for row in catalog["cameras"])


def test_batch_persists_exact_fields_audit_and_one_retention_signal(context):
    one, two, _ = context.rows
    original = (one.enabled, one.events_enabled, one.rtsp_main_url, one.protocol)
    result = save(context, [change(one, segment_minutes=10, retention_days=45), change(two, storage_quota_gb=80)])
    assert result["updated_camera_ids"] == [one.id, two.id]
    assert (one.segment_minutes, one.retention_days, two.storage_quota_gb) == (10, 45, 80)
    assert (one.enabled, one.events_enabled, one.rtsp_main_url, one.protocol) == original
    assert one.retention_policy_version == 2 and two.retention_policy_version == 2
    assert context.db.query(AuditEvent).count() == 2
    assert context.db.query(StorageWorkSignal).count() == 1


def test_segment_only_change_does_not_publish_retention_work(context):
    one = context.rows[0]
    save(context, [change(one, segment_minutes=15)])
    assert one.retention_policy_version == 1
    assert context.db.query(StorageWorkSignal).count() == 0


def test_concurrent_change_rejects_entire_batch(context):
    one, two, _ = context.rows
    changes = [change(one, retention_days=40), change(two, retention_days=40)]
    two.storage_quota_gb = 100; context.db.commit()
    with pytest.raises(HTTPException) as failure:
        save(context, changes)
    assert failure.value.status_code == 409
    context.db.refresh(one); context.db.refresh(two)
    assert one.retention_days == two.retention_days == 30
    assert context.db.query(AuditEvent).count() == 0


def test_deleted_camera_rejects_entire_batch(context):
    one, _, archived = context.rows
    with pytest.raises(HTTPException) as failure:
        save(context, [change(one, retention_days=40), change(archived, retention_days=40)])
    assert failure.value.status_code == 409
    context.db.refresh(one); assert one.retention_days == 30


def test_duplicate_ids_rejected_and_replay_is_noop(context):
    one = context.rows[0]
    item = change(one, retention_days=40)
    with pytest.raises(HTTPException): save(context, [item, item])
    assert save(context, [item])["updated_camera_ids"] == [one.id]
    assert save(context, [item])["updated_camera_ids"] == []
    assert context.db.query(AuditEvent).count() == 1


def test_audit_failure_rolls_back_all_rules(context, monkeypatch):
    one, two, _ = context.rows
    monkeypatch.setattr(rules, "create_event", lambda **kwargs: None)
    with pytest.raises(RuntimeError): save(context, [change(one, retention_days=40), change(two, retention_days=40)])
    context.db.refresh(one); context.db.refresh(two)
    assert one.retention_days == two.retention_days == 30


@pytest.mark.parametrize("values", [{}, {"retention_days": None}, {"retention_days": 0}, {"retention_days": 36501}, {"storage_quota_gb": True}, {"segment_minutes": 1.2}, {"enabled": True}, {"events_enabled": False}, {"password": "not-accepted"}])
def test_batch_never_accepts_invalid_or_unrelated_fields(context, values):
    with pytest.raises(ValidationError): rules.RuleChanges(changes=[change(context.rows[0], **values)])


@pytest.mark.parametrize("role,allowed", [("owner", True), ("admin", True), ("operator", False), ("viewer", False)])
def test_rules_api_permissions(context, role, allowed):
    app = FastAPI(); app.include_router(rules.router, prefix="/cameras")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=7, username="test", role=role, is_active=True)
    app.dependency_overrides[get_db] = lambda: context.db
    with TestClient(app) as client:
        response = client.get("/cameras/archive-rules")
    assert response.status_code == (200 if allowed else 403)
