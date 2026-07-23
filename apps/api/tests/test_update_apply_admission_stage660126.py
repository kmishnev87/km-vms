from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
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


MAX_VERSION = "v" + "1" * 79
SYNTHETIC_SENSITIVE_VERSION = "ghp" + "_SYNTHETIC_STAGE660126"
MISSING = object()


def load_stage125():
    path = Path(__file__).with_name("test_update_apply_admission_stage660125.py")
    spec = importlib.util.spec_from_file_location(f"stage660126_base_{uuid.uuid4().hex}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def base():
    return load_stage125()


def signed_claim_proof(base, field: str | None = None, value: Any = None) -> str:
    payload = update_apply._proof_payload(
        submission_id=base.SECOND_SUBMISSION_ID,
        target_version=base.TARGET_VERSION,
        target_commit=base.TARGET_COMMIT,
        actor_id=1,
        issued_at=update_apply._utcnow(),
    )
    if field is not None:
        payload[field] = copy.deepcopy(value)
    return jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm="HS256",
        headers={"typ": update_apply.SUBMISSION_PROOF_TYPE},
    )


INVALID_RAW_ENUM_VALUES = (
    ("null", None),
    ("boolean", True),
    ("integer", 1),
    ("float", 1.0),
    ("object", {"unexpected": "value"}),
    ("array", ["unexpected"]),
    ("empty-string", ""),
    ("wrong-string", "unexpected"),
)


@pytest.mark.parametrize(
    "confirmed_at,accepted",
    (
        ("2026-07-21T00:00:10Z", True),
        (20260721, False),
        (20260721.0, False),
        (True, False),
        (None, False),
        ({}, False),
        ([], False),
        ("", False),
        ("not-a-timestamp", False),
        ("2" * 81, False),
        (" 2026-07-21T00:00:10Z", False),
        ("2026-07-21T00:00:10Z ", False),
        ("\t2026-07-21T00:00:10Z\n", False),
    ),
    ids=(
        "string",
        "integer",
        "float",
        "boolean",
        "null",
        "object",
        "array",
        "empty",
        "invalid",
        "overlong",
        "leading-space",
        "trailing-space",
        "surrounding-control-whitespace",
    ),
)
def test_confirmed_audit_timestamp_requires_exact_bounded_json_string(base, confirmed_at, accepted):
    helper = base.load_helper()
    payload = base.admission_document(base.entry_payload("admitted_unclaimed"))
    payload["entries"][0]["audit"]["confirmed_at"] = copy.deepcopy(confirmed_at)
    assert base.api_accepts(payload) is accepted
    assert base.helper_accepts(helper, payload) is accepted


@pytest.mark.parametrize(
    "case_name",
    (
        "request-and-entry-requested-at",
        "document-updated-at",
        "entry-updated-at",
        "audit-confirmed-at",
        "claimed-at",
        "terminal-started-at",
        "terminal-updated-at",
        "terminal-finished-at",
    ),
)
def test_every_authority_timestamp_surface_rejects_nonexact_raw_string(base, case_name):
    helper = base.load_helper()
    request = base.request_payload()
    if case_name == "claimed-at":
        payload = base.admission_document(base.entry_payload("claimed", request=request))
    elif case_name.startswith("terminal-"):
        terminal = base.terminal_payload(request, "failed")
        payload = base.admission_document(base.entry_payload("terminal", request=request, terminal=terminal))
    else:
        payload = base.admission_document(base.entry_payload("admitted_unclaimed", request=request))

    entry = payload["entries"][0]
    if case_name == "request-and-entry-requested-at":
        value = " " + entry["requested_at"]
        entry["requested_at"] = value
        entry["request"]["requested_at"] = value
    elif case_name == "document-updated-at":
        payload["updated_at"] += " "
    elif case_name == "entry-updated-at":
        entry["updated_at"] += " "
    elif case_name == "audit-confirmed-at":
        entry["audit"]["confirmed_at"] += " "
    elif case_name == "claimed-at":
        entry["claimed_at"] = " " + entry["claimed_at"]
    elif case_name == "terminal-started-at":
        entry["terminal"]["started_at"] = " " + entry["terminal"]["started_at"]
    elif case_name == "terminal-updated-at":
        entry["terminal"]["updated_at"] += " "
    elif case_name == "terminal-finished-at":
        entry["terminal"]["finished_at"] += " "
    else:
        raise AssertionError(case_name)

    assert base.api_accepts(payload) is False
    assert base.helper_accepts(helper, payload) is False


@pytest.mark.parametrize("field", ("message", "operator_action"))
@pytest.mark.parametrize("blank", (" ", "\t", "\n"), ids=("space", "tab", "newline"))
def test_terminal_error_text_requires_nonblank_raw_string_in_api_and_helper(base, field, blank):
    helper = base.load_helper()
    request = base.request_payload()
    terminal = base.terminal_payload(request, "failed")
    terminal["error"][field] = blank
    payload = base.admission_document(base.entry_payload("terminal", request=request, terminal=terminal))
    assert base.api_accepts(payload) is False
    assert base.helper_accepts(helper, payload) is False


ENUM_MUTATIONS = (
    ("entry-state", lambda payload, value: payload["entries"][0].__setitem__("state", value)),
    (
        "candidate-source",
        lambda payload, value: payload["entries"][0]["request"]["apply_candidate"].__setitem__("source", value),
    ),
    ("audit-state", lambda payload, value: payload["entries"][0]["audit"].__setitem__("state", value)),
)


@pytest.mark.parametrize("case_name,mutate", ENUM_MUTATIONS, ids=[case[0] for case in ENUM_MUTATIONS])
@pytest.mark.parametrize("value_name,value", INVALID_RAW_ENUM_VALUES, ids=[case[0] for case in INVALID_RAW_ENUM_VALUES])
def test_authority_enum_fields_fail_closed_without_typeerror(base, case_name, mutate, value_name, value):
    del case_name, value_name
    helper = base.load_helper()
    payload = base.admission_document(base.entry_payload("admitted_unclaimed"))
    mutate(payload, copy.deepcopy(value))
    assert base.api_accepts(payload) is False
    assert base.helper_accepts(helper, payload) is False


@pytest.mark.parametrize("field", ("name", "status"))
@pytest.mark.parametrize("value_name,value", INVALID_RAW_ENUM_VALUES, ids=[case[0] for case in INVALID_RAW_ENUM_VALUES])
def test_terminal_step_enum_fields_fail_closed_without_typeerror(base, field, value_name, value):
    del value_name
    helper = base.load_helper()
    request = base.request_payload()
    terminal = base.terminal_payload(request, "failed")
    terminal["steps"][0][field] = copy.deepcopy(value)
    payload = base.admission_document(base.entry_payload("terminal", request=request, terminal=terminal))
    assert base.api_accepts(payload) is False
    assert base.helper_accepts(helper, payload) is False


@pytest.mark.parametrize("value_name,value", INVALID_RAW_ENUM_VALUES, ids=[case[0] for case in INVALID_RAW_ENUM_VALUES])
def test_terminal_status_enum_fails_closed_without_typeerror(base, value_name, value):
    del value_name
    helper = base.load_helper()
    request = base.request_payload()
    terminal = base.terminal_payload(request, "failed")
    terminal["status"] = copy.deepcopy(value)
    payload = base.admission_document(base.entry_payload("terminal", request=request, terminal=terminal))
    assert base.api_accepts(payload) is False
    assert base.helper_accepts(helper, payload) is False


PROOF_INVALID_CASES = (
    ("audience-list-one", "aud", [update_apply.SUBMISSION_PROOF_AUDIENCE]),
    ("audience-list-many", "aud", [update_apply.SUBMISSION_PROOF_AUDIENCE, "other"]),
    ("audience-null", "aud", None),
    ("audience-boolean", "aud", True),
    ("audience-integer", "aud", 1),
    ("audience-float", "aud", 1.0),
    ("audience-object", "aud", {"value": update_apply.SUBMISSION_PROOF_AUDIENCE}),
    ("audience-wrong-string", "aud", "other"),
    ("target-version-null", "target_version", None),
    ("target-version-boolean", "target_version", True),
    ("target-version-integer", "target_version", 123),
    ("target-version-float", "target_version", 9.9),
    ("target-version-object", "target_version", {}),
    ("target-version-array", "target_version", []),
    ("target-version-empty", "target_version", ""),
    ("target-version-leading-space", "target_version", " 9.9.9"),
    ("target-version-trailing-space", "target_version", "9.9.9 "),
    ("target-version-length-81", "target_version", "v" * 81),
    ("target-version-length-200", "target_version", "v" * 200),
    ("target-version-sensitive-shaped", "target_version", SYNTHETIC_SENSITIVE_VERSION),
    ("target-commit-null", "target_commit", None),
    ("target-commit-boolean", "target_commit", True),
    ("target-commit-integer", "target_commit", int("1" * 40)),
    ("target-commit-float", "target_commit", 1.0),
    ("target-commit-object", "target_commit", {}),
    ("target-commit-array", "target_commit", []),
    ("target-commit-empty", "target_commit", ""),
    ("target-commit-length-39", "target_commit", "a" * 39),
    ("target-commit-length-41", "target_commit", "a" * 41),
    ("target-commit-nonhex", "target_commit", "z" * 40),
)


@pytest.mark.parametrize("case_name,field,value", PROOF_INVALID_CASES, ids=[case[0] for case in PROOF_INVALID_CASES])
def test_submission_proof_requires_exact_audience_and_bounded_string_targets(base, case_name, field, value):
    del case_name
    proof = signed_claim_proof(base, field, value)
    state, claims = update_apply.verify_update_apply_submission_proof(proof, actor_id=1)
    assert state == "invalid"
    assert claims is None


@pytest.mark.parametrize(
    "field,value,expected_version,expected_commit",
    (
        (None, None, None, None),
        ("target_version", MAX_VERSION, MAX_VERSION, None),
        ("target_commit", "A" * 40, None, "a" * 40),
    ),
    ids=("ordinary", "version-max-80", "uppercase-commit"),
)
def test_submission_proof_exact_string_positive_controls(base, field, value, expected_version, expected_commit):
    proof = signed_claim_proof(base, field, value)
    state, claims = update_apply.verify_update_apply_submission_proof(proof, actor_id=1)
    assert state == "valid_unexpired"
    assert claims is not None
    assert claims["target_version"] == (expected_version or base.TARGET_VERSION)
    assert claims["target_commit"] == (expected_commit or base.TARGET_COMMIT)


def test_target_version_normalizer_never_coerces_trims_or_truncates():
    assert update_apply._normalized_target_version(MAX_VERSION) == MAX_VERSION
    for value in (123, 9.9, True, " 9.9.9", "9.9.9 ", "v" * 81, "v" * 200, SYNTHETIC_SENSITIVE_VERSION):
        with pytest.raises(UpdateApplyBlocked):
            update_apply._normalized_target_version(value)


AUTHORITY_MUTATION_CASES = (
    "confirmed-at-integer-unclaimed",
    "confirmed-at-integer-terminal",
    "confirmed-at-leading-space",
    "request-time-leading-space",
    "document-updated-trailing-space",
    "entry-state-object",
    "entry-state-array",
    "candidate-source-object",
    "candidate-source-array",
    "audit-state-object",
    "audit-state-array",
    "terminal-step-name-object",
    "terminal-step-status-array",
    "terminal-status-object",
    "terminal-status-array",
    "terminal-finished-at-leading-space",
    "terminal-error-blank-message",
    "terminal-error-blank-action",
)


def malformed_authority(base, case_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    request = base.request_payload()
    if case_name.startswith("terminal-") or case_name == "confirmed-at-integer-terminal":
        terminal = base.terminal_payload(request, "failed")
        payload = base.admission_document(base.entry_payload("terminal", request=request, terminal=terminal))
    else:
        payload = base.admission_document(base.entry_payload("admitted_unclaimed", request=request))

    entry = payload["entries"][0]
    if case_name in {"confirmed-at-integer-unclaimed", "confirmed-at-integer-terminal"}:
        entry["audit"]["confirmed_at"] = 20260721
    elif case_name == "confirmed-at-leading-space":
        entry["audit"]["confirmed_at"] = " " + entry["audit"]["confirmed_at"]
    elif case_name == "request-time-leading-space":
        value = " " + entry["requested_at"]
        entry["requested_at"] = value
        entry["request"]["requested_at"] = value
    elif case_name == "document-updated-trailing-space":
        payload["updated_at"] += " "
    elif case_name == "entry-state-object":
        entry["state"] = {}
    elif case_name == "entry-state-array":
        entry["state"] = []
    elif case_name == "candidate-source-object":
        entry["request"]["apply_candidate"]["source"] = {}
    elif case_name == "candidate-source-array":
        entry["request"]["apply_candidate"]["source"] = []
    elif case_name == "audit-state-object":
        entry["audit"]["state"] = {}
    elif case_name == "audit-state-array":
        entry["audit"]["state"] = []
    elif case_name == "terminal-step-name-object":
        entry["terminal"]["steps"][0]["name"] = {}
    elif case_name == "terminal-step-status-array":
        entry["terminal"]["steps"][0]["status"] = []
    elif case_name == "terminal-status-object":
        entry["terminal"]["status"] = {}
    elif case_name == "terminal-status-array":
        entry["terminal"]["status"] = []
    elif case_name == "terminal-finished-at-leading-space":
        entry["terminal"]["finished_at"] = " " + entry["terminal"]["finished_at"]
    elif case_name == "terminal-error-blank-message":
        entry["terminal"]["error"]["message"] = " "
    elif case_name == "terminal-error-blank-action":
        entry["terminal"]["error"]["operator_action"] = " "
    else:
        raise AssertionError(case_name)
    return payload, request


@pytest.mark.parametrize("case_name", AUTHORITY_MUTATION_CASES)
def test_malformed_authority_blocks_every_real_mutation_boundary(base, tmp_path, monkeypatch, case_name):
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    payload, request = malformed_authority(base, case_name)
    (control / "update-request.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    (control / "update-admission-lineage.json").write_text(
        json.dumps(update_apply.LINEAGE_MARKER_PAYLOAD, separators=(",", ":")), encoding="utf-8"
    )
    (control / "update-status.json").write_text('{"schema_version":1,"status":"idle"}', encoding="utf-8")
    for name in ("update-progress.json", "update-apply-history.json", "update-helper-history.json"):
        (control / name).write_text('{"sentinel":"unchanged"}', encoding="utf-8")

    engine = create_engine(f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    actor = SimpleNamespace(id=1, username="owner", role="owner")
    proof, _claims = update_apply._issue_submission_proof(
        submission_id=base.SECOND_SUBMISSION_ID,
        target_version=base.TARGET_VERSION,
        target_commit=base.TARGET_COMMIT,
        actor_id=1,
        issued_at=update_apply._utcnow(),
    )
    helper = base.load_helper()
    base.configure_helper(helper, control)
    run_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(helper, "run_update", lambda candidate: run_calls.append(candidate))
    before = base.surface_bytes(control)
    before_audits = db.query(AuditEvent).count()
    try:
        assert update_apply.read_update_apply_status()["admission"]["authority"] == "unknown"
        with pytest.raises(UpdateApplyBlocked) as ticket_error:
            update_apply.issue_update_apply_submission_ticket(
                db,
                expected_manifest_version=base.TARGET_VERSION,
                expected_manifest_commit=base.TARGET_COMMIT,
                actor=actor,
            )
        assert ticket_error.value.code == "update_admission_unknown"
        with pytest.raises(UpdateApplyBlocked) as apply_error:
            update_apply.request_update_apply(
                db,
                confirm=True,
                submission_id=base.SECOND_SUBMISSION_ID,
                submission_proof=proof,
                expected_manifest_version=base.TARGET_VERSION,
                expected_manifest_commit=base.TARGET_COMMIT,
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
            helper.publish_terminal(request, base.terminal_payload(request, "failed"))

        assert base.surface_bytes(control) == before
        assert db.query(AuditEvent).count() == before_audits == 0
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("case_name,field,value", PROOF_INVALID_CASES, ids=[case[0] for case in PROOF_INVALID_CASES])
def test_malformed_proof_claims_cannot_mutate_admission_or_audit(base, tmp_path, monkeypatch, case_name, field, value):
    del case_name
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    actor = SimpleNamespace(id=1, username="owner", role="owner")
    proof = signed_claim_proof(base, field, value)
    before = base.surface_bytes(control)
    try:
        with pytest.raises(UpdateApplyBlocked) as error:
            update_apply.request_update_apply(
                db,
                confirm=True,
                submission_id=base.SECOND_SUBMISSION_ID,
                submission_proof=proof,
                expected_manifest_version=base.TARGET_VERSION,
                expected_manifest_commit=base.TARGET_COMMIT,
                actor=actor,
            )
        assert error.value.code == "submission_proof_invalid"
        assert base.surface_bytes(control) == before
        assert db.query(AuditEvent).count() == 0
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    "case_name,value",
    (
        ("missing", MISSING),
        ("null", None),
        ("boolean", True),
        ("integer", 1),
        ("float", 1.0),
        ("object", {}),
        ("array", []),
        ("empty", ""),
        ("foreign", "update-" + "f" * 32),
    ),
    ids=("missing", "null", "boolean", "integer", "float", "object", "array", "empty", "foreign"),
)
def test_helper_read_progress_requires_exact_current_request_id(base, tmp_path, case_name, value):
    del case_name
    helper = base.load_helper()
    base.configure_helper(helper, tmp_path)
    progress = {"phase": "preflight"}
    if value is not MISSING:
        progress["request_id"] = copy.deepcopy(value)
    helper.PROGRESS_FILE.write_text(
        json.dumps(progress, separators=(",", ":")), encoding="utf-8"
    )
    before = base.surface_bytes(tmp_path)
    assert helper.read_progress(base.REQUEST_ID) is None
    assert base.surface_bytes(tmp_path) == before


def test_helper_read_progress_exact_request_id_positive_control(base, tmp_path):
    helper = base.load_helper()
    base.configure_helper(helper, tmp_path)
    progress = {"request_id": base.REQUEST_ID, "phase": "preflight"}
    helper.PROGRESS_FILE.write_text(json.dumps(progress, separators=(",", ":")), encoding="utf-8")
    assert helper.read_progress(base.REQUEST_ID) == progress


@pytest.mark.parametrize("value", (MISSING, None, "update-" + "f" * 32), ids=("missing", "null", "foreign"))
def test_unbound_progress_cannot_change_timeout_classification_or_history(base, tmp_path, monkeypatch, value):
    helper = base.load_helper()
    base.configure_helper(helper, tmp_path)
    monkeypatch.setattr(helper, "POLL_SECONDS", 0.01)
    request = base.request_payload()
    progress = {
        "schema_version": 1,
        "status": "running",
        "phase": "rebuild_recreate",
        "current_step": "rebuilding",
        "updated_at": base.CLAIMED_AT,
    }
    if value is not MISSING:
        progress["request_id"] = value
    helper.PROGRESS_FILE.write_text(json.dumps(progress, separators=(",", ":")), encoding="utf-8")
    progress_before = helper.PROGRESS_FILE.read_bytes()

    with pytest.raises(helper.HelperError) as error:
        helper.run_child_with_progress(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            request,
            tmp_path,
            os.environ.copy(),
            timeout_seconds=0.05,
            default_step="acquire_source",
            status_value="applying",
        )

    assert error.value.category == "apply_timeout"
    assert error.value.phase == "acquire_source"
    assert helper.PROGRESS_FILE.read_bytes() == progress_before
    assert not helper.HISTORY_FILE.exists()
    assert not helper.APPLY_HISTORY_FILE.exists()


@pytest.mark.parametrize("container", ({}, []), ids=("object", "array"))
def test_helper_compatibility_predicate_rejects_container_status_without_typeerror(base, tmp_path, container):
    helper = base.load_helper()
    base.configure_helper(helper, tmp_path)
    helper.STATUS_FILE.write_text(
        json.dumps({"request_id": base.REQUEST_ID, "status": container}, separators=(",", ":")), encoding="utf-8"
    )
    processed: set[str] = set()
    before = base.surface_bytes(tmp_path)
    assert helper.should_process({"request_id": base.REQUEST_ID}, processed) is True
    assert processed == set()
    assert base.surface_bytes(tmp_path) == before


def test_exact_current_terminal_and_helper_enum_positive_controls(base):
    helper = base.load_helper()
    unclaimed = base.admission_document(base.entry_payload("admitted_unclaimed"))
    request = base.request_payload()
    terminal = base.admission_document(
        base.entry_payload("terminal", request=request, terminal=base.terminal_payload(request, "completed"))
    )
    assert base.api_accepts(unclaimed)
    assert base.helper_accepts(helper, unclaimed)
    assert base.api_accepts(terminal)
    assert base.helper_accepts(helper, terminal)
    assert update_apply._is_allowed_string("confirmed", {"pending", "confirmed"})
    assert helper.is_allowed_string("completed", helper.TERMINAL)
