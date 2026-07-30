import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.models.storage_operation import StorageOperation, StorageWorkSignal
from app.models.system_settings import SystemSettings
from app.routers import settings as settings_router
from app.routers.cameras import delete_camera, update_camera
from app.routers.settings import SettingsUpdateRequest, patch_settings
from app.schemas.camera import CameraUpdate
from app.services import automatic_retention as automatic_worker
from app.services import retention_automation as automation
from app.services.recording_retention import run_automatic_retention_once, run_retention
from app.services.schema_migrations import (
    MIGRATION_SOURCE,
    MigrationRegistry,
    STAGE4102_RETENTION_MIGRATION,
    execute_migration_plan,
)
from app.services.schema_versioning import CURRENT_BASELINE_ID, CURRENT_STATE_ID
from app.services.storage_operations_foundation import database_now
from app.services.system_settings import (
    AUTO_FREE_SPACE_TERMS_VERSION,
    get_system_settings,
    get_system_settings_read_only,
    serialize_settings,
)


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


def owner():
    return SimpleNamespace(id=41, username="stage4102_owner", role="owner", is_active=True)


@pytest.fixture
def stage4102(tmp_path, monkeypatch):
    original = {
        "storage_root": settings.storage_root,
        "storage_previews": settings.storage_previews,
        "storage_exports": settings.storage_exports,
    }
    archive = tmp_path / "archive-a"
    (archive / "kmvms" / "recordings").mkdir(parents=True)
    settings.storage_root = str(archive)
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")

    engine = create_engine(f"sqlite:///{tmp_path / 'stage4102.sqlite'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    system = SystemSettings(
        system_initialized=True,
        timezone="UTC",
        language="ru",
        storage_path=str(archive),
        recording_format="mkv",
        auto_free_space_cleanup_enabled=False,
        recording_suspended_by_low_disk=False,
    )
    root = ArchiveRoot(
        id="stage4102-root-a",
        label="Volume A",
        root_path=str(archive),
        storage_namespace="kmvms/recordings",
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="fs-stage4102-volume-a",
    )
    db.add_all([system, root])
    db.commit()
    try:
        yield SimpleNamespace(
            db=db,
            engine=engine,
            Session=Session,
            tmp_path=tmp_path,
            archive=archive,
            system=system,
            root=root,
            monkeypatch=monkeypatch,
        )
    finally:
        db.close()
        engine.dispose()
        settings.storage_root = original["storage_root"]
        settings.storage_previews = original["storage_previews"]
        settings.storage_exports = original["storage_exports"]


def add_camera(ctx, *, name="camera-a", enabled=False, days=30, quota=50, deleted=False):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=enabled,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="test",
        recording_mode="always",
        default_live_stream="sub",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=days,
        storage_quota_gb=quota,
        retention_policy_version=1,
        status="disabled",
        deleted_at=datetime.utcnow() if deleted else None,
    )
    ctx.db.add(camera)
    ctx.db.commit()
    ctx.db.refresh(camera)
    return camera


def add_root(ctx, *, root_id, identity, active=False):
    path = ctx.tmp_path / root_id
    (path / "kmvms" / "recordings").mkdir(parents=True)
    root = ArchiveRoot(
        id=root_id,
        label=root_id,
        root_path=str(path),
        storage_namespace="kmvms/recordings",
        is_active=active,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity=identity,
    )
    ctx.db.add(root)
    ctx.db.commit()
    return root, path


def add_segment(
    ctx,
    camera,
    *,
    index,
    days_ago=2,
    size_bytes=1024,
    root=None,
    integrity_status=None,
):
    root = root or ctx.root
    root_path = Path(root.root_path)
    relative = f"kmvms/recordings/camera_{camera.id}/segment-{index}.mkv"
    path = root_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stage4102")
    started = datetime.utcnow() - timedelta(days=days_ago, minutes=index)
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(path),
        relative_path=relative,
        started_at=started,
        ended_at=started + timedelta(minutes=1),
        duration_sec=60,
        size_bytes=size_bytes,
        stream_type="main",
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=root.id,
        archive_root_resolution_status="resolved",
        archive_root_resolution_detail="test",
        archive_root_resolved_at=datetime.utcnow(),
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status=integrity_status,
        finalized_at=datetime.utcnow(),
    )
    ctx.db.add(segment)
    ctx.db.commit()
    ctx.db.refresh(segment)
    return segment, path


def retention_operations(ctx):
    return (
        ctx.db.query(StorageOperation)
        .filter(StorageOperation.operation_type == "retention_auto_run")
        .order_by(StorageOperation.created_at.asc(), StorageOperation.id.asc())
        .all()
    )


def test_stage4102_migration_adds_columns_and_indexes_to_v3_shape():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    removed_columns = {
        "cameras": ["retention_policy_version"],
        "system_settings": [
            "auto_free_space_acknowledged_terms_version",
            "auto_free_space_acknowledged_at",
            "auto_free_space_acknowledged_by_user_id",
            "low_disk_suspended_physical_volume_id",
            "low_disk_suspended_at",
        ],
    }
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX IF EXISTS ix_recording_segments_camera_status_started_id")
        connection.exec_driver_sql("DROP INDEX IF EXISTS ix_recording_segments_root_status_started_id")
        for table_name, column_names in removed_columns.items():
            for column_name in column_names:
                connection.exec_driver_sql(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    now = datetime.utcnow()
    db.add(
        SchemaVersionState(
            id=CURRENT_STATE_ID,
            schema_version=3,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status="current",
            source=MIGRATION_SOURCE,
            applied_at=now,
        )
    )
    db.add(
        SchemaMigrationHistory(
            migration_id="stage4102_seed_v3",
            previous_version=2,
            target_version=3,
            schema_version=3,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status="current",
            source=MIGRATION_SOURCE,
        )
    )
    db.commit()

    result = execute_migration_plan(db, registry=MigrationRegistry([STAGE4102_RETENTION_MIGRATION]))
    from sqlalchemy import inspect as sqlalchemy_inspect

    inspector = sqlalchemy_inspect(engine)
    camera_columns = {item["name"] for item in inspector.get_columns("cameras")}
    setting_columns = {item["name"] for item in inspector.get_columns("system_settings")}
    indexes = {item["name"] for item in inspector.get_indexes("recording_segments")}

    assert result["executed_migrations"] == [STAGE4102_RETENTION_MIGRATION.migration_id]
    assert "retention_policy_version" in camera_columns
    assert set(removed_columns["system_settings"]).issubset(setting_columns)
    assert {
        "ix_recording_segments_camera_status_started_id",
        "ix_recording_segments_root_status_started_id",
    }.issubset(indexes)
    assert db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == 4
    db.close()
    engine.dispose()


def test_age_and_quota_use_or_semantics_and_oldest_first(stage4102):
    camera = add_camera(stage4102, days=1, quota=1)
    oldest, oldest_path = add_segment(
        stage4102,
        camera,
        index=1,
        days_ago=5,
        size_bytes=700 * 1024 * 1024,
    )
    middle, middle_path = add_segment(
        stage4102,
        camera,
        index=3,
        days_ago=0,
        size_bytes=700 * 1024 * 1024,
    )
    newest, newest_path = add_segment(
        stage4102,
        camera,
        index=2,
        days_ago=0,
        size_bytes=700 * 1024 * 1024,
    )

    result = run_automatic_retention_once(stage4102.db, max_candidates=1)

    for row in (oldest, middle, newest):
        stage4102.db.refresh(row)
    assert result["deleted_count"] == 2
    assert oldest.status == "deleted"
    assert middle.status == "deleted"
    assert newest.status == "finalized"
    assert not oldest_path.exists()
    assert not middle_path.exists()
    assert newest_path.exists()
    event_types = {event.event_type for event in stage4102.db.query(AuditEvent).all()}
    assert "storage_operation.started" in event_types
    assert "storage_operation.finished" in event_types


@pytest.mark.parametrize("mode", ["age_only", "quota_only"])
def test_each_retention_rule_independently_triggers_cleanup(stage4102, mode):
    camera = add_camera(
        stage4102,
        name=f"rule-{mode}",
        days=1 if mode == "age_only" else 365,
        quota=500 if mode == "age_only" else 1,
    )
    oldest, oldest_path = add_segment(
        stage4102,
        camera,
        index=2,
        days_ago=4 if mode == "age_only" else 0,
        size_bytes=1024 if mode == "age_only" else 700 * 1024 * 1024,
    )
    newest = None
    newest_path = None
    if mode == "quota_only":
        newest, newest_path = add_segment(
            stage4102,
            camera,
            index=1,
            days_ago=0,
            size_bytes=700 * 1024 * 1024,
        )

    result = run_automatic_retention_once(stage4102.db, max_candidates=1)

    stage4102.db.refresh(oldest)
    assert result["deleted_count"] == 1
    assert oldest.status == "deleted"
    assert not oldest_path.exists()
    if newest is not None:
        stage4102.db.refresh(newest)
        assert newest.status == "finalized"
        assert newest_path.exists()


def test_more_than_25_items_and_multi_gib_metadata_finish_in_one_logical_run(stage4102):
    camera = add_camera(stage4102, days=1, quota=500)
    rows = [
        add_segment(
            stage4102,
            camera,
            index=index,
            days_ago=3,
            size_bytes=160 * 1024 * 1024,
        )[0]
        for index in range(1, 32)
    ]

    result = run_automatic_retention_once(stage4102.db, max_candidates=7)

    assert result["deleted_count"] == 31
    assert sum(int(row.size_bytes or 0) for row in rows) > 1024 * 1024 * 1024
    assert all(stage4102.db.get(RecordingSegment, row.id).status == "deleted" for row in rows)
    operation = retention_operations(stage4102)[0]
    assert operation.status == "completed"
    assert operation.result["status"] == "compliant"
    assert operation.progress["completed_count"] == 31


def test_single_multi_gib_recording_is_not_skipped_by_automatic_byte_budget(stage4102):
    camera = add_camera(stage4102, days=1, quota=50)
    segment, path = add_segment(
        stage4102,
        camera,
        index=1,
        days_ago=4,
        size_bytes=4 * 1024 * 1024 * 1024,
    )

    result = run_automatic_retention_once(stage4102.db, max_candidates=1)

    stage4102.db.refresh(segment)
    assert result["deleted_count"] == 1
    assert segment.status == "deleted"
    assert not path.exists()
    source = inspect.getsource(run_automatic_retention_once)
    assert "max_bytes" not in source
    assert "oversized_single_segment_progress" not in source


def test_manual_support_retention_preserves_explicit_candidate_and_byte_limits(stage4102):
    camera = add_camera(stage4102, days=1, quota=50)
    first, first_path = add_segment(stage4102, camera, index=1, days_ago=4, size_bytes=1024)
    second, second_path = add_segment(stage4102, camera, index=2, days_ago=4, size_bytes=1024)

    candidate_limited = run_retention(
        stage4102.db,
        actor=owner(),
        max_candidates=1,
        max_bytes=10 * 1024,
    )
    byte_limited = run_retention(
        stage4102.db,
        actor=owner(),
        max_candidates=10,
        max_bytes=1,
    )

    for segment in (first, second):
        stage4102.db.refresh(segment)
        assert segment.status == "finalized"
    assert first_path.exists() and second_path.exists()
    assert candidate_limited["status"] == "blocked"
    assert candidate_limited["limit_exceeded"] is True
    assert candidate_limited["limit_applied"]["max_candidates"] == 1
    assert candidate_limited["skipped_reason_counts"]["limit_exceeded"] == 2
    assert byte_limited["status"] == "blocked"
    assert byte_limited["limit_exceeded"] is True
    assert byte_limited["limit_applied"]["max_bytes"] == 1
    assert byte_limited["skipped_reason_counts"]["limit_exceeded"] == 2


@pytest.mark.parametrize("enabled,deleted", [(False, False), (True, True)])
def test_disabled_and_soft_deleted_retained_archives_continue_aging(stage4102, enabled, deleted):
    camera = add_camera(stage4102, name=f"retained-{enabled}-{deleted}", enabled=enabled, days=1, deleted=deleted)
    segment, _path = add_segment(stage4102, camera, index=1, days_ago=4)

    run_automatic_retention_once(stage4102.db, max_candidates=5)

    stage4102.db.refresh(segment)
    assert segment.status == "deleted"


def test_unknown_size_and_unsafe_owned_rows_remain_in_violation_truth(stage4102):
    unknown_camera = add_camera(stage4102, name="unknown-size", days=365, quota=1)
    unknown, unknown_path = add_segment(stage4102, unknown_camera, index=1, days_ago=0, size_bytes=0)
    unsafe_camera = add_camera(stage4102, name="unsafe-over-quota", days=365, quota=1)
    unsafe, unsafe_path = add_segment(
        stage4102,
        unsafe_camera,
        index=2,
        days_ago=0,
        size_bytes=2 * 1024 * 1024 * 1024,
        integrity_status="corrupted_file",
    )

    run_automatic_retention_once(stage4102.db, max_candidates=5)

    stage4102.db.refresh(unknown)
    stage4102.db.refresh(unsafe)
    assert unknown.status == "finalized" and unknown_path.exists()
    assert unsafe.status == "finalized" and unsafe_path.exists()
    operations = retention_operations(stage4102)
    by_camera = {int(row.result["camera_id"]): row for row in operations}
    assert by_camera[unknown_camera.id].status == "blocked"
    assert by_camera[unknown_camera.id].result["measurement_confidence"] == "unknown"
    assert by_camera[unsafe_camera.id].status == "blocked"
    assert by_camera[unsafe_camera.id].result["quota_overage_bytes"] > 0


def test_signal_coalesces_and_new_work_during_claim_stays_pending(stage4102, monkeypatch):
    camera = add_camera(stage4102, days=30, quota=50)
    add_segment(stage4102, camera, index=1, days_ago=0)
    automation.ensure_retention_signal(stage4102.db)
    automation.advance_retention_signal(stage4102.db)
    automation.advance_retention_signal(stage4102.db)
    row = stage4102.db.query(StorageWorkSignal).one()
    assert row.requested_watermark == 2

    handle = automation.claim_retention_signal(stage4102.db, owner_instance_id="stage4102-test")

    def add_pending_work(*_args, **_kwargs):
        automation.advance_retention_signal(stage4102.db)
        return {"status": "compliant", "deleted_count": 0, "bytes_freed": 0}

    monkeypatch.setattr(automation, "run_camera_retention_operation", add_pending_work)
    automation.run_retention_signal_generation(stage4102.db, handle, page_size=5)

    stage4102.db.refresh(row)
    assert row.consumed_watermark == 2
    assert row.requested_watermark == 3
    assert row.status == "pending"


def test_camera_rule_change_advances_signal_and_increments_policy_version(stage4102):
    camera = add_camera(stage4102, days=30, quota=50)

    updated = update_camera(
        camera.id,
        CameraUpdate(retention_days=7, storage_quota_gb=8),
        FakeRequest(),
        db=stage4102.db,
        current_user=owner(),
    )

    signal = stage4102.db.query(StorageWorkSignal).one()
    assert updated.retention_policy_version == 2
    assert signal.requested_watermark == 1
    assert signal.status == "pending"

    updated_again = update_camera(
        camera.id,
        CameraUpdate(retention_days=5),
        FakeRequest(),
        db=stage4102.db,
        current_user=owner(),
    )
    stage4102.db.refresh(signal)
    assert updated_again.retention_policy_version == 3
    assert signal.requested_watermark == 2


def test_soft_delete_with_retained_archive_advances_signal(stage4102):
    camera = add_camera(stage4102, name="soft-delete-retained", days=7, quota=8)
    segment, path = add_segment(stage4102, camera, index=1, days_ago=1)

    result = delete_camera(
        camera.id,
        FakeRequest(),
        delete_files=False,
        operation_id=None,
        db=stage4102.db,
        current_user=owner(),
    )

    stage4102.db.refresh(camera)
    stage4102.db.refresh(segment)
    signal = stage4102.db.query(StorageWorkSignal).one()
    assert result["status"] == "deleted"
    assert camera.deleted_at is not None
    assert camera.retention_policy_version == 2
    assert segment.status == "finalized"
    assert path.exists()
    assert signal.requested_watermark == 1


def test_due_schedule_and_worker_cadence_are_not_hourly_normal_path(stage4102):
    camera = add_camera(stage4102, days=1, quota=50)
    add_segment(stage4102, camera, index=1, days_ago=3)

    due = automation.earliest_retention_due_at(stage4102.db)
    published = automation.publish_due_retention_signal(stage4102.db, now=database_now(stage4102.db))

    assert due is not None and due <= database_now(stage4102.db)
    assert published is not None
    worker_source = inspect.getsource(automatic_worker)
    assert "_signal_poll_seconds = 30" in worker_source
    assert "_stop_event.wait(_signal_poll_seconds)" in worker_source
    assert "force_recovery=force_recovery" in worker_source
    assert "next_recovery_at" in worker_source


def test_retention_status_is_read_only_and_survives_new_session(stage4102):
    camera = add_camera(stage4102, days=1, quota=50)
    add_segment(stage4102, camera, index=1, days_ago=3)
    run_automatic_retention_once(stage4102.db, max_candidates=5)
    before = {
        "operations": stage4102.db.query(StorageOperation).count(),
        "signals": stage4102.db.query(StorageWorkSignal).count(),
        "audits": stage4102.db.query(AuditEvent).count(),
    }

    first = automation.retention_runtime_status(stage4102.db)
    second = automation.retention_runtime_status(stage4102.db)
    after = {
        "operations": stage4102.db.query(StorageOperation).count(),
        "signals": stage4102.db.query(StorageWorkSignal).count(),
        "audits": stage4102.db.query(AuditEvent).count(),
    }
    restart_db = stage4102.Session()
    try:
        restarted = automation.retention_runtime_status(restart_db)
    finally:
        restart_db.close()

    assert before == after
    assert first["last_status"] == second["last_status"] == restarted["last_status"]
    assert restarted["last_status"] != "never_run"


def test_system_settings_read_paths_normalize_without_orm_mutation(stage4102):
    stage4102.system.language = "RU"
    stage4102.db.commit()
    stage4102.db.expire_all()

    row = get_system_settings(stage4102.db)
    readonly_row = get_system_settings_read_only(stage4102.db)
    serialized = serialize_settings(readonly_row)

    assert row is readonly_row
    assert row.language == "RU"
    assert serialized["language"] == "ru"
    assert not stage4102.db.new
    assert not stage4102.db.dirty
    assert not stage4102.db.deleted


def test_missing_system_settings_read_only_fallback_does_not_create_orm_state(stage4102):
    stage4102.db.delete(stage4102.system)
    stage4102.db.commit()

    row = get_system_settings_read_only(stage4102.db)

    assert row.id is None
    assert serialize_settings(row)["language"] == "ru"
    assert stage4102.db.query(SystemSettings).count() == 0
    assert not stage4102.db.new
    assert not stage4102.db.dirty
    assert not stage4102.db.deleted


def test_immutable_policy_snapshot_and_terminal_replay_do_not_repeat_mutation(stage4102, monkeypatch):
    camera = add_camera(stage4102, days=1, quota=50)
    segment, _path = add_segment(stage4102, camera, index=1, days_ago=4)
    snapshot = automation.camera_policy_snapshot(
        camera,
        signal_watermark=77,
        high_watermark=segment.id,
        evaluation_at=database_now(stage4102.db),
    )
    camera.retention_days = 365
    camera.retention_policy_version = 2
    stage4102.db.commit()
    calls = {"count": 0}
    real_execute = automation.execute_segments

    def counted_execute(*args, **kwargs):
        calls["count"] += 1
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(automation, "execute_segments", counted_execute)

    first = automation.run_camera_retention_operation(stage4102.db, snapshot, page_size=5)
    audit_count_before_replay = stage4102.db.query(AuditEvent).count()
    replay = automation.run_camera_retention_operation(stage4102.db, snapshot, page_size=5)
    audit_count_after_replay = stage4102.db.query(AuditEvent).count()

    operation = stage4102.db.get(StorageOperation, f"ret-auto-c{camera.id}-w77")
    assert first["policy_version"] == 1
    assert operation.progress["policy_snapshot"]["retention_days"] == 1
    assert operation.progress["policy_snapshot"]["policy_version"] == 1
    assert replay["replayed"] is True
    assert calls["count"] == 1
    assert audit_count_after_replay == audit_count_before_replay


def test_physical_volume_groups_coalesce_same_identity_and_keep_other_volume_isolated(stage4102):
    root_b, _path_b = add_root(
        stage4102,
        root_id="stage4102-root-b",
        identity=stage4102.root.physical_identity,
    )
    root_c, _path_c = add_root(
        stage4102,
        root_id="stage4102-root-c",
        identity="fs-stage4102-volume-c",
    )

    groups = automation.storage_volume_groups(stage4102.db)
    by_volume = {item["physical_volume_id"]: item for item in groups}
    volume_a = automation._canonical_volume_id(stage4102.db, stage4102.root.physical_identity)
    volume_c = automation._canonical_volume_id(stage4102.db, root_c.physical_identity)

    assert by_volume[volume_a]["root_count"] == 2
    assert by_volume[volume_c]["root_count"] == 1
    assert root_b.id in by_volume[volume_a]["root_ids"]


def test_operational_volume_discovery_is_not_truncated_by_public_status_bound(stage4102):
    for index in range(automation.STATUS_VOLUME_LIMIT + 3):
        path = stage4102.tmp_path / f"many-root-{index}"
        (path / "kmvms" / "recordings").mkdir(parents=True)
        stage4102.db.add(
            ArchiveRoot(
                id=f"many-root-{index}",
                label=f"Volume {index}",
                root_path=str(path),
                storage_namespace="kmvms/recordings",
                is_active=False,
                is_readable=True,
                is_writable=True,
                is_available=True,
                physical_identity=f"fs-many-volume-{index}",
            )
        )
    stage4102.db.commit()

    groups = automation.storage_volume_groups(stage4102.db)
    public = automation.low_disk_policy_status(stage4102.db, groups)

    assert len(groups) == automation.STATUS_VOLUME_LIMIT + 4
    assert len(public["volume_groups"]) == automation.STATUS_VOLUME_LIMIT


def test_auto_free_candidate_order_is_oldest_first_and_never_crosses_volume(stage4102, monkeypatch):
    camera = add_camera(stage4102, days=365, quota=500)
    root_b, _ = add_root(stage4102, root_id="root-b", identity=stage4102.root.physical_identity)
    root_c, _ = add_root(stage4102, root_id="root-c", identity="fs-other-volume")
    oldest_other, _ = add_segment(stage4102, camera, index=1, days_ago=10, root=root_c)
    oldest_target, _ = add_segment(stage4102, camera, index=2, days_ago=8, root=root_b)
    newer_target, _ = add_segment(stage4102, camera, index=3, days_ago=2, root=stage4102.root)
    monkeypatch.setattr(
        automation,
        "validate_segment_for_deletion",
        lambda row, **_kwargs: (True, "eligible", Path(row.file_path), int(row.size_bytes or 0)),
    )

    selected = automation._select_group_candidates(
        stage4102.db,
        [stage4102.root.id, root_b.id],
        limit=2,
    )

    assert [row.id for row in selected["segments"]] == [oldest_target.id, newer_target.id]
    assert oldest_other.id not in {row.id for row in selected["segments"]}


@pytest.mark.parametrize(
    "free_percent,expected",
    [(10.0, "ok"), (9.0, "warning"), (5.0, "warning"), (4.999, "cleanup_threshold"), (1.0, "cleanup_threshold"), (0.999, "critical")],
)
def test_threshold_boundaries_are_strict(free_percent, expected):
    assert automation._volume_state(free_percent) == expected


def volume_group(ctx, free_percent, *, identity=None, active=True):
    raw_identity = identity or ctx.root.physical_identity
    return {
        "_physical_identity": raw_identity,
        "physical_volume_id": automation._canonical_volume_id(ctx.db, raw_identity),
        "display_label": "Volume",
        "root_ids": [ctx.root.id],
        "root_count": 1,
        "scope_bounded": True,
        "active_write_target": active,
        "capacity": {
            "total_bytes": 1000,
            "used_bytes": int(1000 - free_percent * 10),
            "free_bytes": int(free_percent * 10),
            "free_percent": free_percent,
            "filesystem_probe_status": "ok",
        },
        "root_access_problem_count": 0,
    }


def test_critical_protection_hysteresis_and_inactive_volume_isolation(stage4102):
    inactive_critical = volume_group(stage4102, 0.5, identity="fs-inactive", active=False)
    healthy_active = volume_group(stage4102, 50.0, active=True)
    assert automation.apply_critical_recording_protection(
        stage4102.db,
        [healthy_active, inactive_critical],
    )["recording_suspended_by_low_disk"] is False

    suspended = automation.apply_critical_recording_protection(stage4102.db, [volume_group(stage4102, 0.9)])
    assert suspended["recording_suspended_by_low_disk"] is True
    assert automation.apply_critical_recording_protection(
        stage4102.db,
        [volume_group(stage4102, 1.1)],
    )["recording_suspended_by_low_disk"] is True
    assert automation.apply_critical_recording_protection(
        stage4102.db,
        [volume_group(stage4102, 8.9)],
    )["recording_suspended_by_low_disk"] is True
    resumed = automation.apply_critical_recording_protection(stage4102.db, [volume_group(stage4102, 9.0)])
    assert resumed["recording_suspended_by_low_disk"] is False


def test_critical_protection_does_not_resume_on_a_different_healthy_active_volume(stage4102):
    volume_a_critical = volume_group(stage4102, 0.9, identity="fs-volume-a", active=True)
    suspended = automation.apply_critical_recording_protection(stage4102.db, [volume_a_critical])
    assert suspended["recording_suspended_by_low_disk"] is True

    volume_a_still_low = volume_group(stage4102, 0.9, identity="fs-volume-a", active=False)
    volume_b_healthy = volume_group(stage4102, 50.0, identity="fs-volume-b", active=True)
    switched = automation.apply_critical_recording_protection(
        stage4102.db,
        [volume_b_healthy, volume_a_still_low],
    )
    assert switched["recording_suspended_by_low_disk"] is True
    assert switched["suspended_free_percent"] == 0.9

    volume_a_recovered = volume_group(stage4102, 9.0, identity="fs-volume-a", active=False)
    resumed = automation.apply_critical_recording_protection(
        stage4102.db,
        [volume_b_healthy, volume_a_recovered],
    )
    assert resumed["recording_suspended_by_low_disk"] is False


def test_policy_off_still_applies_critical_protection_between_retention_slices(stage4102, monkeypatch):
    stage4102.system.auto_free_space_cleanup_enabled = False
    stage4102.db.commit()
    monkeypatch.setattr(
        automation,
        "storage_volume_groups",
        lambda _db: [volume_group(stage4102, 0.9)],
    )

    should_preempt = automation.retention_slice_preemption_required(stage4102.db)

    stage4102.db.refresh(stage4102.system)
    assert should_preempt is False
    assert stage4102.system.recording_suspended_by_low_disk is True
    assert stage4102.system.low_disk_suspended_physical_volume_id == stage4102.root.physical_identity


def test_retention_status_counts_age_only_and_quota_only_rules_as_configured(stage4102):
    age_only = add_camera(stage4102, name="age-only", days=7, quota=0)
    quota_only = add_camera(stage4102, name="quota-only", days=0, quota=25)
    no_rule = add_camera(stage4102, name="no-rule", days=0, quota=0)
    add_segment(stage4102, age_only, index=1, days_ago=1)
    add_segment(stage4102, quota_only, index=2, days_ago=1)
    add_segment(stage4102, no_rule, index=3, days_ago=1)

    status = automation.retention_runtime_status(stage4102.db)

    assert status["configured_camera_count"] == 3
    assert status["meaningful_rule_camera_count"] == 2
    assert status["missing_or_invalid_rule_camera_count"] == 1


def test_auto_free_continues_across_slices_until_measured_nine_percent(stage4102, monkeypatch):
    stage4102.system.auto_free_space_cleanup_enabled = True
    stage4102.system.auto_free_space_acknowledged_terms_version = AUTO_FREE_SPACE_TERMS_VERSION
    stage4102.db.commit()
    camera = add_camera(stage4102, days=365, quota=500)
    first, _ = add_segment(stage4102, camera, index=1, days_ago=5)
    second, _ = add_segment(stage4102, camera, index=2, days_ago=4)
    group = volume_group(stage4102, 4.0)
    monkeypatch.setattr(automation, "storage_volume_groups", lambda _db: [group])
    monkeypatch.setattr(
        automation,
        "_revalidate_pressure_group",
        lambda *_args, **_kwargs: {
            "physical_identity": stage4102.root.physical_identity,
            "runtime_device_id": "rv1:" + "a" * 32,
        },
    )
    probes = iter([4.0, 6.0, 6.0, 9.0])

    def probe(_db, context):
        free = next(probes)
        return {
            "status": "ok",
            "physical_volume_id": context["snapshot"]["physical_volume_id"],
            "root_ids": [stage4102.root.id],
            "total_bytes": 1000,
            "free_bytes": int(free * 10),
            "free_percent": free,
        }

    def delete_batch(db, segments, **_kwargs):
        segment = list(segments)[0]
        segment.status = "deleted"
        segment.deleted_at = datetime.utcnow()
        db.add(segment)
        db.commit()
        return {
            "planned_count": 1,
            "deleted_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "bytes_freed": int(segment.size_bytes or 0),
            "reason_counts": {},
        }

    monkeypatch.setattr(automation, "_probe_auto_free_context", probe)
    monkeypatch.setattr(automation, "execute_segments", delete_batch)
    result = automation.run_auto_free_pressure_groups(stage4102.db, page_size=1)

    assert result["deleted_count"] == 2
    assert stage4102.db.get(RecordingSegment, first.id).status == "deleted"
    assert stage4102.db.get(RecordingSegment, second.id).status == "deleted"
    operation = (
        stage4102.db.query(StorageOperation)
        .filter(StorageOperation.operation_type == "retention_auto_free_space")
        .one()
    )
    assert operation.status == "completed"
    assert operation.result["final_free_percent"] == 9.0


def test_auto_free_off_never_deletes_and_enabled_policy_does_not_start_at_five_percent(stage4102, monkeypatch):
    calls = {"prepare": 0}

    def unexpected_prepare(*_args, **_kwargs):
        calls["prepare"] += 1
        raise AssertionError("cleanup must not start")

    monkeypatch.setattr(automation, "storage_volume_groups", lambda _db: [volume_group(stage4102, 4.0)])
    monkeypatch.setattr(automation, "prepare_auto_free_context", unexpected_prepare)
    disabled = automation.run_auto_free_pressure_groups(stage4102.db, page_size=1)
    assert disabled["status"] == "policy_not_effective"
    assert calls["prepare"] == 0

    stage4102.system.auto_free_space_cleanup_enabled = True
    stage4102.system.auto_free_space_acknowledged_terms_version = AUTO_FREE_SPACE_TERMS_VERSION
    stage4102.db.commit()
    monkeypatch.setattr(automation, "storage_volume_groups", lambda _db: [volume_group(stage4102, 5.0)])
    enabled = automation.run_auto_free_pressure_groups(stage4102.db, page_size=1)
    assert enabled["pressure_count"] == 0
    assert calls["prepare"] == 0


def test_unknown_physical_identity_fails_closed_before_cleanup_claim(stage4102):
    claim = automation._claim_auto_free_operation(
        stage4102.db,
        {
            "physical_volume_id": None,
            "scope_bounded": True,
            "root_ids": [stage4102.root.id],
            "capacity": {"free_percent": 4.0},
        },
    )

    assert claim == {"state": "blocked", "reason_code": "physical_volume_identity_unknown"}


def test_multiple_pressured_volumes_receive_round_robin_slices(stage4102, monkeypatch):
    stage4102.system.auto_free_space_cleanup_enabled = True
    stage4102.system.auto_free_space_acknowledged_terms_version = AUTO_FREE_SPACE_TERMS_VERSION
    stage4102.db.commit()
    group_a = volume_group(stage4102, 4.0, identity="fs-round-robin-a")
    group_b = volume_group(stage4102, 3.0, identity="fs-round-robin-b")
    monkeypatch.setattr(automation, "storage_volume_groups", lambda _db: [group_a, group_b])
    monkeypatch.setattr(
        automation,
        "apply_critical_recording_protection",
        lambda _db, _groups=None: {"recording_suspended_by_low_disk": False},
    )
    counters = {"fs-round-robin-a": 0, "fs-round-robin-b": 0}
    order = []

    def prepare(_db, group):
        return {
            "snapshot": {"identity": group["_physical_identity"]},
            "totals": {"deleted_count": 0, "bytes_freed": 0},
            "terminal": False,
            "result": None,
        }

    def run_slice(_db, context, *, page_size):
        identity = context["snapshot"]["identity"]
        order.append(identity)
        counters[identity] += 1
        if counters[identity] == 2:
            context["terminal"] = True
            context["result"] = {
                "status": "target_reached",
                "deleted_count": 1,
                "bytes_freed": 1,
            }
        return context["result"] or {"status": "running"}

    monkeypatch.setattr(automation, "prepare_auto_free_context", prepare)
    monkeypatch.setattr(automation, "run_auto_free_slice", run_slice)

    result = automation.run_auto_free_pressure_groups(stage4102.db, page_size=1)

    assert order == ["fs-round-robin-a", "fs-round-robin-b", "fs-round-robin-a", "fs-round-robin-b"]
    assert result["operation_count"] == 2


def test_first_enable_requires_current_terms_and_valid_ack_is_idempotent(stage4102):
    with pytest.raises(HTTPException) as missing:
        patch_settings(
            SettingsUpdateRequest(auto_free_space_cleanup_enabled=True),
            FakeRequest(),
            db=stage4102.db,
            current_user=owner(),
        )
    assert missing.value.status_code == 409
    assert missing.value.detail["reason_code"] == "auto_free_space_acknowledgement_required"

    with pytest.raises(HTTPException) as stale:
        patch_settings(
            SettingsUpdateRequest(
                auto_free_space_cleanup_enabled=True,
                auto_free_space_acknowledgement={"acknowledged": True, "terms_version": "old-terms"},
            ),
            FakeRequest(),
            db=stage4102.db,
            current_user=owner(),
        )
    assert stale.value.detail["reason_code"] == "auto_free_space_acknowledgement_stale"

    payload = SettingsUpdateRequest(
        auto_free_space_cleanup_enabled=True,
        auto_free_space_acknowledgement={
            "acknowledged": True,
            "terms_version": AUTO_FREE_SPACE_TERMS_VERSION,
        },
    )
    first = patch_settings(payload, FakeRequest(), db=stage4102.db, current_user=owner())
    first_updated_at = stage4102.db.get(SystemSettings, stage4102.system.id).updated_at
    first_audits = stage4102.db.query(AuditEvent).filter(
        AuditEvent.event_type == "settings.auto_free_space_acknowledged"
    ).count()
    second = patch_settings(payload, FakeRequest(), db=stage4102.db, current_user=owner())
    second_updated_at = stage4102.db.get(SystemSettings, stage4102.system.id).updated_at
    second_audits = stage4102.db.query(AuditEvent).filter(
        AuditEvent.event_type == "settings.auto_free_space_acknowledged"
    ).count()
    saved_audits = stage4102.db.query(AuditEvent).filter(AuditEvent.event_type == "settings.saved").count()

    assert first["auto_free_space_cleanup_effective"] is True
    assert first["auto_free_space_acknowledged_terms_version"] == AUTO_FREE_SPACE_TERMS_VERSION
    assert second["auto_free_space_cleanup_effective"] is True
    assert first_audits == second_audits == 1
    assert saved_audits == 1
    assert second_updated_at == first_updated_at


def test_acknowledgement_audit_failure_rolls_back_enable(stage4102, monkeypatch):
    real_create_event = settings_router.create_event

    def fail_transactional_ack(**kwargs):
        if kwargs.get("event_type") == "settings.auto_free_space_acknowledged" and kwargs.get("commit") is False:
            return None
        return real_create_event(**kwargs)

    monkeypatch.setattr(settings_router, "create_event", fail_transactional_ack)
    with pytest.raises(RuntimeError, match="auto_free_space_acknowledgement_audit_failed"):
        patch_settings(
            SettingsUpdateRequest(
                auto_free_space_cleanup_enabled=True,
                auto_free_space_acknowledgement={
                    "acknowledged": True,
                    "terms_version": AUTO_FREE_SPACE_TERMS_VERSION,
                },
            ),
            FakeRequest(),
            db=stage4102.db,
            current_user=owner(),
        )

    stage4102.db.refresh(stage4102.system)
    assert stage4102.system.auto_free_space_cleanup_enabled is False
    assert stage4102.system.auto_free_space_acknowledged_terms_version is None
    assert stage4102.db.query(AuditEvent).filter(
        AuditEvent.event_type == "settings.auto_free_space_acknowledged"
    ).count() == 0


def test_disable_needs_no_ack_and_reenable_under_current_terms_needs_no_new_ack(stage4102):
    stage4102.system.auto_free_space_cleanup_enabled = True
    stage4102.system.auto_free_space_acknowledged_terms_version = AUTO_FREE_SPACE_TERMS_VERSION
    stage4102.system.auto_free_space_acknowledged_at = datetime.utcnow()
    stage4102.system.auto_free_space_acknowledged_by_user_id = owner().id
    stage4102.db.commit()

    disabled = patch_settings(
        SettingsUpdateRequest(auto_free_space_cleanup_enabled=False),
        FakeRequest(),
        db=stage4102.db,
        current_user=owner(),
    )
    enabled = patch_settings(
        SettingsUpdateRequest(auto_free_space_cleanup_enabled=True),
        FakeRequest(),
        db=stage4102.db,
        current_user=owner(),
    )

    assert disabled["auto_free_space_cleanup_effective"] is False
    assert enabled["auto_free_space_cleanup_effective"] is True
    assert enabled["auto_free_space_acknowledgement_required"] is False


def test_settings_and_status_do_not_expose_ack_actor_or_raw_physical_identity(stage4102):
    stage4102.system.auto_free_space_acknowledged_by_user_id = owner().id
    stage4102.db.commit()

    serialized = serialize_settings(stage4102.system)
    status = automation.low_disk_policy_status(stage4102.db)

    assert "auto_free_space_acknowledged_by_user_id" not in serialized
    assert status["volume_groups"][0]["physical_volume_id"].startswith("pv1:")
    assert stage4102.root.physical_identity not in str(status)


def test_recorder_finalization_source_advances_signal_in_same_transaction():
    source = Path(__file__).resolve().parents[2] / "recorder" / "main.py"
    text = source.read_text(encoding="utf-8")
    function = text[text.index("def finalize_segment_path"):text.index("def mark_segment_failed")]

    assert "RETURNING id" in function
    assert "INSERT INTO storage_work_signals" in function
    assert "GREATEST(" in function
    assert "requested_watermark" in function
    assert "conn.execute" in function
