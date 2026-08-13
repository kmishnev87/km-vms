from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "update-control-plane"
    / "control_plane.py"
)
SPEC = importlib.util.spec_from_file_location(
    "stage660128_control_plane",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)

REQUEST_ID = "update-" + ("1" * 32)
TARGET_COMMIT = "8" * 40
ATTEMPT_ID = "migration-attempt-" + ("2" * 32)
REGISTRY = "3" * 64
PLAN = "4" * 64
SHAPE = "5" * 64


def _utc(offset_hours: int = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    ).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _bind(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    public = tmp_path / "public"
    raw.mkdir()
    public.mkdir()
    control.CONTROL_ROOT = raw
    control.PUBLIC_ROOT = public
    control.AUTH_SNAPSHOT = raw / "schema-auth-snapshot.signed.json"
    control.BOOTSTRAP_RECEIPT = (
        raw / "schema-control-bootstrap.signed.json"
    )
    control.PREPARATION_RECEIPT = (
        raw / "schema-preparation-receipt.signed.json"
    )
    control.RECOVERY_RECEIPT = (
        raw / "operation-recovery-receipt.signed.json"
    )
    control.GATE_RECEIPT = raw / "schema-gate-receipt.signed.json"
    control.FAILURE_PLANE = (
        public / "update-failure-plane.signed.json"
    )
    control.HELPER_STATUS = raw / "update-status.json"
    control.UPDATE_REQUEST = raw / "update-request.json"
    control.RETRY_ADMISSION = (
        raw / "update-retry-admission.signed.json"
    )


def _write_signed(path: Path, payload: dict) -> None:
    _write_json(path, control.sign_payload(payload))


def _auth() -> dict:
    return {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "target_version": "0.7.25",
        "target_commit": TARGET_COMMIT,
        "actor_subject": "stage660128_owner",
        "actor_user_id": "1",
        "actor_role": "owner",
        "permission": "manage_settings",
        "issued_at": _utc(-1),
        "expires_at": _utc(2),
        "fencing_generation": 1,
    }


def _stage(state: str, *, retryable: bool = False) -> dict:
    details = {"bounded": True}
    if retryable:
        details["retry_evidence"] = {
            "schema_version": 1,
            "mutation_started": False,
            "physical_mutation_possible": False,
            "transaction_rolled_back": True,
            "rollback_verified": True,
            "schema_shape_unchanged": True,
            "history_unchanged": True,
            "canonical_transition_committed": False,
            "foreign_state_detected": False,
        }
    return {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "admission_attempt_id": ATTEMPT_ID,
        "target_version": "0.7.25",
        "target_commit": TARGET_COMMIT,
        "target_schema_version": 9,
        "registry_fingerprint": REGISTRY,
        "plan_fingerprint": PLAN,
        "fencing_generation": 1,
        "attempt_id": ATTEMPT_ID,
        "state": state,
        "phase": "preparing_database",
        "retryable": retryable,
        "error_code": (
            "test_injected_retryable_schema_failure"
            if retryable
            else ""
        ),
        "summary": "Bounded schema result.",
        "operator_action": "Wait or retry.",
        "details": details,
        "updated_at": _utc(),
    }


def _bootstrap() -> dict:
    return {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "admission_attempt_id": ATTEMPT_ID,
        "target_release": "0.7.25",
        "target_commit": TARGET_COMMIT,
        "target_schema_version": 9,
        "installed_version": "0.7.18",
        "installed_commit": "a" * 40,
        "source_schema_version": 1,
        "source_shape_fingerprint": SHAPE,
        "registry_fingerprint": REGISTRY,
        "plan_fingerprint": PLAN,
        "control_definition_fingerprint": "6" * 64,
        "control_shape_fingerprint": "7" * 64,
        "fencing_generation": 1,
        "state": "adopted",
        "updated_at": _utc(),
    }


def _helper(status: str) -> dict:
    return {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "status": status,
        "phase": "rebuilding",
        "current_step": "rebuilding",
    }


def _original_request() -> dict:
    return {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "requested_at": _utc(-1),
        "requested_by": {"user_id": "1", "role": "owner"},
        "intent": "apply_update",
        "confirmed": True,
        "source": {
            "kind": "github-release",
            "channel": "stable",
            "version": "0.7.25",
            "commit": TARGET_COMMIT,
            "apply_ref": TARGET_COMMIT,
            "ref": "v0.7.25",
            "repo": "kmishnev87/km-vms",
            "source_type": "release",
        },
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
    }


def _arrange_retryable_gate_failure(tmp_path: Path) -> None:
    _bind(tmp_path)
    control.JWT_SECRET = "stage660128-control-plane-test-secret"
    _write_signed(control.AUTH_SNAPSHOT, _auth())
    _write_signed(control.BOOTSTRAP_RECEIPT, _bootstrap())
    _write_signed(control.PREPARATION_RECEIPT, _stage("completed"))
    _write_signed(control.RECOVERY_RECEIPT, _stage("completed"))
    _write_signed(
        control.GATE_RECEIPT,
        _stage("failed", retryable=True),
    )


def _failed_contract(tmp_path: Path) -> dict:
    _arrange_retryable_gate_failure(tmp_path)
    _write_json(control.HELPER_STATUS, _helper("failed"))
    contract = control.controller_contract()
    assert contract is not None
    assert contract["target_schema_version"] == 9
    return contract


def test_stage_failure_remains_publicly_nonterminal_until_helper_stops(
    tmp_path: Path,
) -> None:
    _arrange_retryable_gate_failure(tmp_path)
    _write_json(control.HELPER_STATUS, _helper("rebuilding"))

    contract = control.controller_contract()

    assert contract is not None
    assert contract["state"] == "running"
    assert contract["retryable"] is False
    assert contract["helper_terminal"] is False
    apply_payload = control.legacy_apply_payload(contract)
    check_payload = control.legacy_check_payload(contract)
    assert apply_payload["status"] == "rebuilding"
    assert apply_payload["retryable"] is False
    assert check_payload["status"] == "update_available"
    assert check_payload["blockers"] == []
    assert check_payload["can_apply"] is False


def test_exact_retryable_failure_publishes_after_helper_stops(
    tmp_path: Path,
) -> None:
    _arrange_retryable_gate_failure(tmp_path)
    _write_json(control.HELPER_STATUS, _helper("failed"))

    contract = control.controller_contract()

    assert contract is not None
    assert contract["state"] == "failed"
    assert contract["retryable"] is True
    assert contract["helper_terminal"] is True
    apply_payload = control.legacy_apply_payload(contract)
    assert apply_payload["status"] == "failed"
    assert apply_payload["retryable"] is True


@pytest.mark.parametrize("schema_version", (8, 10))
def test_failure_and_stage_contracts_reject_non_current_schema(
    tmp_path: Path,
    schema_version: int,
) -> None:
    contract = _failed_contract(tmp_path)
    invalid_failure = {
        **contract,
        "target_schema_version": schema_version,
    }
    with pytest.raises(
        control.ContractError,
        match="failure_contract_schema_target_invalid",
    ):
        control.validate_failure_contract(invalid_failure)

    invalid_stage = {
        **_stage("completed"),
        "target_schema_version": schema_version,
    }
    with pytest.raises(
        control.ContractError,
        match="stage_receipt_schema_target_invalid",
    ):
        control.validate_stage_receipt(invalid_stage, auth=_auth())


def test_schema9_retry_admission_reconciles_idempotent_replay(
    tmp_path: Path,
) -> None:
    contract = _failed_contract(tmp_path)
    _write_json(control.UPDATE_REQUEST, _original_request())
    body = {
        "confirm": True,
        "expected_manifest_version": contract["target_version"],
        "expected_manifest_commit": contract["target_commit"],
    }

    first = control.create_retry(
        contract,
        contract["actor_subject"],
        body,
    )
    replay = control.create_retry(
        contract,
        contract["actor_subject"],
        body,
    )

    assert first["accepted"] is True
    assert first["idempotent_replay"] is False
    assert replay == {
        **first,
        "idempotent_replay": True,
    }
    admission = control.read_signed(
        control.RETRY_ADMISSION,
        required=True,
    )
    assert admission is not None
    assert admission["target_schema_version"] == 9


@pytest.mark.parametrize("schema_version", (8, 10))
def test_retry_admission_rejects_persisted_non_current_schema(
    tmp_path: Path,
    schema_version: int,
) -> None:
    contract = _failed_contract(tmp_path)
    _write_json(control.UPDATE_REQUEST, _original_request())
    body = {
        "confirm": True,
        "expected_manifest_version": contract["target_version"],
        "expected_manifest_commit": contract["target_commit"],
    }
    control.create_retry(
        contract,
        contract["actor_subject"],
        body,
    )
    admission = control.read_signed(
        control.RETRY_ADMISSION,
        required=True,
    )
    assert admission is not None
    admission["target_schema_version"] = schema_version
    _write_signed(control.RETRY_ADMISSION, admission)

    with pytest.raises(
        control.ContractError,
        match="retry_admission_identity_invalid",
    ):
        control.create_retry(
            contract,
            contract["actor_subject"],
            body,
        )


def test_combined_controller_retry_role_starts_projection_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeThread:
        def __init__(
            self,
            *,
            target: object,
            name: str,
            daemon: bool,
        ) -> None:
            assert target is control.controller_loop
            assert name == "update-status-projector"
            assert daemon is True

        def start(self) -> None:
            events.append("controller")

    monkeypatch.setattr(control, "ROLE", "controller-retry")
    monkeypatch.setattr(control.threading, "Thread", FakeThread)
    monkeypatch.setattr(control, "serve", lambda: events.append("http"))

    control.main()

    assert events == ["controller", "http"]


def test_compose_preserves_read_only_status_and_mutation_boundary() -> None:
    compose = (
        Path(__file__).resolve().parents[3] / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    retry_service = "\n  update-retry-admission:"
    schema_service = "\n  schema-update:"
    reader = compose[
        compose.index("  update-status-reader:")
        : compose.index(retry_service)
    ]
    controller_retry = compose[
        compose.index(retry_service) + 1
        : compose.index(schema_service)
    ]

    assert "update-status-controller" not in compose
    assert (
        "${KM_VMS_HOST_APP_DIR:?KM_VMS_HOST_APP_DIR is required}/data/update-public:/update-public:ro"
        in reader
    )
    assert "/data/update-control" not in reader
    assert "KMVMS_CONTROL_ROLE: controller-retry" in controller_retry
    assert (
        "${KM_VMS_HOST_APP_DIR:?KM_VMS_HOST_APP_DIR is required}/data/update-control:/update-control"
        in controller_retry
    )
    assert (
        "${KM_VMS_HOST_APP_DIR:?KM_VMS_HOST_APP_DIR is required}/data/update-public:/update-public"
        in controller_retry
    )


def test_current_product_schema_is_synchronized_across_active_consumers() -> None:
    root = Path(__file__).resolve().parents[3]
    consumers = {
        "api": (
            root / "apps/api/app/services/schema_versioning.py",
            r"^CURRENT_SCHEMA_VERSION\s*=\s*(\d+)\s*$",
        ),
        "control_plane": (
            root / "apps/update-control-plane/control_plane.py",
            r"^CURRENT_PRODUCT_DB_SCHEMA_VERSION\s*=\s*(\d+)\s*$",
        ),
        "restore_helper": (
            root / "scripts/km-vms-update-helper.py",
            r"^CURRENT_PRODUCT_DB_SCHEMA_VERSION\s*=\s*(\d+)\s*$",
        ),
        "lineage_bridge": (
            root / "scripts/km-vms-update-helper-bridge.py",
            r"^CURRENT_PRODUCT_DB_SCHEMA_VERSION\s*=\s*(\d+)\s*$",
        ),
        "release_cycle": (
            root / "scripts/km-vms-release-cycle.sh",
            r"^CURRENT_PRODUCT_DB_SCHEMA_VERSION\s*=\s*(\d+)\s*$",
        ),
    }
    versions: dict[str, int] = {}
    for name, (path, pattern) in consumers.items():
        match = re.search(
            pattern,
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert match is not None, name
        versions[name] = int(match.group(1))

    assert versions == {name: 9 for name in consumers}
