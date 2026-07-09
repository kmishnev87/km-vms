import json
import sys
import tempfile
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.routers.settings import SettingsUpdateRequest, SetupRequest, patch_settings, setup
from app.services.storage_contract import (
    KMVMS_RECORDINGS_NAMESPACE,
    recording_format_contract,
    storage_contract,
)
from app.services.setup_storage import (
    ACTIVATION_REQUEST_CONTROL_FILE,
    APPLY_STATUS_FILE,
    CONTAINER_ARCHIVE_PATH,
    DISCOVERY_FILE,
    SELECTION_CONTROL_FILE,
    SELECTION_FILE,
)
from app.services.storage_monitoring import build_storage_monitoring_summary
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    _file_checksum,
    active_archive_root,
    apply_storage_migration,
    ensure_archive_roots,
    migration_preview,
    resolve_segment_file_path,
    root_status,
    sanitize_archive_root_path,
    storage_migration_apply_plan,
)
from app.services import recording_reconciliation
from app.services.recording_reconciliation import reconcile_recordings, reconciliation_diagnostics
from app.services.system_settings import (
    active_recording_jobs_count,
    get_system_settings,
    serialize_settings,
    validate_settings_payload,
    validate_storage_path,
)


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


def actor(role="owner"):
    return SimpleNamespace(id=1, username=f"{role}_user", role=role, is_active=True)


def add_camera(db, name="stage2_storage_camera"):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def write_storage_file(relative_path: str, content: bytes = b"video") -> Path:
    path = Path(settings.storage_root) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def add_segment(
    db,
    camera,
    *,
    relative_path="kmvms/recordings/segment.mkv",
    status="finalized",
    ownership="KM VMS",
    source="recorder",
    job_id=None,
    archive_root_id=None,
    updated_at=None,
):
    now = datetime.utcnow()
    segment = RecordingSegment(
        job_id=job_id,
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=relative_path or "",
        relative_path=relative_path,
        started_at=now,
        ended_at=now if status != "writing" else None,
        finalized_at=now if status == "finalized" else None,
        duration_sec=1,
        size_bytes=0,
        status=status,
        ownership=ownership,
        source=source,
        archive_root_id=archive_root_id,
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        container_format="mkv",
        file_extension=".mkv",
        updated_at=updated_at or now,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def add_archive_root(db, root_path: Path, *, root_id="root_extra", active=False):
    root = ArchiveRoot(
        id=root_id,
        label=root_id,
        root_path=str(root_path),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=active,
        is_readable=True,
        is_writable=True,
        is_available=True,
    )
    db.add(root)
    db.commit()
    db.refresh(root)
    return root


def write_setup_storage_selection(host_path: str) -> None:
    control = Path(settings.storage_install_control)
    control.mkdir(parents=True, exist_ok=True)
    (control / SELECTION_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selected_host_path": host_path,
                "selected_mount_path": str(Path(host_path).parent),
                "folder_name": Path(host_path).name,
                "container_archive_path": CONTAINER_ARCHIVE_PATH,
                "candidate_id": "stage3-storage-contract",
                "selected_at": "2026-05-07T00:00:00Z",
                "apply_status": "active",
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage2_storage_contract_")
    tmp_path = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    original_storage_exports = settings.storage_exports
    original_control = settings.storage_install_control
    settings.storage_root = str(tmp_path / "archive")
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")
    settings.storage_install_control = str(tmp_path / "install-control")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        settings.storage_previews = original_storage_previews
        settings.storage_exports = original_storage_exports
        settings.storage_install_control = original_control
        tmp.cleanup()


def test_settings_serialization_distinguishes_runtime_storage_contract(db, monkeypatch):
    monkeypatch.setenv("STORAGE_HOST_ROOT", "/mnt/km-vms-archive")
    row = SystemSettings(
        system_initialized=True,
        timezone="UTC",
        language="en",
        storage_path="/legacy/display/path",
        recording_format="mkv",
    )
    db.add(row)
    db.commit()

    data = serialize_settings(row)

    assert data["storage_path"] == "/legacy/display/path"
    assert data["archive_host_path"] == "/mnt/km-vms-archive"
    assert data["archive_primary_path"] == "/mnt/km-vms-archive"
    assert data["archive_primary_path_source"] == "host_bind_env"
    assert data["storage_root"] == settings.storage_root
    assert data["container_runtime_storage_root"] == settings.storage_root
    assert data["storage_namespace"] == KMVMS_RECORDINGS_NAMESPACE
    assert data["storage_recordings_path"].endswith("kmvms/recordings")
    assert data["storage_editable"] is False
    assert data["storage_change_requires"] == "installer_or_deploy_remount"
    assert data["storage_contract"]["db_storage_path"] == "/legacy/display/path"


def test_public_system_status_after_initialization_does_not_expose_host_path(db, monkeypatch):
    from app.routers.settings import system_status

    monkeypatch.setenv("STORAGE_HOST_ROOT", "/mnt/private-km-vms-archive")
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="en", storage_path="/storage/archive"))
    db.commit()

    response = system_status(db)

    assert response == {"initialized": True, "setup_required": False, "language": "en", "timezone": "UTC"}
    assert "/mnt/private-km-vms-archive" not in str(response)


def test_patch_settings_ignores_storage_path_as_runtime_mutation(db):
    row = get_system_settings(db)
    original_storage_path = row.storage_path

    result = patch_settings(
        SettingsUpdateRequest(storage_path="/new/runtime/path"),
        FakeRequest(),
        db=db,
        current_user=actor("owner"),
    )

    refreshed = get_system_settings(db)
    assert refreshed.storage_path == original_storage_path
    assert result["storage_root"] == settings.storage_root
    assert result["storage_path"] == original_storage_path


def test_setup_custom_storage_path_is_not_runtime_source_of_truth(db):
    custom_path = str(Path(settings.storage_root).parent / "custom-user-request")
    write_setup_storage_selection(str(Path(settings.storage_root).parent / "selected-host-archive"))

    result = setup(
        SetupRequest(
            username="stage2_owner",
            password="stage2-password",
            password_confirm="stage2-password",
            timezone="UTC",
            language="en",
            storage_path=custom_path,
            recording_format="mp4",
        ),
        db=db,
    )

    refreshed = get_system_settings(db)
    assert refreshed.system_initialized is True
    assert refreshed.storage_path == settings.storage_root
    assert result["settings"]["storage_path"] == settings.storage_root
    assert result["settings"]["storage_root"] == settings.storage_root
    assert result["settings"]["storage_editable"] is False
    assert result["settings"]["storage_change_requires"] == "installer_or_deploy_remount"
    assert result["storage_validation"]["requested_storage_path"] == custom_path
    assert result["storage_validation"]["effective_storage_path"] == settings.storage_root
    assert result["storage_validation"]["setup_storage_path_behavior"] == "stage2_selected_host_path_required_container_path_remains_internal"
    assert result["storage_validation"]["storage_confirmation"]["selected_host_path"].endswith("selected-host-archive")
    assert not Path(custom_path).exists()


def test_recording_format_validation_accepts_only_mkv_mp4():
    assert validate_settings_payload({"recording_format": "mkv"}, partial=True)["recording_format"] == "mkv"
    assert validate_settings_payload({"recording_format": "MP4"}, partial=True)["recording_format"] == "mp4"
    with pytest.raises(ValueError):
        validate_settings_payload({"recording_format": "avi"}, partial=True)


def test_recording_format_contract_maps_profile_labels():
    assert recording_format_contract("mkv")["recording_profile"] == "reliability"
    assert recording_format_contract("mp4")["recording_profile"] == "compatibility"
    assert recording_format_contract("bad")["recording_format"] == "mkv"


def test_recording_format_change_blocks_when_active_jobs_exist(db):
    row = get_system_settings(db)
    row.recording_format = "mkv"
    job = RecordingJob(
        id="stage2_format_contract_active_job",
        camera_id=1,
        state="recording",
        started_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()

    assert active_recording_jobs_count(db) == 1
    with pytest.raises(HTTPException) as exc:
        patch_settings(
            SettingsUpdateRequest(recording_format="mp4"),
            FakeRequest(),
            db=db,
            current_user=actor("owner"),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "recording_format_change_blocked_active_recordings"
    assert get_system_settings(db).recording_format == "mkv"


def test_storage_diagnostics_include_consistent_contract_fields(db):
    summary = build_storage_monitoring_summary(db, include_namespace_observations=False)

    assert summary["storage_contract"]["storage_root"] == settings.storage_root
    assert summary["container_runtime_storage_root"] == settings.storage_root
    assert summary["storage_namespace"] == KMVMS_RECORDINGS_NAMESPACE
    assert summary["container_recordings_namespace_root"].endswith("kmvms/recordings")
    operations = summary["storage_operations"]
    assert operations["capacity"]["total_bytes"] == summary["capacity"]["total_bytes"]
    if summary["capacity"]["total_bytes"] is None:
        assert operations["capacity"]["usage_percent"] is None
        assert operations["capacity"]["free_percent"] is None
    else:
        assert operations["capacity"]["usage_percent"] is not None
        assert operations["capacity"]["free_percent"] is not None
    assert isinstance(operations["path_health"]["readable"], bool)
    assert isinstance(operations["path_health"]["writable"], bool)
    assert isinstance(operations["path_health"]["available"], bool)
    assert operations["low_disk_policy"]["warning_threshold_percent"] == 10.0
    assert operations["low_disk_policy"]["cleanup_threshold_percent"] == 5.0
    assert operations["low_disk_policy"]["critical_threshold_percent"] == 1.0
    assert operations["low_disk_policy"]["auto_free_space_cleanup_enabled"] is False
    assert operations["low_disk_policy"]["recording_suspended_by_low_disk"] is False
    assert operations["recent_operations"]["available"] is False
    assert settings.storage_root not in str(operations)


def test_storage_status_route_is_read_only_and_does_not_write_audit(db, monkeypatch):
    from app.routers import storage as storage_router

    def fail_audit(*args, **kwargs):
        raise AssertionError("storage status must not write audit from read-only refresh")

    monkeypatch.setattr("app.services.storage_monitoring._maybe_audit_storage_transition", fail_audit)

    result = storage_router.storage_status(db=db, current_user=actor("owner"))

    assert result["storage_operations"]["low_disk_policy"]["warning_threshold_percent"] == 10.0
    assert result["storage_operations"]["recent_operations"]["items"] == []


def test_storage_validation_is_explicit_container_not_host_remount():
    result = validate_storage_path(str(Path(settings.storage_root) / "stage2_probe"), create=False)

    assert result["path_role"] == "container_runtime_or_reference_path"
    assert result["runtime_storage_source"] == "settings.storage_root_env"
    assert "does not remount host storage" in result["host_mount_note"]


def test_archive_roots_bootstrap_backfills_default_root_and_is_idempotent(db):
    camera = add_camera(db)
    write_storage_file("kmvms/recordings/default.mkv", b"video")
    original_updated_at = datetime.utcnow() - timedelta(hours=2)
    segment = add_segment(db, camera, relative_path="kmvms/recordings/default.mkv", updated_at=original_updated_at)

    roots = ensure_archive_roots(db)
    db.refresh(segment)
    assert [root.id for root in roots if root.is_active] == [DEFAULT_ARCHIVE_ROOT_ID]
    assert segment.archive_root_id == DEFAULT_ARCHIVE_ROOT_ID
    assert segment.updated_at == original_updated_at

    second = ensure_archive_roots(db)
    assert len(second) == 1
    assert active_archive_root(db).id == DEFAULT_ARCHIVE_ROOT_ID


def test_root_aware_resolver_keeps_old_segment_on_default_after_active_switch(db):
    camera = add_camera(db)
    default_file = write_storage_file("kmvms/recordings/default-old.mkv", b"old")
    old_segment = add_segment(db, camera, relative_path="kmvms/recordings/default-old.mkv")
    ensure_archive_roots(db)

    new_root_path = Path(settings.storage_root).parent / "archive-new"
    new_file = new_root_path / "kmvms/recordings/new-segment.mkv"
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_bytes(b"new")
    add_archive_root(db, new_root_path, root_id="root_new", active=True)
    db.query(ArchiveRoot).filter(ArchiveRoot.id == DEFAULT_ARCHIVE_ROOT_ID).update({ArchiveRoot.is_active: False})
    new_segment = add_segment(db, camera, relative_path="kmvms/recordings/new-segment.mkv", archive_root_id="root_new")

    assert resolve_segment_file_path(db, old_segment, require_exists=True) == default_file.resolve()
    assert resolve_segment_file_path(db, new_segment, require_exists=True) == new_file.resolve()


def test_default_archive_root_path_stays_stable_after_settings_storage_root_changes(db, monkeypatch):
    camera = add_camera(db)
    default_file = write_storage_file("kmvms/recordings/default-stable.mkv", b"old")
    old_segment = add_segment(db, camera, relative_path="kmvms/recordings/default-stable.mkv")
    roots = ensure_archive_roots(db)
    default_root = next(root for root in roots if root.id == DEFAULT_ARCHIVE_ROOT_ID)
    original_root_path = default_root.root_path

    new_storage_root = Path(settings.storage_root).parent / "settings-new-root"
    (new_storage_root / "kmvms/recordings").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "storage_root", str(new_storage_root))

    ensure_archive_roots(db)
    db.refresh(default_root)

    assert default_root.root_path == original_root_path
    assert resolve_segment_file_path(db, old_segment, require_exists=True) == default_file.resolve()


def test_archive_root_validation_rejects_traversal_outside_base_and_surveillance(db):
    base = Path(settings.storage_root).parent
    assert sanitize_archive_root_path(str(base / "archive2")).name == "archive2"
    with pytest.raises(ValueError, match="outside_approved"):
        sanitize_archive_root_path(str(base.parent / "outside"))
    with pytest.raises(ValueError, match="surveillance"):
        sanitize_archive_root_path(str(base / "Surveillance"))


def test_archive_root_status_requires_namespace_and_real_write_probe(db):
    base = Path(settings.storage_root).parent
    candidate = base / "archive-no-namespace"
    candidate.mkdir(parents=True, exist_ok=True)

    missing_namespace = root_status(candidate)
    assert missing_namespace["exists"] is True
    assert missing_namespace["namespace_exists"] is False
    assert missing_namespace["writable"] is False
    assert missing_namespace["available"] is False
    assert missing_namespace["problem"] == "namespace_missing"

    sanitized = sanitize_archive_root_path(str(candidate), allow_create=True)
    ready = root_status(sanitized)
    assert ready["namespace_exists"] is True
    assert ready["writable"] is True
    assert ready["problem"] is None


def test_storage_status_and_migration_preview_are_root_aware_and_non_mutating(db):
    camera = add_camera(db)
    default_path = write_storage_file("kmvms/recordings/default-preview.mkv", b"old")
    add_segment(db, camera, relative_path="kmvms/recordings/default-preview.mkv")
    ensure_archive_roots(db)

    new_root_path = Path(settings.storage_root).parent / "archive-target"
    (new_root_path / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, new_root_path, root_id="root_target", active=False)

    summary = build_storage_monitoring_summary(db, include_namespace_observations=False)
    assert {root["id"] for root in summary["archive_roots"]} == {DEFAULT_ARCHIVE_ROOT_ID, "root_target"}
    assert summary["storage_operations"]["archive_roots"]

    preview = migration_preview(db, target_root_id="root_target")
    assert preview["apply_available"] is True
    assert preview["non_mutating"] is True
    assert preview["total_would_move_count"] == 1
    assert preview["apply_contract"] == "copy_only_server_side_plan_confirm_required_source_preserved"
    assert default_path.exists()


def test_storage_migration_apply_requires_confirm_and_rejects_raw_override_fields(db):
    from app.routers import storage as storage_router

    with pytest.raises(ValidationError):
        storage_router.MigrationApplyRequest.model_validate(
            {"confirm": True, "target_root_id": "root_target", "source_root": "request-controlled-source", "command": "mv"}
        )

    with pytest.raises(HTTPException) as exc:
        storage_router.storage_migration_apply(
            storage_router.MigrationApplyRequest(confirm=False, target_root_id="root_target"),
            db=db,
            current_user=actor("owner"),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "archive_migration_apply_requires_confirm"


def test_storage_migration_checksum_helper_streams_stable_hash(tmp_path):
    sample = tmp_path / "sample.mkv"
    payload = b"known-video-bytes"
    sample.write_bytes(payload)

    assert _file_checksum(sample) == _file_checksum(sample)
    assert _file_checksum(sample) == sha256(payload).hexdigest()


def test_storage_migration_apply_blocks_active_recorders_and_stale_plan(db):
    camera = add_camera(db)
    write_storage_file("kmvms/recordings/default-active.mkv", b"old")
    add_segment(db, camera, relative_path="kmvms/recordings/default-active.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)

    db.add(RecordingJob(id="stage9-active-job", camera_id=camera.id, state="recording", started_at=datetime.utcnow()))
    db.commit()
    active_plan = storage_migration_apply_plan(db, target_root_id="root_target")
    assert active_plan["apply_available"] is False
    assert any(blocker["reason"] == "active_recording_jobs" for blocker in active_plan["blockers"])

    db.query(RecordingJob).delete()
    db.commit()
    stale = apply_storage_migration(db, target_root_id="root_target", expected_plan_id="tampered")
    assert stale["status"] == "blocked"
    assert stale["mutation_performed"] is False
    assert any(blocker["reason"] == "stale_or_tampered_plan" for blocker in stale["blockers"])


def test_storage_migration_apply_copy_only_success_preserves_source_and_foreign_sentinel(db):
    camera = add_camera(db)
    source_file = write_storage_file("kmvms/recordings/default-apply.mkv", b"owned-video")
    foreign_sentinel = Path(settings.storage_root) / "Surveillance" / "foreign-sentinel.mkv"
    foreign_sentinel.parent.mkdir(parents=True, exist_ok=True)
    foreign_sentinel.write_bytes(b"foreign")
    segment = add_segment(db, camera, relative_path="kmvms/recordings/default-apply.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)

    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    assert plan["apply_available"] is True
    result = apply_storage_migration(db, target_root_id="root_target", expected_plan_id=plan["plan_id"])
    db.refresh(segment)

    assert result["status"] == "completed"
    assert result["verification_method"] == "sha256_streaming_size_stat_source_stability"
    assert result["checksum_algorithm"] == "sha256"
    assert result["verified_item_count"] == 1
    assert result["verified_bytes"] == len(b"owned-video")
    assert result["source_preserved"] is True
    assert result["cleanup_pending"] is True
    assert result["recorder_runtime_affected"] is False
    assert result["executed"][0]["copy_finalized"] is True
    assert result["executed"][0]["metadata_update_staged"] is True
    assert result["executed"][0]["metadata_persisted"] is True
    assert segment.archive_root_id == "root_target"
    assert source_file.exists()
    assert (target_root / "kmvms/recordings/default-apply.mkv").read_bytes() == b"owned-video"
    assert not list((target_root / "kmvms/recordings").glob(".kmvms_migration_tmp_*"))
    assert foreign_sentinel.read_bytes() == b"foreign"


def test_storage_migration_apply_checksum_mismatch_preserves_source_and_metadata(db, monkeypatch):
    from app.services import recording_storage

    camera = add_camera(db)
    source_file = write_storage_file("kmvms/recordings/checksum-mismatch.mkv", b"owned-video")
    segment = add_segment(db, camera, relative_path="kmvms/recordings/checksum-mismatch.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)
    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    original_root_id = segment.archive_root_id
    real_checksum = recording_storage._file_checksum

    def mismatched_checksum(path):
        value = real_checksum(path)
        return "0" * 64 if path.name.startswith(".kmvms_migration_tmp_") else value

    monkeypatch.setattr(recording_storage, "_file_checksum", mismatched_checksum)
    result = apply_storage_migration(db, target_root_id="root_target", expected_plan_id=plan["plan_id"])
    db.refresh(segment)

    assert result["status"] == "failed"
    assert any(blocker["reason"] == "checksum_mismatch" for blocker in result["blockers"])
    assert segment.archive_root_id == original_root_id
    assert source_file.exists()
    assert not (target_root / "kmvms/recordings/checksum-mismatch.mkv").exists()
    assert not list((target_root / "kmvms/recordings").glob(".kmvms_migration_tmp_*"))


def test_storage_migration_apply_source_changed_during_copy_preserves_metadata(db, monkeypatch):
    from app.services import recording_storage

    camera = add_camera(db)
    source_file = write_storage_file("kmvms/recordings/source-changed.mkv", b"owned-video")
    segment = add_segment(db, camera, relative_path="kmvms/recordings/source-changed.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)
    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    original_root_id = segment.archive_root_id
    real_copy2 = recording_storage.shutil.copy2

    def changing_copy(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        Path(src).write_bytes(b"owned-video-changed")
        return result

    monkeypatch.setattr(recording_storage.shutil, "copy2", changing_copy)
    result = apply_storage_migration(db, target_root_id="root_target", expected_plan_id=plan["plan_id"])
    db.refresh(segment)

    assert result["status"] == "failed"
    assert any(blocker["reason"] == "source_changed_during_copy" for blocker in result["blockers"])
    assert segment.archive_root_id == original_root_id
    assert source_file.exists()
    assert source_file.read_bytes() == b"owned-video-changed"
    assert not (target_root / "kmvms/recordings/source-changed.mkv").exists()


def test_storage_migration_apply_temp_collision_blocks_without_metadata_update(db, monkeypatch):
    import uuid

    camera = add_camera(db)
    source_file = write_storage_file("kmvms/recordings/temp-collision.mkv", b"owned-video")
    segment = add_segment(db, camera, relative_path="kmvms/recordings/temp-collision.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    target_dir = target_root / KMVMS_RECORDINGS_NAMESPACE
    target_dir.mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)
    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    temp_name = ".kmvms_migration_tmp_fixedhex_temp-collision.mkv"
    (target_dir / temp_name).write_bytes(b"existing-temp")
    original_root_id = segment.archive_root_id

    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="fixedhex"))
    result = apply_storage_migration(db, target_root_id="root_target", expected_plan_id=plan["plan_id"])
    db.refresh(segment)

    assert result["status"] == "failed"
    assert any(blocker["reason"] == "temp_target_collision" for blocker in result["blockers"])
    assert segment.archive_root_id == original_root_id
    assert source_file.exists()
    assert (target_dir / temp_name).read_bytes() == b"existing-temp"


def test_storage_migration_apply_finalization_failure_preserves_metadata_and_source(db, monkeypatch):
    camera = add_camera(db)
    source_file = write_storage_file("kmvms/recordings/finalization-fails.mkv", b"owned-video")
    segment = add_segment(db, camera, relative_path="kmvms/recordings/finalization-fails.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)
    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    original_root_id = segment.archive_root_id
    real_replace = Path.replace

    def fail_replace(self, target):
        if self.name.startswith(".kmvms_migration_tmp_"):
            raise OSError("simulated finalization failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    result = apply_storage_migration(db, target_root_id="root_target", expected_plan_id=plan["plan_id"])
    db.refresh(segment)

    assert result["status"] == "failed"
    assert any(blocker["reason"] == "finalization_failed" for blocker in result["blockers"])
    assert segment.archive_root_id == original_root_id
    assert source_file.exists()
    assert not (target_root / "kmvms/recordings/finalization-fails.mkv").exists()
    assert not list((target_root / "kmvms/recordings").glob(".kmvms_migration_tmp_*"))


def test_storage_migration_apply_post_final_checksum_failure_reports_cleanup_pending(db, monkeypatch):
    from app.services import recording_storage

    camera = add_camera(db)
    source_file = write_storage_file("kmvms/recordings/post-final-checksum.mkv", b"owned-video")
    segment = add_segment(db, camera, relative_path="kmvms/recordings/post-final-checksum.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)
    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    original_root_id = segment.archive_root_id
    real_checksum = recording_storage._file_checksum

    def final_mismatched_checksum(path):
        value = real_checksum(path)
        return "0" * 64 if target_root in Path(path).parents and path.name == "post-final-checksum.mkv" else value

    monkeypatch.setattr(recording_storage, "_file_checksum", final_mismatched_checksum)
    result = apply_storage_migration(db, target_root_id="root_target", expected_plan_id=plan["plan_id"])
    db.refresh(segment)

    assert result["status"] == "failed"
    assert any(blocker["reason"] == "final_checksum_mismatch" for blocker in result["blockers"])
    assert segment.archive_root_id == original_root_id
    assert source_file.exists()
    assert (target_root / "kmvms/recordings/post-final-checksum.mkv").exists()
    failed = result["executed"][0]
    assert failed["copy_finalized"] is True
    assert failed["metadata_persisted"] is False
    assert failed["cleanup_pending"] is True
    assert failed["manual_review_required"] is True
    assert str(settings.storage_root) not in json.dumps(result)


def test_storage_migration_apply_source_changed_after_finalization_reports_manual_review(db, monkeypatch):
    camera = add_camera(db)
    source_file = write_storage_file("kmvms/recordings/source-after-final.mkv", b"owned-video")
    segment = add_segment(db, camera, relative_path="kmvms/recordings/source-after-final.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)
    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    original_root_id = segment.archive_root_id
    real_replace = Path.replace

    def changing_replace(self, target):
        result = real_replace(self, target)
        source_file.write_bytes(b"owned-video-changed")
        return result

    monkeypatch.setattr(Path, "replace", changing_replace)
    result = apply_storage_migration(db, target_root_id="root_target", expected_plan_id=plan["plan_id"])
    db.refresh(segment)

    assert result["status"] == "failed"
    assert any(blocker["reason"] == "source_changed_after_finalization" for blocker in result["blockers"])
    assert segment.archive_root_id == original_root_id
    assert source_file.exists()
    assert (target_root / "kmvms/recordings/source-after-final.mkv").exists()
    failed = result["executed"][0]
    assert failed["copy_finalized"] is True
    assert failed["metadata_persisted"] is False
    assert failed["cleanup_pending"] is True
    assert failed["manual_review_required"] is True


def test_storage_migration_apply_multi_item_rollback_does_not_report_metadata_persisted(db, monkeypatch):
    from app.services import recording_storage

    camera = add_camera(db)
    first_source = write_storage_file("kmvms/recordings/rollback-first.mkv", b"first-video")
    second_source = write_storage_file("kmvms/recordings/rollback-second.mkv", b"second-video")
    first = add_segment(db, camera, relative_path="kmvms/recordings/rollback-first.mkv")
    second = add_segment(db, camera, relative_path="kmvms/recordings/rollback-second.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, target_root, root_id="root_target", active=False)
    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    first_original_root = first.archive_root_id
    second_original_root = second.archive_root_id
    real_copy2 = recording_storage.shutil.copy2

    def fail_second_copy(src, dst, *args, **kwargs):
        if Path(src).name == "rollback-second.mkv":
            raise OSError("simulated second copy failure")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(recording_storage.shutil, "copy2", fail_second_copy)
    result = apply_storage_migration(db, target_root_id="root_target", expected_plan_id=plan["plan_id"])
    db.refresh(first)
    db.refresh(second)

    assert result["status"] == "failed"
    assert first.archive_root_id == first_original_root
    assert second.archive_root_id == second_original_root
    assert first_source.exists()
    assert second_source.exists()
    assert (target_root / "kmvms/recordings/rollback-first.mkv").exists()
    assert not (target_root / "kmvms/recordings/rollback-second.mkv").exists()
    finalized = result["executed"][0]
    assert finalized["copy_finalized"] is True
    assert finalized["metadata_update_staged"] is True
    assert finalized["metadata_persisted"] is False
    assert finalized["cleanup_pending"] is True
    assert finalized["manual_review_required"] is True
    assert finalized["result"] == "copy_finalized_metadata_rolled_back"


def test_storage_migration_apply_blocks_traversal_symlink_overlap_and_target_collision(db):
    camera = add_camera(db)
    write_storage_file("kmvms/recordings/collision.mkv", b"owned-video")
    add_segment(db, camera, relative_path="kmvms/recordings/collision.mkv")
    add_segment(db, camera, relative_path="../escape.mkv")
    ensure_archive_roots(db)
    target_root = Path(settings.storage_root).parent / "archive-target"
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    (target_root / "kmvms/recordings/collision.mkv").write_bytes(b"existing")
    add_archive_root(db, target_root, root_id="root_target", active=False)

    plan = storage_migration_apply_plan(db, target_root_id="root_target")
    reasons = {blocker["reason"] for blocker in plan["blockers"]}
    assert "target_collision" in reasons
    assert "path_outside_archive_root" in reasons

    overlap_root = Path(settings.storage_root) / "nested-root"
    (overlap_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    add_archive_root(db, overlap_root, root_id="root_overlap", active=False)
    overlap_plan = storage_migration_apply_plan(db, target_root_id="root_overlap")
    assert any(blocker["reason"] == "archive_root_overlap" for blocker in overlap_plan["blockers"])


def test_serialized_settings_expose_ui_storage_and_format_contract(db):
    data = serialize_settings(get_system_settings(db))

    assert data["storage_root"] == settings.storage_root
    assert data["storage_recordings_path"].endswith("kmvms/recordings")
    assert data["storage_change_requires"] == "installer_or_deploy_remount"
    assert data["recording_profile"] == "reliability"
    assert data["recording_format_contract"]["profile_mapping"] == {
        "reliability": "mkv",
        "compatibility": "mp4",
    }


def test_auto_free_space_setting_defaults_off_and_patches_explicitly(db):
    initial = serialize_settings(get_system_settings(db))
    assert initial["auto_free_space_cleanup_enabled"] is False
    assert initial["auto_free_space_warning_threshold_percent"] == 10.0
    assert initial["auto_free_space_cleanup_threshold_percent"] == 5.0
    assert initial["auto_free_space_critical_threshold_percent"] == 1.0

    result = patch_settings(
        SettingsUpdateRequest(auto_free_space_cleanup_enabled=True),
        FakeRequest(),
        db=db,
        current_user=actor("owner"),
    )

    refreshed = get_system_settings(db)
    assert refreshed.auto_free_space_cleanup_enabled is True
    assert result["auto_free_space_cleanup_enabled"] is True


def test_reconciliation_product_summary_classifies_archive_integrity_cases(db, monkeypatch):
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    camera = add_camera(db)
    active_job = RecordingJob(id="stage2-active-job", camera_id=camera.id, state="recording", started_at=datetime.utcnow())
    db.add(active_job)
    db.commit()
    monkeypatch.setattr(recording_reconciliation, "_probe_media_file", lambda path: (False, "probe_failed", "simulated probe failure") if "corrupt" in str(path) else (True, "probe_ok", None))

    add_segment(db, camera, relative_path="kmvms/recordings/missing.mkv", status="finalized")
    write_storage_file("kmvms/recordings/zero.mkv", b"")
    add_segment(db, camera, relative_path="kmvms/recordings/zero.mkv", status="finalized")
    write_storage_file("kmvms/recordings/corrupt.mkv", b"video")
    add_segment(db, camera, relative_path="kmvms/recordings/corrupt.mkv", status="finalized")
    write_storage_file("kmvms/recordings/partial.mkv", b"video")
    add_segment(db, camera, relative_path="kmvms/recordings/partial.mkv", status="writing", job_id=active_job.id)
    write_storage_file("kmvms/recordings/stale.mkv", b"video")
    add_segment(db, camera, relative_path="kmvms/recordings/stale.mkv", status="writing", updated_at=datetime.utcnow() - recording_reconciliation.STALE_WRITING_AFTER - recording_reconciliation.timedelta(minutes=1))
    add_segment(db, camera, relative_path="../escape.mkv", status="finalized")
    add_segment(db, camera, relative_path=None, status="finalized")
    write_storage_file("kmvms/recordings/orphan.mkv", b"video")
    write_storage_file("camera_1_2026-01-01-00-00-00.mkv", b"video")
    write_storage_file("legacy/old.mkv", b"video")
    write_storage_file("surveillance/foreign.mkv", b"video")
    write_storage_file("misc/unknown.mkv", b"video")

    summary = reconcile_recordings(db, mode="dry_run", write_audit=False)
    counts = summary["classification_counts"]

    assert summary["mode"] == "dry_run"
    assert summary["status"] == "problems_found"
    assert summary["deleted_files_count"] == 0
    assert counts["missing_file"] == 1
    assert counts["zero_size_file"] == 1
    assert counts["corrupted_file"] == 1
    assert counts["partial_file"] == 1
    assert counts["stale_writing_segment"] == 1
    assert counts["path_outside_storage"] == 1
    assert counts["invalid_path"] == 1
    assert counts["orphan_file"] == 1
    assert counts["pre_metadata_km_vms_file"] == 1
    assert counts["legacy_archive_file"] == 1
    assert counts["foreign_file"] == 1
    assert counts["unknown_file"] == 1
    assert summary["cleanup_candidates_summary"]["review_only"] is True
    assert summary["cleanup_candidates_summary"]["count"] == 3
    assert summary["problem_details"]["total_problem_count"] >= 12
    assert summary["problem_details"]["category_counts"]["missing_file"] == 1
    assert summary["problem_details"]["raw_absolute_paths_included"] is False
    assert settings.storage_root not in json.dumps(summary["problem_details"])
    assert all(item["safe_action_status"] in {"none", "future_safe_cleanup_possible", "manual_review_required"} for item in summary["problem_details"]["categories"])
    assert summary["per_camera"][0]["camera_id"] == camera.id
    assert summary["per_camera"][0]["problem_count"] >= 7

    diagnostics = reconciliation_diagnostics(db)
    assert diagnostics["classification_counts"]["legacy_archive_file"] == 1
    assert "samples" not in diagnostics["cleanup_candidates"]
    assert settings.storage_root not in str(diagnostics)


def test_storage_status_exposes_structured_problem_details_without_raw_paths(db):
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    camera = add_camera(db)
    add_segment(db, camera, relative_path="kmvms/recordings/missing-status.mkv", status="finalized")
    write_storage_file("kmvms/recordings/orphan-status.mkv", b"video")

    summary = build_storage_monitoring_summary(db, write_audit=False)
    details = summary["storage_operations"]["reconciliation"]["problem_details"]

    assert details["total_problem_count"] == 2
    assert details["category_counts"] == {"missing_file": 1, "orphan_file": 1}
    assert details["raw_absolute_paths_included"] is False
    assert settings.storage_root not in json.dumps(details)
    assert details["categories"][0]["safe_action_status"] == "manual_review_required"


def test_archive_root_selection_payload_checks_and_adds_inactive_root(db, monkeypatch):
    from app.routers import storage as storage_router
    from app.services import setup_storage

    monkeypatch.setattr(setup_storage, "BLOCKED_PATHS", setup_storage.BLOCKED_PATHS - {"/tmp"})
    host_root = Path(tempfile.mkdtemp(prefix="km-vms-archive-root-outside-approved-base-")) / "host-volume"
    host_root.mkdir(parents=True)
    control = Path(settings.storage_install_control)
    control.mkdir(parents=True, exist_ok=True)
    (control / DISCOVERY_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created_at": "2026-07-07T00:00:00Z",
                "host_visibility": True,
                "candidates": [{"id": "host-volume", "path": str(host_root), "filesystem_type": "ext4", "writable": True}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = storage_router.ArchiveRootCreateRequest(candidate_id="host-volume", folder_name="Archive2")
    with pytest.raises(ValueError, match="archive_root_outside_approved_storage_base"):
        sanitize_archive_root_path(str(host_root / "Archive2"), allow_create=True)
    created = storage_router.create_archive_root(payload, db=db, current_user=actor("owner"))

    assert created["configured_path"] == str(host_root / "Archive2")
    assert created["is_active"] is False
    assert created["is_available"] is False
    assert not (host_root / "Archive2" / KMVMS_RECORDINGS_NAMESPACE).exists()


def test_archive_root_activation_allows_active_jobs_to_finish_current_session(db):
    from app.routers import storage as storage_router

    active_root = Path(settings.storage_root).resolve(strict=False)
    target_root = Path(tempfile.mkdtemp(prefix="km-vms-archive-root-switch-")).resolve(strict=False)
    (active_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    (target_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    ensure_archive_roots(db)
    target = ArchiveRoot(
        id="switch-target-root",
        label="Switch target",
        root_path=str(target_root),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        last_seen_at=datetime.utcnow(),
    )
    camera = add_camera(db, name="stage2_switch_camera")
    db.add(target)
    db.add(
        RecordingJob(
            id="active-switch-job",
            camera_id=camera.id,
            state="recording",
            started_at=datetime.utcnow(),
        )
    )
    db.commit()

    result = storage_router.activate_archive_root(
        "switch-target-root",
        storage_router.ArchiveRootActivateRequest(confirm=True),
        db=db,
        current_user=actor("owner"),
    )

    assert result["is_active"] is True
    assert active_archive_root(db).id == "switch-target-root"
    control = Path(settings.storage_install_control)
    selection_control = (control / SELECTION_CONTROL_FILE).read_text(encoding="utf-8")
    activation_request_control = (control / ACTIVATION_REQUEST_CONTROL_FILE).read_text(encoding="utf-8")
    apply_state = json.loads((control / APPLY_STATUS_FILE).read_text(encoding="utf-8"))
    assert f"selected_host_path={target_root}" in selection_control
    assert f"selected_mount_path={target_root.parent}" in selection_control
    assert f"folder_name={target_root.name}" in selection_control
    assert "status=requested" in activation_request_control
    assert apply_state["status"] == "activation_requested"
    assert apply_state["next_action"] == "runtime_storage_activation"


def test_archive_root_delete_removes_inactive_root_and_hides_its_recordings(db):
    from app.routers import recordings as recordings_router
    from app.routers import storage as storage_router

    active_root = Path(settings.storage_root).resolve(strict=False)
    inactive_root = Path(tempfile.mkdtemp(prefix="km-vms-archive-root-delete-")).resolve(strict=False)
    (active_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    (inactive_root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True, exist_ok=True)
    ensure_archive_roots(db)
    root = ArchiveRoot(
        id="delete-target-root",
        label="Delete target",
        root_path=str(inactive_root),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        last_seen_at=datetime.utcnow(),
    )
    camera = add_camera(db, name="stage2_delete_root_camera")
    relative = "kmvms/recordings/root-delete.mkv"
    file_path = inactive_root / relative
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"root-delete-video")
    db.add(root)
    db.commit()
    segment = add_segment(
        db,
        camera,
        relative_path=relative,
        status="finalized",
        archive_root_id="delete-target-root",
    )
    segment.size_bytes = file_path.stat().st_size
    db.add(segment)
    db.commit()

    result = storage_router.delete_archive_root(
        "delete-target-root",
        storage_router.ArchiveRootDeleteRequest(confirm=True),
        db=db,
        current_user=actor("owner"),
    )

    assert result["ok"] is True
    assert result["segments_deleted"] == 1
    assert result["files_deleted"] == 1
    assert not file_path.exists()
    assert db.get(ArchiveRoot, "delete-target-root") is None
    refreshed = db.get(RecordingSegment, segment.id)
    assert refreshed.status == "deleted"
    assert refreshed.deleted_at is not None
    assert refreshed.archive_root_id is None
    listed = recordings_router.list_recordings(db=db, current_user=actor("owner"))
    assert listed["total"] == 0


def test_archive_root_delete_blocks_active_root(db):
    from app.routers import storage as storage_router

    ensure_archive_roots(db)
    active = active_archive_root(db)

    with pytest.raises(HTTPException) as exc:
        storage_router.delete_archive_root(
            active.id,
            storage_router.ArchiveRootDeleteRequest(confirm=True),
            db=db,
            current_user=actor("owner"),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "archive_root_delete_active_root_blocked"


def test_archive_root_create_returns_structured_problem_for_blocker(db, monkeypatch):
    from app.routers import storage as storage_router
    from app.services import setup_storage

    monkeypatch.setattr(setup_storage, "BLOCKED_PATHS", setup_storage.BLOCKED_PATHS - {"/tmp"})
    host_root = Path(tempfile.mkdtemp(prefix="km-vms-archive-root-blocked-")) / "host-volume"
    target = host_root / "Archive2"
    target.mkdir(parents=True)
    (target / "foreign.txt").write_text("not km vms", encoding="utf-8")
    control = Path(settings.storage_install_control)
    control.mkdir(parents=True, exist_ok=True)
    (control / DISCOVERY_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created_at": "2026-07-07T00:00:00Z",
                "host_visibility": True,
                "candidates": [{"id": "host-volume-blocked", "path": str(host_root), "filesystem_type": "ext4", "writable": True}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = storage_router.ArchiveRootCreateRequest(candidate_id="host-volume-blocked", folder_name="Archive2")
    with pytest.raises(HTTPException) as exc:
        storage_router.create_archive_root(payload, db=db, current_user=actor("owner"))

    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "non_empty_unmarked_folder"
    assert exc.value.detail["blockers"] == [{"reason": "non_empty_unmarked_folder"}]


def test_archive_root_manual_path_is_not_available_from_storage_page_api():
    from app.routers import storage as storage_router

    payload = storage_router.ArchiveRootCreateRequest(candidate_id="manual", folder_name="Archive2")
    with pytest.raises(ValueError, match="manual_archive_root_path_disabled"):
        storage_router._archive_root_path_from_payload(payload)

    for blocked_payload in [{"root_path": "/Volume3/Archive2"}, {"candidate_id": "volume3", "folder_name": "Archive2", "manual_root_path": "/Volume3"}]:
        with pytest.raises(ValidationError):
            storage_router.ArchiveRootCreateRequest(**blocked_payload)


def test_reconciliation_dry_run_and_apply_safe_are_non_destructive_and_do_not_adopt_files(db, monkeypatch):
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    camera = add_camera(db)
    missing = add_segment(db, camera, relative_path="kmvms/recordings/missing-apply.mkv", status="finalized")
    foreign = add_segment(db, camera, relative_path="kmvms/recordings/foreign-owned.mkv", status="finalized", ownership="foreign", source="import")
    orphan_path = write_storage_file("kmvms/recordings/orphan-apply.mkv", b"video")
    original_status = missing.status
    original_count = db.query(RecordingSegment).count()

    monkeypatch.setattr(Path, "unlink", lambda self, *args, **kwargs: (_ for _ in ()).throw(AssertionError("reconciliation must not delete files")))

    dry_run = reconcile_recordings(db, mode="dry_run", write_audit=False)
    db.refresh(missing)
    assert dry_run["deleted_files_count"] == 0
    assert missing.status == original_status
    assert orphan_path.exists()

    apply_one = reconcile_recordings(db, mode="apply_safe", write_audit=False)
    db.refresh(missing)
    db.refresh(foreign)
    assert apply_one["apply_safe_summary"]["deleted_files_count"] == 0
    assert apply_one["deleted_product_metadata_count"] == 0
    assert missing.status == "missing"
    assert missing.ownership == "KM VMS"
    assert missing.source == "recorder"
    assert foreign.ownership == "foreign"
    assert foreign.source == "import"
    assert db.query(RecordingSegment).count() == original_count
    assert orphan_path.exists()

    apply_two = reconcile_recordings(db, mode="apply_safe", write_audit=False)
    assert apply_two["deleted_files_count"] == 0
    assert orphan_path.exists()


def test_reconciliation_apply_safe_rolls_back_metadata_when_storage_scan_fails(db, monkeypatch):
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    camera = add_camera(db)
    segment = add_segment(db, camera, relative_path="kmvms/recordings/missing-rollback.mkv", status="finalized")

    def fail_scan():
        raise OSError("/private/storage unavailable")

    monkeypatch.setattr(recording_reconciliation, "_iter_storage_video_files", fail_scan)

    summary = reconcile_recordings(db, mode="apply_safe", write_audit=False)
    db.refresh(segment)

    assert summary["status"] == "storage_unavailable"
    assert summary["updated_metadata_count"] == 0
    assert summary["deleted_files_count"] == 0
    assert segment.status == "finalized"
    assert "/private/storage" not in str(summary)
