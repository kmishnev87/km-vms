from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


HELPER_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "km-vms-update-helper.py"
)
SPEC = importlib.util.spec_from_file_location(
    "stage1378_current_restore_helper",
    HELPER_PATH,
)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
REAL_RUN_RESTORE_API_INTEGRITY_EXECUTOR = (
    helper.run_restore_api_integrity_executor
)


ARTIFACT_A = "kmvms-db-20260729T120000Z-aaaaaaaaaaaa"
ARTIFACT_B = "kmvms-db-20260729T120100Z-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _isolate_post_restore_integrity_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helper,
        "_write_restore_integrity_convergence",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        helper,
        "run_restore_api_integrity_executor",
        lambda *_args, **_kwargs: {
            "scan_id": "41030000-0000-0000-0000-000000000001",
            "status": "queued",
        },
    )


def _request(*, state: str = "claimed") -> dict:
    return {
        "schema": helper.RESTORE_REQUEST_SCHEMA,
        "operation_id": "restore-" + ("a" * 32),
        "submission_id": "674f28e0-b8b9-4b59-a931-00da55df9e4d",
        "intent": "restore_current_database",
        "requested_at": "2026-07-29T12:00:00Z",
        "updated_at": "2026-07-29T12:00:01Z",
        "requested_by": {
            "user_id": 1,
            "subject": "owner",
            "role": "owner",
            "binding": "b" * 64,
        },
        "artifact": {
            "artifact_id": ARTIFACT_A,
            "artifact_created_at": "2026-07-29T11:00:00Z",
            "artifact_schema_version": 8,
            "db_backend": "postgresql",
            "file_size": 1024,
            "fingerprint": "c" * 64,
        },
        "confirmed": True,
        "confirmation_phrase": "RESTORE KM VMS",
        "state": state,
        "claimed_at": (
            "2026-07-29T12:00:01Z"
            if state == "claimed"
            else None
        ),
        "terminal": None,
        "video_archive_scope": "excluded",
        "migration_auto_apply": False,
    }


def _terminal_request(*, status: str = "completed") -> dict:
    request = _request(state="claimed")
    finished_at = "2026-07-29T12:00:30Z"
    return {
        **request,
        "state": "terminal",
        "updated_at": finished_at,
        "terminal": {
            "status": status,
            "finished_at": finished_at,
            "reason_code": None if status == "completed" else "source_restore_failed",
            "failed_phase": None if status == "completed" else "restore_running",
        },
    }


def _successful_executor(
    _request_value: dict,
    action: str,
    *,
    artifact_id: str | None = None,
    mode: str = "source",
    **_kwargs,
) -> dict:
    if action == "pre-restore-backup":
        return {
            "pre_restore_backup_id": ARTIFACT_B,
            "verified": True,
        }
    if action == "restore":
        assert artifact_id in {ARTIFACT_A, ARTIFACT_B}
        assert mode in {"source", "rollback"}
        return {"mutation_started": True}
    return {}


def test_restore_request_contract_rejects_extra_control_fields() -> None:
    request = _request()
    assert helper.validate_restore_request(request) == request
    assert helper.validate_restore_request(
        {**request, "database_url": "postgresql://forbidden"}
    ) is None


def test_stop_proof_rejects_partially_running_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = {"api": False, "recorder": True}
    monkeypatch.setattr(
        helper,
        "_service_running_state",
        lambda service: states[service],
    )

    assert (
        helper.wait_for_restore_writers_stopped(timeout_seconds=0)
        is False
    )


def test_existing_restore_service_action_uses_compose_scope_and_exact_docker_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "a" * 64
    compose_calls: list[tuple[str, ...]] = []
    docker_calls: list[tuple[str, ...]] = []

    def compose(*args, **_kwargs):
        compose_calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            f"{container_id}\n",
            "",
        )

    def docker_run(args, **_kwargs):
        docker_calls.append(tuple(args))
        if args[1] == "inspect":
            return subprocess.CompletedProcess(
                args,
                0,
                "api\n",
                "",
            )
        return subprocess.CompletedProcess(args, 0, None, None)

    monkeypatch.setattr(helper, "restore_compose_command", compose)
    monkeypatch.setattr(helper.subprocess, "run", docker_run)

    assert (
        helper._run_existing_restore_service_action("api", "start")
        == container_id
    )
    assert compose_calls == [("ps", "-q", "--all", "api")]
    assert docker_calls == [
        (
            "docker",
            "inspect",
            "--format",
            '{{ index .Config.Labels "com.docker.compose.service" }}',
            container_id,
        ),
        ("docker", "start", container_id),
    ]


@pytest.mark.parametrize(
    ("service", "compose_stdout", "inspect_label"),
    (
        ("api", "", "api"),
        ("api", f"{'a' * 64}\n{'b' * 64}\n", "api"),
        ("api", "not-a-container-id\n", "api"),
        ("api", f"{'a' * 64}\n", "recorder"),
        ("schema-update", f"{'a' * 64}\n", "schema-update"),
    ),
)
def test_existing_restore_service_action_rejects_untrusted_discovery(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    compose_stdout: str,
    inspect_label: str,
) -> None:
    docker_actions: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        helper,
        "restore_compose_command",
        lambda *args, **_kwargs: subprocess.CompletedProcess(
            args,
            0,
            compose_stdout,
            "",
        ),
    )

    def docker_run(args, **_kwargs):
        if args[1] != "inspect":
            docker_actions.append(tuple(args))
        return subprocess.CompletedProcess(
            args,
            0,
            f"{inspect_label}\n",
            "",
        )

    monkeypatch.setattr(helper.subprocess, "run", docker_run)

    with pytest.raises(helper.HelperError):
        helper._run_existing_restore_service_action(service, "start")
    assert docker_actions == []


def test_post_restore_integrity_scan_runs_inside_existing_api_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    request = _request()
    monkeypatch.setattr(
        helper,
        "restore_compose_command",
        lambda *args, **_kwargs: (
            calls.append(tuple(args))
            or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )
    monkeypatch.setattr(
        helper,
        "read_json",
        lambda path: (
            {
                "schema_version": 1,
                "operation_id": request["operation_id"],
                "action": "enqueue-integrity",
                "status": "completed",
                "reason_code": None,
                "details": {
                    "scan_id": "41030000-0000-0000-0000-000000000010",
                },
            }
            if path == helper.RESTORE_EXECUTOR_RESULT_FILE
            else None
        ),
    )

    details = REAL_RUN_RESTORE_API_INTEGRITY_EXECUTOR(
        request,
        final_db_outcome="source",
    )

    assert details["scan_id"] == "41030000-0000-0000-0000-000000000010"
    assert calls == [
        (
            "exec",
            "-T",
            "api",
            "python3",
            "-m",
            "app.services.current_db_restore_executor",
            "enqueue-integrity",
            "--operation-id",
            request["operation_id"],
            "--final-db-outcome",
            "source",
        )
    ]


def test_recorder_restart_uses_exact_existing_id_and_fresh_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "b" * 64
    calls: list[tuple] = []
    monkeypatch.setattr(
        helper,
        "_run_existing_restore_service_action",
        lambda service, action: (
            calls.append(("docker", action, service))
            or container_id
        ),
    )
    monkeypatch.setattr(
        helper,
        "wait_for_service",
        lambda service, **kwargs: (
            calls.append(
                (
                    "wait",
                    service,
                    kwargs["expected_container_id"],
                )
            )
            or True
        ),
    )
    monkeypatch.setattr(
        helper,
        "run_restore_executor",
        lambda _request_value, action, **kwargs: calls.append(
            (
                "executor",
                action,
                kwargs["recorder_not_before_epoch"],
            )
        ),
    )

    helper.restart_restore_recorder_with_proof(_request())

    assert calls[0] == ("docker", "restart", "recorder")
    assert calls[1] == ("wait", "recorder", container_id)
    assert calls[2][0:2] == ("executor", "recorder-proof")
    assert calls[2][2] > 0
    assert calls[3] == ("wait", "recorder", container_id)


@pytest.mark.parametrize("recorder_was_running", (False, True))
def test_writer_reconciliation_uses_required_recorder_proof(
    monkeypatch: pytest.MonkeyPatch,
    recorder_was_running: bool,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        helper,
        "_service_running_state",
        lambda service: (
            recorder_was_running if service == "recorder" else True
        ),
    )
    monkeypatch.setattr(
        helper,
        "start_restore_api",
        lambda: events.append("api:start"),
    )
    monkeypatch.setattr(
        helper,
        "start_restore_recorder",
        lambda: events.append("recorder:start"),
    )
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: events.append("recorder:new-proof"),
    )
    monkeypatch.setattr(
        helper,
        "run_restore_executor",
        lambda _request_value, action, **_kwargs: events.append(
            f"executor:{action}"
        ),
    )
    monkeypatch.setattr(
        helper,
        "wait_for_service",
        lambda *_args, **_kwargs: True,
    )

    assert helper.restart_restore_writers_best_effort(_request()) is True
    if recorder_was_running:
        assert events == [
            "api:start",
            "recorder:start",
            "executor:recorder-live-proof",
        ]
    else:
        assert events == ["api:start", "recorder:new-proof"]


def test_partial_stop_with_unknown_recorder_restarts_both_writers_without_db_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []
    events: list[str] = []
    terminal: list[dict] = []
    recorder_boundaries: list[float] = []
    monkeypatch.setattr(helper, "read_json", lambda _path: None)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda *_args, **_kwargs: None,
    )

    def executor(_request_value, action, **kwargs):
        actions.append(action)
        if action == "recorder-proof":
            recorder_boundaries.append(
                kwargs["recorder_not_before_epoch"]
            )
        return {}

    monkeypatch.setattr(helper, "run_restore_executor", executor)
    monkeypatch.setattr(
        helper,
        "stop_restore_writers",
        lambda: (_ for _ in ()).throw(
            helper.HelperError(
                "restore_writer_isolation_failed",
                "Injected partial stop.",
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "_service_running_state",
        lambda _service: None,
    )
    monkeypatch.setattr(
        helper,
        "start_restore_api",
        lambda: events.append("api:start"),
    )

    monkeypatch.setattr(
        helper,
        "_run_existing_restore_service_action",
        lambda service, action: (
            events.append(f"docker:{action}:{service}")
            or ("a" * 64)
        ),
    )
    monkeypatch.setattr(
        helper,
        "wait_for_service",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.run_current_restore(_request())

    assert actions == ["preflight", "recorder-proof"]
    assert "pre-restore-backup" not in actions
    assert "restore" not in actions
    assert events == ["api:start", "docker:restart:recorder"]
    assert len(recorder_boundaries) == 1
    assert recorder_boundaries[0] > 0
    assert terminal == [
        {
            "result": "blocked",
            "reason_code": "restore_writer_isolation_failed",
            "pre_restore_backup_id": None,
            "destructive_started": False,
            "failed_phase": "writers_paused",
        }
    ]


def test_writer_reconciliation_attempts_recorder_when_api_recovery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        helper,
        "_service_running_state",
        lambda _service: None,
    )
    monkeypatch.setattr(
        helper,
        "start_restore_api",
        lambda: (_ for _ in ()).throw(
            helper.HelperError(
                "restore_api_health_failed",
                "Injected API recovery failure.",
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "restart_restore_recorder_with_proof",
        lambda _request_value: events.append("recorder:restart"),
    )
    monkeypatch.setattr(
        helper,
        "wait_for_service",
        lambda *_args, **_kwargs: True,
    )

    assert helper.restart_restore_writers_best_effort(_request()) is False
    assert events == ["recorder:restart"]


def test_restore_helper_happy_path_orders_writers_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    terminal: list[dict] = []
    monkeypatch.setattr(helper, "read_json", lambda _path: None)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda _request_value, *, phase, **_kwargs: events.append(
            f"phase:{phase}"
        ),
    )
    monkeypatch.setattr(
        helper,
        "run_restore_executor",
        lambda request_value, action, **kwargs: (
            events.append(f"executor:{action}")
            or _successful_executor(
                request_value,
                action,
                **kwargs,
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "stop_restore_writers",
        lambda: events.append("writers:stop"),
    )
    monkeypatch.setattr(
        helper,
        "start_restore_api",
        lambda: events.append("api:start"),
    )
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: events.append("recorder:start"),
    )
    monkeypatch.setattr(
        helper,
        "run_restore_api_integrity_executor",
        lambda _request_value, *, final_db_outcome, **_kwargs: (
            events.append(f"integrity:{final_db_outcome}")
            or {"scan_id": "41030000-0000-0000-0000-000000000001"}
        ),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.run_current_restore(_request())

    assert terminal == [
        {
            "result": "completed",
            "reason_code": None,
            "pre_restore_backup_id": ARTIFACT_B,
            "destructive_started": True,
        }
    ]
    assert events.index("writers:stop") < events.index(
        "executor:pre-restore-backup"
    )
    assert events.index("executor:restore") < events.index("api:start")
    assert events.index("executor:invalidate-integrity") < events.index("api:start")
    assert events.index("api:start") < events.index("executor:post-check")
    assert events.index("executor:post-check") < events.index(
        "recorder:start"
    )
    assert events.index("recorder:start") < events.index("integrity:source")
    assert events.count("integrity:source") == 1


def test_failure_after_mutation_converges_to_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollbacks: list[dict] = []
    monkeypatch.setattr(helper, "read_json", lambda _path: None)
    monkeypatch.setattr(helper, "publish_restore_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "stop_restore_writers", lambda: None)
    monkeypatch.setattr(helper, "start_restore_api", lambda: None)
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: None,
    )

    def executor(request_value, action, **kwargs):
        if action == "pre-restore-backup":
            return {
                "pre_restore_backup_id": ARTIFACT_B,
                "verified": True,
            }
        if action == "restore" and kwargs.get("mode") == "source":
            raise helper.HelperError(
                "injected_after_mutation",
                "Injected failure.",
                diagnostics={"mutation_started": True},
            )
        return {}

    monkeypatch.setattr(helper, "run_restore_executor", executor)
    monkeypatch.setattr(
        helper,
        "rollback_current_restore",
        lambda _request_value, **kwargs: rollbacks.append(kwargs),
    )

    helper.run_current_restore(_request())

    assert rollbacks == [
        {
            "pre_restore_backup_id": ARTIFACT_B,
            "reason_code": "injected_after_mutation",
            "failed_phase": "restore_running",
        }
    ]


def test_source_recorder_proof_failure_converges_to_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollbacks: list[dict] = []
    monkeypatch.setattr(helper, "read_json", lambda _path: None)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(helper, "stop_restore_writers", lambda: None)
    monkeypatch.setattr(helper, "start_restore_api", lambda: None)

    def executor(request_value, action, **kwargs):
        return _successful_executor(
            request_value,
            action,
            **kwargs,
        )

    monkeypatch.setattr(helper, "run_restore_executor", executor)
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: (_ for _ in ()).throw(
            helper.HelperError(
                "restore_recorder_start_failed",
                "Injected heartbeat timeout.",
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "rollback_current_restore",
        lambda _request_value, **kwargs: rollbacks.append(kwargs),
    )

    helper.run_current_restore(_request())

    assert rollbacks == [
        {
            "pre_restore_backup_id": ARTIFACT_B,
            "reason_code": "restore_recorder_start_failed",
            "failed_phase": "post_restore_check",
        }
    ]


def test_successful_rollback_reports_failed_rolled_back_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal: list[dict] = []
    sequence: list[str] = []
    monkeypatch.setattr(
        helper,
        "stop_restore_writers",
        lambda: None,
    )
    monkeypatch.setattr(helper, "run_restore_executor", _successful_executor)
    monkeypatch.setattr(helper, "publish_restore_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "start_restore_api", lambda: None)
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: None,
    )
    scheduled: list[str] = []
    monkeypatch.setattr(
        helper,
        "run_restore_api_integrity_executor",
        lambda _request_value, *, final_db_outcome, **_kwargs: (
            sequence.append("schedule")
            or
            scheduled.append(final_db_outcome)
            or {"scan_id": "41030000-0000-0000-0000-000000000002"}
        ),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: (
            sequence.append("terminal") or terminal.append(kwargs)
        ),
    )

    helper.rollback_current_restore(
        _request(),
        pre_restore_backup_id=ARTIFACT_B,
        reason_code="source_restore_failed",
    )

    assert terminal == [
        {
            "result": "failed_rolled_back",
            "reason_code": "source_restore_failed",
            "pre_restore_backup_id": ARTIFACT_B,
            "destructive_started": True,
            "failed_phase": "restore_running",
        }
    ]
    assert scheduled == ["rollback"]
    assert sequence == ["terminal", "schedule"]


def test_source_validation_failure_schedules_only_rollback_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_checks = 0
    terminal: list[dict] = []
    scheduled: list[str] = []
    monkeypatch.setattr(helper, "read_json", lambda _path: None)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(helper, "stop_restore_writers", lambda: None)
    monkeypatch.setattr(helper, "start_restore_api", lambda: None)
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: None,
    )

    def executor(request_value, action, **kwargs):
        nonlocal post_checks
        if action == "post-check":
            post_checks += 1
            if post_checks == 1:
                raise helper.HelperError(
                    "post_restore_metadata_invalid",
                    "Injected source DB validation failure.",
                )
        return _successful_executor(
            request_value,
            action,
            **kwargs,
        )

    monkeypatch.setattr(helper, "run_restore_executor", executor)
    monkeypatch.setattr(
        helper,
        "run_restore_api_integrity_executor",
        lambda _request_value, *, final_db_outcome, **_kwargs: (
            scheduled.append(final_db_outcome)
            or {"scan_id": "41030000-0000-0000-0000-000000000003"}
        ),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.run_current_restore(_request())

    assert post_checks == 2
    assert scheduled == ["rollback"]
    assert terminal[0]["result"] == "failed_rolled_back"


def test_integrity_enqueue_failure_keeps_verified_source_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal: list[dict] = []
    rollbacks: list[dict] = []
    convergence: list[dict] = []
    monkeypatch.setattr(helper, "read_json", lambda _path: None)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(helper, "run_restore_executor", _successful_executor)
    monkeypatch.setattr(helper, "stop_restore_writers", lambda: None)
    monkeypatch.setattr(helper, "start_restore_api", lambda: None)
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: None,
    )
    monkeypatch.setattr(
        helper,
        "run_restore_api_integrity_executor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            helper.HelperError(
                "restore_integrity_enqueue_failed",
                "Injected auxiliary scheduling failure.",
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "_write_restore_integrity_convergence",
        lambda _request_value, **kwargs: convergence.append(kwargs),
    )
    monkeypatch.setattr(
        helper,
        "rollback_current_restore",
        lambda _request_value, **kwargs: rollbacks.append(kwargs),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.run_current_restore(_request())

    assert rollbacks == []
    assert terminal[0]["result"] == "completed"
    assert [item["state"] for item in convergence] == [
        "invalidated",
        "retry_required",
    ]
    assert convergence[-1]["reason_code"] == "restore_integrity_enqueue_failed"


def test_verified_source_terminal_survives_crash_before_integrity_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    terminal_request: dict = {}
    convergence: dict = {}
    sequence: list[str] = []
    enqueue_calls: list[str] = []
    rollbacks: list[dict] = []
    restart_phase = False

    monkeypatch.setattr(helper, "publish_restore_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "run_restore_executor", _successful_executor)
    monkeypatch.setattr(helper, "stop_restore_writers", lambda: None)
    monkeypatch.setattr(helper, "start_restore_api", lambda: None)
    monkeypatch.setattr(helper, "start_restore_recorder_with_proof", lambda _request_value: None)
    monkeypatch.setattr(
        helper,
        "rollback_current_restore",
        lambda _request_value, **kwargs: rollbacks.append(kwargs),
    )

    def finish(_request_value: dict, **kwargs) -> None:
        sequence.append("terminal")
        terminal_request.update(_terminal_request(status=kwargs["result"]))

    def write_convergence(
        request_value: dict,
        *,
        final_db_outcome: str,
        state: str,
        attempt_count: int,
        scan_id: str | None = None,
        reason_code: str | None = None,
        next_retry_at_epoch: float | None = None,
    ) -> None:
        convergence.clear()
        convergence.update(
            {
                "schema": helper.RESTORE_INTEGRITY_CONVERGENCE_SCHEMA,
                "operation_id": request_value["operation_id"],
                "final_db_outcome": final_db_outcome,
                "idempotency_key": helper._restore_integrity_idempotency_key(
                    request_value,
                    final_db_outcome,
                ),
                "state": state,
                "attempt_count": attempt_count,
                "scan_id": scan_id,
                "reason_code": reason_code,
                "next_retry_at_epoch": next_retry_at_epoch,
            }
        )

    def enqueue(_request_value: dict, *, final_db_outcome: str, **_kwargs) -> dict:
        enqueue_calls.append(final_db_outcome)
        if len(enqueue_calls) == 1:
            raise SystemExit("injected helper stop after terminal write")
        return {"scan_id": "41030000-0000-0000-0000-000000000004"}

    def read(path: Path):
        if not restart_phase:
            return None
        if path == helper.RESTORE_REQUEST_FILE:
            return terminal_request
        if path == helper.RESTORE_INTEGRITY_CONVERGENCE_FILE:
            return convergence or None
        return None

    monkeypatch.setattr(helper, "read_json", read)
    monkeypatch.setattr(helper, "finish_restore_request", finish)
    monkeypatch.setattr(helper, "_write_restore_integrity_convergence", write_convergence)
    monkeypatch.setattr(helper, "run_restore_api_integrity_executor", enqueue)

    with pytest.raises(SystemExit, match="injected helper stop"):
        helper.run_current_restore(request)

    assert sequence == ["terminal"]
    assert terminal_request["terminal"]["status"] == "completed"
    assert convergence["state"] == "invalidated"
    assert rollbacks == []

    restart_phase = True
    helper.reconcile_restore_integrity_convergence()
    assert enqueue_calls == ["source", "source"]
    assert convergence["state"] == "scheduled"

    helper.reconcile_restore_integrity_convergence()
    assert enqueue_calls == ["source", "source"]
    assert rollbacks == []


def test_terminal_restore_without_convergence_schedules_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = _terminal_request()
    scheduled: list[str] = []

    monkeypatch.setattr(
        helper,
        "read_json",
        lambda path: terminal if path == helper.RESTORE_REQUEST_FILE else None,
    )
    monkeypatch.setattr(
        helper,
        "schedule_post_restore_integrity",
        lambda _request_value, *, final_db_outcome: (
            scheduled.append(final_db_outcome) or True
        ),
    )

    helper.reconcile_restore_integrity_convergence()

    assert scheduled == ["source"]


def test_rollback_api_failure_reports_database_returned_but_recovery_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal: list[dict] = []
    monkeypatch.setattr(helper, "stop_restore_writers", lambda: None)
    monkeypatch.setattr(helper, "run_restore_executor", _successful_executor)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        helper,
        "start_restore_api",
        lambda: (_ for _ in ()).throw(
            helper.HelperError(
                "restore_api_health_failed",
                "Injected rollback API failure.",
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: pytest.fail(
            "recorder must not start before API recovery"
        ),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.rollback_current_restore(
        _request(),
        pre_restore_backup_id=ARTIFACT_B,
        reason_code="source_restore_failed",
    )

    assert terminal == [
        {
            "result": "failed_recovery_required",
            "reason_code": "automatic_rollback_api_recovery_failed",
            "pre_restore_backup_id": ARTIFACT_B,
            "destructive_started": True,
            "failed_phase": "services_starting",
        }
    ]


def test_rollback_recorder_proof_failure_requires_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal: list[dict] = []
    monkeypatch.setattr(helper, "stop_restore_writers", lambda: None)
    monkeypatch.setattr(helper, "run_restore_executor", _successful_executor)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(helper, "start_restore_api", lambda: None)
    monkeypatch.setattr(
        helper,
        "start_restore_recorder_with_proof",
        lambda _request_value: (_ for _ in ()).throw(
            helper.HelperError(
                "restore_recorder_start_failed",
                "Injected rollback heartbeat timeout.",
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.rollback_current_restore(
        _request(),
        pre_restore_backup_id=ARTIFACT_B,
        reason_code="source_restore_failed",
    )

    assert terminal == [
        {
            "result": "failed_recovery_required",
            "reason_code": "automatic_rollback_recorder_recovery_failed",
            "pre_restore_backup_id": ARTIFACT_B,
            "destructive_started": True,
            "failed_phase": "post_restore_check",
        }
    ]


def test_restart_after_destructive_marker_never_repeats_source_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    rollbacks: list[dict] = []

    def read(path: Path):
        if path == helper.RESTORE_DESTRUCTIVE_MARKER_FILE:
            return {
                "operation_id": request["operation_id"],
                "mutation_started": True,
            }
        if path == helper.RESTORE_JOURNAL_FILE:
            return {
                "operation_id": request["operation_id"],
                "pre_restore_backup_id": ARTIFACT_B,
            }
        return None

    monkeypatch.setattr(helper, "read_json", read)
    monkeypatch.setattr(
        helper,
        "rollback_current_restore",
        lambda _request_value, **kwargs: rollbacks.append(kwargs),
    )
    monkeypatch.setattr(
        helper,
        "run_restore_executor",
        lambda *_args, **_kwargs: pytest.fail(
            "source restore must not be repeated after destructive marker"
        ),
    )

    helper.run_current_restore(request)

    assert rollbacks == [
        {
            "pre_restore_backup_id": ARTIFACT_B,
            "reason_code": "restore_interrupted_after_mutation",
            "failed_phase": "restore_running",
        }
    ]


def test_unexpected_failure_after_destructive_start_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollbacks: list[dict] = []
    monkeypatch.setattr(helper, "read_json", lambda _path: None)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(helper, "stop_restore_writers", lambda: None)

    def executor(_request_value, action, **_kwargs):
        if action == "pre-restore-backup":
            return {
                "pre_restore_backup_id": ARTIFACT_B,
                "verified": True,
            }
        if action == "restore":
            raise RuntimeError("injected unexpected failure")
        return {}

    monkeypatch.setattr(helper, "run_restore_executor", executor)
    monkeypatch.setattr(
        helper,
        "rollback_current_restore",
        lambda _request_value, **kwargs: rollbacks.append(kwargs),
    )

    helper.run_current_restore(_request())

    assert rollbacks == [
        {
            "pre_restore_backup_id": ARTIFACT_B,
            "reason_code": "restore_helper_exception",
            "failed_phase": "restore_running",
        }
    ]


def test_rollback_failure_reports_recovery_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal: list[dict] = []
    monkeypatch.setattr(
        helper,
        "stop_restore_writers",
        lambda: None,
    )
    monkeypatch.setattr(
        helper,
        "run_restore_executor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected rollback failure")
        ),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.rollback_current_restore(
        _request(),
        pre_restore_backup_id=ARTIFACT_B,
        reason_code="source_restore_failed",
    )

    assert terminal == [
        {
            "result": "failed_recovery_required",
            "reason_code": "automatic_rollback_database_failed",
            "pre_restore_backup_id": ARTIFACT_B,
            "destructive_started": True,
            "failed_phase": "restore_running",
        }
    ]


def test_partial_initial_writer_stop_recovers_without_db_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []
    recoveries: list[str] = []
    terminal: list[dict] = []
    monkeypatch.setattr(helper, "read_json", lambda _path: None)
    monkeypatch.setattr(
        helper,
        "publish_restore_phase",
        lambda *_args, **_kwargs: None,
    )

    def executor(_request_value, action, **_kwargs):
        actions.append(action)
        return {}

    monkeypatch.setattr(helper, "run_restore_executor", executor)
    monkeypatch.setattr(
        helper,
        "stop_restore_writers",
        lambda: (_ for _ in ()).throw(
            helper.HelperError(
                "restore_writer_isolation_failed",
                "Injected partial stop.",
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "restart_restore_writers_best_effort",
        lambda _request_value: (
            recoveries.append("writers:reconciled") or True
        ),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.run_current_restore(_request())

    assert actions == ["preflight"]
    assert recoveries == ["writers:reconciled"]
    assert terminal == [
        {
            "result": "blocked",
            "reason_code": "restore_writer_isolation_failed",
            "pre_restore_backup_id": None,
            "destructive_started": False,
            "failed_phase": "writers_paused",
        }
    ]


def test_rollback_writer_stop_failure_never_calls_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal: list[dict] = []
    monkeypatch.setattr(
        helper,
        "stop_restore_writers",
        lambda: (_ for _ in ()).throw(
            helper.HelperError(
                "restore_writer_isolation_failed",
                "Injected rollback stop failure.",
            )
        ),
    )
    monkeypatch.setattr(
        helper,
        "run_restore_executor",
        lambda *_args, **_kwargs: pytest.fail(
            "rollback executor must not run without writer isolation"
        ),
    )
    monkeypatch.setattr(
        helper,
        "finish_restore_request",
        lambda _request_value, **kwargs: terminal.append(kwargs),
    )

    helper.rollback_current_restore(
        _request(),
        pre_restore_backup_id=ARTIFACT_B,
        reason_code="source_restore_failed",
    )

    assert terminal == [
        {
            "result": "failed_recovery_required",
            "reason_code": "automatic_rollback_isolation_failed",
            "pre_restore_backup_id": ARTIFACT_B,
            "destructive_started": True,
            "failed_phase": "writers_paused",
        }
    ]


def test_terminal_request_repairs_missing_public_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    request["state"] = "terminal"
    request["updated_at"] = "2026-07-29T12:05:00Z"
    request["terminal"] = {
        "status": "completed",
        "finished_at": "2026-07-29T12:05:00Z",
        "reason_code": None,
    }
    control = tmp_path / "restore-control"
    public = tmp_path / "restore-public"
    monkeypatch.setattr(helper, "RESTORE_CONTROL_DIR", control)
    monkeypatch.setattr(helper, "RESTORE_PUBLIC_DIR", public)
    monkeypatch.setattr(
        helper,
        "RESTORE_REQUEST_FILE",
        control / "restore-request.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_PUBLIC_STATUS_FILE",
        public / "restore-status.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_JOURNAL_FILE",
        control / "restore-journal.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_JOURNAL_DIR",
        control / "journal",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_RECEIPT_DIR",
        control / "receipts",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_DESTRUCTIVE_MARKER_FILE",
        control / "restore-destructive-started.json",
    )
    helper.write_json(helper.RESTORE_REQUEST_FILE, request)

    helper.reconcile_restore_terminal_projection()

    public_status = helper.read_json(
        helper.RESTORE_PUBLIC_STATUS_FILE
    )
    assert public_status is not None
    assert public_status["terminal_result"] == "completed"
    assert public_status["finished_at"] == "2026-07-29T12:05:00Z"
    assert helper.restore_request_may_need_execution() is False
