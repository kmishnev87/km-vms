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
from app.models.recording import RecordingJob
from app.models.system_settings import SystemSettings
from app.routers.settings import SettingsUpdateRequest, SetupRequest, patch_settings, setup
from app.services.storage_contract import (
    KMVMS_RECORDINGS_NAMESPACE,
    recording_format_contract,
    storage_contract,
)
from app.services.storage_monitoring import build_storage_monitoring_summary
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
