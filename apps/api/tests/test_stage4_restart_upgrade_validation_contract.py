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
from app.models.user import User
from app.routers.settings import SetupRequest, setup
from app.services.setup_storage import CONTAINER_ARCHIVE_PATH, SELECTION_CONTROL_FILE, SELECTION_FILE
from app.services.system_settings import get_system_settings, update_system_settings


ROOT = Path(__file__).resolve().parents[3]


class FakeRequest:
    headers = {}
    client = type("Client", (), {"host": "127.0.0.1"})()


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage4_restart_upgrade_")
    tmp_path = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_control = settings.storage_install_control
    settings.storage_root = str(tmp_path / "archive")
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
        settings.storage_install_control = original_control
        tmp.cleanup()


def write_storage_selection(apply_status="active"):
    control = Path(settings.storage_install_control)
    control.mkdir(parents=True, exist_ok=True)
    selected = str(control.parent / "host archive with spaces")
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
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def payload(**overrides):
    data = {
        "username": "owner_stage4",
        "system_name": "KM VMS Stage 4",
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


def test_system_name_is_saved_serialized_and_patchable(db):
    write_storage_selection()

    setup(payload(system_name="Garage VMS"), db=db, request=FakeRequest())
    row = get_system_settings(db)
    assert row.system_name == "Garage VMS"

    updated = update_system_settings(db, {"system_name": "Office VMS"})
    assert updated.system_name == "Office VMS"


def test_system_name_rejects_control_and_secret_like_values(db):
    with pytest.raises(ValueError):
        update_system_settings(db, {"system_name": "bad\nname"})
    with pytest.raises(ValueError):
        update_system_settings(db, {"system_name": "secret token"})


def test_install_source_dir_excludes_private_key_material():
    script = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    for pattern in ("--exclude='./.ssh'", "--exclude='id_rsa'", "--exclude='id_ed25519'", "--exclude='*.pem'", "--exclude='*.key'", "--exclude='*.p12'", "--exclude='*.pfx'"):
        assert pattern in script
