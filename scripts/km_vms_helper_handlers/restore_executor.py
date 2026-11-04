"""Operation implementation; command/path authority stays with the admission facade."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class RestoreCommandExecutor:
    HelperError: type[RuntimeError]
    MACHINE_CODE_RE: Any
    RESTORE_EXECUTOR_RESULT_FILE: Path
    read_json: Callable
    restore_compose_command: Callable
    restore_destructive_started: Callable
    safe_text: Callable

    def run_restore_executor(self,
        request: dict[str, Any],
        action: str,
        *,
        artifact_id: str | None = None,
        mode: str = "source",
        recorder_not_before_epoch: float | None = None,
        final_db_outcome: str | None = None,
        timeout_seconds: int = 1200,
    ) -> dict[str, Any]:
        command = [
            "run",
            "--rm",
            "--no-deps",
            "restore-executor",
            action,
            "--operation-id",
            request["operation_id"],
        ]
        if artifact_id:
            command.extend(["--artifact-id", artifact_id])
        if action == "restore":
            command.extend(["--mode", mode])
        if action == "invalidate-integrity":
            if final_db_outcome not in {"source", "rollback"}:
                raise self.HelperError(
                    "restore_integrity_outcome_invalid",
                    "Restore integrity outcome is invalid.",
                )
            command.extend(
                ["--final-db-outcome", final_db_outcome]
            )
        if action == "recorder-proof":
            if recorder_not_before_epoch is None:
                raise self.HelperError(
                    "restore_recorder_proof_boundary_missing",
                    "Recorder proof boundary is unavailable.",
                )
            command.extend(
                [
                    "--recorder-not-before-epoch",
                    f"{float(recorder_not_before_epoch):.6f}",
                ]
            )
        result = self.restore_compose_command(
            *command,
            timeout_seconds=timeout_seconds,
        )
        payload = self.read_json(self.RESTORE_EXECUTOR_RESULT_FILE)
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("operation_id")
            != request["operation_id"]
            or payload.get("action") != action
            or payload.get("status")
            not in {"completed", "failed"}
        ):
            raise self.HelperError(
                "restore_executor_result_invalid",
                "Restore executor result is unavailable.",
                diagnostics={
                    "mutation_started": bool(
                        self.restore_destructive_started(
                            request["operation_id"]
                        )
                    )
                },
            )
        if result.returncode != 0 or payload["status"] != "completed":
            code = self.safe_text(
                payload.get("reason_code"),
                80,
            ) or "restore_executor_failed"
            raise self.HelperError(
                code
                if self.MACHINE_CODE_RE.fullmatch(code)
                else "restore_executor_failed",
                "Restore executor failed.",
                diagnostics={
                    "mutation_started": bool(
                        payload.get("mutation_started")
                    )
                },
            )
        details = payload.get("details")
        return details if isinstance(details, dict) else {}

    def run_restore_api_integrity_executor(self,
        request: dict[str, Any],
        *,
        final_db_outcome: str,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        command = [
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
            final_db_outcome,
        ]
        result = self.restore_compose_command(
            *command,
            timeout_seconds=timeout_seconds,
        )
        payload = self.read_json(self.RESTORE_EXECUTOR_RESULT_FILE)
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("operation_id") != request["operation_id"]
            or payload.get("action") != "enqueue-integrity"
            or payload.get("status") not in {"completed", "failed"}
        ):
            raise self.HelperError(
                "restore_integrity_enqueue_result_invalid",
                "Archive integrity scheduling result is unavailable.",
            )
        if result.returncode != 0 or payload["status"] != "completed":
            raise self.HelperError(
                "restore_integrity_enqueue_failed",
                "Archive integrity scan could not be scheduled.",
            )
        details = payload.get("details")
        if not isinstance(details, dict) or not self.safe_text(
            details.get("scan_id"),
            64,
        ):
            raise self.HelperError(
                "restore_integrity_enqueue_result_invalid",
                "Archive integrity scheduling result is unavailable.",
            )
        return details
