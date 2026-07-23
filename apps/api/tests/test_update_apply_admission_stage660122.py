from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.services import update_apply
from app.services.update_apply import UpdateApplyBlocked


TARGET_VERSION = "9.9.9"
TARGET_COMMIT = "c" * 40
OTHER_VERSION = "10.0.0"
OTHER_COMMIT = "d" * 40
SUBMISSION_A = "11111111-1111-4111-8111-111111111111"
SUBMISSION_B = "22222222-2222-4222-8222-222222222222"


def actor() -> SimpleNamespace:
    return SimpleNamespace(id=1, role="owner", username="owner")


def latest_release(version: str = TARGET_VERSION, commit: str = TARGET_COMMIT) -> dict:
    return {
        "version": version,
        "commit": commit,
        "channel": "stable",
        "source_type": "github_tarball",
        "source_repo": "owner/repo",
        "source_ref": "main",
    }


def apply_candidate() -> dict:
    return {
        "source": "live_check",
        "snapshot": {
            "available": False,
            "fresh": False,
            "age_seconds": None,
            "fresh_for_seconds": 900,
            "version": None,
            "commit_short": None,
            "provider": None,
        },
    }


def candidate_for_expected(_db, *, expected_version=None, expected_commit=None):
    return (
        latest_release(expected_version or TARGET_VERSION, expected_commit or TARGET_COMMIT),
        apply_candidate(),
    )


def signed_ticket(
    submission_id: str,
    *,
    version: str = TARGET_VERSION,
    commit: str = TARGET_COMMIT,
) -> dict:
    proof, _claims = update_apply._issue_submission_proof(
        submission_id=submission_id,
        target_version=version,
        target_commit=commit,
        actor_id=1,
        issued_at=update_apply._utcnow(),
    )
    return {
        "submission_id": submission_id,
        "submission_proof": proof,
        "target_version": version,
        "target_commit": commit,
    }


def apply_ticket(db, ticket: dict) -> dict:
    return update_apply.request_update_apply(
        db,
        confirm=True,
        submission_id=ticket["submission_id"],
        submission_proof=ticket["submission_proof"],
        expected_manifest_version=ticket["target_version"],
        expected_manifest_commit=ticket["target_commit"],
        actor=actor(),
        ip_address="127.0.0.1",
        user_agent="stage-660122",
    )


def issue_ticket(db) -> dict:
    return update_apply.issue_update_apply_submission_ticket(
        db,
        expected_manifest_version=TARGET_VERSION,
        expected_manifest_commit=TARGET_COMMIT,
        actor=actor(),
    )


def audit_count(db) -> int:
    return db.query(AuditEvent).filter(AuditEvent.event_type == update_apply.AUDIT_EVENT_TYPE).count()


def write_marker(control: Path) -> None:
    update_apply._atomic_write_json(control / "update-admission-lineage.json", update_apply.LINEAGE_MARKER_PAYLOAD)


def request_entry(
    submission_id: str,
    request_id: str,
    state: str,
    *,
    event_id: str | None = None,
    confirmed_at: object = "default",
) -> dict:
    now = "2026-07-20T08:00:00Z"
    request = {
        "schema_version": 2,
        "request_id": request_id,
        "submission_id": submission_id,
        "requested_at": now,
        "requested_by": {"user_id": 1, "username": "owner", "role": "owner", "ip_address": None, "user_agent": None},
        "intent": "apply_update",
        "source": {
            "kind": "trusted_manifest",
            "channel": "stable",
            "source_type": "github_tarball",
            "repo": "owner/repo",
            "ref": "main",
            "version": TARGET_VERSION,
            "commit": TARGET_COMMIT,
            "apply_ref": TARGET_COMMIT,
        },
        "apply_candidate": apply_candidate(),
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
    }
    pending = state == "audit_pending"
    if confirmed_at == "default":
        confirmed_at = None if pending else now
    terminal = None
    if state == "terminal":
        terminal = {
            "schema_version": 1,
            "request_id": request_id,
            "submission_id": submission_id,
            "target_version": TARGET_VERSION,
            "status": "cancelled",
            "phase": "cancelled",
            "current_step": "cancelled",
            "started_at": now,
            "updated_at": now,
            "finished_at": now,
            "source": request["source"],
            "apply_candidate": request["apply_candidate"],
            "steps": [{"name": "request", "status": "completed"}],
            "can_cancel": False,
            "rollback_supported": False,
            "expected_commit": TARGET_COMMIT,
            "installed_commit": None,
            "commit_verified": False,
            "error": {
                "category": "cancelled_before_start",
                "message": "Queued update apply was cancelled before helper started.",
                "operator_action": "No update was applied.",
            },
        }
    return {
        "submission_id": submission_id,
        "request_id": request_id,
        "target_version": TARGET_VERSION,
        "target_commit": TARGET_COMMIT,
        "requested_at": now,
        "updated_at": now,
        "state": state,
        "request": request,
        "audit": {
            "state": "pending" if pending else "confirmed",
            "event_id": event_id or update_apply._audit_event_id(request_id),
            "confirmed_at": confirmed_at,
        },
        "claimed_at": now if state == "claimed" else None,
        "terminal": terminal,
    }


def admission_document(entries: list[dict], current_submission_id: str | None) -> dict:
    return {
        "schema_version": 2,
        "document_type": update_apply.ADMISSION_DOCUMENT_TYPE,
        "current_submission_id": current_submission_id,
        "entries": entries,
        "updated_at": "2026-07-20T08:00:00Z",
    }


def load_helper():
    path = Path(__file__).resolve().parents[3] / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location(f"stage660122_helper_{os.getpid()}", path)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def configure_helper(helper, control: Path) -> None:
    helper.CONTROL_DIR = control
    helper.REQUEST_FILE = control / "update-request.json"
    helper.LINEAGE_FILE = control / "update-admission-lineage.json"
    helper.STATUS_FILE = control / "update-status.json"
    helper.HISTORY_FILE = control / "update-helper-history.json"
    helper.PROGRESS_FILE = control / "update-progress.json"
    helper.APPLY_HISTORY_FILE = control / "update-apply-history.json"
    helper.ADMISSION_LOCK_FILE = control / "update-admission.lock"
    helper.HELPER_LEASE_FILE = control / "update-helper-claim.lock"


@pytest.fixture
def env(tmp_path, monkeypatch):
    control = tmp_path / "control"
    db_path = tmp_path / "stage660122.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    monkeypatch.setattr(settings, "kmvms_update_source_private", False)
    monkeypatch.setattr(settings, "kmvms_update_token_configured", False)
    monkeypatch.setattr(update_apply, "SessionLocal", sessions)
    monkeypatch.setattr(update_apply, "_select_apply_candidate", candidate_for_expected)
    yield control, db_path, sessions
    engine.dispose()


def test_pristine_ticket_is_read_only_and_first_apply_linearizes_marker_ledger_audit(env):
    control, _db_path, sessions = env
    control.mkdir(parents=True)
    (control / "update-helper-claim.lock").touch()
    db = sessions()
    try:
        ticket = issue_ticket(db)
        assert ticket["submission_proof"]
        for name in (
            "update-admission-lineage.json",
            "update-request.json",
            "update-status.json",
            "update-progress.json",
            "update-apply-history.json",
            "update-helper-history.json",
        ):
            assert not (control / name).exists()
        assert audit_count(db) == 0

        result = apply_ticket(db, signed_ticket(SUBMISSION_A))
        assert result["accepted"] is True and result["replayed"] is False
        assert json.loads((control / "update-admission-lineage.json").read_text()) == update_apply.LINEAGE_MARKER_PAYLOAD
        assert (control / "update-admission-lineage.json").stat().st_mode & 0o777 == 0o600
        document = json.loads((control / "update-request.json").read_text())
        assert document["current_submission_id"] == SUBMISSION_A
        assert len(document["entries"]) == 1
        assert audit_count(db) == 1
        assert db.query(AuditEvent).one().id == update_apply._audit_event_id(result["request_id"])
    finally:
        db.close()


def test_marker_write_failure_creates_no_ledger_audit_or_helper_work(env, monkeypatch):
    control, _db_path, sessions = env
    db = sessions()
    original = update_apply._atomic_write_json

    def fail_marker(path, payload):
        if path == control / "update-admission-lineage.json":
            raise OSError("synthetic marker failure")
        return original(path, payload)

    monkeypatch.setattr(update_apply, "_atomic_write_json", fail_marker)
    try:
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply_ticket(db, signed_ticket(SUBMISSION_A))
        assert exc.value.code == "update_lineage_unavailable"
        assert not (control / "update-admission-lineage.json").exists()
        assert not (control / "update-request.json").exists()
        assert not (control / "update-status.json").exists()
        assert audit_count(db) == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    "footprint",
    ["update.lock", "update-status.json", "update-progress.json", "update-apply-history.json", "update-helper-history.json"],
)
def test_missing_ledger_with_execution_footprint_blocks_ticket_and_apply(env, monkeypatch, footprint):
    base, _db_path, sessions = env
    control = base.parent / footprint.replace(".", "-")
    control.mkdir(parents=True)
    (control / footprint).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "update_control_root", str(control))
    db = sessions()
    try:
        with pytest.raises(UpdateApplyBlocked) as ticket_error:
            issue_ticket(db)
        with pytest.raises(UpdateApplyBlocked) as apply_error:
            apply_ticket(db, signed_ticket(SUBMISSION_A))
        assert ticket_error.value.code == apply_error.value.code == "update_admission_unknown"
        assert not (control / "update-request.json").exists()
        assert not (control / "update-admission-lineage.json").exists()
        assert audit_count(db) == 0
    finally:
        db.close()


def test_lost_ledger_and_marker_corruption_are_read_only_fail_closed(env):
    control, _db_path, sessions = env
    db = sessions()
    try:
        ticket = signed_ticket(SUBMISSION_A)
        first = apply_ticket(db, ticket)
        assert update_apply.cancel_update_apply()["status"] == "cancelled"
        assert audit_count(db) == 1
        (control / "update-request.json").unlink()
        before_marker = (control / "update-admission-lineage.json").read_bytes()

        status = update_apply.read_update_apply_status()
        assert status["admission"]["authority"] == "unknown"
        with pytest.raises(UpdateApplyBlocked):
            update_apply.read_update_apply_reconciliation(
                submission_id=SUBMISSION_A,
                submission_proof=ticket["submission_proof"],
                actor_id=1,
            )
        with pytest.raises(UpdateApplyBlocked):
            apply_ticket(db, ticket)
        assert audit_count(db) == 1
        assert first["request_id"].startswith("update-")
        assert not (control / "update-request.json").exists()
        assert (control / "update-admission-lineage.json").read_bytes() == before_marker

        (control / "update-admission-lineage.json").write_text('{"initialized":false}', encoding="utf-8")
        assert update_apply.read_update_apply_status()["admission"]["authority"] == "unknown"
        with pytest.raises(UpdateApplyBlocked):
            issue_ticket(db)
        assert audit_count(db) == 1
    finally:
        db.close()


def test_startup_adopts_only_marker_for_valid_current_and_legacy(env, monkeypatch):
    control, _db_path, _sessions = env
    control.mkdir(parents=True)
    current = admission_document(
        [request_entry(SUBMISSION_A, "update-" + "a" * 32, "admitted_unclaimed")],
        SUBMISSION_A,
    )
    update_apply._atomic_write_json(control / "update-request.json", current)
    before = (control / "update-request.json").read_bytes()
    assert update_apply.read_update_apply_status()["admission"]["reason_code"] == "lineage_incomplete"
    ticket = signed_ticket(SUBMISSION_A)
    with pytest.raises(UpdateApplyBlocked):
        update_apply.read_update_apply_reconciliation(
            submission_id=SUBMISSION_A,
            submission_proof=ticket["submission_proof"],
            actor_id=1,
        )
    assert not (control / "update-admission-lineage.json").exists()
    adopted = update_apply.adopt_update_apply_lineage_on_startup()
    assert adopted["status"] == "adopted" and adopted["classification"] == "current"
    assert (control / "update-request.json").read_bytes() == before
    assert json.loads((control / "update-admission-lineage.json").read_text()) == update_apply.LINEAGE_MARKER_PAYLOAD

    legacy_root = control.parent / "legacy-control"
    legacy_root.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(legacy_root))
    now = "2026-07-20T08:00:00Z"
    legacy = {
        "schema_version": 1,
        "request_id": "release-legacy-1",
        "requested_at": now,
        "intent": "apply_update",
        "confirmed": True,
        "source": {"version": TARGET_VERSION, "commit": TARGET_COMMIT},
    }
    terminal = {
        "schema_version": 1,
        "request_id": legacy["request_id"],
        "status": "completed",
        "phase": "completed",
        "current_step": "completed",
        "started_at": now,
        "updated_at": now,
        "finished_at": now,
        "expected_commit": TARGET_COMMIT,
        "installed_commit": TARGET_COMMIT,
        "commit_verified": True,
        "error": None,
    }
    update_apply._atomic_write_json(legacy_root / "update-request.json", legacy)
    update_apply._atomic_write_json(legacy_root / "update-status.json", terminal)
    adopted = update_apply.adopt_update_apply_lineage_on_startup()
    assert adopted["status"] == "adopted" and adopted["classification"] == "legacy"
    assert json.loads((legacy_root / "update-request.json").read_text()) == legacy


def invalid_topologies() -> list[tuple[str, dict]]:
    a_pending = request_entry(SUBMISSION_A, "update-" + "a" * 32, "audit_pending")
    b_pending = request_entry(SUBMISSION_B, "update-" + "b" * 32, "audit_pending")
    a_admitted = request_entry(SUBMISSION_A, "update-" + "a" * 32, "admitted_unclaimed")
    b_admitted = request_entry(SUBMISSION_B, "update-" + "b" * 32, "admitted_unclaimed")
    a_claimed = request_entry(SUBMISSION_A, "update-" + "a" * 32, "claimed")
    b_claimed = request_entry(SUBMISSION_B, "update-" + "b" * 32, "claimed")
    a_terminal = request_entry(SUBMISSION_A, "update-" + "a" * 32, "terminal")
    b_unknown = request_entry(SUBMISSION_B, "update-" + "b" * 32, "unknown")
    duplicate_request = request_entry(SUBMISSION_B, "update-" + "a" * 32, "terminal")
    return [
        ("two_pending", admission_document([a_pending, b_pending], SUBMISSION_A)),
        ("two_admitted", admission_document([a_admitted, b_admitted], SUBMISSION_A)),
        ("two_claimed", admission_document([a_claimed, b_claimed], SUBMISSION_A)),
        ("pending_claimed", admission_document([a_pending, b_claimed], SUBMISSION_A)),
        ("hidden_nonterminal", admission_document([a_terminal, b_admitted], SUBMISSION_A)),
        ("null_current", admission_document([a_admitted], None)),
        ("terminal_hidden_claimed", admission_document([a_terminal, b_claimed], SUBMISSION_A)),
        ("orphan_unknown", admission_document([a_terminal, b_unknown], SUBMISSION_A)),
        ("duplicate_request", admission_document([a_terminal, duplicate_request], SUBMISSION_A)),
    ]


@pytest.mark.parametrize("case_name,payload", invalid_topologies(), ids=lambda value: value if isinstance(value, str) else None)
def test_invalid_whole_ledger_topology_is_immutable_everywhere(env, monkeypatch, case_name, payload):
    base, _db_path, sessions = env
    control = base.parent / f"topology-{case_name}"
    control.mkdir(parents=True)
    monkeypatch.setattr(settings, "update_control_root", str(control))
    write_marker(control)
    update_apply._atomic_write_json(control / "update-request.json", payload)
    before = (control / "update-request.json").read_bytes()
    db = sessions()
    helper = load_helper()
    configure_helper(helper, control)
    try:
        assert update_apply._read_admission_document_unlocked()[0] == "invalid"
        assert update_apply.read_update_apply_status()["admission"]["authority"] == "unknown"
        for mutation in (
            lambda: issue_ticket(db),
            lambda: apply_ticket(db, signed_ticket("33333333-3333-4333-8333-333333333333")),
            update_apply.cancel_update_apply,
        ):
            with pytest.raises(UpdateApplyBlocked):
                mutation()
        assert update_apply.reconcile_update_apply_audit_once(session_factory=sessions)["repaired"] is False
        with pytest.raises(UpdateApplyBlocked):
            update_apply._prune_eligible_terminal_entries(json.loads(json.dumps(payload)), now=update_apply._utcnow())
        with pytest.raises(helper.HelperError):
            helper.claim_current_request()
        assert (control / "update-request.json").read_bytes() == before
        assert audit_count(db) == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    "state,audit_state,confirmed_at",
    [
        ("audit_pending", "confirmed", "2026-07-20T08:00:00Z"),
        ("audit_pending", "pending", "2026-07-20T08:00:00Z"),
        ("admitted_unclaimed", "confirmed", None),
        ("admitted_unclaimed", "confirmed", "not-a-time"),
        ("admitted_unclaimed", "confirmed", "2" * 200),
    ],
)
def test_audit_timestamp_shapes_are_strict_in_api_and_helper(env, monkeypatch, state, audit_state, confirmed_at):
    control, _db_path, _sessions = env
    control.mkdir(parents=True, exist_ok=True)
    entry = request_entry(SUBMISSION_A, "update-" + "a" * 32, state)
    entry["audit"]["state"] = audit_state
    entry["audit"]["confirmed_at"] = confirmed_at
    payload = admission_document([entry], SUBMISSION_A)
    write_marker(control)
    update_apply._atomic_write_json(control / "update-request.json", payload)
    assert update_apply._read_admission_document_unlocked()[0] == "invalid"
    helper = load_helper()
    configure_helper(helper, control)
    with pytest.raises(helper.HelperError):
        helper.validate_admission_document(payload)


def test_arbitrary_audit_uuid_never_reaches_db_or_creates_second_event(env):
    control, _db_path, sessions = env
    db = sessions()
    try:
        random_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        entry = request_entry(SUBMISSION_A, "update-" + "a" * 32, "audit_pending", event_id=random_id)
        control.mkdir(parents=True)
        write_marker(control)
        update_apply._atomic_write_json(control / "update-request.json", admission_document([entry], SUBMISSION_A))
        before = (control / "update-request.json").read_bytes()
        assert update_apply.reconcile_update_apply_audit_once(session_factory=sessions)["repaired"] is False
        assert audit_count(db) == 0
        assert (control / "update-request.json").read_bytes() == before

        (control / "update-request.json").unlink()
        (control / "update-admission-lineage.json").unlink()
        first = apply_ticket(db, signed_ticket(SUBMISSION_A))
        assert audit_count(db) == 1
        payload = json.loads((control / "update-request.json").read_text())
        payload["entries"][0]["state"] = "audit_pending"
        payload["entries"][0]["audit"] = {"state": "pending", "event_id": random_id, "confirmed_at": None}
        update_apply._atomic_write_json(control / "update-request.json", payload)
        assert update_apply.reconcile_update_apply_audit_once(session_factory=sessions)["repaired"] is False
        assert audit_count(db) == 1
        assert first["request_id"] != random_id
    finally:
        db.close()


def create_process_case(tmp_path: Path, name: str) -> tuple[Path, Path]:
    root = tmp_path / name
    control = root / "control"
    db_path = root / "admission.sqlite3"
    root.mkdir(parents=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    return control, db_path


def configure_process_api(control: Path, db_path: Path):
    settings.update_control_root = str(control)
    settings.kmvms_update_helper_enabled = True
    settings.kmvms_update_source_private = False
    settings.kmvms_update_token_configured = False
    update_apply._select_apply_candidate = candidate_for_expected
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    update_apply.SessionLocal = sessions
    return engine, sessions


def process_apply_worker(control, db_path, ticket, start_event, output, crash_point=None):
    engine, sessions = configure_process_api(Path(control), Path(db_path))
    if crash_point == "marker_before_ledger":
        original = update_apply._atomic_write_json

        def crash_on_ledger(path, payload):
            if path == update_apply._request_path():
                os._exit(91)
            return original(path, payload)

        update_apply._atomic_write_json = crash_on_ledger
    elif crash_point == "reservation_before_audit":
        update_apply._ensure_deterministic_accepted_audit = lambda *_args, **_kwargs: os._exit(92)
    elif crash_point == "audit_before_confirmation":
        original_write = update_apply._write_admission_document
        calls = {"count": 0}

        def crash_on_confirmation(payload):
            calls["count"] += 1
            if calls["count"] == 2:
                os._exit(93)
            return original_write(payload)

        update_apply._write_admission_document = crash_on_confirmation
    db = sessions()
    try:
        start_event.wait(10)
        result = apply_ticket(db, ticket)
        output.put(
            {
                "outcome": "accepted",
                "request_id": result.get("request_id"),
                "replayed": bool(result.get("replayed")),
            }
        )
    except UpdateApplyBlocked as exc:
        output.put({"outcome": exc.code, "request_id": None, "replayed": False})
    except Exception:
        output.put({"outcome": "unexpected_error", "request_id": None, "replayed": False})
    finally:
        db.close()
        engine.dispose()


def run_apply_pair(context, control: Path, db_path: Path, tickets: list[dict]) -> list[dict]:
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(target=process_apply_worker, args=(control, db_path, ticket, start, output))
        for ticket in tickets
    ]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=20) for _process in processes]
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive()
        assert process.exitcode == 0
    return results


@pytest.mark.parametrize("mode", ["same_target", "different_target", "same_submission"])
def test_real_process_apply_races_use_independent_sessions(tmp_path, mode):
    context = multiprocessing.get_context("fork")
    control, db_path = create_process_case(tmp_path, mode)
    if mode == "same_target":
        tickets = [signed_ticket(SUBMISSION_A), signed_ticket(SUBMISSION_B)]
    elif mode == "different_target":
        tickets = [
            signed_ticket(SUBMISSION_A),
            signed_ticket(SUBMISSION_B, version=OTHER_VERSION, commit=OTHER_COMMIT),
        ]
    else:
        shared = signed_ticket(SUBMISSION_A)
        tickets = [shared, dict(shared)]

    results = run_apply_pair(context, control, db_path, tickets)
    if mode == "same_submission":
        assert [item["outcome"] for item in results] == ["accepted", "accepted"]
        assert {item["replayed"] for item in results} == {False, True}
        assert len({item["request_id"] for item in results}) == 1
    else:
        assert [item["outcome"] for item in results].count("accepted") == 1
        assert [item["outcome"] for item in results].count("update_already_running") == 1

    payload = json.loads((control / "update-request.json").read_text())
    assert len(payload["entries"]) == 1
    engine = create_engine(f"sqlite:///{db_path}")
    sessions = sessionmaker(bind=engine)
    db = sessions()
    try:
        assert audit_count(db) == 1
    finally:
        db.close()
        engine.dispose()


def run_crashing_apply(context, control: Path, db_path: Path, crash_point: str) -> int:
    start = context.Event()
    output = context.Queue()
    process = context.Process(
        target=process_apply_worker,
        args=(control, db_path, signed_ticket(SUBMISSION_A), start, output, crash_point),
    )
    process.start()
    start.set()
    process.join(timeout=20)
    assert not process.is_alive()
    return process.exitcode


def audit_repair_worker(control, db_path, start_event, output):
    engine, sessions = configure_process_api(Path(control), Path(db_path))
    try:
        start_event.wait(10)
        result = update_apply.reconcile_update_apply_audit_once(session_factory=sessions)
        output.put({"repaired": bool(result.get("repaired")), "status": result.get("status")})
    except Exception:
        output.put({"repaired": False, "status": "unexpected_error"})
    finally:
        engine.dispose()


def run_repair_pair(context, control: Path, db_path: Path) -> list[dict]:
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(target=audit_repair_worker, args=(control, db_path, start, output))
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=20) for _process in processes]
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive() and process.exitcode == 0
    return results


def configure_parent_case(monkeypatch, control: Path, db_path: Path):
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    monkeypatch.setattr(settings, "kmvms_update_source_private", False)
    monkeypatch.setattr(settings, "kmvms_update_token_configured", False)
    monkeypatch.setattr(update_apply, "_select_apply_candidate", candidate_for_expected)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(update_apply, "SessionLocal", sessions)
    return engine, sessions


def test_real_process_crash_boundaries_converge_without_a2_or_second_audit(tmp_path, monkeypatch):
    context = multiprocessing.get_context("fork")

    marker_control, marker_db = create_process_case(tmp_path, "crash-marker")
    assert run_crashing_apply(context, marker_control, marker_db, "marker_before_ledger") == 91
    assert (marker_control / "update-admission-lineage.json").exists()
    assert not (marker_control / "update-request.json").exists()
    engine, sessions = configure_parent_case(monkeypatch, marker_control, marker_db)
    db = sessions()
    try:
        with pytest.raises(UpdateApplyBlocked):
            apply_ticket(db, signed_ticket(SUBMISSION_A))
        assert audit_count(db) == 0
    finally:
        db.close()
        engine.dispose()

    pending_control, pending_db = create_process_case(tmp_path, "crash-pending")
    assert run_crashing_apply(context, pending_control, pending_db, "reservation_before_audit") == 92
    engine, sessions = configure_parent_case(monkeypatch, pending_control, pending_db)
    db = sessions()
    try:
        before = json.loads((pending_control / "update-request.json").read_text())
        request_id = before["entries"][0]["request_id"]
        assert before["entries"][0]["state"] == "audit_pending"
        assert audit_count(db) == 0
        db.rollback()
        repairs = run_repair_pair(context, pending_control, pending_db)
        assert [item["repaired"] for item in repairs].count(True) == 1
        assert all(item["status"] != "unexpected_error" for item in repairs)
        after = json.loads((pending_control / "update-request.json").read_text())
        assert after["entries"][0]["request_id"] == request_id
        assert after["entries"][0]["state"] == "admitted_unclaimed"
        assert audit_count(db) == 1
    finally:
        db.close()
        engine.dispose()

    committed_control, committed_db = create_process_case(tmp_path, "crash-committed")
    assert run_crashing_apply(context, committed_control, committed_db, "audit_before_confirmation") == 93
    engine, sessions = configure_parent_case(monkeypatch, committed_control, committed_db)
    db = sessions()
    try:
        before = json.loads((committed_control / "update-request.json").read_text())
        request_id = before["entries"][0]["request_id"]
        assert before["entries"][0]["state"] == "audit_pending"
        assert audit_count(db) == 1
        assert update_apply.reconcile_update_apply_audit_once(session_factory=sessions)["repaired"] is True
        assert json.loads((committed_control / "update-request.json").read_text())["entries"][0]["request_id"] == request_id
        assert audit_count(db) == 1
    finally:
        db.close()
        engine.dispose()


def helper_claim_worker(control, wait_event, ready_event, output, pre_read=False, crash_after_claim=False):
    helper = load_helper()
    configure_helper(helper, Path(control))
    if pre_read:
        helper.validate_admission_document(helper.read_json(helper.REQUEST_FILE))
    ready_event.set()
    wait_event.wait(10)
    try:
        request = helper.claim_current_request()
        if crash_after_claim and request:
            os._exit(94)
        output.put({"claimed": bool(request), "request_id_present": bool(request and request.get("request_id"))})
    except helper.HelperError as exc:
        output.put({"claimed": False, "category": exc.category})


def cancel_worker(control, wait_event, ready_event, output):
    settings.update_control_root = str(control)
    ready_event.set()
    wait_event.wait(10)
    try:
        result = update_apply.cancel_update_apply()
        output.put({"status": result.get("status")})
    except UpdateApplyBlocked as exc:
        output.put({"status": exc.code})


def prepare_admission(monkeypatch, tmp_path: Path, name: str):
    control, db_path = create_process_case(tmp_path, name)
    engine, sessions = configure_parent_case(monkeypatch, control, db_path)
    db = sessions()
    first = apply_ticket(db, signed_ticket(SUBMISSION_A))
    db.close()
    return control, db_path, engine, sessions, first


def test_helper_cancel_and_stale_snapshot_process_schedules_are_deterministic(tmp_path, monkeypatch):
    context = multiprocessing.get_context("fork")

    control, _db_path, engine, sessions, _first = prepare_admission(monkeypatch, tmp_path, "cancel-wins")
    helper_release = context.Event()
    helper_ready = context.Event()
    helper_output = context.Queue()
    helper_process = context.Process(
        target=helper_claim_worker,
        args=(control, helper_release, helper_ready, helper_output),
    )
    helper_process.start()
    assert helper_ready.wait(10)
    cancel_release = context.Event()
    cancel_release.set()
    cancel_ready = context.Event()
    cancel_output = context.Queue()
    cancel_process = context.Process(target=cancel_worker, args=(control, cancel_release, cancel_ready, cancel_output))
    cancel_process.start()
    assert cancel_ready.wait(10)
    cancel_process.join(timeout=10)
    assert cancel_process.exitcode == 0 and cancel_output.get(timeout=5)["status"] == "cancelled"
    helper_release.set()
    helper_process.join(timeout=10)
    assert helper_process.exitcode == 0 and helper_output.get(timeout=5)["claimed"] is False
    db = sessions()
    try:
        assert apply_ticket(db, signed_ticket(SUBMISSION_B))["accepted"] is True
        assert audit_count(db) == 2
    finally:
        db.close()
        engine.dispose()

    control, _db_path, engine, sessions, _first = prepare_admission(monkeypatch, tmp_path, "helper-wins")
    cancel_release = context.Event()
    cancel_ready = context.Event()
    cancel_output = context.Queue()
    cancel_process = context.Process(target=cancel_worker, args=(control, cancel_release, cancel_ready, cancel_output))
    cancel_process.start()
    assert cancel_ready.wait(10)
    helper_release = context.Event()
    helper_release.set()
    helper_ready = context.Event()
    helper_output = context.Queue()
    helper_process = context.Process(target=helper_claim_worker, args=(control, helper_release, helper_ready, helper_output))
    helper_process.start()
    assert helper_ready.wait(10)
    helper_process.join(timeout=10)
    assert helper_process.exitcode == 0 and helper_output.get(timeout=5)["claimed"] is True
    cancel_release.set()
    cancel_process.join(timeout=10)
    assert cancel_process.exitcode == 0 and cancel_output.get(timeout=5)["status"] == "not_cancelable"
    db = sessions()
    try:
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply_ticket(db, signed_ticket(SUBMISSION_B))
        assert exc.value.code == "update_already_running"
    finally:
        db.close()
        engine.dispose()

    control, _db_path, engine, sessions, _first = prepare_admission(monkeypatch, tmp_path, "stale-snapshot")
    helper_release = context.Event()
    helper_ready = context.Event()
    helper_output = context.Queue()
    helper_process = context.Process(
        target=helper_claim_worker,
        args=(control, helper_release, helper_ready, helper_output, True),
    )
    helper_process.start()
    assert helper_ready.wait(10)
    cancel_release = context.Event()
    cancel_release.set()
    cancel_ready = context.Event()
    cancel_output = context.Queue()
    cancel_process = context.Process(target=cancel_worker, args=(control, cancel_release, cancel_ready, cancel_output))
    cancel_process.start()
    cancel_process.join(timeout=10)
    assert cancel_process.exitcode == 0 and cancel_output.get(timeout=5)["status"] == "cancelled"
    helper_release.set()
    helper_process.join(timeout=10)
    assert helper_process.exitcode == 0 and helper_output.get(timeout=5)["claimed"] is False
    engine.dispose()


def test_helper_crash_after_claim_converges_without_second_execution(tmp_path, monkeypatch):
    context = multiprocessing.get_context("fork")
    control, _db_path, engine, _sessions, first = prepare_admission(monkeypatch, tmp_path, "helper-crash")
    release = context.Event()
    release.set()
    ready = context.Event()
    output = context.Queue()
    process = context.Process(
        target=helper_claim_worker,
        args=(control, release, ready, output, False, True),
    )
    process.start()
    assert ready.wait(10)
    process.join(timeout=10)
    assert not process.is_alive() and process.exitcode == 94

    helper = load_helper()
    configure_helper(helper, control)
    assert helper.claim_current_request() is None
    payload = json.loads((control / "update-request.json").read_text())
    entry = payload["entries"][0]
    assert entry["request_id"] == first["request_id"]
    assert entry["state"] == "terminal"
    assert entry["terminal"]["error"]["category"] == "helper_restart_interrupted"
    assert len(payload["entries"]) == 1
    engine.dispose()


def test_helper_and_api_share_uuid5_namespace_and_marker_topology_contract(env):
    control, _db_path, _sessions = env
    helper = load_helper()
    configure_helper(helper, control)
    request_id = "update-" + "a" * 32
    assert helper.deterministic_audit_event_id(request_id) == update_apply._audit_event_id(request_id)

    entry = request_entry(SUBMISSION_A, request_id, "admitted_unclaimed")
    payload = admission_document([entry], SUBMISSION_A)
    control.mkdir(parents=True)
    write_marker(control)
    update_apply._atomic_write_json(control / "update-request.json", payload)
    assert update_apply._read_admission_authority_unlocked()["classification"] == "current"
    assert helper.read_admission_authority()[0] == "current"

    (control / "update-admission-lineage.json").unlink()
    assert update_apply._read_admission_authority_unlocked()["classification"] == "lineage_incomplete"
    with pytest.raises(helper.HelperError) as exc:
        helper.read_admission_authority()
    assert exc.value.category == "admission_lineage_incomplete"

    (control / "update-admission-lineage.json").write_text('{"initialized":false}', encoding="utf-8")
    with pytest.raises(helper.HelperError) as exc:
        helper.read_admission_authority()
    assert exc.value.category == "admission_lineage_invalid"


def test_helper_missing_document_with_marker_or_execution_footprint_never_claims(env, monkeypatch):
    base, _db_path, _sessions = env
    helper = load_helper()
    for name, marker, footprint in (
        ("marker-only", True, None),
        ("status-only", False, "update-status.json"),
        ("history-only", False, "update-helper-history.json"),
    ):
        control = base.parent / name
        control.mkdir(parents=True)
        configure_helper(helper, control)
        monkeypatch.setattr(settings, "update_control_root", str(control))
        if marker:
            write_marker(control)
        if footprint:
            (control / footprint).write_text("{}", encoding="utf-8")
        with pytest.raises(helper.HelperError) as exc:
            helper.claim_current_request()
        assert exc.value.category == "admission_missing_unexpected"
        assert not (control / "update-request.json").exists()
