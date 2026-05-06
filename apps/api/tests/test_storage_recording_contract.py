import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.routers.settings import SettingsUpdateRequest, SetupRequest, patch_settings, setup
from app.services.storage_contract import (
    KMVMS_RECORDINGS_NAMESPACE,
    recording_format_contract,
    storage_contract,
)
from app.services.storage_monitoring import build_storage_monitoring_summary
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
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        container_format="mkv",
        file_extension=".mkv",
        updated_at=updated_at or now,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage2_storage_contract_")
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
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        settings.storage_previews = original_storage_previews
        settings.storage_exports = original_storage_exports
        tmp.cleanup()


def test_settings_serialization_distinguishes_runtime_storage_contract(db):
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
    assert data["storage_root"] == settings.storage_root
    assert data["container_runtime_storage_root"] == settings.storage_root
    assert data["storage_namespace"] == KMVMS_RECORDINGS_NAMESPACE
    assert data["storage_recordings_path"].endswith("kmvms/recordings")
    assert data["storage_editable"] is False
    assert data["storage_change_requires"] == "installer_or_deploy_remount"
    assert data["storage_contract"]["db_storage_path"] == "/legacy/display/path"


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

    result = setup(
        SetupRequest(
            username="stage2_owner",
            password="stage2-password",
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
    assert result["storage_validation"]["setup_storage_path_behavior"] == "requested_path_is_not_runtime_source_of_truth"
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
    assert summary["per_camera"][0]["camera_id"] == camera.id
    assert summary["per_camera"][0]["problem_count"] >= 7

    diagnostics = reconciliation_diagnostics(db)
    assert diagnostics["classification_counts"]["legacy_archive_file"] == 1
    assert "samples" not in diagnostics["cleanup_candidates"]
    assert settings.storage_root not in str(diagnostics)


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
