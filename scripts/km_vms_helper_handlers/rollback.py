"""Operation implementation; command/path authority stays with the admission facade."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class RestoreRollbackHandler:
    HelperError: type[RuntimeError]
    RESTORE_ARTIFACT_ID_RE: Any
    RESTORE_OPERATIONAL_PHASES: tuple
    finish_restore_request: Callable
    invalidate_post_restore_integrity: Callable
    publish_restore_phase: Callable
    run_restore_executor: Callable
    schedule_post_restore_integrity: Callable
    start_restore_api: Callable
    start_restore_recorder_with_proof: Callable
    stop_restore_writers: Callable

    def rollback_current_restore(self,
        request: dict[str, Any],
        *,
        pre_restore_backup_id: str | None,
        reason_code: str,
        failed_phase: str | None = None,
    ) -> None:
        source_failed_phase = (
            failed_phase
            if failed_phase in self.RESTORE_OPERATIONAL_PHASES
            else "restore_running"
        )

        def recovery_required(
            recovery_reason: str,
            recovery_phase: str,
        ) -> None:
            self.finish_restore_request(
                request,
                result="failed_recovery_required",
                reason_code=recovery_reason,
                pre_restore_backup_id=pre_restore_backup_id,
                destructive_started=True,
                failed_phase=recovery_phase,
            )

        if (
            not pre_restore_backup_id
            or not self.RESTORE_ARTIFACT_ID_RE.fullmatch(
                pre_restore_backup_id
            )
        ):
            recovery_required(
                "pre_restore_backup_missing",
                "restore_running",
            )
            return
        try:
            self.stop_restore_writers()
        except self.HelperError:
            recovery_required(
                "automatic_rollback_isolation_failed",
                "writers_paused",
            )
            return
        try:
            self.run_restore_executor(
                request,
                "restore",
                artifact_id=pre_restore_backup_id,
                mode="rollback",
            )
            self.invalidate_post_restore_integrity(
                request,
                final_db_outcome="rollback",
            )
        except Exception:
            recovery_required(
                "automatic_rollback_database_failed",
                "restore_running",
            )
            return
        self.publish_restore_phase(
            request,
            phase="services_starting",
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=True,
        )
        try:
            self.start_restore_api()
        except Exception:
            recovery_required(
                "automatic_rollback_api_recovery_failed",
                "services_starting",
            )
            return
        self.publish_restore_phase(
            request,
            phase="post_restore_check",
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=True,
        )
        try:
            self.run_restore_executor(request, "post-check")
        except Exception:
            recovery_required(
                "automatic_rollback_validation_failed",
                "post_restore_check",
            )
            return
        try:
            self.start_restore_recorder_with_proof(request)
        except Exception:
            recovery_required(
                "automatic_rollback_recorder_recovery_failed",
                "post_restore_check",
            )
            return
        self.finish_restore_request(
            request,
            result="failed_rolled_back",
            reason_code=reason_code,
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=True,
            failed_phase=source_failed_phase,
        )
        self.schedule_post_restore_integrity(
            request,
            final_db_outcome="rollback",
        )
