import json
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.permissions import ROLE_OWNER
from app.db.session import Base
from app.models.setup_lock import SetupLock
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers.settings import SettingsUpdateRequest, SetupRequest, setup
from app.services.setup_storage import (
    APPLY_STATUS_FILE,
    CONTAINER_ARCHIVE_PATH,
    RUNTIME_CONVERGENCE_FILE,
    SELECTION_CONTROL_FILE,
    SELECTION_FILE,
)
from app.services.system_settings import get_system_settings, serialize_settings


ROOT = Path(__file__).resolve().parents[3]


class FakeRequest:
    headers = {}
    client = type("Client", (), {"host": "127.0.0.1"})()


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.TemporaryDirectory(prefix="stage4_restart_upgrade_")
    tmp_path = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_control = settings.storage_install_control
    settings.storage_root = str(tmp_path / "archive")
    settings.storage_install_control = str(tmp_path / "install-control")
    from app.services import setup_storage

    healthy = {
        "marker_matches": True,
        "namespace_exists": True,
        "readable": True,
        "writable": True,
    }
    monkeypatch.setattr(
        setup_storage,
        "_initial_storage_runtime_state",
        lambda *_args, **_kwargs: {
            "manifest_matches": True,
            "database_matches": True,
            "canonical": dict(healthy),
            "per_root": dict(healthy),
        },
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        settings.storage_install_control = original_control
        tmp.cleanup()


def write_storage_selection(apply_status="active"):
    control = Path(settings.storage_install_control)
    control.mkdir(parents=True, exist_ok=True)
    selected = str(control.parent / "host archive with spaces")
    request_id = "setup-storage-stage4"
    (control / SELECTION_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selected_host_path": selected,
                "selected_mount_path": str(Path(selected).parent),
                "folder_name": Path(selected).name,
                "container_archive_path": CONTAINER_ARCHIVE_PATH,
                "candidate_id": "stage4-test-candidate",
                "selected_at": "2026-05-07T00:00:00Z",
                "apply_status": apply_status,
                "activation_request_id": request_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (control / SELECTION_CONTROL_FILE).write_text(
        "\n".join(
            [
                "schema_version=1",
                f"selected_host_path={selected}",
                f"selected_mount_path={Path(selected).parent}",
                f"folder_name={Path(selected).name}",
                f"container_archive_path={CONTAINER_ARCHIVE_PATH}",
                "candidate_id=stage4-test-candidate",
                f"apply_status={apply_status}",
                f"activation_request_id={request_id}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (control / APPLY_STATUS_FILE).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": apply_status,
                "request_id": request_id,
                "selected_host_path": selected,
                "container_archive_path": CONTAINER_ARCHIVE_PATH,
                "runtime_proof": {
                    "api_canonical_marker": True,
                    "recorder_canonical_marker": True,
                    "api_default_runtime_marker": True,
                    "api_default_runtime_namespace": True,
                    "api_default_runtime_read_write": True,
                },
            }
        ),
        encoding="utf-8",
    )
    (control / RUNTIME_CONVERGENCE_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request_id,
                "selected_host_path": selected,
                "state": "runtime_verified",
            }
        ),
        encoding="utf-8",
    )


def payload(**overrides):
    data = {
        "username": "owner_stage4",
        "password": "stage4-password",
        "password_confirm": "stage4-password",
        "timezone": "UTC",
        "language": "en",
        "storage_path": "/storage/archive",
        "recording_format": "mkv",
    }
    data.update(overrides)
    return SetupRequest(**data)


def test_db_backed_setup_lock_blocks_duplicate_owner_after_lock_row(db):
    write_storage_selection()
    db.add(SetupLock(name="first_run_setup"))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        setup(payload(username="second_owner"), db=db, request=FakeRequest())

    assert exc.value.status_code == 409
    assert db.query(User).filter(User.role == ROLE_OWNER).count() == 0
    assert get_system_settings(db).system_initialized is False


def test_removed_system_name_is_absent_from_active_contracts(db):
    row = get_system_settings(db)
    assert "system_name" not in SetupRequest.model_fields
    assert "system_name" not in SettingsUpdateRequest.model_fields
    assert "system_name" not in serialize_settings(row)
    assert not hasattr(SystemSettings, "system_name")


def test_install_source_dir_excludes_private_key_material():
    script = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    for pattern in ("--exclude='./.ssh'", "--exclude='id_rsa'", "--exclude='id_ed25519'", "--exclude='*.pem'", "--exclude='*.key'", "--exclude='*.p12'", "--exclude='*.pfx'"):
        assert pattern in script
