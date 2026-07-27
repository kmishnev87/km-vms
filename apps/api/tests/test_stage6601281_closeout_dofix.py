from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import (
    operation_recovery,
    schema_migration_gate,
    schema_preparation,
    schema_update_control as control,
)


ROOT = Path(__file__).resolve().parents[3]


def _authority(**overrides: bool) -> dict[str, bool]:
    result = {
        "request": False,
        "pre_overlay_identity": False,
        "auth_snapshot": False,
        "control_bootstrap_receipt": False,
        "preparation_receipt": False,
        "recovery_receipt": False,
        "gate_receipt": False,
        "retry_admission": False,
    }
    result.update(overrides)
    return result


def test_schema_mode_distinguishes_fresh_update_noop_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control,
        "schema_execution_authority_presence",
        lambda: _authority(),
    )
    monkeypatch.setattr(control, "_read_update_status_for_execution", lambda: None)
    monkeypatch.setattr(control, "database_is_empty", lambda _db: True)
    assert control.resolve_schema_execution_mode(object()) == "fresh_install"

    request_id = "update-" + "1" * 32
    request_payload = {"request_id": request_id}
    monkeypatch.setattr(
        control,
        "schema_execution_authority_presence",
        lambda: _authority(request=True, pre_overlay_identity=True),
    )
    monkeypatch.setattr(
        control,
        "read_regular_json",
        lambda path, **_kwargs: (
            request_payload if path == control.REQUEST_PATH else None
        ),
    )
    monkeypatch.setattr(control, "validate_update_request", lambda _value: "current")
    monkeypatch.setattr(
        control,
        "target_identity",
        lambda _value, **_kwargs: ("0.7.25", "2" * 40),
    )
    monkeypatch.setattr(
        control,
        "_read_update_status_for_execution",
        lambda: {
            "schema_version": 1,
            "request_id": request_id,
            "status": "applying",
            "target_version": "0.7.25",
            "expected_commit": "2" * 40,
            "source": {"commit": "2" * 40},
        },
    )
    monkeypatch.setattr(control, "_schema_control_row_for_execution", lambda _db: None)
    assert control.resolve_schema_execution_mode(object()) == "authorized_update"

    monkeypatch.setattr(
        control,
        "schema_execution_authority_presence",
        lambda: _authority(),
    )
    monkeypatch.setattr(control, "_read_update_status_for_execution", lambda: None)
    monkeypatch.setattr(control, "database_is_empty", lambda _db: False)
    validated: list[object] = []
    monkeypatch.setattr(
        control,
        "validate_exact_target_noop",
        lambda db: validated.append(db),
    )
    db = object()
    assert control.resolve_schema_execution_mode(db) == "exact_target_noop"
    assert validated == [db]

    monkeypatch.setattr(
        control,
        "validate_exact_target_noop",
        lambda _db: (_ for _ in ()).throw(
            control.SchemaControlError("no_active_schema_below_target")
        ),
    )
    with pytest.raises(
        control.SchemaControlError,
        match="no_active_schema_below_target",
    ):
        control.resolve_schema_execution_mode(object())


@pytest.mark.parametrize(
    "stale_receipt",
    (
        "auth_snapshot",
        "control_bootstrap_receipt",
        "preparation_receipt",
        "recovery_receipt",
        "gate_receipt",
        "retry_admission",
    ),
)
def test_exact_target_noop_ignores_one_regular_stale_derived_receipt(
    monkeypatch: pytest.MonkeyPatch,
    stale_receipt: str,
) -> None:
    monkeypatch.setattr(
        control,
        "schema_execution_authority_presence",
        lambda: _authority(**{stale_receipt: True}),
    )
    monkeypatch.setattr(control, "_read_update_status_for_execution", lambda: None)
    monkeypatch.setattr(control, "database_is_empty", lambda _db: False)
    validated: list[object] = []
    monkeypatch.setattr(
        control,
        "validate_exact_target_noop",
        lambda db: validated.append(db),
    )
    db = object()

    assert control.resolve_schema_execution_mode(db) == "exact_target_noop"
    assert validated == [db]


def test_stale_derived_receipt_does_not_authorize_fresh_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control,
        "schema_execution_authority_presence",
        lambda: _authority(gate_receipt=True),
    )
    monkeypatch.setattr(control, "_read_update_status_for_execution", lambda: None)
    monkeypatch.setattr(control, "database_is_empty", lambda _db: True)

    with pytest.raises(
        control.SchemaControlError,
        match="schema_update_signed_authority_orphaned",
    ):
        control.resolve_schema_execution_mode(object())


def test_exact_v0724_active_status_without_version_uses_request_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "update-" + "2" * 32
    request_payload = {
        "request_id": request_id,
        "source": {"version": "0.7.25"},
    }
    monkeypatch.setattr(
        control,
        "schema_execution_authority_presence",
        lambda: _authority(request=True, pre_overlay_identity=True),
    )
    monkeypatch.setattr(
        control,
        "_read_update_status_for_execution",
        lambda: {
            "schema_version": 1,
            "request_id": request_id,
            "status": "applying",
            "expected_commit": "3" * 40,
            "source": {"commit": "3" * 40},
        },
    )
    monkeypatch.setattr(
        control,
        "read_regular_json",
        lambda path, **_kwargs: (
            request_payload if path == control.REQUEST_PATH else None
        ),
    )
    monkeypatch.setattr(control, "validate_update_request", lambda _payload: "snapshot")
    monkeypatch.setattr(
        control,
        "target_identity",
        lambda _payload, **_kwargs: ("0.7.25", "3" * 40),
    )
    monkeypatch.setattr(control, "_schema_control_row_for_execution", lambda _db: None)

    assert control.resolve_schema_execution_mode(object()) == "authorized_update"


def _v0724_completed_status(request_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "status": "completed",
        "commit_verified": True,
        "error": None,
        "expected_commit": "4" * 40,
        "installed_commit": "4" * 40,
        "phase": "completed",
        "current_step": "completed",
        "started_at": "2026-07-26T10:00:00Z",
        "updated_at": "2026-07-26T10:01:00Z",
        "finished_at": "2026-07-26T10:01:00Z",
    }


def test_exact_v0724_completed_status_without_version_remains_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "update-" + "4" * 32
    context = SimpleNamespace(
        request={
            "request_id": request_id,
            "source": {"version": "0.7.25"},
        },
        request_id=request_id,
        target_release="0.7.25",
        target_commit="4" * 40,
    )
    monkeypatch.setattr(
        control,
        "validate_terminal_update_request",
        lambda _payload: "snapshot",
    )

    control._validate_matching_completed_update_status(
        _v0724_completed_status(request_id),
        context=context,
    )


def test_terminal_noop_skips_writer_wait_and_authorized_run_rechecks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control,
        "resolve_schema_execution_mode",
        lambda _db: "exact_target_noop",
    )
    monkeypatch.setattr(
        control,
        "wait_for_writer_quiescence",
        lambda _db: pytest.fail("terminal no-op must not wait for writers"),
    )
    assert (
        control.resolve_schema_pipeline_execution_mode(object())
        == "exact_target_noop"
    )

    modes = iter(("authorized_update", "exact_target_noop"))
    calls: list[str] = []
    monkeypatch.setattr(
        control,
        "resolve_schema_execution_mode",
        lambda _db: next(modes),
    )
    monkeypatch.setattr(
        control,
        "wait_for_writer_quiescence",
        lambda _db: calls.append("quiesced"),
    )
    assert (
        control.resolve_schema_pipeline_execution_mode(object())
        == "exact_target_noop"
    )
    assert calls == ["quiesced"]


class _NoopSession:
    def __init__(self, value: object):
        self.value = value

    def __enter__(self) -> object:
        return self.value

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.mark.parametrize(
    "module",
    (schema_preparation, operation_recovery, schema_migration_gate),
)
def test_schema_phases_exit_before_mutation_for_terminal_noop(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
) -> None:
    db = object()
    calls: list[str] = []
    monkeypatch.setattr(module, "Session", lambda _engine: _NoopSession(db))
    monkeypatch.setattr(
        module,
        "acquire_schema_lock",
        lambda value: calls.append("lock") if value is db else None,
    )
    monkeypatch.setattr(
        module,
        "resolve_schema_pipeline_execution_mode",
        lambda value: (
            calls.append("resolve") or "exact_target_noop"
            if value is db
            else "invalid"
        ),
    )
    monkeypatch.setattr(
        module,
        "write_stage_receipt",
        lambda *_args, **_kwargs: pytest.fail(
            "terminal no-op must not rewrite receipts"
        ),
    )
    monkeypatch.setattr(
        module,
        "release_schema_lock",
        lambda value: calls.append("unlock") if value is db else None,
    )

    module.main()

    assert calls == ["lock", "resolve", "unlock"]


def test_safe_restart_reconciles_only_persistent_services(tmp_path: Path) -> None:
    app = tmp_path / "app"
    scripts = app / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/km-vms-restart.sh", scripts)
    shutil.copy2(ROOT / "scripts/km-vms-compose-common.sh", scripts)
    (app / ".env").write_text("COMPOSE_PROJECT_NAME=stage-test\n")
    (app / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: example.invalid/api\n",
        encoding="utf-8",
    )
    control_dir = app / "data/install-control"
    control_dir.mkdir(parents=True)
    (control_dir / "docker-compose.archive-roots.yml").write_text(
        "services:\n  api:\n    volumes: []\n",
        encoding="utf-8",
    )
    compose_log = tmp_path / "compose.log"
    fake_compose = tmp_path / "fake-compose"
    fake_compose.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$KMVMS_FAKE_COMPOSE_LOG\"\n",
        encoding="utf-8",
    )
    fake_compose.chmod(0o755)

    result = subprocess.run(
        [
            "sh",
            str(scripts / "km-vms-restart.sh"),
            "--app-dir",
            str(app),
            "--project-name",
            "stage-test",
        ],
        cwd=app,
        env={
            **os.environ,
            "KM_VMS_DOCKER_COMPOSE": str(fake_compose),
            "KMVMS_FAKE_COMPOSE_LOG": str(compose_log),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert result.returncode == 0, result.stdout
    invocations = compose_log.read_text(encoding="utf-8").splitlines()
    up = [line for line in invocations if " up " in f" {line} "]
    assert len(up) == 1
    command = up[0]
    assert "up -d --no-deps" in command
    for service in (
        "update-status-reader",
        "update-retry-admission",
        "api",
        "recorder",
        "web",
        "nginx",
        "setup-helper",
        "update-helper",
    ):
        assert service in command
    for one_shot in ("update-helper-bootstrap", "schema-update"):
        assert one_shot not in command


def _safe_retry_evidence(**overrides: bool) -> dict[str, bool | int]:
    result: dict[str, bool | int] = {
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
    result.update(overrides)
    return result


def test_retry_requires_known_reason_and_verified_safe_outcome() -> None:
    allowed = control.classify_retry(
        "test_injected_preparation_failure_before_ddl",
        _safe_retry_evidence(),
    )
    assert allowed.retryable is True
    assert allowed.public_state == "failed"

    for reason, evidence in (
        ("unexpected_exception", _safe_retry_evidence()),
        (
            "test_injected_preparation_failure_before_ddl",
            _safe_retry_evidence(rollback_verified=False),
        ),
        (
            "test_injected_preparation_failure_before_ddl",
            _safe_retry_evidence(physical_mutation_possible=True),
        ),
    ):
        denied = control.classify_retry(reason, evidence)
        assert denied.retryable is False
        assert denied.public_state == "recovery_required"
