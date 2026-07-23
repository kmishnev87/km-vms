from __future__ import annotations

import copy
import importlib.util
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

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
REQUEST_ID = "update-" + "a" * 32
REQUESTED_AT = "2026-07-21T00:00:00Z"
CONFIRMED_AT = "2026-07-21T00:00:10Z"
CLAIMED_AT = "2026-07-21T00:00:20Z"
FINISHED_AT = "2026-07-21T00:01:00Z"


def load_helper():
    path = Path(__file__).resolve().parents[3] / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location(f"stage660124_helper_{uuid.uuid4().hex}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_candidate(*, source: str = "trusted_snapshot", available: bool = True) -> dict:
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


def compact_candidate() -> dict:
    return {
        "source": "live_check",
        "snapshot": {
            "available": False,
            "fresh": False,
            "age_seconds": None,
            "fresh_for_seconds": 900,
        },
    }


def request_payload(*, candidate: dict | None = None) -> dict:
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
            "user_agent": "stage-660124",
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


def side_effects() -> dict:
    return {
        "api_docker_socket": False,
        "api_shell_execution": False,
        "request_controlled_source": False,
        "helper_has_docker_socket": True,
        "helper_public_ports": False,
    }


def exact_error(category: str = "health_check_failed") -> dict:
    return {
        "category": category,
        "message": "Update operation did not complete.",
        "operator_action": "Review update status before retrying.",
    }


def terminal_payload(request: dict, status: str, *, error=None) -> dict:
    if status == "completed":
        phase = "completed"
        step = {"name": "commit_verification", "status": "completed"}
    elif status == "cancelled":
        phase = "cancelled"
        step = {"name": "request", "status": "completed"}
    else:
        phase = "health_check_failed"
        step = {"name": "preflight", "status": "failed"}
    payload = {
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
        "side_effects": side_effects(),
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


def entry_payload(state: str, *, candidate: dict | None = None, terminal: dict | None = None) -> dict:
    request = request_payload(candidate=candidate)
    if state == "audit_pending":
        audit_state, confirmed_at, claimed_at, updated_at = "pending", None, None, REQUESTED_AT
    elif state == "admitted_unclaimed":
        audit_state, confirmed_at, claimed_at, updated_at = "confirmed", CONFIRMED_AT, None, CONFIRMED_AT
    elif state == "claimed":
        audit_state, confirmed_at, claimed_at, updated_at = "confirmed", CONFIRMED_AT, CLAIMED_AT, CLAIMED_AT
    else:
        terminal = terminal or terminal_payload(request, "cancelled", error=exact_error("cancelled_before_start"))
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


def admission_document(entry: dict, *, current: bool = True) -> dict:
    return {
        "schema_version": 2,
        "document_type": update_apply.ADMISSION_DOCUMENT_TYPE,
        "current_submission_id": entry["submission_id"] if current else None,
        "entries": [entry],
        "updated_at": entry["updated_at"],
    }


def api_accepts(payload: dict) -> bool:
    contract, document = update_apply._admission_document_contract(copy.deepcopy(payload), "valid")
    return contract in {"current", "legacy"} and document is not None


def helper_accepts(helper, payload: dict) -> bool:
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


MALFORMED_COMPLETED_ERRORS = [
    pytest.param(exact_error(), id="valid-non-null-object"),
    pytest.param("malformed", id="string"),
    pytest.param([], id="list"),
    pytest.param(0, id="integer-zero"),
    pytest.param(7, id="integer-nonzero"),
    pytest.param(1.5, id="finite-float"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param({}, id="empty-object"),
    pytest.param({"unknown": "value"}, id="unknown-key-object"),
    pytest.param({"category": "health_check_failed"}, id="category-only"),
    pytest.param(
        {"category": "health_check_failed", "operator_action": "Review status."},
        id="missing-message",
    ),
    pytest.param(
        {"category": "health_check_failed", "message": "Failure."},
        id="missing-operator-action",
    ),
    pytest.param(
        {"category": "health_check_failed", "message": 3, "operator_action": "Review status."},
        id="invalid-field-type",
    ),
    pytest.param(
        {"category": "health_check_failed", "message": "x" * 301, "operator_action": "Review status."},
        id="over-bound-text",
    ),
    pytest.param(
        {"category": "health_check_failed", "message": "Bearer synthetic.value", "operator_action": "Review status."},
        id="sensitive-text",
    ),
    pytest.param(
        {"category": "health_check_failed", "message": "Failure at /tmp/synthetic", "operator_action": "Review status."},
        id="unsafe-text",
    ),
]


def test_candidate_validator_preserves_internal_profile_with_api_helper_parity():
    helper = load_helper()
    cases = [
        (canonical_candidate(), "canonical_current"),
        (canonical_candidate(source="live_check", available=False), "canonical_current"),
        (compact_candidate(), "compact_read_only"),
    ]
    for raw, expected_profile in cases:
        api_contract = update_apply._strict_apply_candidate(copy.deepcopy(raw))
        helper_contract = helper.strict_apply_candidate(copy.deepcopy(raw))
        assert api_contract is not None and helper_contract is not None
        assert api_contract[1] == helper_contract[1] == expected_profile
        assert "candidate_profile" not in json.dumps(api_contract[0])


@pytest.mark.parametrize("raw_error", [pytest.param(None, id="exact-null"), *MALFORMED_COMPLETED_ERRORS])
def test_completed_raw_error_shared_corpus_has_api_helper_parity(raw_error):
    helper = load_helper()
    request = request_payload()
    terminal = terminal_payload(request, "completed", error=copy.deepcopy(raw_error))
    payload = admission_document(entry_payload("terminal", terminal=terminal))
    expected = raw_error is None
    assert api_accepts(payload) is expected
    assert helper_accepts(helper, payload) is expected


def test_failed_and_cancelled_exact_error_positive_and_malformed_controls():
    helper = load_helper()
    request = request_payload()
    failed = admission_document(
        entry_payload("terminal", terminal=terminal_payload(request, "failed", error=exact_error()))
    )
    cancelled_terminal = terminal_payload(
        request,
        "cancelled",
        error=exact_error("cancelled_before_start"),
    )
    cancelled = admission_document(entry_payload("terminal", terminal=cancelled_terminal))
    assert api_accepts(failed) and helper_accepts(helper, failed)
    assert api_accepts(cancelled) and helper_accepts(helper, cancelled)

    for base in (failed, cancelled):
        for malformed in (None, "invalid", {}, {"category": "health_check_failed"}):
            payload = copy.deepcopy(base)
            payload["entries"][0]["terminal"]["error"] = malformed
            assert not api_accepts(payload)
            assert not helper_accepts(helper, payload)


@pytest.mark.parametrize("state", ["audit_pending", "admitted_unclaimed", "claimed"])
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param(canonical_candidate(), id="trusted-snapshot"),
        pytest.param(canonical_candidate(source="live_check", available=False), id="full-live-check"),
    ],
)
def test_canonical_current_candidates_remain_valid_in_current_nonterminal_states(state, candidate):
    helper = load_helper()
    payload = admission_document(entry_payload(state, candidate=candidate))
    assert api_accepts(payload)
    assert helper_accepts(helper, payload)


@pytest.mark.parametrize("state", ["audit_pending", "admitted_unclaimed", "claimed"])
def test_compact_read_only_candidate_rejects_every_current_nonterminal_state(state):
    helper = load_helper()
    payload = admission_document(entry_payload(state, candidate=compact_candidate()))
    before = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert not api_accepts(payload)
    assert not helper_accepts(helper, payload)
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == before


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_compact_read_only_candidate_rejects_current_helper_terminal(status):
    helper = load_helper()
    request = request_payload(candidate=compact_candidate())
    error = None if status == "completed" else exact_error()
    terminal = terminal_payload(request, status, error=error)
    payload = admission_document(entry_payload("terminal", candidate=compact_candidate(), terminal=terminal))
    assert not api_accepts(payload)
    assert not helper_accepts(helper, payload)


def test_exact_precloseout_cancel_and_legacy_compact_profiles_remain_read_only_compatible(tmp_path):
    helper = load_helper()
    compact = compact_candidate()
    request = request_payload(candidate=compact)
    terminal = terminal_payload(request, "cancelled", error=exact_error("cancelled_before_start"))
    terminal.pop("side_effects")
    terminal["source"] = request["source"]
    terminal["apply_candidate"] = compact
    terminal["installed_commit"] = None
    payload = admission_document(entry_payload("terminal", candidate=compact, terminal=terminal))
    assert api_accepts(payload) and helper_accepts(helper, payload)

    legacy = {
        "schema_version": 1,
        "request_id": "legacy-request",
        "requested_at": REQUESTED_AT,
        "requested_by": {"user_id": "1", "role": "owner"},
        "intent": "apply_update",
        "source": request["source"],
        "apply_candidate": compact,
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
    }
    for accepted in (legacy, {**legacy, "submission_id": SUBMISSION_ID}):
        assert api_accepts(accepted) and helper_accepts(helper, accepted)
        control = tmp_path / uuid.uuid4().hex
        control.mkdir()
        configure_helper(helper, control)
        helper.REQUEST_FILE.write_text(json.dumps(accepted), encoding="utf-8")
        before = surface_bytes(control)
        assert helper.claim_current_request() is None
        assert surface_bytes(control) == before


def test_mutated_compact_profiles_reject_with_api_helper_parity():
    helper = load_helper()
    mutations = []
    extra = compact_candidate()
    extra["snapshot"]["unexpected"] = True
    mutations.append(extra)
    missing = compact_candidate()
    missing["snapshot"].pop("fresh")
    mutations.append(missing)
    wrong_source = compact_candidate()
    wrong_source["source"] = "trusted_snapshot"
    mutations.append(wrong_source)
    available = compact_candidate()
    available["snapshot"]["available"] = True
    mutations.append(available)
    wrong_type = compact_candidate()
    wrong_type["snapshot"]["fresh_for_seconds"] = "900"
    mutations.append(wrong_type)

    for candidate in mutations:
        payload = admission_document(entry_payload("admitted_unclaimed", candidate=candidate))
        assert not api_accepts(payload)
        assert not helper_accepts(helper, payload)


def test_helper_claim_independently_fences_compact_candidate_before_every_write(tmp_path, monkeypatch):
    helper = load_helper()
    control = tmp_path / "control"
    control.mkdir()
    configure_helper(helper, control)
    payload = admission_document(entry_payload("admitted_unclaimed", candidate=compact_candidate()))
    helper.REQUEST_FILE.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    helper.LINEAGE_FILE.write_text(json.dumps(helper.LINEAGE_PAYLOAD, separators=(",", ":")), encoding="utf-8")
    before = surface_bytes(control)
    writes = []
    monkeypatch.setattr(helper, "write_json", lambda *args, **kwargs: writes.append((args, kwargs)))

    with pytest.raises(helper.HelperError) as exc:
        helper.claim_current_request()
    assert exc.value.category == "admission_candidate_profile_invalid"
    assert writes == []
    assert surface_bytes(control) == before


@pytest.mark.parametrize("raw_error", MALFORMED_COMPLETED_ERRORS)
def test_malformed_completed_error_blocks_api_gates_helper_claim_and_all_mutation(
    tmp_path,
    monkeypatch,
    raw_error,
):
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    request = request_payload()
    terminal = terminal_payload(request, "completed", error=copy.deepcopy(raw_error))
    payload = admission_document(entry_payload("terminal", terminal=terminal))
    (control / "update-request.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    (control / "update-admission-lineage.json").write_text(
        json.dumps(update_apply.LINEAGE_MARKER_PAYLOAD, separators=(",", ":")),
        encoding="utf-8",
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    helper = load_helper()
    configure_helper(helper, control)
    actor = SimpleNamespace(id=1, username="owner", role="owner")
    second_submission = "22222222-2222-4222-8222-222222222222"
    proof, _claims = update_apply._issue_submission_proof(
        submission_id=second_submission,
        target_version=TARGET_VERSION,
        target_commit=TARGET_COMMIT,
        actor_id=1,
        issued_at=update_apply._utcnow(),
    )
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
                submission_id=second_submission,
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
            helper.claim_current_request()
        assert surface_bytes(control) == before
        assert db.query(AuditEvent).count() == before_audits
    finally:
        db.close()
        engine.dispose()


def test_compact_current_authority_cannot_claim_cancel_or_authorize_submission_b(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    payload = admission_document(entry_payload("admitted_unclaimed", candidate=compact_candidate()))
    (control / "update-request.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    (control / "update-admission-lineage.json").write_text(
        json.dumps(update_apply.LINEAGE_MARKER_PAYLOAD, separators=(",", ":")),
        encoding="utf-8",
    )
    helper = load_helper()
    configure_helper(helper, control)
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    actor = SimpleNamespace(id=1, username="owner", role="owner")
    second_submission = "22222222-2222-4222-8222-222222222222"
    proof, _claims = update_apply._issue_submission_proof(
        submission_id=second_submission,
        target_version=TARGET_VERSION,
        target_commit=TARGET_COMMIT,
        actor_id=1,
        issued_at=update_apply._utcnow(),
    )
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
                submission_id=second_submission,
                submission_proof=proof,
                expected_manifest_version=TARGET_VERSION,
                expected_manifest_commit=TARGET_COMMIT,
                actor=actor,
            )
        assert apply_error.value.code == "update_admission_unknown"
        with pytest.raises(UpdateApplyBlocked) as cancel_error:
            update_apply.cancel_update_apply()
        assert cancel_error.value.code == "update_admission_unknown"
        with pytest.raises(helper.HelperError) as claim_error:
            helper.claim_current_request()
        assert claim_error.value.category == "admission_candidate_profile_invalid"
        assert surface_bytes(control) == before
        assert db.query(AuditEvent).count() == before_audits
    finally:
        db.close()
        engine.dispose()
