from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = ROOT / "scripts/km-vms-update-helper.py"
SPEC = importlib.util.spec_from_file_location(
    f"stage660_reasonable_helper_{uuid.uuid4().hex}",
    HELPER_PATH,
)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

TARGET_COMMIT = "c" * 40
SUBMISSION_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = "update-" + ("a" * 32)


def configure_control(tmp_path: Path) -> Path:
    control = tmp_path / "update-control"
    helper.CONTROL_DIR = control
    helper.REQUEST_FILE = control / "update-request.json"
    helper.STATUS_FILE = control / "update-status.json"
    helper.PROGRESS_FILE = control / "update-progress.json"
    helper.APPLY_HISTORY_FILE = control / "update-apply-history.json"
    helper.ACTIVATION_JOURNAL_FILE = (
        control / "activation-journal.json"
    )
    helper.ADMISSION_LOCK_FILE = control / "update-admission.lock"
    helper.HELPER_LEASE_FILE = control / "update-helper-claim.lock"
    control.mkdir(parents=True)
    return control


def current_request(*, state: str = "admitted") -> dict:
    claimed_at = "2026-07-28T00:00:01Z" if state == "claimed" else None
    return {
        "schema_version": 3,
        "document_type": "update_apply_request",
        "request_id": REQUEST_ID,
        "submission_id": SUBMISSION_ID,
        "requested_at": "2026-07-28T00:00:00Z",
        "updated_at": claimed_at or "2026-07-28T00:00:00Z",
        "requested_by": {
            "user_id": 1,
            "username": "owner",
            "role": "owner",
            "ip_address": "127.0.0.1",
            "user_agent": "test",
        },
        "intent": "apply_update",
        "source": {
            "kind": "trusted_manifest",
            "channel": "stable",
            "version": "9.9.9",
            "commit": TARGET_COMMIT,
            "apply_ref": TARGET_COMMIT,
            "ref": "main",
            "repo": "kmishnev87/km-vms",
            "source_type": "github_tarball",
        },
        "apply_candidate": {
            "source": "live_check",
            "snapshot": {
                "available": False,
                "fresh": False,
                "age_seconds": None,
                "fresh_for_seconds": 900,
                "version": None,
                "commit_short": None,
                "provider": None,
            },
        },
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
        "state": state,
        "claimed_at": claimed_at,
        "terminal": None,
        "audit_event_id": helper.deterministic_audit_event_id(REQUEST_ID),
    }


def test_helper_claims_once_and_persists_terminal_single_slot(tmp_path: Path) -> None:
    control = configure_control(tmp_path)
    helper.write_json(helper.REQUEST_FILE, current_request())

    claimed = helper.claim_current_request()

    assert claimed is not None
    assert claimed["state"] == "claimed"
    on_disk = json.loads(helper.REQUEST_FILE.read_text(encoding="utf-8"))
    assert on_disk["state"] == "claimed"
    completed = helper.base_status(
        claimed,
        "completed",
        "completed",
        helper.steps_for("completed"),
    )
    completed["installed_commit"] = TARGET_COMMIT
    completed["commit_verified"] = True
    completed["finished_at"] = completed["updated_at"]
    helper.publish_terminal(claimed, completed)

    terminal = json.loads(helper.REQUEST_FILE.read_text(encoding="utf-8"))
    assert terminal["state"] == "terminal"
    assert terminal["terminal"]["status"] == "completed"
    history = json.loads((control / "update-apply-history.json").read_text(encoding="utf-8"))
    assert len(history["items"]) == 1


def test_claimed_request_after_helper_restart_becomes_truthful_failure(tmp_path: Path) -> None:
    configure_control(tmp_path)
    helper.write_json(helper.REQUEST_FILE, current_request(state="claimed"))

    assert helper.claim_current_request() is None

    terminal = json.loads(helper.REQUEST_FILE.read_text(encoding="utf-8"))
    status = json.loads(helper.STATUS_FILE.read_text(encoding="utf-8"))
    assert terminal["state"] == "terminal"
    assert terminal["terminal"]["error_category"] == "helper_restart_interrupted"
    assert status["status"] == "failed"


def test_claimed_request_with_matching_activation_resumes_instead_of_failing(
    tmp_path: Path,
) -> None:
    configure_control(tmp_path)
    helper.write_json(helper.REQUEST_FILE, current_request(state="claimed"))
    helper.write_json(
        helper.ACTIVATION_JOURNAL_FILE,
        {
            "schema_version": 1,
            "document_type": "release_slot_activation",
            "request_id": REQUEST_ID,
            "phase": "verifying_target",
            "previous": {
                "slot_id": "adopted-" + ("1" * 64),
                "version": "0.8.2",
                "commit": "2" * 40,
            },
            "target": {
                "slot_id": "release-" + TARGET_COMMIT,
                "version": "9.9.9",
                "commit": TARGET_COMMIT,
            },
            "failure_category": None,
            "rollback_trigger": None,
            "pointer_slot_id": "release-" + TARGET_COMMIT,
            "target_verified": False,
            "previous_verified": False,
        },
    )

    claimed = helper.claim_current_request()

    assert claimed is not None
    assert claimed.pop("_resume_activation") is True
    assert claimed["state"] == "claimed"
    on_disk = json.loads(helper.REQUEST_FILE.read_text(encoding="utf-8"))
    assert on_disk["state"] == "claimed"
    assert not helper.STATUS_FILE.exists()


def test_terminal_schema_retry_is_not_executed_again(tmp_path: Path) -> None:
    configure_control(tmp_path)
    retry_request_id = "update-" + ("b" * 32)
    retry = {
        "schema_version": 1,
        "request_id": retry_request_id,
        "requested_at": "2026-07-28T00:00:00Z",
        "requested_by": {"user_id": "1", "role": "owner"},
        "intent": "apply_update",
        "source": current_request()["source"],
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
        "retry_of_request_id": REQUEST_ID,
        "migration_attempt_id": "migration-attempt-" + ("d" * 32),
    }
    helper.write_json(helper.REQUEST_FILE, retry)
    helper.write_json(
        helper.STATUS_FILE,
        {
            "schema_version": 1,
            "request_id": retry_request_id,
            "status": "failed",
        },
    )

    assert helper.validate_schema_retry(retry) is not None
    assert helper.request_may_need_execution() is False
    assert helper.claim_current_request() is None
