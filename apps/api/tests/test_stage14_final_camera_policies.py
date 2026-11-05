"""F02/F03: bounded writes and isolated legacy retention failures."""
from datetime import datetime
import os

import pytest
from pydantic import ValidationError

from app.core.camera_limits import MAX_RETENTION_DAYS
from app.schemas.camera import CameraCreate, CameraUpdate
from app.services import retention_automation as retention
from app.services.recording_retention import _policy_candidates, build_retention_plan, run_retention
from app.services import system_runtime_status
from app.services.timezone_contract import timezone_context
from test_stage14_history_large_totals_postgres import history_database
from test_stage11_retention_automation import db, add_camera, add_segment, camera_payload


@pytest.mark.parametrize("days", [1, MAX_RETENTION_DAYS])
def test_supported_retention_boundaries_create_update(days):
    assert camera_payload(retention_days=days).retention_days == days
    assert CameraUpdate(retention_days=days).retention_days == days


@pytest.mark.parametrize("days", [0, -1, MAX_RETENTION_DAYS + 1, 10000000, None])
def test_invalid_retention_rejected_before_persistence(days):
    with pytest.raises(ValidationError):
        camera_payload(retention_days=days)
    with pytest.raises(ValidationError):
        CameraUpdate(retention_days=days)
    assert CameraUpdate().model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize("character", ["a", "К", "🎥"])
def test_name_bounds_count_unicode_code_points(character):
    for size in [1, 255]:
        assert CameraUpdate(name=character * size).name == character * size
        assert camera_payload(name=character * size).name == character * size
    with pytest.raises(ValidationError):
        CameraUpdate(name=character * 256)
    with pytest.raises(ValidationError):
        camera_payload(name=character * 256)


@pytest.mark.parametrize("name", ["", None])
def test_explicit_empty_name_is_not_missing(name):
    with pytest.raises(ValidationError):
        CameraUpdate(name=name)
    assert "name" not in CameraUpdate(enabled=False).model_dump(exclude_unset=True)


@pytest.mark.parametrize("bad_first", [True, False])
def test_legacy_invalid_retention_does_not_break_other_cameras_or_status(db, bad_first):
    values = [("bad", 10000000), ("good", 1)]
    if not bad_first:
        values.reverse()
    cameras = {name: add_camera(db, name=name, retention_days=days) for name, days in values}
    segments = {name: add_segment(db, camera, name=f"{name}-segment", days_ago=3)[0]
                for name, camera in cameras.items()}
    due = retention.earliest_retention_due_at(db)
    assert due is not None and due < datetime.utcnow()
    snapshot = retention.camera_policy_snapshot(cameras["bad"], signal_watermark=1,
        high_watermark=max(row.id for row in segments.values()), evaluation_at=datetime.utcnow())
    measurement = retention.measure_camera_retention(db, snapshot)
    assert measurement["invalid_age_rule"] is True
    assert measurement["_cutoff_storage"] is None
    assert measurement["age_due_count"] == 0
    selected = retention.select_camera_retention_candidates(db, snapshot, measurement, limit=25)
    assert selected["segments"] == []
    assert selected["blocker_counts"]["retention_days_invalid"] == 1
    status = retention.retention_runtime_status(db)
    problem = next(item for item in status["problem_cameras"] if item["camera_id"] == cameras["bad"].id)
    assert problem["invalid_age_rule"] is True
    candidates, summary = _policy_candidates(db)
    assert {item.id for item, _reason in candidates} == {segments["good"].id}
    assert summary["days"][str(cameras["bad"].id)]["status"] == "invalid"
    assert db.get(type(cameras["bad"]), cameras["bad"].id).retention_days == 10000000


def test_due_date_overflow_isolated_without_changing_policy(db):
    assert retention._segment_due_at(datetime(9999, 12, 20), 30, db) is None
    assert retention._segment_due_at(datetime(9999, 12, 31, 23, 30), 1, db) is None


@pytest.mark.parametrize("mixed", [False, True])
def test_manual_plan_and_apply_do_not_report_invalid_policy_as_completed(db, mixed):
    bad = add_camera(db, name="bad-policy", retention_days=10000000)
    _bad_segment, bad_path = add_segment(db, bad, name="bad", days_ago=3)
    good_path = None
    if mixed:
        good = add_camera(db, name="good-policy", retention_days=1)
        _good_segment, good_path = add_segment(db, good, name="good", days_ago=3)
    plan = build_retention_plan(db)
    assert plan["status"] == ("partial" if mixed else "blocked")
    assert not plan["ok"] and plan["skipped_count"] == 0
    assert plan["reason_counts"]["retention_days_invalid"] == 1
    result = run_retention(db, actor=None)
    assert result["status"] == ("partial" if mixed else "blocked")
    assert not result["ok"] and result["skipped_count"] == 0
    assert result["reason_counts"]["retention_days_invalid"] == 1
    assert "retention_days_invalid" in result["warnings"]
    assert bad_path.exists()
    if good_path is not None:
        assert result["deleted_count"] == 1 and not good_path.exists()


def test_invalid_policy_auto_operation_is_blocked_not_compliant(db):
    bad = add_camera(db, name="bad-policy", retention_days=10000000)
    segment, path = add_segment(db, bad, name="bad", days_ago=3)
    snapshot = retention.camera_policy_snapshot(bad, signal_watermark=1,
        high_watermark=segment.id, evaluation_at=datetime.utcnow())
    result = retention.run_camera_retention_operation(db, snapshot, page_size=25, should_preempt=lambda: False)
    assert result["status"] == "no_safe_candidate"
    assert result["measurement_confidence"] == "unknown"
    assert result["reason_counts"]["retention_days_invalid"] >= 1
    assert path.exists()


@pytest.mark.parametrize("last_status,measurement,expected", [
    ("completed", "unknown", "unknown"), ("completed", "violating", "warning"),
    ("failed", "unknown", "error"), ("completed", "compliant", "ok"),
])
def test_system_overview_uses_current_measurement_without_erasing_history(db, last_status, measurement, expected):
    state = {"last_status": last_status, "measurement_state": measurement, "enabled": True}
    domain = system_runtime_status._build_retention_domain(db, datetime.utcnow(), timezone_context(db),
        {"storage_operations": {"retention": state, "auto_free_space_cleanup": {"enabled": False}}})
    assert domain["severity"] == expected
    assert domain["last_status"] == last_status


@pytest.mark.skipif(not os.getenv("KMVMS_STAGE2_POSTGRES_URL"), reason="owned isolated PostgreSQL required")
def test_real_http_update_with_postgres_rejects_invalid_values_without_mutation(history_database):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from app.routers import cameras
    from app.routers.deps import get_current_user, get_db
    from app.models.user import User

    with Session(history_database) as session:
        row = add_camera(session, name="policy-http-original")
        user = User(username="synthetic-policy-owner", password_hash="unused-synthetic", role="owner")
        session.add(user)
        session.commit()
        app = FastAPI()
        app.include_router(cameras.router)
        app.dependency_overrides[get_db] = lambda: session
        app.dependency_overrides[get_current_user] = lambda: user
        client = TestClient(app)
        for payload in [{"name": "a" * 256}, {"name": ""}, {"name": None},
                        {"retention_days": 10000000}, {"retention_days": None}]:
            response = client.put(f"/cameras/{row.id}", json=payload)
            assert response.status_code == 422
            session.refresh(row)
            assert row.name == "policy-http-original" and row.retention_days == 1
        accepted = client.put(f"/cameras/{row.id}", json={"name": "🎥" * 255, "retention_days": MAX_RETENTION_DAYS})
        assert accepted.status_code == 200
        session.refresh(row)
        assert row.name == "🎥" * 255 and row.retention_days == MAX_RETENTION_DAYS
