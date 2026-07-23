import json
import importlib.util
import multiprocessing
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
from app.services.update_apply import (
    UpdateApplyBlocked,
    _issue_submission_proof,
    cancel_update_apply,
    read_update_apply_status,
    reconcile_update_apply_audit_once,
    request_update_apply,
)


TARGET_COMMIT = "c" * 40
TARGET_VERSION = "9.9.9"
SUBMISSION_A = "11111111-1111-4111-8111-111111111111"
SUBMISSION_B = "22222222-2222-4222-8222-222222222222"


def admission_lock_probe(control_root, label, hold_seconds, queue):
    settings.update_control_root = control_root
    with update_apply._admission_guard():
        queue.put((label, time.monotonic()))
        time.sleep(hold_seconds)


def latest_release():
    return {
        "version": TARGET_VERSION,
        "commit": TARGET_COMMIT,
        "channel": "stable",
        "source_type": "github_tarball",
        "source_repo": "owner/repo",
        "source_ref": "main",
    }


def apply_candidate():
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


@pytest.fixture
def admission_env(tmp_path, monkeypatch):
    control = tmp_path / "control"
    engine = create_engine(
        f"sqlite:///{tmp_path / 'admission.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    monkeypatch.setattr(settings, "kmvms_update_source_private", False)
    monkeypatch.setattr(settings, "kmvms_update_token_configured", False)
    monkeypatch.setattr(update_apply, "_select_apply_candidate", lambda *_args, **_kwargs: (latest_release(), apply_candidate()))
    yield control, sessions
    engine.dispose()


def apply(db, submission_id, actor=None):
    actor = actor or SimpleNamespace(id=1, role="owner", username="owner")
    proof = None
    if submission_id and update_apply.SUBMISSION_ID_RE.fullmatch(submission_id):
        proof = _issue_submission_proof(
            submission_id=submission_id,
            target_version=TARGET_VERSION,
            target_commit=TARGET_COMMIT,
            actor_id=actor.id,
            issued_at=update_apply._utcnow(),
        )[0]
    return request_update_apply(
        db,
        confirm=True,
        submission_id=submission_id,
        submission_proof=proof,
        expected_manifest_version=TARGET_VERSION,
        expected_manifest_commit=TARGET_COMMIT,
        actor=actor,
        ip_address="127.0.0.1",
        user_agent="stage-66012-test",
    )


def test_atomic_admission_same_submission_replay_and_exactly_once_audit(admission_env):
    control, sessions = admission_env
    db = sessions()
    try:
        first = apply(db, SUBMISSION_A)
        request_path = control / "update-request.json"
        first_bytes = request_path.read_bytes()
        replay = apply(db, SUBMISSION_A)

        assert first["accepted"] is True and first["replayed"] is False
        assert replay["accepted"] is True and replay["replayed"] is True
        assert replay["request_id"] == first["request_id"]
        assert replay["submission_id"] == SUBMISSION_A
        assert request_path.read_bytes() == first_bytes
        assert not (control / "update-status.json").exists()
        assert db.query(AuditEvent).filter(AuditEvent.event_type == "system.update_apply_requested").count() == 1
        assert first["apply_status"]["admission"]["state"] == "admitted_unclaimed"
    finally:
        db.close()


def test_distinct_concurrent_submissions_admit_exactly_one(admission_env):
    control, sessions = admission_env

    def worker(submission_id):
        db = sessions()
        try:
            try:
                return "accepted", apply(db, submission_id)
            except UpdateApplyBlocked as exc:
                return exc.code, None
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, [SUBMISSION_A, SUBMISSION_B]))

    assert [item[0] for item in results].count("accepted") == 1
    assert [item[0] for item in results].count("update_already_running") == 1
    request = json.loads((control / "update-request.json").read_text(encoding="utf-8"))
    assert request["current_submission_id"] in {SUBMISSION_A, SUBMISSION_B}
    db = sessions()
    try:
        assert db.query(AuditEvent).filter(AuditEvent.event_type == "system.update_apply_requested").count() == 1
    finally:
        db.close()


def test_admission_lock_serializes_distinct_api_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    control = str(tmp_path / "process-control")
    first = context.Process(target=admission_lock_probe, args=(control, "first", 0.35, queue))
    second = context.Process(target=admission_lock_probe, args=(control, "second", 0.0, queue))
    first.start()
    first_label, first_entered = queue.get(timeout=5)
    second.start()
    second_label, second_entered = queue.get(timeout=5)
    first.join(timeout=5)
    second.join(timeout=5)
    assert first_label == "first" and second_label == "second"
    assert first.exitcode == 0 and second.exitcode == 0
    assert second_entered - first_entered >= 0.25


def test_crash_boundaries_have_one_durable_handoff(admission_env, monkeypatch):
    control, sessions = admission_env
    db = sessions()
    original_write = update_apply._atomic_write_json

    def fail_before_request(path, payload):
        if path == control / "update-request.json":
            raise OSError("injected-before-linearization")
        return original_write(path, payload)

    monkeypatch.setattr(update_apply, "_atomic_write_json", fail_before_request)
    with pytest.raises(OSError):
        apply(db, SUBMISSION_A)
    assert not (control / "update-request.json").exists()
    assert (control / "update-admission-lineage.json").exists()
    assert db.query(AuditEvent).count() == 0

    with pytest.raises(UpdateApplyBlocked) as exc:
        apply(db, SUBMISSION_A)
    assert exc.value.code == "update_admission_unknown"

    monkeypatch.setattr(update_apply, "_atomic_write_json", original_write)
    control = control.parent / "audit-control"
    monkeypatch.setattr(settings, "update_control_root", str(control))
    original_audit = update_apply._ensure_deterministic_accepted_audit

    def fail_after_request(*_args, **_kwargs):
        raise RuntimeError("injected-after-linearization")

    monkeypatch.setattr(update_apply, "_ensure_deterministic_accepted_audit", fail_after_request)
    with pytest.raises(RuntimeError):
        apply(db, SUBMISSION_A)
    durable = json.loads((control / "update-request.json").read_text(encoding="utf-8"))
    assert durable["current_submission_id"] == SUBMISSION_A

    monkeypatch.setattr(update_apply, "_ensure_deterministic_accepted_audit", original_audit)
    reconcile_update_apply_audit_once(session_factory=sessions)
    replay = apply(db, SUBMISSION_A)
    assert replay["replayed"] is True
    assert replay["request_id"] == durable["entries"][0]["request_id"]
    assert db.query(AuditEvent).filter(AuditEvent.event_type == "system.update_apply_requested").count() == 1
    db.close()


def test_terminal_replay_and_contradictory_control_truth(admission_env):
    control, sessions = admission_env
    db = sessions()
    first = apply(db, SUBMISSION_A)
    request_id = first["request_id"]
    assert cancel_update_apply()["status"] == "cancelled"
    replay = apply(db, SUBMISSION_A)
    assert replay["replayed"] is True
    assert replay["status"] == "cancelled"
    assert replay["request_id"] == request_id

    foreign = json.loads((control / "update-status.json").read_text(encoding="utf-8"))
    foreign["request_id"] = "update-" + "f" * 32
    foreign["submission_id"] = SUBMISSION_B
    foreign["updated_at"] = "2099-07-19T00:01:00Z"
    (control / "update-status.json").write_text(json.dumps(foreign), encoding="utf-8")
    status = read_update_apply_status()
    assert status["admission"]["authority"] == "inactive"
    assert status["request_id"] == request_id
    db.close()


def test_malformed_submission_and_request_are_fail_closed_without_mutation(admission_env):
    control, sessions = admission_env
    db = sessions()
    for submission_id in (None, "bad-id"):
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply(db, submission_id)
        assert exc.value.code in {"submission_id_required", "submission_id_invalid"}
    assert not (control / "update-request.json").exists()
    assert db.query(AuditEvent).count() == 0

    control.mkdir(parents=True, exist_ok=True)
    (control / "update-request.json").write_text("{not-json", encoding="utf-8")
    assert read_update_apply_status()["admission"]["authority"] == "unknown"
    with pytest.raises(UpdateApplyBlocked) as exc:
        apply(db, SUBMISSION_A)
    assert exc.value.code == "update_admission_unknown"
    assert (control / "update-request.json").read_text(encoding="utf-8") == "{not-json"
    db.close()


def test_helper_preserves_submission_in_status_and_history(tmp_path, monkeypatch):
    helper_path = Path(__file__).resolve().parents[3] / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("stage66012_update_helper", helper_path)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    history = tmp_path / "update-apply-history.json"
    monkeypatch.setattr(helper, "APPLY_HISTORY_FILE", history)
    request = {
        "schema_version": 2,
        "request_id": "update-" + "a" * 32,
        "submission_id": SUBMISSION_A,
        "requested_at": "2026-07-19T00:00:00Z",
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
    helper.validate_request(request)
    for status_name in ("queued", "applying", "failed", "cancelled", "completed"):
        status_payload = helper.base_status(request, status_name, status_name, [])
        assert status_payload["submission_id"] == SUBMISSION_A
        assert status_payload["request_id"] == request["request_id"]
        assert status_payload["target_version"] == TARGET_VERSION
    helper.append_apply_history(helper.base_status(request, "failed", "health_check", []))
    history_item = json.loads(history.read_text(encoding="utf-8"))["items"][0]
    assert history_item["submission_id"] == SUBMISSION_A
    assert history_item["target_version"] == TARGET_VERSION
    with pytest.raises(helper.HelperError) as exc:
        helper.validate_request({**request, "submission_id": None})
    assert exc.value.category == "request_contract_invalid"

    status_file = tmp_path / "update-status.json"
    processed_file = tmp_path / "update-helper-history.json"
    monkeypatch.setattr(helper, "STATUS_FILE", status_file)
    monkeypatch.setattr(helper, "HISTORY_FILE", processed_file)
    status_file.write_text(json.dumps(helper.base_status(request, "applying", "overlay", [])), encoding="utf-8")
    assert helper.should_process(request, set()) is True
