import copy
import importlib.util
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main_module
from app.core.config import settings
from app.core.permissions import ROLE_OWNER
from app.core.security import create_access_token, hash_password
from app.db.session import Base, get_db
from app.main import app
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.services import update_apply


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
    spec = importlib.util.spec_from_file_location(f"stage660123_helper_{uuid.uuid4().hex}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate() -> dict:
    return {
        "source": "trusted_snapshot",
        "snapshot": {
            "available": True,
            "fresh": True,
            "age_seconds": 1,
            "fresh_for_seconds": 900,
            "version": TARGET_VERSION,
            "commit_short": TARGET_COMMIT[:12],
            "provider": "github_release",
        },
    }


def request_payload(*, submission_id: str = SUBMISSION_ID, request_id: str = REQUEST_ID) -> dict:
    return {
        "schema_version": 2,
        "request_id": request_id,
        "submission_id": submission_id,
        "requested_at": REQUESTED_AT,
        "requested_by": {
            "user_id": 1,
            "username": "owner",
            "role": "owner",
            "ip_address": None,
            "user_agent": "stage-660123",
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
        "apply_candidate": candidate(),
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


def terminal_payload(
    request: dict,
    *,
    status: str,
    category: str | None = None,
    phase: str | None = None,
    steps: list[dict] | None = None,
) -> dict:
    phase = phase or ("completed" if status == "completed" else "cancelled" if status == "cancelled" else category)
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
        "steps": steps or [
            {
                "name": "commit_verification" if status == "completed" else "request" if status == "cancelled" else "preflight",
                "status": "completed" if status in {"completed", "cancelled"} else "failed",
            }
        ],
        "can_cancel": False,
        "rollback_supported": False,
        "side_effects": side_effects(),
        "error": None
        if status == "completed"
        else {
            "category": category or "cancelled_before_start",
            "message": "Update operation did not complete.",
            "operator_action": "Review update status before retrying.",
        },
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


def entry_payload(
    state: str,
    *,
    terminal: dict | None = None,
    submission_id: str = SUBMISSION_ID,
    request_id: str = REQUEST_ID,
) -> dict:
    request = request_payload(submission_id=submission_id, request_id=request_id)
    if state == "audit_pending":
        audit_state, confirmed_at, claimed_at, updated_at = "pending", None, None, REQUESTED_AT
    elif state == "admitted_unclaimed":
        audit_state, confirmed_at, claimed_at, updated_at = "confirmed", CONFIRMED_AT, None, CONFIRMED_AT
    elif state == "claimed":
        audit_state, confirmed_at, claimed_at, updated_at = "confirmed", CONFIRMED_AT, CLAIMED_AT, CLAIMED_AT
    elif state == "terminal":
        terminal = terminal or terminal_payload(request, status="cancelled")
        claimed_at = None if terminal.get("status") == "cancelled" else CLAIMED_AT
        audit_state, confirmed_at, updated_at = "confirmed", CONFIRMED_AT, terminal["finished_at"]
    else:
        audit_state, confirmed_at, claimed_at, updated_at = "confirmed", CONFIRMED_AT, None, CONFIRMED_AT
    return {
        "submission_id": submission_id,
        "request_id": request_id,
        "target_version": TARGET_VERSION,
        "target_commit": TARGET_COMMIT,
        "requested_at": REQUESTED_AT,
        "updated_at": updated_at,
        "state": state,
        "request": request,
        "audit": {
            "state": audit_state,
            "event_id": update_apply._audit_event_id(request_id),
            "confirmed_at": confirmed_at,
        },
        "claimed_at": claimed_at,
        "terminal": terminal,
    }


def admission_document(entry: dict, *, current: str | None = "entry") -> dict:
    current_id = entry["submission_id"] if current == "entry" else current
    return {
        "schema_version": 2,
        "document_type": update_apply.ADMISSION_DOCUMENT_TYPE,
        "current_submission_id": current_id,
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


@pytest.fixture
def route_env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage660123.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = sessions()
    owner = User(
        username="stage660123_owner",
        full_name="Stage 660123 Owner",
        password_hash=hash_password("stage660123-password"),
        role=ROLE_OWNER,
        is_active=True,
    )
    db.add(owner)
    db.commit()
    db.refresh(owner)
    control = tmp_path / "control"
    monkeypatch.setattr(settings, "update_control_root", str(control))
    monkeypatch.setattr(settings, "kmvms_update_helper_enabled", True)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), owner, db, control
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.username)}"}


def apply_body(**overrides) -> dict:
    body = {
        "confirm": True,
        "submission_id": SUBMISSION_ID,
        "submission_proof": "synthetic-proof",
        "expected_manifest_version": TARGET_VERSION,
        "expected_manifest_commit": TARGET_COMMIT,
    }
    body.update(overrides)
    return body


def assert_no_route_mutation(control: Path, db) -> None:
    assert not control.exists() or not any(control.iterdir())
    assert db.query(AuditEvent).filter(AuditEvent.event_type == update_apply.AUDIT_EVENT_TYPE).count() == 0


def test_route_422_omits_rejected_values_field_names_and_logs(route_env, caplog):
    client, owner, db, control = route_env
    caplog.set_level(logging.DEBUG)
    headers = auth_headers(owner)
    value_sentinel = "proof_" + uuid.uuid4().hex
    field_sentinel = "field_" + uuid.uuid4().hex
    nested_sentinel = "nested_" + uuid.uuid4().hex

    responses = [
        client.post("/system/update/apply", json=apply_body(submission_proof=value_sentinel * 100), headers=headers),
        client.post("/system/update/apply", json={**apply_body(), field_sentinel: "x"}, headers=headers),
        client.post("/system/update/apply", json=apply_body(submission_proof={nested_sentinel: value_sentinel}), headers=headers),
        client.post(
            "/system/update/apply",
            content='{"confirm":true,"submission_proof":"' + value_sentinel,
            headers={**headers, "content-type": "application/json"},
        ),
        client.post(
            "/system/update/apply",
            json={key: value for key, value in apply_body().items() if key != "submission_proof"},
            headers=headers,
        ),
    ]

    for response in responses:
        rendered = response.content.decode("utf-8", errors="replace")
        assert response.status_code == 422
        assert set(response.json()) == {"detail"}
        assert all(set(item) == {"loc", "type", "msg"} for item in response.json()["detail"])
        assert len(response.content) <= main_module.MAX_VALIDATION_RESPONSE_BYTES
        for sentinel in (value_sentinel, field_sentinel, nested_sentinel):
            assert sentinel not in rendered
            assert sentinel not in caplog.text
        assert all(forbidden not in rendered for forbidden in ('"input"', '"ctx"', '"url"'))
    assert any("<extra>" in response.text for response in responses)
    assert_no_route_mutation(control, db)


def test_unrelated_route_uses_same_bounded_validation_contract(route_env):
    client, _owner, db, control = route_env
    sentinel = "login_" + uuid.uuid4().hex
    response = client.post("/auth/login", json={"username": sentinel, "password": {"value": sentinel}})
    assert response.status_code == 422
    assert set(response.json()) == {"detail"}
    assert sentinel not in response.text
    assert_no_route_mutation(control, db)


def test_validation_response_bounds_and_neutral_locations(monkeypatch):
    errors = [
        {"loc": ("body", "known", index), "type": "extra_forbidden", "msg": "raw", "input": "raw"}
        for index in range(33)
    ]
    content = main_module._safe_validation_response_content(errors)
    assert len(content["detail"]) == 32
    assert all(item["loc"][-1] == "<extra>" for item in content["detail"])
    assert len(json.dumps(content, separators=(",", ":")).encode()) <= 16 * 1024

    depth = main_module._safe_validation_loc(("body", "a", "b", "c", "d", "e", "f", "g"), error_type="missing")
    assert len(depth) == 8
    assert len(main_module._safe_validation_loc(("body", "a", "b", "c", "d", "e", "f", "g", "h"), error_type="missing")) == 8
    deep_extra = main_module._safe_validation_loc(
        ("body", "a", "b", "c", "d", "e", "f", "g", "attacker-controlled-extra"),
        error_type="extra_forbidden",
    )
    assert len(deep_extra) == 8 and deep_extra[-1] == "<extra>"
    assert main_module._safe_validation_loc(("body", 1_000_000), error_type="missing")[-1] == 1_000_000
    assert main_module._safe_validation_loc(("body", 1_000_001), error_type="missing")[-1] == "<index>"
    assert main_module._safe_validation_loc(("body", -1), error_type="missing")[-1] == "<index>"
    assert main_module._safe_validation_loc((object(), object()), error_type="missing") == ["<source>", "<field>"]

    type_at_bound = "a" + "b" * 79
    assert main_module._normalized_validation_type(type_at_bound) == type_at_bound
    assert main_module._normalized_validation_type(type_at_bound + "c") == "validation_error"
    monkeypatch.setitem(main_module.VALIDATION_MESSAGES, "missing", "m" * 301)
    detail = main_module._safe_validation_detail({"loc": ("body", "x"), "type": "missing"})
    assert len(detail["msg"]) == 300


@pytest.mark.parametrize("state", ["audit_pending", "admitted_unclaimed", "claimed"])
def test_valid_nonterminal_state_parity(state):
    helper = load_helper()
    payload = admission_document(entry_payload(state))
    assert api_accepts(payload)
    assert helper_accepts(helper, payload)


@pytest.mark.parametrize(
    ("status", "category", "phase"),
    [
        ("cancelled", "cancelled_before_start", "cancelled"),
        ("completed", None, "completed"),
        ("failed", "helper_restart_interrupted", "helper_restart_interrupted"),
        ("failed", "helper_host_app_dir_missing", "helper_host_app_dir_missing"),
        ("failed", "helper_host_app_dir_invalid", "helper_host_app_dir_invalid"),
        ("failed", "helper_host_app_dir_unmounted", "helper_host_app_dir_unmounted"),
        ("failed", "preflight_failed", "preflight_failed"),
        ("failed", "compose_config_failed", "compose_config_failed"),
        ("failed", "jellyfin_ffmpeg_repo_unavailable", "jellyfin_ffmpeg_repo_unavailable"),
        ("failed", "build_network_dependency_failed", "build_network_dependency_failed"),
        ("failed", "docker_build_failed", "docker_build_failed"),
        ("failed", "health_check_failed", "health_check_failed"),
        ("failed", "commit_mismatch", "commit_verification"),
        ("failed", "commit_missing", "commit_verification"),
        ("failed", "metadata_invalid", "commit_verification"),
        ("failed", "apply_timeout", "rebuilding"),
        ("failed", "apply_failed", "apply_failed"),
        ("failed", "helper_exception", "helper_exception"),
    ],
)
def test_valid_terminal_category_phase_parity(status, category, phase):
    helper = load_helper()
    request = request_payload()
    terminal = terminal_payload(request, status=status, category=category, phase=phase)
    payload = admission_document(entry_payload("terminal", terminal=terminal))
    assert api_accepts(payload)
    assert helper_accepts(helper, payload)


def test_valid_retained_history_topologies_and_legacy_parity():
    helper = load_helper()
    first = entry_payload("terminal")
    second_submission = "22222222-2222-4222-8222-222222222222"
    second_request = "update-" + "b" * 32
    second = entry_payload("terminal", submission_id=second_submission, request_id=second_request)
    payload = {
        "schema_version": 2,
        "document_type": update_apply.ADMISSION_DOCUMENT_TYPE,
        "current_submission_id": None,
        "entries": [first, second],
        "updated_at": FINISHED_AT,
    }
    assert api_accepts(payload) and helper_accepts(helper, payload)
    payload["current_submission_id"] = second_submission
    assert api_accepts(payload) and helper_accepts(helper, payload)

    legacy = {
        "schema_version": 1,
        "request_id": "legacy-request",
        "requested_at": REQUESTED_AT,
        "intent": "apply_update",
        "confirmed": True,
        "source": {"version": TARGET_VERSION, "commit": TARGET_COMMIT},
    }
    assert api_accepts(legacy) and helper_accepts(helper, legacy)

    historical = {
        **legacy,
        "requested_by": {"user_id": "1", "role": "owner"},
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
        "source": request_payload()["source"],
    }
    snapshot_profile = {**historical, "apply_candidate": candidate()}
    transitional = {**snapshot_profile, "submission_id": SUBMISSION_ID}
    for accepted in (historical, snapshot_profile, transitional):
        assert api_accepts(accepted) and helper_accepts(helper, accepted)

    historical_terminal = {
        "schema_version": 1,
        "request_id": historical["request_id"],
        "status": "completed",
        "phase": "completed",
        "current_step": "completed",
        "started_at": historical["requested_at"],
        "updated_at": FINISHED_AT,
        "source": {
            "kind": "github-tarball",
            "repo": historical["source"]["repo"],
            "ref": historical["source"]["ref"],
            "commit": historical["source"]["commit"],
            "apply_ref": historical["source"]["apply_ref"],
        },
        "expected_commit": historical["source"]["commit"],
        "installed_commit": historical["source"]["commit"],
        "commit_verified": True,
        "steps": [
            {"name": name, "status": "completed"}
            for name in update_apply.LEGACY_HISTORICAL_COMPLETED_STEP_NAMES
        ],
        "can_cancel": False,
        "rollback_supported": False,
        "side_effects": side_effects(),
        "error": None,
    }
    legacy_entry = update_apply._legacy_request_contract(historical, "valid")[1]
    assert legacy_entry is not None
    assert update_apply._strict_terminal_snapshot(historical_terminal, legacy_entry)
    assert helper.terminal_status_for_request(historical_terminal, historical)

    for mutation in (
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value["steps"][2].update(name="overlay"),
        lambda value: value["side_effects"].update(helper_public_ports=True),
        lambda value: value.update(commit_verified=False),
    ):
        malformed_terminal = copy.deepcopy(historical_terminal)
        mutation(malformed_terminal)
        assert update_apply._strict_terminal_snapshot(malformed_terminal, legacy_entry) is None
        assert not helper.terminal_status_for_request(malformed_terminal, historical)

    malformed_legacy = copy.deepcopy(historical)
    malformed_legacy["requested_by"]["unexpected"] = True
    assert not api_accepts(malformed_legacy) and not helper_accepts(helper, malformed_legacy)


def invalid_mutations() -> list[tuple[str, Callable[[dict], None]]]:
    return [
        ("audit_pending_claim", lambda d: d["entries"][0].update(claimed_at=CLAIMED_AT)),
        ("admitted_claim", lambda d: d["entries"][0].update(claimed_at=CLAIMED_AT)),
        ("claimed_without_claim", lambda d: d["entries"][0].update(claimed_at=None)),
        ("claimed_with_terminal", lambda d: d["entries"][0].update(terminal=terminal_payload(d["entries"][0]["request"], status="failed", category="health_check_failed", phase="health_check"))),
        ("unsupported_state", lambda d: d["entries"][0].update(state="unknown")),
        ("request_time_mismatch", lambda d: d["entries"][0].update(requested_at="2026-07-21T00:00:01Z")),
        ("entry_commit_mismatch", lambda d: d["entries"][0].update(target_commit="d" * 40)),
    ]


@pytest.mark.parametrize(("case_name", "mutation"), invalid_mutations())
def test_invalid_state_claim_identity_parity_and_immutability(case_name, mutation):
    helper = load_helper()
    base_state = "audit_pending" if case_name == "audit_pending_claim" else "admitted_unclaimed" if case_name == "admitted_claim" else "claimed"
    payload = admission_document(entry_payload(base_state))
    mutation(payload)
    before = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert not api_accepts(payload)
    assert not helper_accepts(helper, payload)
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == before


def recursive_extra_mutations() -> list[tuple[str, Callable[[dict, str], None], str]]:
    return [
        ("document", lambda d, k: d.__setitem__(k, "x"), "benign_extra"),
        ("entry", lambda d, k: d["entries"][0].__setitem__(k, "x"), "benign_extra"),
        ("request", lambda d, k: d["entries"][0]["request"].__setitem__(k, "x"), "benign_extra"),
        ("actor", lambda d, k: d["entries"][0]["request"]["requested_by"].__setitem__(k, "x"), "benign_extra"),
        ("source", lambda d, k: d["entries"][0]["request"]["source"].__setitem__(k, "x"), "benign_extra"),
        ("candidate", lambda d, k: d["entries"][0]["request"]["apply_candidate"].__setitem__(k, "x"), "benign_extra"),
        ("snapshot", lambda d, k: d["entries"][0]["request"]["apply_candidate"]["snapshot"].__setitem__(k, "x"), "benign_extra"),
        ("audit", lambda d, k: d["entries"][0]["audit"].__setitem__(k, "x"), "benign_extra"),
        ("terminal", lambda d, k: d["entries"][0]["terminal"].__setitem__(k, "x"), "benign_extra"),
        ("terminal_source", lambda d, k: d["entries"][0]["terminal"]["source"].__setitem__(k, "x"), "benign_extra"),
        ("terminal_step", lambda d, k: d["entries"][0]["terminal"]["steps"][0].__setitem__(k, "x"), "benign_extra"),
        ("terminal_error", lambda d, k: d["entries"][0]["terminal"]["error"].__setitem__(k, "x"), "benign_extra"),
        ("release_identity", lambda d, k: d["entries"][0]["terminal"]["release_identity"].__setitem__(k, "x"), "benign_extra"),
        ("side_effects", lambda d, k: d["entries"][0]["terminal"]["side_effects"].__setitem__(k, "x"), "benign_extra"),
    ]


@pytest.mark.parametrize(("layer", "mutation", "key"), recursive_extra_mutations())
@pytest.mark.parametrize("forbidden", [False, True])
def test_recursive_exact_key_parity(layer, mutation, key, forbidden):
    helper = load_helper()
    request = request_payload()
    terminal = terminal_payload(
        request,
        status="failed" if layer == "terminal_error" else "completed",
        category="health_check_failed" if layer == "terminal_error" else None,
        phase="health_check" if layer == "terminal_error" else None,
    )
    payload = admission_document(entry_payload("terminal", terminal=terminal))
    mutation(payload, "submission_proof" if forbidden else key)
    before = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert not api_accepts(payload)
    assert not helper_accepts(helper, payload)
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == before


@pytest.mark.parametrize(
    ("category", "phase"),
    [
        ("status_invalid_json", "preflight_failed"),
        ("status_too_large", "apply_failed"),
        ("status_invalid_shape", "health_check_failed"),
        ("status_sensitive_content", "commit_verification"),
        ("status_future_case", "preflight_failed"),
        ("status_read", "preflight_failed"),
        ("status_redaction", "preflight_failed"),
        ("ordinary_safe_failure", "preflight_failed"),
        ("health_check_failed", "preflight_failed"),
    ],
)
def test_synthetic_or_mismatched_terminal_never_becomes_authority(category, phase):
    helper = load_helper()
    request = request_payload()
    terminal = terminal_payload(request, status="failed", category=category, phase=phase)
    payload = admission_document(entry_payload("terminal", terminal=terminal))
    before = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert not api_accepts(payload)
    assert not helper_accepts(helper, payload)
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == before


def test_terminal_claim_commit_error_and_nested_shape_contradictions():
    helper = load_helper()
    request = request_payload()
    cases = []

    failed_without_claim = admission_document(entry_payload("terminal", terminal=terminal_payload(request, status="failed", category="health_check_failed", phase="health_check")))
    failed_without_claim["entries"][0]["claimed_at"] = None
    cases.append(failed_without_claim)

    completed_error = admission_document(entry_payload("terminal", terminal=terminal_payload(request, status="completed")))
    completed_error["entries"][0]["terminal"]["error"] = {"category": "helper_exception", "message": "Failure.", "operator_action": "Review status."}
    cases.append(completed_error)

    completed_commit = admission_document(entry_payload("terminal", terminal=terminal_payload(request, status="completed")))
    completed_commit["entries"][0]["terminal"]["installed_commit"] = "d" * 40
    cases.append(completed_commit)

    cancelled_claim = admission_document(entry_payload("terminal"))
    cancelled_claim["entries"][0]["claimed_at"] = CLAIMED_AT
    cases.append(cancelled_claim)

    for payload in cases:
        assert not api_accepts(payload)
        assert not helper_accepts(helper, payload)


@pytest.mark.parametrize(
    "injection",
    [
        lambda raw: raw.replace('"schema_version":2', '"schema_version":2,"schema_version":2', 1),
        lambda raw: raw.replace('"state":"admitted_unclaimed"', '"state":"admitted_unclaimed","state":"admitted_unclaimed"', 1),
        lambda raw: raw.replace('"kind":"trusted_manifest"', '"kind":"trusted_manifest","kind":"trusted_manifest"', 1),
        lambda raw: raw.replace('"state":"confirmed"', '"state":"confirmed","state":"pending"', 1),
    ],
)
def test_duplicate_keys_rejected_before_normalization_with_api_helper_parity(injection):
    helper = load_helper()
    raw = json.dumps(admission_document(entry_payload("admitted_unclaimed")), separators=(",", ":"))
    duplicate = injection(raw)
    with pytest.raises(Exception):
        update_apply._decode_authority_json(duplicate)
    with pytest.raises(helper.HelperError):
        helper.decode_authority_json(duplicate)


@pytest.mark.parametrize(
    ("status", "needle", "replacement"),
    [
        ("failed", '"current_submission_id":"11111111-1111-4111-8111-111111111111"', '"current_submission_id":"11111111-1111-4111-8111-111111111111","current_submission_id":"11111111-1111-4111-8111-111111111111"'),
        ("failed", '"entries":[', '"entries":[],"entries":['),
        ("failed", '"category":"health_check_failed"', '"category":"health_check_failed","category":"health_check_failed"'),
        ("failed", '"name":"preflight"', '"name":"preflight","name":"preflight"'),
        ("failed", '"helper_public_ports":false', '"helper_public_ports":false,"helper_public_ports":false'),
        ("completed", '"host_metadata_status":"complete"', '"host_metadata_status":"complete","host_metadata_status":"complete"'),
    ],
)
def test_duplicate_document_terminal_nested_families_are_rejected(status, needle, replacement):
    helper = load_helper()
    request = request_payload()
    terminal = terminal_payload(
        request,
        status=status,
        category="health_check_failed" if status == "failed" else None,
        phase="health_check_failed" if status == "failed" else None,
    )
    raw = json.dumps(admission_document(entry_payload("terminal", terminal=terminal)), separators=(",", ":"))
    assert needle in raw
    duplicate = raw.replace(needle, replacement, 1)
    with pytest.raises(Exception):
        update_apply._decode_authority_json(duplicate)
    with pytest.raises(helper.HelperError):
        helper.decode_authority_json(duplicate)


def test_malformed_and_nesting_boundaries_have_parser_parity():
    helper = load_helper()
    for raw in ("{", '{"x":1} trailing', '{"x":NaN}', '{"x":Infinity}'):
        with pytest.raises(Exception):
            update_apply._decode_authority_json(raw)
        with pytest.raises(helper.HelperError):
            helper.decode_authority_json(raw)

    at_bound = '{"x":' + "[" * 31 + "0" + "]" * 31 + "}"
    over_bound = '{"x":' + "[" * 32 + "0" + "]" * 32 + "}"
    assert update_apply._decode_authority_json(at_bound)
    assert helper.decode_authority_json(at_bound)
    with pytest.raises(Exception):
        update_apply._decode_authority_json(over_bound)
    with pytest.raises(helper.HelperError):
        helper.decode_authority_json(over_bound)


@pytest.mark.parametrize(("kind", "limit"), [("admission", update_apply.MAX_ADMISSION_BYTES), ("status", update_apply.MAX_CONTROL_BYTES)])
def test_preparse_byte_limit_at_boundary_and_one_over(tmp_path, monkeypatch, kind, limit):
    helper = load_helper()
    control = tmp_path / kind
    control.mkdir()
    monkeypatch.setattr(settings, "update_control_root", str(control))
    helper.REQUEST_FILE = control / "update-request.json"
    helper.LINEAGE_FILE = control / "update-admission-lineage.json"
    helper.STATUS_FILE = control / "update-status.json"
    path = helper.REQUEST_FILE if kind == "admission" else helper.STATUS_FILE
    prefix, suffix = '{"pad":"', '"}'
    text = prefix + "x" * (limit - len(prefix.encode()) - len(suffix.encode())) + suffix
    path.write_text(text, encoding="utf-8")
    api_payload, api_state = update_apply._read_json(path)
    assert api_state == "valid" and api_payload is not None
    assert helper.read_json(path) is not None
    assert path.read_bytes() == text.encode()
    path.write_text(text + " ", encoding="utf-8")
    over_bytes = path.read_bytes()
    assert update_apply._read_json(path)[1] == "too_large"
    with pytest.raises(helper.HelperError) as exc:
        helper.read_json(path)
    assert exc.value.category == "control_file_too_large"
    assert path.read_bytes() == over_bytes


def test_scalar_entry_and_step_numerical_boundaries_have_parity():
    helper = load_helper()
    at_bound = admission_document(entry_payload("admitted_unclaimed"))
    at_bound["entries"][0]["request"]["requested_by"]["user_agent"] = "u" * 300
    assert api_accepts(at_bound) and helper_accepts(helper, at_bound)
    over = copy.deepcopy(at_bound)
    over["entries"][0]["request"]["requested_by"]["user_agent"] += "u"
    assert not api_accepts(over) and not helper_accepts(helper, over)

    request = request_payload()
    all_steps = [{"name": name, "status": "completed"} for name in ["request", *load_helper().STEP_ORDER]]
    terminal = terminal_payload(request, status="completed", steps=all_steps)
    payload = admission_document(entry_payload("terminal", terminal=terminal))
    assert len(all_steps) == 12
    assert api_accepts(payload) and helper_accepts(helper, payload)
    payload["entries"][0]["terminal"]["steps"].append({"name": "request", "status": "completed"})
    assert not api_accepts(payload) and not helper_accepts(helper, payload)


def test_max_admission_entries_and_one_over_have_parity():
    helper = load_helper()
    entries = []
    for index in range(update_apply.MAX_ADMISSION_ENTRIES):
        submission_id = f"00000000-0000-4000-8000-{index + 1:012x}"
        request_id = f"update-{index + 1:032x}"
        entries.append(entry_payload("terminal", submission_id=submission_id, request_id=request_id))
    payload = {
        "schema_version": 2,
        "document_type": update_apply.ADMISSION_DOCUMENT_TYPE,
        "current_submission_id": None,
        "entries": entries,
        "updated_at": FINISHED_AT,
    }
    assert api_accepts(payload) and helper_accepts(helper, payload)
    payload["entries"].append(entry_payload("terminal", submission_id="ffffffff-ffff-4fff-8fff-ffffffffffff", request_id="update-" + "f" * 32))
    assert not api_accepts(payload) and not helper_accepts(helper, payload)


def test_precloseout_cancel_and_compact_live_candidate_remain_exactly_compatible():
    helper = load_helper()
    request = request_payload()
    request["apply_candidate"] = {
        "source": "live_check",
        "snapshot": {"available": False, "fresh": False, "age_seconds": None, "fresh_for_seconds": 900},
    }
    terminal = terminal_payload(request, status="cancelled")
    terminal.pop("side_effects")
    terminal["source"] = request["source"]
    terminal["apply_candidate"] = request["apply_candidate"]
    terminal["installed_commit"] = None
    payload = admission_document(entry_payload("terminal", terminal=terminal))
    payload["entries"][0]["request"] = request
    assert api_accepts(payload) and helper_accepts(helper, payload)
    payload["entries"][0]["terminal"]["apply_candidate"]["snapshot"]["unexpected"] = True
    assert not api_accepts(payload) and not helper_accepts(helper, payload)
