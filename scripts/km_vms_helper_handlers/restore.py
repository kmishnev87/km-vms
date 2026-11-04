"""Operation implementation; command/path authority stays with the admission facade."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class RestoreOperationHandler:
    HelperError: type[RuntimeError]
    RESTORE_ARTIFACT_ID_RE: Any
    RESTORE_DESTRUCTIVE_MARKER_FILE: Path
    RESTORE_JOURNAL_FILE: Path
    RESTORE_OPERATIONAL_PHASES: tuple
    finish_restore_request: Callable
    invalidate_post_restore_integrity: Callable
    publish_restore_phase: Callable
    read_json: Callable
    restart_restore_writers_best_effort: Callable
    restore_destructive_started: Callable
    rollback_current_restore: Callable
    run_restore_executor: Callable
    safe_text: Callable
    schedule_post_restore_integrity: Callable
    start_restore_api: Callable
    start_restore_recorder_with_proof: Callable
    stop_restore_writers: Callable

    def run_current_restore(self, request: dict[str, Any]) -> None:
        prior_journal = self.read_json(self.RESTORE_JOURNAL_FILE)
        prior_marker = self.read_json(
            self.RESTORE_DESTRUCTIVE_MARKER_FILE
        )
        if (
            isinstance(prior_marker, dict)
            and prior_marker.get("operation_id")
            == request["operation_id"]
            and prior_marker.get("mutation_started") is True
        ):
            pre_restore_backup_id = (
                prior_journal.get("pre_restore_backup_id")
                if isinstance(prior_journal, dict)
                else None
            )
            self.rollback_current_restore(
                request,
                pre_restore_backup_id=pre_restore_backup_id,
                reason_code="restore_interrupted_after_mutation",
                failed_phase=(
                    prior_journal.get("phase")
                    if isinstance(prior_journal, dict)
                    and prior_journal.get("phase")
                    in self.RESTORE_OPERATIONAL_PHASES
                    else "restore_running"
                ),
            )
            return
        if (
            request["state"] == "claimed"
            and isinstance(prior_journal, dict)
            and prior_journal.get("operation_id")
            == request["operation_id"]
        ):
            writers_recovered = self.restart_restore_writers_best_effort(
                request
            )
            self.finish_restore_request(
                request,
                result="blocked",
                reason_code=(
                    "restore_interrupted_before_mutation"
                    if writers_recovered
                    else "restore_writer_isolation_failed"
                ),
                pre_restore_backup_id=prior_journal.get(
                    "pre_restore_backup_id"
                ),
                destructive_started=False,
                failed_phase=(
                    prior_journal.get("phase")
                    if prior_journal.get("phase")
                    in self.RESTORE_OPERATIONAL_PHASES
                    else "preflight"
                ),
            )
            return

        pre_restore_backup_id: str | None = None
        writers_isolation_attempted = False
        destructive_started = False
        current_phase = "preflight"
        try:
            current_phase = "preflight"
            self.publish_restore_phase(request, phase="preflight")
            self.run_restore_executor(request, "preflight")
            current_phase = "pre_restore_backup"
            self.publish_restore_phase(
                request,
                phase="pre_restore_backup",
            )
            writers_isolation_attempted = True
            current_phase = "writers_paused"
            self.publish_restore_phase(
                request,
                phase="writers_paused",
            )
            self.stop_restore_writers()
            current_phase = "pre_restore_backup"
            backup = self.run_restore_executor(
                request,
                "pre-restore-backup",
            )
            pre_restore_backup_id = self.safe_text(
                backup.get("pre_restore_backup_id"),
                80,
            )
            if (
                not pre_restore_backup_id
                or not self.RESTORE_ARTIFACT_ID_RE.fullmatch(
                    pre_restore_backup_id
                )
                or backup.get("verified") is not True
            ):
                raise self.HelperError(
                    "pre_restore_backup_verification_failed",
                    "Pre-restore backup verification failed.",
                )
            current_phase = "restore_running"
            self.publish_restore_phase(
                request,
                phase="restore_running",
                pre_restore_backup_id=pre_restore_backup_id,
            )
            destructive_started = True
            self.run_restore_executor(
                request,
                "restore",
                artifact_id=request["artifact"]["artifact_id"],
                mode="source",
            )
            self.invalidate_post_restore_integrity(
                request,
                final_db_outcome="source",
            )
            current_phase = "services_starting"
            self.publish_restore_phase(
                request,
                phase="services_starting",
                pre_restore_backup_id=pre_restore_backup_id,
                destructive_started=True,
            )
            self.start_restore_api()
            current_phase = "post_restore_check"
            self.publish_restore_phase(
                request,
                phase="post_restore_check",
                pre_restore_backup_id=pre_restore_backup_id,
                destructive_started=True,
            )
            self.run_restore_executor(request, "post-check")
            self.start_restore_recorder_with_proof(request)
            self.finish_restore_request(
                request,
                result="completed",
                reason_code=None,
                pre_restore_backup_id=pre_restore_backup_id,
                destructive_started=True,
            )
            self.schedule_post_restore_integrity(
                request,
                final_db_outcome="source",
            )
            return
        except self.HelperError as exc:
            destructive_started = bool(
                destructive_started
                or exc.diagnostics.get("mutation_started")
                or self.restore_destructive_started(
                    request["operation_id"]
                )
            )
            if destructive_started:
                self.rollback_current_restore(
                    request,
                    pre_restore_backup_id=pre_restore_backup_id,
                    reason_code=exc.category,
                    failed_phase=current_phase,
                )
                return
            writers_recovered = bool(
                not writers_isolation_attempted
                or self.restart_restore_writers_best_effort(request)
            )
            self.finish_restore_request(
                request,
                result="blocked",
                reason_code=(
                    exc.category
                    if writers_recovered
                    else "restore_writer_isolation_failed"
                ),
                pre_restore_backup_id=pre_restore_backup_id,
                destructive_started=False,
                failed_phase=current_phase,
            )
            return
        except Exception:
            destructive_started = bool(
                destructive_started
                or self.restore_destructive_started(
                    request["operation_id"]
                )
            )
            if destructive_started:
                self.rollback_current_restore(
                    request,
                    pre_restore_backup_id=pre_restore_backup_id,
                    reason_code="restore_helper_exception",
                    failed_phase=current_phase,
                )
                return
            writers_recovered = bool(
                not writers_isolation_attempted
                or self.restart_restore_writers_best_effort(request)
            )
            self.finish_restore_request(
                request,
                result="blocked",
                reason_code=(
                    "restore_helper_exception"
                    if writers_recovered
                    else "restore_writer_isolation_failed"
                ),
                pre_restore_backup_id=pre_restore_backup_id,
                destructive_started=False,
                failed_phase=current_phase,
            )
            return

    def converge_unhandled_restore_failure(self,
        request: dict[str, Any],
        *,
        reason_code: str,
    ) -> None:
        journal = self.read_json(self.RESTORE_JOURNAL_FILE)
        pre_restore_backup_id = (
            journal.get("pre_restore_backup_id")
            if isinstance(journal, dict)
            and journal.get("operation_id") == request["operation_id"]
            else None
        )
        if self.restore_destructive_started(request["operation_id"]):
            self.rollback_current_restore(
                request,
                pre_restore_backup_id=pre_restore_backup_id,
                reason_code=reason_code,
                failed_phase=(
                    journal.get("phase")
                    if isinstance(journal, dict)
                    and journal.get("phase")
                    in self.RESTORE_OPERATIONAL_PHASES
                    else "restore_running"
                ),
            )
            return
        writers_recovered = self.restart_restore_writers_best_effort(
            request
        )
        self.finish_restore_request(
            request,
            result="blocked",
            reason_code=(
                reason_code
                if writers_recovered
                else "restore_writer_isolation_failed"
            ),
            pre_restore_backup_id=pre_restore_backup_id,
            destructive_started=False,
            failed_phase=(
                journal.get("phase")
                if isinstance(journal, dict)
                and journal.get("phase")
                in self.RESTORE_OPERATIONAL_PHASES
                else "preflight"
            ),
        )
