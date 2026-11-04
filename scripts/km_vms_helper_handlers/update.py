"""Operation implementation; command/path authority stays with the admission facade."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class UpdateOperationHandler:
    HelperError: type[RuntimeError]
    PROGRESS_FILE: Path
    STATUS_FILE: Path
    activation_journal: Callable
    base_status: Callable
    classify_apply_failure: Callable
    compose_app_dir: Callable
    publish_activation_outcome: Callable
    publish_terminal: Callable
    resolve_update_source_dir: Callable
    run_child_with_progress: Callable
    steps_for: Callable
    update_child_env: Callable
    verify_installed_commit: Callable
    write_json: Callable

    def run_update(self, request: dict[str, Any]) -> int:
        source = request["source"]
        update_dir = self.compose_app_dir()
        update_source_dir = self.resolve_update_source_dir(update_dir)
        expected_commit = str(source["commit"])
        command = [
            "sh",
            str(update_source_dir / "scripts" / "update.sh"),
            "--github-repo",
            source["repo"],
            "--branch",
            source["apply_ref"],
            "--trusted-commit",
            source["commit"],
            "--yes",
        ]
        if (
            os.getenv("KM_VMS_GITHUB_PRIVATE", "0") == "1"
            or os.getenv("KMVMS_UPDATE_SOURCE_PRIVATE", "0") == "1"
        ):
            command.append("--github-private")
        env = self.update_child_env(request)
        try:
            self.PROGRESS_FILE.unlink()
        except FileNotFoundError:
            pass
        self.write_json(
            self.STATUS_FILE,
            self.base_status(
                request,
                "preflight",
                "preflight",
                self.steps_for("preflight"),
            ),
        )
        applied = self.run_child_with_progress(
            command,
            request,
            update_dir,
            env,
            timeout_seconds=7200,
            default_step="preflight",
            status_value="preflight",
        )
        journal = self.activation_journal(request["request_id"])
        if journal:
            if self.publish_activation_outcome(
                request,
                journal,
                update_dir,
            ):
                return 0
            raise self.HelperError(
                "activation_journal_invalid",
                "Release activation did not reach terminal state.",
            )
        if applied.returncode != 0:
            raise self.classify_apply_failure(update_dir, applied.stderr.strip())
        self.write_json(
            self.STATUS_FILE,
            self.base_status(
                request,
                "applying",
                "commit_verification",
                self.steps_for("commit_verification"),
            ),
        )
        installed, expected, release_identity = self.verify_installed_commit(
            update_dir,
            expected_commit,
        )
        completed = self.base_status(
            request,
            "completed",
            "completed",
            self.steps_for("completed"),
        )
        completed["commit_verified"] = True
        completed["installed_commit"] = installed
        completed["expected_commit"] = expected
        completed["release_identity"] = release_identity
        completed["finished_at"] = completed["updated_at"]
        self.publish_terminal(request, completed)
        return 0
