"""The real updater lifecycle and the schema gate must agree, not bypass it."""
from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import schema_update_control as control
from app.services import schema_update_pipeline as pipeline
from test_stage661_activation_convergence import bridge


ROOT = Path(__file__).resolve().parents[3]
REQUEST = "update-" + "1" * 32
COMMIT = "2" * 40
VERSION = "0.8.25"


def status_payload(status: str) -> dict:
    return {"schema_version": 1, "request_id": REQUEST, "status": status,
            "target_version": VERSION, "expected_commit": COMMIT,
            "source": {"commit": COMMIT, "version": VERSION}}


def test_every_real_helper_status_is_recognized_by_schema_reader():
    tree = ast.parse((ROOT / "scripts/km-vms-update-helper.py").read_text())
    assignments = {node.targets[0].id: node.value for node in tree.body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    assert ast.literal_eval(assignments["RUNNING"]) <= control.ACTIVE_UPDATE_STATUSES
    assert ast.literal_eval(assignments["TERMINAL"]) <= control.TERMINAL_UPDATE_STATUSES


@pytest.mark.parametrize("status", ["preparing", "staging", "activating", "reconnecting", "rolling_back", "failed_rolled_back"])
def test_lifecycle_status_is_parsed_without_changing_its_meaning(tmp_path, monkeypatch, status):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(status_payload(status)))
    monkeypatch.setattr(control, "UPDATE_STATUS_PATH", path)
    assert control._read_update_status_for_execution()["status"] == status


@pytest.mark.parametrize("status", ["preparing", "staging", "applying"])
def test_pre_mutation_phase_still_binds_exact_request_release_and_commit(status):
    control._validate_matching_active_update_status(
        status_payload(status), request_id=REQUEST, request_profile="current",
        request_target_version=VERSION, target_release=VERSION, target_commit=COMMIT,
    )
    for field, wrong in [("request_id", "update-" + "3" * 32),
                         ("expected_commit", "3" * 40), ("target_version", "0.8.99"),
                         ("source", {"commit": "3" * 40})]:
        changed = status_payload(status) | {field: wrong}
        with pytest.raises(control.SchemaControlError, match="schema_execution_active_status_mismatch"):
            control._validate_matching_active_update_status(
                changed, request_id=REQUEST, request_profile="current",
                request_target_version=VERSION, target_release=VERSION, target_commit=COMMIT,
            )


@pytest.mark.parametrize("status", ["activating", "reconnecting", "rolling_back", "failed_rolled_back", "arbitrary_status"])
def test_post_mutation_and_unknown_stages_never_admit_ddl(status):
    with pytest.raises(control.SchemaControlError, match="schema_execution_active_status_mismatch"):
        control._validate_matching_active_update_status(
            status_payload(status), request_id=REQUEST, request_profile="current",
            request_target_version=VERSION, target_release=VERSION, target_commit=COMMIT,
        )


@pytest.mark.parametrize("status", ["preparing", "activating", "reconnecting"])
def test_completed_schema_can_start_target_during_activation_only_after_authority_validation(monkeypatch, status):
    payload = status_payload(status)
    context = SimpleNamespace(request_id=REQUEST, target_release=VERSION, target_commit=COMMIT)
    completed = SimpleNamespace(state="completed", request_id=REQUEST, target_release=VERSION, target_commit=COMMIT)
    monkeypatch.setattr(control, "forward_runtime_startup_noop", lambda _db: False)
    monkeypatch.setattr(control, "_read_update_status_for_execution", lambda: payload)
    monkeypatch.setattr(control, "schema_execution_authority_presence", lambda: {"request": True, "pre_overlay_identity": True})
    monkeypatch.setattr(control, "read_regular_json", lambda *_args, **_kwargs: {"request_id": REQUEST, "source": {"version": VERSION}})
    monkeypatch.setattr(control, "validate_update_request", lambda _value: "current")
    monkeypatch.setattr(control, "target_identity", lambda *_args, **_kwargs: (VERSION, COMMIT))
    monkeypatch.setattr(control, "_schema_control_row_for_execution", lambda _db: completed)
    checked = []
    monkeypatch.setattr(control, "_validate_completed_terminal_authority", lambda _db: (checked.append(True) or context))
    assert control.resolve_schema_execution_mode(object()) == "exact_target_noop"
    assert checked == [True]
    payload["expected_commit"] = "f" * 40
    with pytest.raises(control.SchemaControlError, match="schema_execution_active_status_mismatch"):
        control.resolve_schema_execution_mode(object())
    payload["expected_commit"] = COMMIT
    def missing_receipt(_db):
        raise control.SchemaControlError("schema_gate_receipt_missing")
    monkeypatch.setattr(control, "_validate_completed_terminal_authority", missing_receipt)
    with pytest.raises(control.SchemaControlError, match="schema_gate_receipt_missing"):
        control.resolve_schema_execution_mode(object())


@pytest.mark.parametrize("error,reason", [
    (control.SchemaControlError("schema_execution_active_status_mismatch"), "schema_execution_active_status_mismatch"),
    (RuntimeError("postgresql://sensitive-example must never be logged"), "schema_pipeline_unexpected"),
])
def test_cli_retains_only_stable_failure_code(monkeypatch, capsys, error, reason):
    def failed(_args):
        raise error
    monkeypatch.setattr(pipeline, "main", failed)
    assert pipeline.cli(["--migrate"]) == 1
    output = capsys.readouterr()
    assert output.err == f"schema_failure_reason={reason}\n"
    assert output.out == ""


def test_bridge_failure_reason_is_bounded_single_machine_code():
    def parse(value):
        return bridge.schema_pipeline_failure_reason(subprocess.CompletedProcess([], 1, "", value), "schema_update_failed")
    assert parse("schema_failure_reason=schema_execution_active_status_mismatch\n") == "schema_execution_active_status_mismatch"
    for unsafe in ["raw sensitive traceback", "schema_failure_reason=not a machine code\n",
                   "schema_failure_reason=" + "a" * 81,
                   "schema_failure_reason=first\nschema_failure_reason=second\n"]:
        assert parse(unsafe) == "schema_update_failed"
