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
from app.db.session import Base
from app.models.system_settings import SystemSettings
from app.routers.settings import (
    SetupStorageSelectionRequest,
    get_settings,
    setup_storage_apply,
    setup_storage_discovery,
    setup_storage_preview,
    system_status,
)
from app.services.setup_storage import (
    CONTAINER_ARCHIVE_PATH,
    DISCOVERY_FILE,
    SELECTION_FILE,
    discovery_snapshot,
    validate_folder_name,
)


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.TemporaryDirectory(prefix="stage2_setup_storage_")
    root = Path(tmp.name)
    original_control = settings.storage_install_control
    settings.storage_install_control = str(root / "install-control")
    from app.services import setup_storage

    monkeypatch.setattr(setup_storage, "BLOCKED_PATHS", setup_storage.BLOCKED_PATHS - {"/tmp"})

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session, root
    finally:
        session.close()
        settings.storage_install_control = original_control
        tmp.cleanup()


def write_discovery(root: Path, candidates: list[dict]) -> None:
    control = root / "install-control"
    control.mkdir(parents=True, exist_ok=True)
    (control / DISCOVERY_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created_at": "2026-05-06T00:00:00Z",
                "discovery_source": "test-host-snapshot",
                "host_visibility": True,
                "candidates": candidates,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_discovery_uses_host_snapshot_and_sanitizes_blocked_paths(db):
    _session, root = db
    write_discovery(
        root,
        [
            {"id": "var", "path": "/var/km-vms", "filesystem_type": "ext4", "writable": True},
            {"id": "overlay", "path": "/nas/docker", "filesystem_type": "overlay", "writable": True},
            {"id": "nas", "path": "/mnt/km-vms-archive", "filesystem_type": "ext4", "writable": True},
        ],
    )

    payload = discovery_snapshot()
    by_id = {item["id"]: item for item in payload["candidates"]}

    assert payload["status"] == "ready"
    assert payload["container_archive_path"] == CONTAINER_ARCHIVE_PATH
    assert by_id["var"]["safety_status"] == "blocked"
    assert by_id["var"]["reason"] == "dangerous_system_path"
    assert by_id["overlay"]["safety_status"] == "blocked"
    assert by_id["nas"]["safety_status"] == "allowed"
    for key in ("total_bytes", "used_bytes", "free_bytes", "writable", "safety_status", "reason"):
        assert key in by_id["nas"]


def test_blocked_candidate_cannot_be_applied(db):
    session, root = db
    write_discovery(
        root,
        [{"id": "blocked", "path": "/var/km-vms", "filesystem_type": "ext4", "writable": True}],
    )
    payload = SetupStorageSelectionRequest(candidate_id="blocked", folder_name="Archive")

    with pytest.raises(HTTPException) as exc:
        setup_storage_apply(payload, db=session)

    assert exc.value.status_code == 422
    assert "blocked" in str(exc.value.detail)


def test_authorized_settings_exposes_host_archive_path_without_public_status_leak(db, monkeypatch):
    session, _root = db
    monkeypatch.setenv("STORAGE_HOST_ROOT", "/mnt/km-vms-archive")
    session.add(SystemSettings(system_initialized=True, timezone="UTC", language="en", storage_path="/storage/archive"))
    session.commit()

    settings_payload = get_settings(db=session, current_user=object())
    public_payload = system_status(db=session)

    assert settings_payload["archive_primary_path"] == "/mnt/km-vms-archive"
    assert settings_payload["archive_host_path"] == "/mnt/km-vms-archive"
    assert settings_payload["storage_container_path"] == settings.storage_root
    assert settings_payload["container_runtime_storage_root"] == settings.storage_root
    assert "/mnt/km-vms-archive" not in str(public_payload)


def test_preview_apply_write_pending_selection_without_secrets(db):
    session, root = db
    mount = root / "host-storage"
    mount.mkdir()
    write_discovery(
        root,
        [{"id": "host-storage", "path": str(mount), "filesystem_type": "ext4", "writable": True}],
    )
    payload = SetupStorageSelectionRequest(candidate_id="host-storage", folder_name="KM-VMS-Recordings")

    preview = setup_storage_preview(payload, db=session)
    result = setup_storage_apply(payload, db=session)
    selection = json.loads((root / "install-control" / SELECTION_FILE).read_text(encoding="utf-8"))

    assert preview["final_host_path"] == str(mount / "KM-VMS-Recordings")
    assert result["container_archive_path"] == CONTAINER_ARCHIVE_PATH
    assert result["apply_status"] == "pending_host_helper_restart_required"
    assert result["host_validation_required"] is True
    assert result["write_test"] == {"ok": False, "reason": "pending_host_helper"}
    assert not (mount / "KM-VMS-Recordings").exists()
    assert selection["selected_host_path"] == str(mount / "KM-VMS-Recordings")
    assert selection["selected_mount_path"] == str(mount)
    assert selection["folder_name"] == "KM-VMS-Recordings"
    assert "password" not in json.dumps(selection).lower()


def test_non_empty_unmarked_folder_is_blocked(db):
    session, root = db
    mount = root / "host-storage"
    target = mount / "Archive"
    target.mkdir(parents=True)
    (target / "existing.mp4").write_bytes(b"not ours")
    write_discovery(root, [{"id": "host-storage", "path": str(mount), "filesystem_type": "ext4", "writable": True}])

    payload = SetupStorageSelectionRequest(candidate_id="host-storage", folder_name="Archive")

    with pytest.raises(HTTPException) as exc:
        setup_storage_apply(payload, db=session)
    assert exc.value.status_code == 422
    assert "non_empty_unmarked_folder" in str(exc.value.detail)


def test_setup_storage_endpoints_close_after_initialization(db):
    session, root = db
    session.add(SystemSettings(system_initialized=True, timezone="UTC", language="en", storage_path="/storage/archive"))
    session.commit()
    write_discovery(root, [])

    with pytest.raises(HTTPException) as exc:
        setup_storage_discovery(db=session)
    assert exc.value.status_code == 409


def test_folder_name_rejects_paths_traversal_and_control_characters():
    for value in ("/absolute", "../escape", "nested/path", "nested\\path", "bad\nname"):
        with pytest.raises(ValueError):
            validate_folder_name(value)
