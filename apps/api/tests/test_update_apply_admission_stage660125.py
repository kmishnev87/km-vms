from __future__ import annotations

import copy
import importlib.util
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.services import update_apply
from app.services.update_apply import UpdateApplyBlocked


TARGET_VERSION = "9.9.9"
TARGET_COMMIT = "c" * 40
SUBMISSION_ID = "11111111-1111-4111-8111-111111111111"
SECOND_SUBMISSION_ID = "22222222-2222-4222-8222-222222222222"
REQUEST_ID = "update-" + "a" * 32
REQUESTED_AT = "2026-07-21T00:00:00Z"
CONFIRMED_AT = "2026-07-21T00:00:10Z"
CLAIMED_AT = "2026-07-21T00:00:20Z"
FINISHED_AT = "2026-07-21T00:01:00Z"


def load_helper():
    path = Path(__file__).resolve().parents[3] / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location(f"stage660125_helper_{uuid.uuid4().hex}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_candidate(*, source: str = "trusted_snapshot", available: bool = True) -> dict[str, Any]:
    return {
        "source": source,
        "snapshot": {
            "available": available,
            "fresh": True,
            "age_seconds": 1,
            "fresh_for_seconds": 900,
            "version": TARGET_VERSION,
            "commit_short": TARGET_COMMIT[:12],
            "provider": "github_release",
        },
    }


def compact_candidate() -> dict[str, Any]:
    return {
        "source": "live_check",
        "snapshot": {
            "available": False,
            "fresh": False,
            "age_seconds": None,
            "fresh_for_seconds": 900,
        },
    }


def request_payload(*, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "request_id": REQUEST_ID,
        "submission_id": SUBMISSION_ID,
        "requested_at": REQUESTED_AT,
        "requested_by": {
            "user_id": 1,
            "username": "owner",
            "role": "owner",
            "ip_address": None,
            "user_agent": "stage-660125",
        },
        "intent": "apply_update",
        "source": {
            "kind": "trusted_manifest",
            "channel": "stable",
            "version": TARGET_VERSION,
            "commit": TARGET_COMMIT,
            "apply_ref": TARGET_COMMIT,
            "ref": "main",
            "repo": "owner/repo",
            "source_type": "github_tarball",
        },
        "apply_candidate": copy.deepcopy(candidate or canonical_candidate()),
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
    }


def legacy_request_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": "legacy-request",
        "requested_at": REQUESTED_AT,
        "intent": "apply_update",
        "confirmed": True,
        "source": {"version": TARGET_VERSION, "commit": TARGET_COMMIT},
    }


def terminal_side_effects() -> dict[str, bool]:
    return {
        "api_docker_socket": False,
        "api_shell_execution": False,
        "request_controlled_source": False,
        "helper_has_docker_socket": True,
        "helper_public_ports": False,
    }


def exact_error(category: str = "health_check_failed") -> dict[str, str]:
    return {
        "category": category,
        "message": "Update operation did not complete.",
        "operator_action": "Review update status before retrying.",
    }


def terminal_payload(request: dict[str, Any], status: str) -> dict[str, Any]:
    if status == "completed":
        phase = "completed"
        step = {"name": "commit_verification", "status": "completed"}
        error = None
    elif status == "cancelled":
        phase = "cancelled"
        step = {"name": "request", "status": "completed"}
        error = exact_error("cancelled_before_start")
    else:
        phase = "health_check_failed"
        step = {"name": "preflight", "status": "failed"}
        error = exact_error()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "submission_id": request["submission_id"],
        "target_version": request["source"]["version"],
        "status": status,
        "phase": phase,
        "current_step": phase,
        "started_at": request["requested_at"],
        "updated_at": FINISHED_AT,
        "finished_at": FINISHED_AT,
        "source": {
            "kind": "github-tarball",
            "repo": request["source"]["repo"],
            "ref": request["source"]["ref"],
            "commit": request["source"]["commit"],
            "apply_ref": request["source"]["apply_ref"],
        },
        "expected_commit": request["source"]["commit"],
        "commit_verified": status == "completed",
        "steps": [step],
        "can_cancel": False,
        "rollback_supported": False,
        "side_effects": terminal_side_effects(),
        "error": error,
    }
    if status == "completed":
        payload["installed_commit"] = request["source"]["commit"]
        payload["release_identity"] = {
            "host_metadata_status": "complete",
            "api_metadata_status": "complete",
            "api_visible": True,
            "commit_verified": True,
        }
    return payload


def verified_legacy_terminal(request: dict[str, Any]) -> dict[str, Any]:
    source = request["source"]
    return {
        "schema_version": 1,
        "request_id": request["request_id"],
        "status": "completed",
        "phase": "commit_verification",
        "current_step": "commit_verification",
        "started_at": request["requested_at"],
        "updated_at": FINISHED_AT,
        "source": {
            "kind": "github-tarball",
            "repo": source["repo"],
            "ref": source["ref"],
            "commit": source["commit"],
            "apply_ref": source["apply_ref"],
        },
        "expected_commit": source["commit"],
        "installed_commit": source["commit"],
        "commit_verified": True,
        "steps": [
            {"name": name, "status": "completed"}
            for name in update_apply.LEGACY_VERIFIED_COMPLETED_STEP_NAMES
        ],
        "can_cancel": False,
        "rollback_supported": False,
        "side_effects": terminal_side_effects(),
        "release_identity": {
            "host_metadata_status": "complete",
            "api_metadata_status": "complete",
            "api_visible": True,
            "commit_verified": True,
        },
        "error": None,
    }


def active_status_payload(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": request["request_id"],
        "submission_id": request["submission_id"],
        "target_version": request["source"]["version"],
        "status": "starting_helper",
        "phase": "starting_helper",
        "current_step": "starting_helper",
        "updated_at": CLAIMED_AT,
        "expected_commit": request["source"]["commit"],
    }


def entry_payload(
    state: str,
    *,
    request: dict[str, Any] | None = None,
    terminal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = copy.deepcopy(request or request_payload())
    if state == "audit_pending":
        audit_state, confirmed_at, claimed_at, updated_at = "pending", None, None, REQUESTED_AT
    elif state == "admitted_unclaimed":
        audit_state, confirmed_at, claimed_at, updated_at = "confirmed", CONFIRMED_AT, None, CONFIRMED_AT
    elif state == "claimed":
        audit_state, confirmed_at, claimed_at, updated_at = "confirmed", CONFIRMED_AT, CLAIMED_AT, CLAIMED_AT
    else:
        terminal = copy.deepcopy(terminal or terminal_payload(request, "cancelled"))
        claimed_at = None if terminal["status"] == "cancelled" else CLAIMED_AT
        audit_state, confirmed_at, updated_at = "confirmed", CONFIRMED_AT, terminal["finished_at"]
    return {
        "submission_id": SUBMISSION_ID,
        "request_id": REQUEST_ID,
        "target_version": TARGET_VERSION,
        "target_commit": TARGET_COMMIT,
        "requested_at": REQUESTED_AT,
        "updated_at": updated_at,
        "state": state,
        "request": request,
        "audit": {
            "state": audit_state,
            "event_id": update_apply._audit_event_id(REQUEST_ID),
            "confirmed_at": confirmed_at,
        },
        "claimed_at": claimed_at,
        "terminal": terminal,
    }


def admission_document(entry: dict[str, Any]) -> dict[str, Any]:
    current = entry["state"] != "terminal"
    return {
        "schema_version": 2,
        "document_type": update_apply.ADMISSION_DOCUMENT_TYPE,
        "current_submission_id": entry["submission_id"] if current else None,
        "entries": [entry],
        "updated_at": entry["updated_at"],
    }


def api_accepts(payload: dict[str, Any]) -> bool:
    contract, document = update_apply._admission_document_contract(copy.deepcopy(payload), "valid")
    return contract in {"current", "legacy"} and document is not None


def helper_accepts(helper, payload: dict[str, Any]) -> bool:
    try:
        contract, _document = helper.validate_admission_document(copy.deepcopy(payload))
    except helper.HelperError:
        return False
    return contract in {"current", "legacy"}


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


def surface_bytes(control: Path) -> dict[str, bytes | None]:
    names = (
        "update-request.json",
        "update-admission-lineage.json",
        "update-status.json",
        "update-progress.json",
        "update-apply-history.json",
        "update-helper-history.json",
    )
    return {name: (control / name).read_bytes() if (control / name).exists() else None for name in names}


def signed_proof(version: Any, *, submission_id: str = SECOND_SUBMISSION_ID) -> str:
    payload = update_apply._proof_payload(
        submission_id=submission_id,
        target_version=TARGET_VERSION,
        target_commit=TARGET_COMMIT,
        actor_id=1,
        issued_at=update_apply._utcnow(),
    )
    payload["version"] = copy.deepcopy(version)
    return jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm="HS256",
        headers={"typ": update_apply.SUBMISSION_PROOF_TYPE},
    )


def test_exact_positive_current_live_check_terminal_and_legacy_controls(tmp_path):
    helper = load_helper()
    trusted = admission_document(entry_payload("admitted_unclaimed"))
    live_request = request_payload(candidate=canonical_candidate(source="live_check", available=False))
    live = admission_document(entry_payload("admitted_unclaimed", request=live_request))
    completed_request = request_payload()
    completed = admission_document(
        entry_payload("terminal", request=completed_request, terminal=terminal_payload(completed_request, "completed"))
    )
    legacy = legacy_request_payload()
    for payload in (trusted, live, completed, legacy):
        assert api_accepts(payload)
        assert helper_accepts(helper, payload)

    configure_helper(helper, tmp_path)
    helper.REQUEST_FILE.write_text(json.dumps(legacy, separators=(",", ":")), encoding="utf-8")
    before = surface_bytes(tmp_path)
    assert helper.claim_current_request() is None
    assert surface_bytes(tmp_path) == before


def test_exact_verified_legacy_completion_retires_and_allows_next_ticket(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)

    source = request_payload()["source"]
    legacy = {
        "schema_version": 1,
        "request_id": "legacy-snapshot-request",
        "requested_at": REQUESTED_AT,
        "requested_by": {"user_id": 1, "role": "owner"},
        "intent": "apply_update",
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
        "source": source,
        "apply_candidate": canonical_candidate(),
    }
    terminal = verified_legacy_terminal(legacy)
    (control / "update-request.json").write_text(
        json.dumps(legacy, separators=(",", ":")),
        encoding="utf-8",
    )
    (control / "update-status.json").write_text(
        json.dumps(terminal, separators=(",", ":")),
        encoding="utf-8",
    )

    helper = load_helper()
    assert helper.terminal_status_for_request(terminal, legacy)
    assert update_apply._legacy_can_retire(
        update_apply._legacy_request_contract(legacy, "valid")[1]
    )
    status = update_apply.read_update_apply_status()
    assert status["status"] == "completed"
    assert status["effective_status"] == "completed"
    assert status["admission"]["authority"] == "inactive"

    monkeypatch.setattr(
        update_apply,
        "_select_apply_candidate",
        lambda _db, *, expected_version, expected_commit: (
            {"version": expected_version, "commit": expected_commit},
            {"source": "trusted_snapshot", "snapshot": {}},
        ),
    )
    monkeypatch.setattr(update_apply, "_check_token_precondition", lambda: None)
    ticket = update_apply.issue_update_apply_submission_ticket(
        SimpleNamespace(rollback=lambda: None),
        expected_manifest_version=TARGET_VERSION,
        expected_manifest_commit=TARGET_COMMIT,
        actor=SimpleNamespace(id=1, username="owner", role="owner"),
    )
    assert ticket["target_version"] == TARGET_VERSION
    assert ticket["target_commit"] == TARGET_COMMIT

    for mutation in (
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value["steps"][3].update(status="failed"),
        lambda value: value["release_identity"].update(api_visible=False),
        lambda value: value.update(installed_commit="d" * 40),
    ):
        malformed = copy.deepcopy(terminal)
        mutation(malformed)
        assert not helper.terminal_status_for_request(malformed, legacy)
        entry = update_apply._legacy_request_contract(legacy, "valid")[1]
        assert update_apply._strict_terminal_snapshot(malformed, entry) is None


def test_canonical_claim_fence_and_compact_read_only_compatibility(tmp_path):
    helper = load_helper()
    executable_root = tmp_path / "executable"
    executable_root.mkdir()
    configure_helper(helper, executable_root)
    executable = admission_document(entry_payload("admitted_unclaimed"))
    helper.REQUEST_FILE.write_text(json.dumps(executable, separators=(",", ":")), encoding="utf-8")
    helper.LINEAGE_FILE.write_text(json.dumps(helper.LINEAGE_PAYLOAD, separators=(",", ":")), encoding="utf-8")
    claimed = helper.claim_current_request()
    assert claimed and claimed["request_id"] == REQUEST_ID

    compact_request = request_payload(candidate=compact_candidate())
    compact_executable = admission_document(entry_payload("admitted_unclaimed", request=compact_request))
    assert not api_accepts(compact_executable)
    assert not helper_accepts(helper, compact_executable)

    cancelled = terminal_payload(compact_request, "cancelled")
    cancelled.pop("side_effects")
    cancelled["source"] = compact_request["source"]
    cancelled["apply_candidate"] = compact_request["apply_candidate"]
    cancelled["installed_commit"] = None
    compact_terminal = admission_document(entry_payload("terminal", request=compact_request, terminal=cancelled))
    assert api_accepts(compact_terminal)
    assert helper_accepts(helper, compact_terminal)


def build_malformed_authority(case_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    request = request_payload()
    if case_name == "terminal-pseudo-false":
        terminal = terminal_payload(request, "failed")
        terminal["commit_verified"] = 0
        return admission_document(entry_payload("terminal", request=request, terminal=terminal)), update_apply.LINEAGE_MARKER_PAYLOAD
    payload = admission_document(entry_payload("admitted_unclaimed", request=request))
    if case_name == "document-float-schema":
        payload["schema_version"] = 2.0
    elif case_name == "request-float-schema":
        payload["entries"][0]["request"]["schema_version"] = 2.0
    elif case_name == "lineage-pseudo-true":
        marker = copy.deepcopy(update_apply.LINEAGE_MARKER_PAYLOAD)
        marker["initialized"] = 1
        return payload, marker
    else:
        raise AssertionError(case_name)
    return payload, update_apply.LINEAGE_MARKER_PAYLOAD


@pytest.mark.parametrize(
    "case_name",
    ("terminal-pseudo-false", "document-float-schema", "request-float-schema", "lineage-pseudo-true"),
)
def test_malformed_authority_blocks_every_real_mutation_boundary(tmp_path, monkeypatch, case_name):
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    payload, marker = build_malformed_authority(case_name)
    request = payload["entries"][0]["request"]
    (control / "update-request.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    (control / "update-admission-lineage.json").write_text(json.dumps(marker, separators=(",", ":")), encoding="utf-8")
    if case_name == "terminal-pseudo-false":
        status_payload = payload["entries"][0]["terminal"]
    else:
        status_payload = {"schema_version": 1, "status": "idle"}
    (control / "update-status.json").write_text(json.dumps(status_payload, separators=(",", ":")), encoding="utf-8")
    for name in ("update-progress.json", "update-apply-history.json", "update-helper-history.json"):
        (control / name).write_text('{"sentinel":"unchanged"}', encoding="utf-8")

    engine = create_engine(f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    actor = SimpleNamespace(id=1, username="owner", role="owner")
    proof, _claims = update_apply._issue_submission_proof(
        submission_id=SECOND_SUBMISSION_ID,
        target_version=TARGET_VERSION,
        target_commit=TARGET_COMMIT,
        actor_id=1,
        issued_at=update_apply._utcnow(),
    )
    helper = load_helper()
    configure_helper(helper, control)
    run_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(helper, "run_update", lambda request: run_calls.append(request))
    before = surface_bytes(control)
    before_audits = db.query(AuditEvent).count()
    try:
        assert update_apply.read_update_apply_status()["admission"]["authority"] == "unknown"
        with pytest.raises(UpdateApplyBlocked) as ticket_error:
            update_apply.issue_update_apply_submission_ticket(
                db,
                expected_manifest_version=TARGET_VERSION,
                expected_manifest_commit=TARGET_COMMIT,
                actor=actor,
            )
        assert ticket_error.value.code == "update_admission_unknown"
        with pytest.raises(UpdateApplyBlocked) as apply_error:
            update_apply.request_update_apply(
                db,
                confirm=True,
                submission_id=SECOND_SUBMISSION_ID,
                submission_proof=proof,
                expected_manifest_version=TARGET_VERSION,
                expected_manifest_commit=TARGET_COMMIT,
                actor=actor,
            )
        assert apply_error.value.code == "update_admission_unknown"
        with pytest.raises(UpdateApplyBlocked) as cancel_error:
            update_apply.cancel_update_apply()
        assert cancel_error.value.code == "update_admission_unknown"

        with pytest.raises(helper.HelperError):
            helper.read_admission_authority()
        request_to_run = None
        try:
            request_to_run = helper.claim_current_request()
        except helper.HelperError:
            pass
        if request_to_run:
            helper.run_update(request_to_run)
        assert request_to_run is None
        assert run_calls == []
        with pytest.raises(helper.HelperError):
            helper.publish_terminal(request, terminal_payload(request, "failed"))

        assert surface_bytes(control) == before
        assert db.query(AuditEvent).count() == before_audits == 0
    finally:
        db.close()
        engine.dispose()


def test_invalid_proof_version_cannot_mutate_admission_or_audit(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    actor = SimpleNamespace(id=1, username="owner", role="owner")
    before = surface_bytes(control)
    try:
        with pytest.raises(UpdateApplyBlocked) as error:
            update_apply.request_update_apply(
                db,
                confirm=True,
                submission_id=SECOND_SUBMISSION_ID,
                submission_proof=signed_proof(True),
                expected_manifest_version=TARGET_VERSION,
                expected_manifest_commit=TARGET_COMMIT,
                actor=actor,
            )
        assert error.value.code == "submission_proof_invalid"
        assert surface_bytes(control) == before
        assert db.query(AuditEvent).count() == 0
    finally:
        db.close()
        engine.dispose()
