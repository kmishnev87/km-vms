import json
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.services.audit_log import create_event, events_as_text, serialize_event
from app.services.recording_reconciliation import reconcile_recordings
from app.services.recording_retention import build_retention_plan, run_automatic_retention_once, run_retention
from app.services.storage_monitoring import build_storage_monitoring_summary, reset_storage_audit_state
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
)


def actor(role="owner"):
    return SimpleNamespace(id=1, username=f"{role}_user", role=role, is_active=True)


def make_db():
    tmp = tempfile.TemporaryDirectory(prefix="stage2_audit_events_")
    tmp_path = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    original_storage_exports = settings.storage_exports
    settings.storage_root = str(tmp_path / "archive")
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    return session, tmp, original_storage_root, original_storage_previews, original_storage_exports


def close_db(session, tmp, original_storage_root, original_storage_previews, original_storage_exports):
    session.close()
    settings.storage_root = original_storage_root
    settings.storage_previews = original_storage_previews
    settings.storage_exports = original_storage_exports
    tmp.cleanup()


def add_retention_camera(db, *, name="stage2_audit_retention_camera", retention_days=1, storage_quota_gb=1):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="user",
        password_encrypted=None,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=retention_days,
        storage_quota_gb=storage_quota_gb,
        status="created",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def add_retention_segment(
    db,
    camera,
    *,
    name,
    days_ago=2,
    status="finalized",
    ownership="KM VMS",
    source="recorder",
    integrity_status=None,
    job_id=None,
):
    ensure_archive_roots(db)
    rel_path = f"kmvms/recordings/{camera.storage_folder_name}/{name}.mkv"
    file_path = Path(settings.storage_root) / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"stage2")
    started_at = datetime.utcnow() - timedelta(days=days_ago)
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id=job_id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(file_path),
        relative_path=rel_path,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=60),
        duration_sec=60,
        size_bytes=file_path.stat().st_size,
        stream_type="main",
        status=status,
        ownership=ownership,
        source=source,
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=datetime.utcnow(),
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status=integrity_status,
        finalized_at=started_at + timedelta(seconds=60) if status == "finalized" else None,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def test_stage2_categories_are_first_class_and_unknown_fallback_is_safe():
    db, tmp, old_root, old_previews, old_exports = make_db()
    try:
        for category in ["recorder", "storage", "retention", "reconciliation", "auth", "records", "system"]:
            create_event(
                db=db,
                category=category,
                event_type=f"{category}.contract",
                message_ru=f"{category} event",
                metadata={"safe": True},
            )
        create_event(db=db, category="unknown-stage2", event_type="unknown.contract", message_ru="unknown")

        categories = [event.category for event in db.query(AuditEvent).order_by(AuditEvent.created_at.asc()).all()]
        assert "recorder" in categories
        assert "storage" in categories
        assert "retention" in categories
        assert "reconciliation" in categories
        assert "auth" in categories
        assert categories[-1] == "system"
    finally:
        close_db(db, tmp, old_root, old_previews, old_exports)


def test_storage_status_audits_transitions_without_poll_spam():
    db, tmp, old_root, old_previews, old_exports = make_db()
    reset_storage_audit_state()
    try:
        build_storage_monitoring_summary(db, write_audit=True, audit_actor=actor())
        build_storage_monitoring_summary(db, write_audit=True, audit_actor=actor())
        Path(settings.storage_root, "kmvms", "recordings").mkdir(parents=True)
        build_storage_monitoring_summary(db, write_audit=True, audit_actor=actor())

        events = db.query(AuditEvent).order_by(AuditEvent.created_at.asc()).all()
        assert [event.event_type for event in events] == ["storage.unavailable", "storage.available"]
        assert all(event.category == "storage" for event in events)
        assert events[0].event_metadata["current_status"] == "unavailable"
        assert events[1].event_metadata["previous_status"] == "unavailable"
    finally:
        reset_storage_audit_state()
        close_db(db, tmp, old_root, old_previews, old_exports)


def test_retention_dry_run_apply_and_auto_emit_safe_summary_events():
    db, tmp, old_root, old_previews, old_exports = make_db()
    try:
        Path(settings.storage_root).mkdir(parents=True)
        build_retention_plan(db, actor=actor(), write_audit=True)
        run_retention(db, actor=actor(), max_candidates=5, max_bytes=1024)
        run_automatic_retention_once(db, max_candidates=5, max_bytes=1024)

        events = db.query(AuditEvent).order_by(AuditEvent.created_at.asc()).all()
        event_types = [event.event_type for event in events]
        assert "retention.dry_run_started" in event_types
        assert "retention.dry_run_completed" in event_types
        assert "retention.apply_started" in event_types
        assert "retention.apply_completed" in event_types
        assert "retention.auto_run_started" in event_types
        assert "retention.auto_run_completed" in event_types
        assert all(event.category == "retention" for event in events)
        assert not any(event.event_type == "retention.deleted_segment" for event in events)
        dry_run_completed = next(event for event in events if event.event_type == "retention.dry_run_completed")
        assert "observability" in dry_run_completed.event_metadata
        assert "foreign_or_unowned_count" in dry_run_completed.event_metadata["observability"]
        apply_completed = next(event for event in events if event.event_type == "retention.apply_completed")
        assert "skipped_reason_counts" in apply_completed.event_metadata
        assert "failed_reason_counts" in apply_completed.event_metadata
        for event in events:
            text = json.dumps(serialize_event(event), ensure_ascii=False)
            assert str(settings.storage_root) not in text
    finally:
        close_db(db, tmp, old_root, old_previews, old_exports)


def test_retention_summary_includes_available_observability_and_skipped_reason_counts():
    db, tmp, old_root, old_previews, old_exports = make_db()
    try:
        Path(settings.storage_root).mkdir(parents=True)
        camera = add_retention_camera(db)
        active_job = RecordingJob(
            id="stage2_audit_active_job",
            camera_id=camera.id,
            state="recording",
            started_at=datetime.utcnow(),
        )
        db.add(active_job)
        db.commit()
        add_retention_segment(db, camera, name="eligible_old")
        add_retention_segment(db, camera, name="foreign_old", ownership="Foreign")
        add_retention_segment(db, camera, name="active_old", job_id=active_job.id)
        add_retention_segment(db, camera, name="problem_old", integrity_status="missing_file")

        build_retention_plan(db, actor=actor(), write_audit=True)
        run_retention(db, actor=actor(), max_candidates=10, max_bytes=1)

        dry_run = (
            db.query(AuditEvent)
            .filter(AuditEvent.event_type == "retention.dry_run_completed")
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        apply_blocked = (
            db.query(AuditEvent)
            .filter(AuditEvent.event_type == "retention.apply_blocked")
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        assert dry_run.event_metadata["observability"]["foreign_or_unowned_count"] == 1
        assert dry_run.event_metadata["observability"]["active_or_writing_count"] >= 1
        assert dry_run.event_metadata["observability"]["integrity_problem_count"] >= 1
        assert apply_blocked.event_metadata["skipped_reason_counts"]["limit_exceeded"] == 2
        assert "items" not in apply_blocked.event_metadata
        assert "relative_path" not in json.dumps(apply_blocked.event_metadata, ensure_ascii=False)
    finally:
        close_db(db, tmp, old_root, old_previews, old_exports)


def test_reconciliation_scan_and_apply_safe_emit_bounded_safe_summary_events():
    db, tmp, old_root, old_previews, old_exports = make_db()
    try:
        Path(settings.storage_root).mkdir(parents=True)
        reconcile_recordings(db, mode="dry_run", actor=actor(), write_audit=True)
        reconcile_recordings(db, mode="apply_safe", actor=actor(), write_audit=True)

        events = db.query(AuditEvent).order_by(AuditEvent.created_at.asc()).all()
        event_types = [event.event_type for event in events]
        assert "reconciliation.scan_started" in event_types
        assert "reconciliation.scan_completed" in event_types
        assert "reconciliation.apply_started" in event_types
        assert "reconciliation.apply_completed" in event_types
        assert all(event.category == "reconciliation" for event in events)
        completed = [event for event in events if event.event_type.endswith("_completed")]
        assert completed
        assert all("samples" not in (event.event_metadata or {}) for event in completed)
        assert all(event.event_metadata["deleted_files_count"] == 0 for event in completed)
        assert all(event.event_metadata["deleted_product_metadata_count"] == 0 for event in completed)
    finally:
        close_db(db, tmp, old_root, old_previews, old_exports)


def test_stage2_audit_export_redacts_tokens_credentials_and_raw_ffmpeg_command_text():
    db, tmp, old_root, old_previews, old_exports = make_db()
    bearer_value = "real" + "-token"
    rtsp_pass = "camera" + "-pass"
    query_key = "media_" + "token"
    sensitive_plain = "plain" + "-secret"
    cookie_value = "session=" + "secret"
    rtsp_url = "rtsp://" + "admin" + ":" + rtsp_pass + "@host/live"
    try:
        event = create_event(
            db=db,
            category="recorder",
            event_type="recorder.ffmpeg_crashed",
            severity="error",
            message_ru=f"Authorization: Bearer {bearer_value} {rtsp_url}",
            metadata={
                "ffmpeg_command": f"ffmpeg -i {rtsp_url}?{query_key}=abc",
                "cookie": cookie_value,
                "password": sensitive_plain,
                "safe_reason": "process_crashed",
            },
        )
        serialized = json.dumps(serialize_event(event), ensure_ascii=False)
        exported = events_as_text([event])

        forbidden = [bearer_value, rtsp_pass, f"{query_key}=abc", sensitive_plain, cookie_value]
        assert not any(value in serialized for value in forbidden)
        assert not any(value in exported for value in forbidden)
        assert "Bearer ***" in serialized
        assert "rtsp://admin:***@host/live" in serialized
    finally:
        close_db(db, tmp, old_root, old_previews, old_exports)


def _recorder_audit_insert_columns_and_values(text: str) -> tuple[list[str], list[str]]:
    match = re.search(
        r"INSERT\s+INTO\s+audit_events\s*\((?P<columns>.*?)\)\s*VALUES\s*\(",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match, "recorder audit INSERT block not found"
    columns = [item.strip() for item in match.group("columns").split(",")]
    depth = 1
    value_chars = []
    for char in text[match.end() :]:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        value_chars.append(char)
    assert depth == 0, "recorder audit VALUES block is not balanced"
    values = []
    current = []
    depth = 0
    for char in "".join(value_chars):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            values.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        values.append("".join(current).strip())
    assert len(columns) == len(values)
    return columns, values


def test_recorder_direct_sql_maps_category_column_to_recorder_not_actor_fields():
    candidates = [
        Path(__file__).resolve().parents[2] / "recorder" / "main.py",
        Path("/app/recorder/main.py"),
    ]
    recorder_main = next((path for path in candidates if path.exists()), None)
    if recorder_main is None:
        pytest.skip("recorder source is outside the api test image")
    text = recorder_main.read_text(encoding="utf-8")
    columns, values = _recorder_audit_insert_columns_and_values(text)
    mapping = dict(zip(columns, values))

    assert mapping["actor_username"] == "'recorder'"
    assert mapping["actor_role"] == "'system'"
    assert mapping["category"] == "'recorder'"
    assert mapping["category"] != "'system'"
    old_bug_values = dict(mapping)
    old_bug_values["category"] = "'system'"
    assert old_bug_values["category"] != "'recorder'"
    assert "event_type=\"backoff_entered\"" in text
    assert "event_type=\"ffmpeg_start_failed\"" in text
    assert "event_type=\"camera_started\"" in text
    assert "event_type=\"recovery_succeeded\"" in text
    assert "event_type=\"stale_writing_detected\"" in text
    assert "metadata={\"pid\": job.pid, \"state\": job.state, \"output_pattern\"" not in text
