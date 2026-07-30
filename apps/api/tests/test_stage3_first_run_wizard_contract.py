import json
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.permissions import ROLE_OWNER
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers.settings import SetupRequest, setup, setup_storage_discovery, system_status
from app.services.setup_storage import APPLY_STATUS_FILE, CONTAINER_ARCHIVE_PATH, SELECTION_FILE, SETUP_COMPLETE_FILE
from app.services.system_settings import get_system_settings


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage3_first_run_wizard_")
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


def payload(**overrides) -> SetupRequest:
    data = {
        "username": "owner_admin",
        "password": "stage3-password",
        "password_confirm": "stage3-password",
        "timezone": "UTC",
        "language": "en",
        "storage_path": "/storage/archive",
        "recording_format": "mkv",
    }
    data.update(overrides)
    return SetupRequest(**data)


def assert_no_partial_owner(db):
    row = get_system_settings(db)
    assert row.system_initialized is False
    assert db.query(User).count() == 0


def write_storage_selection(host_path: str | None = None, apply_status: str = "active"):
    control = Path(settings.storage_install_control)
    control.mkdir(parents=True, exist_ok=True)
    selected = host_path or str(control.parent / "host-archive")
    (control / SELECTION_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selected_host_path": selected,
                "selected_mount_path": str(Path(selected).parent),
                "folder_name": Path(selected).name,
                "container_archive_path": CONTAINER_ARCHIVE_PATH,
                "candidate_id": "stage3-test-candidate",
                "selected_at": "2026-05-07T00:00:00Z",
                "apply_status": apply_status,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (control / APPLY_STATUS_FILE).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": apply_status,
                "selected_host_path": selected,
                "container_archive_path": CONTAINER_ARCHIVE_PATH,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_password_confirmation_mismatch_does_not_create_partial_user_or_initialize(db):
    write_storage_selection()
    with pytest.raises(HTTPException) as exc:
        setup(payload(password_confirm="different-password"), db=db, request=FakeRequest())

    assert exc.value.status_code == 422
    assert "password_confirm" in str(exc.value.detail)
    assert_no_partial_owner(db)


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("username", "bad name", "username"),
        ("language", "de", "language"),
        ("timezone", "No/Such_Zone", "timezone"),
        ("recording_format", "avi", "recording_format"),
    ],
)
def test_setup_rejects_invalid_wizard_values_without_partial_owner(db, field, value, detail):
    write_storage_selection()
    with pytest.raises(HTTPException) as exc:
        setup(payload(**{field: value}), db=db, request=FakeRequest())

    assert exc.value.status_code == 422
    assert detail in str(exc.value.detail)
    assert_no_partial_owner(db)


def test_successful_setup_creates_exactly_one_owner_and_initializes_system(db):
    write_storage_selection()
    before = system_status(db)

    response = setup(payload(recording_format="mp4"), db=db, request=FakeRequest())

    owners = db.query(User).filter(User.role == ROLE_OWNER, User.is_active == True).all()  # noqa: E712
    row = get_system_settings(db)
    assert before["setup_required"] is True
    assert response["ok"] is True
    assert row.system_initialized is True
    assert row.language == "en"
    assert row.timezone == "UTC"
    assert row.recording_format == "mp4"
    assert len(owners) == 1
    assert owners[0].username == "owner_admin"


def test_replay_after_setup_does_not_duplicate_owner_and_closes_setup_endpoints(db):
    write_storage_selection()
    setup(payload(), db=db, request=FakeRequest())

    with pytest.raises(HTTPException) as replay:
        setup(payload(username="second_owner"), db=db, request=FakeRequest())
    with pytest.raises(HTTPException) as storage_closed:
        setup_storage_discovery(db=db)

    assert replay.value.status_code == 409
    assert storage_closed.value.status_code == 409
    assert db.query(User).filter(User.role == ROLE_OWNER).count() == 1
    assert system_status(db) == {"initialized": True, "setup_required": False, "language": "en", "timezone": "UTC"}


def test_failed_storage_validation_rolls_back_owner_and_initialization(db):
    write_storage_selection()
    settings.storage_root = str(Path(settings.storage_root) / "not-a-directory")
    Path(settings.storage_root).parent.write_text("blocked", encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        setup(payload(), db=db, request=FakeRequest())

    assert exc.value.status_code == 422
    assert_no_partial_owner(db)


def test_setup_response_and_audit_metadata_do_not_expose_password_or_hash(db):
    write_storage_selection()
    response = setup(payload(), db=db, request=FakeRequest())
    event = db.query(AuditEvent).filter(AuditEvent.event_type == "system.setup_completed").one()
    rendered = json.dumps({"response": response, "metadata": event.event_metadata}, default=str).lower()

    assert "stage3-password" not in rendered
    assert "password_hash" not in rendered
    assert "secret" not in rendered
    assert "token" not in rendered


def test_setup_rejects_missing_storage_confirmation_without_partial_owner(db):
    with pytest.raises(HTTPException) as exc:
        setup(payload(), db=db, request=FakeRequest())

    assert exc.value.status_code == 422
    assert "storage_confirmation" in str(exc.value.detail)
    assert_no_partial_owner(db)


def test_setup_rejects_naked_container_storage_confirmation(db):
    write_storage_selection(CONTAINER_ARCHIVE_PATH, apply_status="active")

    with pytest.raises(HTTPException) as exc:
        setup(payload(), db=db, request=FakeRequest())

    assert exc.value.status_code == 422
    assert "selected_host_path_must_be_host_path" in str(exc.value.detail)
    assert_no_partial_owner(db)


def test_concurrent_setup_attempts_create_exactly_one_owner(db):
    write_storage_selection()
    race_db = Path(settings.storage_install_control).parent / "race.db"
    engine = create_engine(f"sqlite:///{race_db}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    results = []
    lock = threading.Lock()

    def run_setup(username):
        session = Session()
        try:
            setup(payload(username=username), db=session, request=FakeRequest())
            outcome = "created"
        except HTTPException as exc:
            outcome = f"blocked:{exc.status_code}"
        finally:
            session.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run_setup, args=(f"owner_{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("created") == 1
    assert any(item == "blocked:409" for item in results)
    session = Session()
    try:
        assert session.query(User).filter(User.role == ROLE_OWNER).count() == 1
        assert get_system_settings(session).system_initialized is True
    finally:
        session.close()


def test_setup_writes_setup_complete_signal_for_activation_helper(db):
    write_storage_selection()

    result = setup(payload(), db=db, request=FakeRequest())

    control = Path(settings.storage_install_control)
    setup_complete = json.loads((control / SETUP_COMPLETE_FILE).read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert setup_complete["status"] == "completed"


def test_setup_rejects_non_active_storage_confirmation_without_partial_owner(db):
    write_storage_selection(apply_status="activation_requested")

    with pytest.raises(HTTPException) as exc:
        setup(payload(), db=db, request=FakeRequest())

    assert exc.value.status_code == 422
    assert "storage_apply_status_not_active" in str(exc.value.detail)
    assert_no_partial_owner(db)
