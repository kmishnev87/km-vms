from __future__ import annotations

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
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
SUBMISSION_A = "11111111-1111-4111-8111-111111111111"
SUBMISSION_B = "22222222-2222-4222-8222-222222222222"


def actor(user_id=1, username="owner"):
    return SimpleNamespace(id=user_id, role="owner", username=username)


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
def env(tmp_path, monkeypatch):
    control = tmp_path / "control"
    engine = create_engine(
        f"sqlite:///{tmp_path / 'stage660121.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    monkeypatch.setattr(settings, "kmvms_update_source_private", False)
    monkeypatch.setattr(settings, "kmvms_update_token_configured", False)
    monkeypatch.setattr(update_apply, "SessionLocal", sessions)
    monkeypatch.setattr(
        update_apply,
        "_select_apply_candidate",
        lambda *_args, **_kwargs: (latest_release(), apply_candidate()),
    )
    yield control, sessions
    engine.dispose()


def signed_ticket(submission_id, *, current_actor=None, issued_at=None, version=TARGET_VERSION, commit=TARGET_COMMIT):
    current_actor = current_actor or actor()
    proof, claims = update_apply._issue_submission_proof(
        submission_id=submission_id,
        target_version=version,
        target_commit=commit,
        actor_id=current_actor.id,
        issued_at=issued_at or update_apply._utcnow(),
    )
    return {
        "submission_id": submission_id,
        "submission_proof": proof,
        "target_version": version,
        "target_commit": commit,
        "claims": claims,
    }


def apply(db, ticket, *, current_actor=None, version=None, commit=None):
    current_actor = current_actor or actor()
    return update_apply.request_update_apply(
        db,
        confirm=True,
        submission_id=ticket["submission_id"],
        submission_proof=ticket["submission_proof"],
        expected_manifest_version=version or ticket["target_version"],
        expected_manifest_commit=commit or ticket["target_commit"],
        actor=current_actor,
        ip_address="127.0.0.1",
        user_agent="stage-660121",
    )


def document(control):
    return json.loads((control / "update-request.json").read_text(encoding="utf-8"))


def audit_count(db):
    return db.query(AuditEvent).filter(AuditEvent.event_type == update_apply.AUDIT_EVENT_TYPE).count()


def mark_claimed(control):
    with update_apply._admission_guard():
        contract, current = update_apply._read_admission_document_unlocked()
        assert contract == "current" and current and current["current"]
        payload = update_apply._document_copy(current)
        raw = json.loads(json.dumps(current["current"]["entry"]))
        raw["state"] = "claimed"
        raw["claimed_at"] = update_apply._iso()
        raw["updated_at"] = raw["claimed_at"]
        update_apply._replace_raw_entry(payload, raw)
        update_apply._write_admission_document(payload)
        return raw


def failed_status(entry, *, malformed=False):
    request_source = entry["request"]["source"]
    finished_at = update_apply._iso()
    return {
        "schema_version": 1,
        "request_id": entry["request_id"],
        "submission_id": entry["submission_id"],
        "target_version": entry["target_version"],
        "status": "failed",
        "phase": "unknown" if malformed else "health_check_failed",
        "current_step": "unknown" if malformed else "health_check_failed",
        "started_at": entry["requested_at"],
        "updated_at": finished_at,
        "finished_at": finished_at,
        "source": {
            "kind": "github-tarball",
            "repo": request_source["repo"],
            "ref": request_source["ref"],
            "commit": entry["target_commit"],
            "apply_ref": entry["target_commit"],
        },
        "expected_commit": entry["target_commit"],
        "commit_verified": False,
        "steps": [{"name": "health_check", "status": "failed"}],
        "can_cancel": False,
        "rollback_supported": False,
        "side_effects": {
            "api_docker_socket": False,
            "api_shell_execution": False,
            "request_controlled_source": False,
            "helper_has_docker_socket": True,
            "helper_public_ports": False,
        },
        "error": None if malformed else {
            "category": "health_check_failed",
            "message": "Health check failed.",
            "operator_action": "Review status.",
        },
    }


def load_helper():
    path = Path(__file__).resolve().parents[3] / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("stage660121_helper", path)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def configure_helper(helper, control, tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "CONTROL_DIR", control)
    monkeypatch.setattr(helper, "REQUEST_FILE", control / "update-request.json")
    monkeypatch.setattr(helper, "LINEAGE_FILE", control / "update-admission-lineage.json")
    monkeypatch.setattr(helper, "STATUS_FILE", control / "update-status.json")
    monkeypatch.setattr(helper, "ADMISSION_LOCK_FILE", control / "update-admission.lock")
    monkeypatch.setattr(helper, "HELPER_LEASE_FILE", control / "update-helper-claim.lock")
    monkeypatch.setattr(helper, "HISTORY_FILE", tmp_path / "helper-history.json")
    monkeypatch.setattr(helper, "APPLY_HISTORY_FILE", tmp_path / "apply-history.json")
    monkeypatch.setattr(helper, "PROGRESS_FILE", tmp_path / "progress.json")


def test_server_proof_verifier_is_strict_and_expiry_is_server_time():
    current_actor = actor()
    ticket = signed_ticket(SUBMISSION_A, current_actor=current_actor)
    state, claims = update_apply.verify_update_apply_submission_proof(
        ticket["submission_proof"],
        actor_id=current_actor.id,
        submission_id=SUBMISSION_A,
        target_version=TARGET_VERSION,
        target_commit=TARGET_COMMIT,
    )
    assert state == "valid_unexpired" and claims["submission_id"] == SUBMISSION_A

    expired = signed_ticket(
        SUBMISSION_A,
        current_actor=current_actor,
        issued_at=update_apply._utcnow() - timedelta(hours=1),
    )
    assert update_apply.verify_update_apply_submission_proof(expired["submission_proof"], actor_id=current_actor.id)[0] == "valid_expired"
    assert update_apply.verify_update_apply_submission_proof(ticket["submission_proof"], actor_id=2)[0] == "invalid"
    assert update_apply.verify_update_apply_submission_proof(ticket["submission_proof"], actor_id=1, target_commit="d" * 40)[0] == "invalid"

    now = int(update_apply._utcnow().replace(tzinfo=update_apply.timezone.utc).timestamp())
    wrong_type = jwt.encode(
        {
            "typ": "access",
            "aud": update_apply.SUBMISSION_PROOF_AUDIENCE,
            "purpose": update_apply.SUBMISSION_PROOF_PURPOSE,
            "version": 1,
            "jti": SUBMISSION_A,
            "sub": "1",
            "target_version": TARGET_VERSION,
            "target_commit": TARGET_COMMIT,
            "iat": now,
            "nbf": now,
            "exp": now + 900,
        },
        settings.jwt_secret,
        algorithm="HS256",
        headers={"typ": "access"},
    )
    assert update_apply.verify_update_apply_submission_proof(wrong_type, actor_id=1)[0] == "invalid"


def test_ticket_is_server_issued_and_does_not_mutate_admission_or_audit(env):
    control, sessions = env
    db = sessions()
    try:
        result = update_apply.issue_update_apply_submission_ticket(
            db,
            expected_manifest_version=TARGET_VERSION,
            expected_manifest_commit=TARGET_COMMIT,
            actor=actor(),
        )
        assert update_apply.SUBMISSION_ID_RE.fullmatch(result["submission_id"])
        assert result["target_commit"] == TARGET_COMMIT
        assert update_apply.verify_update_apply_submission_proof(
            result["submission_proof"],
            actor_id=1,
            submission_id=result["submission_id"],
            target_version=TARGET_VERSION,
            target_commit=TARGET_COMMIT,
        )[0] == "valid_unexpired"
        assert not (control / "update-request.json").exists()
        assert audit_count(db) == 0
    finally:
        db.close()


def test_delayed_a_after_later_terminal_b_replays_original_without_a2(env):
    control, sessions = env
    db = sessions()
    try:
        ticket_a = signed_ticket(SUBMISSION_A)
        first_a = apply(db, ticket_a)
        assert update_apply.cancel_update_apply()["status"] == "cancelled"
        ticket_b = signed_ticket(SUBMISSION_B)
        first_b = apply(db, ticket_b)
        assert update_apply.cancel_update_apply()["status"] == "cancelled"
        before = (control / "update-request.json").read_bytes()
        replay_a = apply(db, ticket_a)
        assert replay_a["replayed"] is True
        assert replay_a["request_id"] == first_a["request_id"]
        assert replay_a["request_id"] != first_b["request_id"]
        assert replay_a["status"] == "cancelled"
        assert (control / "update-request.json").read_bytes() == before
        assert audit_count(db) == 2
    finally:
        db.close()


def test_exact_lookup_returns_terminal_a_while_b_is_current_without_mutation(env):
    control, sessions = env
    db = sessions()
    try:
        ticket_a = signed_ticket(SUBMISSION_A)
        first_a = apply(db, ticket_a)
        update_apply.cancel_update_apply()
        ticket_b = signed_ticket(SUBMISSION_B)
        first_b = apply(db, ticket_b)
        before_file = (control / "update-request.json").read_bytes()
        before_audits = audit_count(db)
        result = update_apply.read_update_apply_reconciliation(
            submission_id=SUBMISSION_A,
            submission_proof=ticket_a["submission_proof"],
            actor_id=1,
        )
        assert result["found"] is True
        assert result["request_id"] == first_a["request_id"]
        assert result["apply_status"]["request_id"] == first_a["request_id"]
        assert result["apply_status"]["admission"]["request_id"] == first_a["request_id"]
        assert update_apply.read_update_apply_status()["request_id"] == first_b["request_id"]
        assert (control / "update-request.json").read_bytes() == before_file
        assert audit_count(db) == before_audits
    finally:
        db.close()


def test_same_submission_with_different_target_is_rejected_without_mutation(env):
    control, sessions = env
    db = sessions()
    try:
        ticket = signed_ticket(SUBMISSION_A)
        apply(db, ticket)
        before = (control / "update-request.json").read_bytes()
        before_audits = audit_count(db)
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply(db, ticket, version="10.0.0")
        assert exc.value.code == "submission_target_mismatch"
        assert (control / "update-request.json").read_bytes() == before
        assert audit_count(db) == before_audits
    finally:
        db.close()


def test_malformed_terminal_is_unknown_and_cannot_release_gate(env):
    control, sessions = env
    db = sessions()
    try:
        apply(db, signed_ticket(SUBMISSION_A))
        claimed = mark_claimed(control)
        (control / "update-status.json").write_text(json.dumps(failed_status(claimed, malformed=True)), encoding="utf-8")
        status = update_apply.read_update_apply_status()
        assert status["admission"]["authority"] == "unknown"
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply(db, signed_ticket(SUBMISSION_B))
        assert exc.value.code == "update_admission_unknown"
        assert document(control)["current_submission_id"] == SUBMISSION_A
        assert audit_count(db) == 1
    finally:
        db.close()


def test_strict_terminal_classifier_accepts_complete_failed_cancelled_only(env):
    control, sessions = env
    db = sessions()
    try:
        first = apply(db, signed_ticket(SUBMISSION_A))
        claimed = mark_claimed(control)
        failed = failed_status(claimed)
        assert update_apply._strict_terminal_snapshot(failed, update_apply._admission_entry_contract(claimed))
        assert update_apply._strict_terminal_snapshot({**failed, "error": None}, update_apply._admission_entry_contract(claimed)) is None
        completed = {
            **failed,
            "status": "completed",
            "phase": "completed",
            "current_step": "completed",
            "installed_commit": TARGET_COMMIT,
            "commit_verified": True,
            "error": None,
            "release_identity": {
                "host_metadata_status": "complete",
                "api_metadata_status": "complete",
                "api_visible": True,
                "commit_verified": True,
            },
        }
        assert update_apply._strict_terminal_snapshot(completed, update_apply._admission_entry_contract(claimed))
        assert update_apply._strict_terminal_snapshot({**completed, "installed_commit": "d" * 40}, update_apply._admission_entry_contract(claimed)) is None
        assert first["request_id"] == claimed["request_id"]
    finally:
        db.close()


def test_public_status_uses_fixed_shapes_and_never_exposes_nested_raw_payload(env):
    control, _sessions = env
    control.mkdir(parents=True, exist_ok=True)
    marker = "/volume/private/raw-status-marker"
    now = update_apply._iso()
    raw_status = {
        "schema_version": 1,
        "request_id": "update-" + "a" * 32,
        "submission_id": SUBMISSION_A,
        "target_version": TARGET_VERSION,
        "status": "failed",
        "effective_status": "failed",
        "phase": "health_check",
        "current_step": "health_check",
        "started_at": now,
        "updated_at": now,
        "source": {
            "kind": "github-tarball",
            "repo": "owner/repo",
            "ref": marker,
            "commit": TARGET_COMMIT,
            "apply_ref": TARGET_COMMIT,
            "internal_path": marker,
        },
        "apply_candidate": {
            "source": "trusted_snapshot",
            "snapshot": {"available": True, "fresh": False, "age_seconds": 901, "internal_path": marker},
            "internal_path": marker,
        },
        "steps": [{"name": "health_check", "status": "failed", "internal_path": marker}],
        "expected_commit": TARGET_COMMIT,
        "installed_commit": None,
        "commit_verified": False,
        "error": {
            "category": "health_check_failed",
            "message": f"raw stderr at {marker}",
            "operator_action": f"inspect {marker}",
            "internal_path": marker,
        },
    }
    (control / "update-status.json").write_text(json.dumps(raw_status), encoding="utf-8")

    status = update_apply.read_update_apply_status()
    rendered = json.dumps(status, ensure_ascii=False)
    assert marker not in rendered
    assert set(status["source"]) == {"kind", "channel", "version", "commit", "apply_ref", "ref", "repo", "source_type"}
    assert status["source"]["ref"] is None
    assert status["apply_candidate"] == {
        "source": "trusted_snapshot",
        "snapshot": {"available": True, "fresh": False, "age_seconds": 901, "fresh_for_seconds": None},
    }
    assert status["error"] == update_apply._public_error_for_category("health_check_failed")


def test_terminal_snapshot_rejects_foreign_or_extra_nested_status(env):
    control, sessions = env
    db = sessions()
    marker = "/volume/private/foreign-terminal-marker"
    try:
        apply(db, signed_ticket(SUBMISSION_A))
        claimed = mark_claimed(control)
        raw = failed_status(claimed)
        raw["source"] = {"repo": "foreign/repo", "ref": marker, "internal_path": marker}
        raw["apply_candidate"] = {"source": "foreign_status", "snapshot": {"internal_path": marker}}
        unsafe_error = {
            "category": "health_check_failed",
            "message": f"raw failure {marker}",
            "operator_action": f"inspect {marker}",
        }
        assert update_apply._strict_terminal_snapshot(raw, update_apply._admission_entry_contract(claimed)) is None
        terminal = update_apply._strict_terminal_snapshot(failed_status(claimed), update_apply._admission_entry_contract(claimed))
        assert terminal is not None
        assert terminal["source"]["repo"] == "owner/repo"
        assert terminal["source"]["ref"] == "main"
        assert terminal["apply_candidate"]["source"] == "live_check"
        assert terminal["error"] == update_apply._public_error_for_category("health_check_failed")
        assert marker not in json.dumps(terminal, ensure_ascii=False)
        assert update_apply._strict_terminal_snapshot({**raw, "error": unsafe_error}, update_apply._admission_entry_contract(claimed)) is None
    finally:
        db.close()


def test_missing_audit_converges_automatically_and_exactly_once(env, monkeypatch):
    control, sessions = env
    db = sessions()
    ticket = signed_ticket(SUBMISSION_A)
    original = update_apply._ensure_deterministic_accepted_audit
    try:
        monkeypatch.setattr(
            update_apply,
            "_ensure_deterministic_accepted_audit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(UpdateApplyBlocked("accepted_audit_unavailable", "injected")),
        )
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply(db, ticket)
        assert exc.value.code == "accepted_audit_unavailable"
        assert document(control)["entries"][0]["state"] == "audit_pending"
        assert audit_count(db) == 0
        update_apply.read_update_apply_status()
        assert audit_count(db) == 0, "status GET must remain read-only"

        monkeypatch.setattr(update_apply, "_ensure_deterministic_accepted_audit", original)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: update_apply.reconcile_update_apply_audit_once(session_factory=sessions), range(2)))
        assert any(item["repaired"] for item in results)
        assert document(control)["entries"][0]["state"] == "admitted_unclaimed"
        assert audit_count(db) == 1
        event = db.query(AuditEvent).one()
        assert event.actor_user_id == 1 and event.actor_username == "owner"
    finally:
        db.close()


def test_helper_never_claims_audit_pending(env, tmp_path, monkeypatch):
    control, sessions = env
    db = sessions()
    helper = load_helper()
    configure_helper(helper, control, tmp_path, monkeypatch)
    original = update_apply._ensure_deterministic_accepted_audit
    try:
        monkeypatch.setattr(
            update_apply,
            "_ensure_deterministic_accepted_audit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(UpdateApplyBlocked("accepted_audit_unavailable", "injected")),
        )
        with pytest.raises(UpdateApplyBlocked):
            apply(db, signed_ticket(SUBMISSION_A))
        assert helper.claim_current_request() is None
        assert document(control)["entries"][0]["state"] == "audit_pending"
        assert not (control / "update-status.json").exists()
    finally:
        monkeypatch.setattr(update_apply, "_ensure_deterministic_accepted_audit", original)
        db.close()


def test_expired_absent_submission_never_becomes_work_and_fresh_c_can(env):
    control, sessions = env
    db = sessions()
    try:
        expired_a = signed_ticket(SUBMISSION_A, issued_at=update_apply._utcnow() - timedelta(hours=1))
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply(db, expired_a)
        assert exc.value.code == "submission_expired"
        assert not (control / "update-request.json").exists()
        assert audit_count(db) == 0
        absence = update_apply.read_update_apply_reconciliation(
            submission_id=SUBMISSION_A,
            submission_proof=expired_a["submission_proof"],
            actor_id=1,
        )
        assert absence["status"] == "submission_expired" and absence["found"] is False
        fresh = signed_ticket(SUBMISSION_B)
        assert apply(db, fresh)["accepted"] is True
    finally:
        db.close()


def test_old_terminal_is_pruned_only_after_retention_and_expired_replay_stays_expired(env):
    control, sessions = env
    db = sessions()
    try:
        ticket_a = signed_ticket(SUBMISSION_A)
        apply(db, ticket_a)
        update_apply.cancel_update_apply()
        payload = document(control)
        old = update_apply._iso(update_apply._utcnow() - timedelta(days=91))
        payload["entries"][0]["requested_at"] = old
        payload["entries"][0]["request"]["requested_at"] = old
        payload["entries"][0]["terminal"]["started_at"] = old
        payload["entries"][0]["terminal"]["updated_at"] = old
        payload["entries"][0]["terminal"]["finished_at"] = old
        update_apply._atomic_write_json(control / "update-request.json", payload)
        apply(db, signed_ticket(SUBMISSION_B))
        assert SUBMISSION_A not in {item["submission_id"] for item in document(control)["entries"]}
        expired_a = signed_ticket(SUBMISSION_A, issued_at=update_apply._utcnow() - timedelta(hours=1))
        absence = update_apply.read_update_apply_reconciliation(
            submission_id=SUBMISSION_A,
            submission_proof=expired_a["submission_proof"],
            actor_id=1,
        )
        assert absence["status"] == "submission_expired"
    finally:
        db.close()


def test_ledger_count_capacity_never_evicts_recent_terminal_entries(env, monkeypatch):
    control, sessions = env
    db = sessions()
    monkeypatch.setattr(update_apply, "MAX_ADMISSION_BYTES", 1024 * 1024)
    now = update_apply._iso()
    entries = []
    for index in range(update_apply.MAX_ADMISSION_ENTRIES):
        submission_id = f"00000000-0000-4000-8000-{index + 1:012x}"
        request_id = f"update-{index + 1:032x}"
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
                "message": "Cancelled before start.",
                "operator_action": "No update was applied.",
            },
        }
        entries.append({
            "submission_id": submission_id,
            "request_id": request_id,
            "target_version": TARGET_VERSION,
            "target_commit": TARGET_COMMIT,
            "requested_at": now,
            "updated_at": now,
            "state": "terminal",
            "request": request,
            "audit": {"state": "confirmed", "event_id": update_apply._audit_event_id(request_id), "confirmed_at": now},
            "claimed_at": None,
            "terminal": terminal,
        })
    payload = {
        "schema_version": 2,
        "document_type": update_apply.ADMISSION_DOCUMENT_TYPE,
        "current_submission_id": entries[-1]["submission_id"],
        "entries": entries,
        "updated_at": now,
    }
    update_apply._atomic_write_json(control / "update-admission-lineage.json", update_apply.LINEAGE_MARKER_PAYLOAD)
    update_apply._atomic_write_json(control / "update-request.json", payload)
    try:
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply(db, signed_ticket(SUBMISSION_A))
        assert exc.value.code == "submission_ledger_capacity"
        assert len(document(control)["entries"]) == update_apply.MAX_ADMISSION_ENTRIES
        assert audit_count(db) == 0
    finally:
        db.close()


def test_ledger_byte_capacity_rejects_without_request_or_audit(env, monkeypatch):
    control, sessions = env
    db = sessions()
    monkeypatch.setattr(update_apply, "MAX_ADMISSION_BYTES", 1024)
    try:
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply(db, signed_ticket(SUBMISSION_A))
        assert exc.value.code == "submission_ledger_capacity"
        assert not (control / "update-request.json").exists()
        assert audit_count(db) == 0
    finally:
        db.close()


def test_helper_claim_and_cancel_have_one_winner(env, tmp_path, monkeypatch):
    control, sessions = env
    db = sessions()
    helper = load_helper()
    configure_helper(helper, control, tmp_path, monkeypatch)
    try:
        first = apply(db, signed_ticket(SUBMISSION_A))
        claimed_request = helper.claim_current_request()
        assert claimed_request["request_id"] == first["request_id"]
        assert update_apply.cancel_update_apply()["status"] == "not_cancelable"
        assert document(control)["entries"][0]["state"] == "claimed"
        assert json.loads((control / "update-status.json").read_text(encoding="utf-8"))["status"] == "starting_helper"
        with pytest.raises(UpdateApplyBlocked) as exc:
            apply(db, signed_ticket(SUBMISSION_B))
        assert exc.value.code == "update_already_running"
    finally:
        db.close()


def test_cancel_before_claim_prevents_helper_execution(env, tmp_path, monkeypatch):
    control, sessions = env
    db = sessions()
    helper = load_helper()
    configure_helper(helper, control, tmp_path, monkeypatch)
    try:
        apply(db, signed_ticket(SUBMISSION_A))
        assert update_apply.cancel_update_apply()["status"] == "cancelled"
        assert helper.claim_current_request() is None
        assert document(control)["entries"][0]["state"] == "terminal"
    finally:
        db.close()


def test_claimed_request_after_helper_restart_fails_closed_without_rerun(env, tmp_path, monkeypatch):
    control, sessions = env
    db = sessions()
    helper = load_helper()
    configure_helper(helper, control, tmp_path, monkeypatch)
    try:
        apply(db, signed_ticket(SUBMISSION_A))
        assert helper.claim_current_request()
        assert helper.claim_current_request() is None
        payload = document(control)
        assert payload["entries"][0]["state"] == "terminal"
        assert payload["entries"][0]["terminal"]["error"]["category"] == "helper_restart_interrupted"
        assert update_apply.read_update_apply_status()["status"] == "failed"
    finally:
        db.close()


def test_request_db_transaction_is_rolled_back_before_every_admission_lock(env, monkeypatch):
    _control, sessions = env
    db = sessions()
    original_guard = update_apply._admission_guard

    @contextmanager
    def guarded():
        assert not db.in_transaction()
        with original_guard():
            yield

    monkeypatch.setattr(update_apply, "_admission_guard", guarded)
    try:
        assert apply(db, signed_ticket(SUBMISSION_A))["accepted"] is True
    finally:
        db.close()
