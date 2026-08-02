import json
import sys
import tempfile
from datetime import datetime
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
    setup_storage_status,
    system_status,
)
from app.services.setup_storage import (
    ACTIVATION_REQUEST_FILE,
    ACTIVATION_REQUEST_CONTROL_FILE,
    APPLY_STATUS_FILE,
    CONTAINER_ARCHIVE_PATH,
    DISCOVERY_FILE,
    RUNTIME_CONVERGENCE_FILE,
    SELECTION_FILE,
    SELECTION_CONTROL_FILE,
    discovery_snapshot,
    queue_runtime_activation,
    storage_confirmation_status,
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
                "created_at": datetime.utcnow().isoformat() + "Z",
                "snapshot_id": "stage2-current-snapshot",
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
    assert by_id["nas"]["safety_status"] == "allowed"
    assert "var" not in by_id
    assert "overlay" not in by_id
    assert payload["hidden_candidate_count"] == 2
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
    assert "storage candidate is blocked" in str(exc.value.detail)


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


def test_preview_apply_queues_activation_without_secrets(db):
    session, root = db
    mount = root / "host-storage"
    mount.mkdir()
    write_discovery(
        root,
        [{"id": "host-storage", "path": str(mount), "filesystem_type": "ext4", "writable": True}],
    )
    payload = SetupStorageSelectionRequest(candidate_id="manual", folder_name="KM-VMS-Recordings", manual_root_path=str(mount))

    preview = setup_storage_preview(payload, db=session)
    result = setup_storage_apply(payload, db=session)
    selection = json.loads((root / "install-control" / SELECTION_FILE).read_text(encoding="utf-8"))
    selection_control = (root / "install-control" / SELECTION_CONTROL_FILE).read_text(encoding="utf-8")
    activation_request = json.loads((root / "install-control" / ACTIVATION_REQUEST_FILE).read_text(encoding="utf-8"))
    activation_request_control = (root / "install-control" / ACTIVATION_REQUEST_CONTROL_FILE).read_text(encoding="utf-8")
    apply_state = json.loads((root / "install-control" / APPLY_STATUS_FILE).read_text(encoding="utf-8"))

    assert preview["final_host_path"] == str(mount / "KM-VMS-Recordings")
    assert result["container_archive_path"] == CONTAINER_ARCHIVE_PATH
    assert result["apply_status"] == "activation_requested"
    assert result["host_validation_required"] is True
    assert result["write_test"] == {"ok": False, "reason": "activation_helper_pending"}
    assert not (mount / "KM-VMS-Recordings").exists()
    assert selection["selected_host_path"] == str(mount / "KM-VMS-Recordings")
    assert selection["selected_mount_path"] == str(mount)
    assert selection["folder_name"] == "KM-VMS-Recordings"
    assert f"selected_host_path={mount / 'KM-VMS-Recordings'}" in selection_control
    assert f"selected_mount_path={mount}" in selection_control
    assert "folder_name=KM-VMS-Recordings" in selection_control
    assert activation_request["status"] == "requested"
    assert "status=requested" in activation_request_control
    assert f"selected_host_path={mount / 'KM-VMS-Recordings'}" in activation_request_control
    assert apply_state["status"] == "activation_requested"
    assert "password" not in json.dumps(selection).lower()

    status_payload = setup_storage_status(db=session)
    assert status_payload["ready"] is False
    assert status_payload["selected_host_path"] == str(mount / "KM-VMS-Recordings")
    assert status_payload["container_archive_path"] == CONTAINER_ARCHIVE_PATH
    assert status_payload["status"] == "activation_requested"
    assert status_payload["next_action"] == "wait_for_storage_activation"


def test_non_empty_unmarked_folder_is_blocked(db):
    session, root = db
    mount = root / "host-storage"
    target = mount / "Archive"
    target.mkdir(parents=True)
    (target / "existing.mp4").write_bytes(b"not ours")
    write_discovery(root, [{"id": "host-storage", "path": str(mount), "filesystem_type": "ext4", "writable": True}])

    payload = SetupStorageSelectionRequest(candidate_id="manual", folder_name="Archive", manual_root_path=str(mount))

    with pytest.raises(HTTPException) as exc:
        setup_storage_apply(payload, db=session)
    assert exc.value.status_code == 422
    assert "non_empty_unmarked_folder" in str(exc.value.detail)


def test_runtime_activation_writes_same_helper_contract_as_first_run(db):
    session, root = db
    mount = root / "host-storage"
    target = mount / "RuntimeArchive"
    mount.mkdir()

    result = queue_runtime_activation(str(target), request_prefix="archive-root")
    selection = json.loads((root / "install-control" / SELECTION_FILE).read_text(encoding="utf-8"))
    selection_control = (root / "install-control" / SELECTION_CONTROL_FILE).read_text(encoding="utf-8")
    activation_request = json.loads((root / "install-control" / ACTIVATION_REQUEST_FILE).read_text(encoding="utf-8"))
    activation_request_control = (root / "install-control" / ACTIVATION_REQUEST_CONTROL_FILE).read_text(encoding="utf-8")
    apply_state = json.loads((root / "install-control" / APPLY_STATUS_FILE).read_text(encoding="utf-8"))

    assert result["apply_status"] == "activation_requested"
    assert selection["selected_host_path"] == str(target)
    assert selection["selected_mount_path"] == str(mount)
    assert selection["folder_name"] == "RuntimeArchive"
    assert f"selected_host_path={target}" in selection_control
    assert f"selected_mount_path={mount}" in selection_control
    assert "folder_name=RuntimeArchive" in selection_control
    assert activation_request["status"] == "requested"
    assert activation_request["request_id"].startswith("archive-root-")
    assert "status=requested" in activation_request_control
    assert apply_state["status"] == "activation_requested"
    assert apply_state["next_action"] == "runtime_storage_activation"


def test_runtime_root_activation_does_not_require_initial_setup_proof(db):
    _session, root = db
    target = root / "host-storage" / "RuntimeArchive"
    target.parent.mkdir()
    queued = queue_runtime_activation(
        str(target),
        request_prefix="archive-root",
        operation_id="archive-root-operation-1",
    )
    control = root / "install-control"
    (control / APPLY_STATUS_FILE).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "active",
                "request_id": queued["request_id"],
                "operation_id": "archive-root-operation-1",
                "selected_host_path": str(target),
                "container_archive_path": CONTAINER_ARCHIVE_PATH,
            }
        ),
        encoding="utf-8",
    )

    status = storage_confirmation_status()

    assert status["ready"] is True
    assert status["operation_id"] == "archive-root-operation-1"


def test_setup_storage_endpoints_close_after_initialization(db):
    session, root = db
    session.add(SystemSettings(system_initialized=True, timezone="UTC", language="en", storage_path="/storage/archive"))
    session.commit()
    write_discovery(root, [])

    with pytest.raises(HTTPException) as exc:
        setup_storage_discovery(db=session)
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        setup_storage_status(db=session)
    assert exc.value.status_code == 409


def test_folder_name_rejects_paths_traversal_and_control_characters():
    for value in ("/absolute", "../escape", "nested/path", "nested\\path", 'bad"name', "bad\nname"):
        with pytest.raises(ValueError):
            validate_folder_name(value)


def test_folder_name_accepts_safe_unicode_and_manual_root(db):
    session, root = db
    manual_root = root / "manual-root"
    manual_root.mkdir()
    payload = SetupStorageSelectionRequest(candidate_id="manual", folder_name="Архив KM VMS", manual_root_path=str(manual_root))

    preview = setup_storage_preview(payload, db=session)

    assert preview["selected_mount_path"] == str(manual_root)
    assert preview["final_host_path"] == str(manual_root / "Архив KM VMS")


@pytest.mark.parametrize(
    ("volume_name", "folder_name"),
    [
        ("volume-default", "KM-VMS-Recordings"),
        ("volume-default", "Custom Archive"),
        ("volume-other", "KM-VMS-Recordings"),
    ],
)
def test_initial_selection_preserves_exact_allowed_volume_and_folder(
    db,
    volume_name,
    folder_name,
):
    session, root = db
    mount = root / volume_name
    mount.mkdir()
    payload = SetupStorageSelectionRequest(
        candidate_id="manual",
        folder_name=folder_name,
        manual_root_path=str(mount),
    )

    result = setup_storage_apply(payload, db=session)
    apply_state = json.loads(
        (root / "install-control" / APPLY_STATUS_FILE).read_text(
            encoding="utf-8"
        )
    )

    assert result["selected_host_path"] == str(mount / folder_name)
    assert apply_state["selected_host_path"] == str(mount / folder_name)
    assert apply_state["request_id"] == result["activation_request_id"]


def _write_active_storage_contract(root: Path, *, apply_request_id: str) -> None:
    control = root / "install-control"
    control.mkdir(parents=True, exist_ok=True)
    selected_path = str(root / "selected-archive")
    selection_request_id = "setup-storage-123"
    (control / SELECTION_FILE).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "selected_host_path": selected_path,
                "container_archive_path": CONTAINER_ARCHIVE_PATH,
                "activation_request_id": selection_request_id,
                "apply_status": "activation_requested",
            }
        ),
        encoding="utf-8",
    )
    (control / APPLY_STATUS_FILE).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "active",
                "request_id": apply_request_id,
                "selected_host_path": selected_path,
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
                "request_id": selection_request_id,
                "selected_host_path": selected_path,
                "state": "runtime_verified",
            }
        ),
        encoding="utf-8",
    )


def test_setup_completion_rejects_request_mismatch(db):
    _session, root = db
    _write_active_storage_contract(root, apply_request_id="different-request")

    status = storage_confirmation_status()

    assert status["ready"] is False
    assert "storage_activation_request_mismatch" in status["errors"]
    assert status["next_action"] == "resolve_storage_activation_error"


def test_setup_completion_rejects_missing_default_runtime_proof(db, monkeypatch):
    _session, root = db
    _write_active_storage_contract(root, apply_request_id="setup-storage-123")
    from app.services import setup_storage

    monkeypatch.setattr(
        setup_storage,
        "_initial_storage_runtime_state",
        lambda *_args, **_kwargs: {
            "manifest_matches": True,
            "database_matches": True,
            "canonical": {
                "marker_matches": True,
                "namespace_exists": True,
                "readable": True,
                "writable": True,
            },
            "per_root": {
                "marker_matches": False,
                "namespace_exists": True,
                "readable": True,
                "writable": True,
            },
        },
    )

    status = storage_confirmation_status()

    assert status["ready"] is False
    assert "storage_default_runtime_proof_failed" in status["errors"]


def test_setup_completion_accepts_matching_canonical_and_default_runtime_proof(
    db,
    monkeypatch,
):
    _session, root = db
    _write_active_storage_contract(root, apply_request_id="setup-storage-123")
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

    status = storage_confirmation_status()

    assert status["ready"] is True
    assert status["errors"] == []
    assert status["next_action"] == "continue_setup"
