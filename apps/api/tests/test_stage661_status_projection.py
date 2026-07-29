from __future__ import annotations

import json
from pathlib import Path

from app.core.config import settings
from app.services import update_apply


REQUEST_ID = "update-" + ("4" * 32)
SUBMISSION_ID = "55555555-5555-4555-8555-555555555555"
TARGET_COMMIT = "6" * 40


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )


def _request() -> dict:
    return {
        "schema_version": 3,
        "document_type": "update_apply_request",
        "request_id": REQUEST_ID,
        "submission_id": SUBMISSION_ID,
        "requested_at": "2026-07-28T00:00:00Z",
        "updated_at": "2026-07-28T00:00:01Z",
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
            "version": "0.8.3",
            "commit": TARGET_COMMIT,
            "apply_ref": TARGET_COMMIT,
            "ref": "v0.8.3",
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
        "state": "claimed",
        "claimed_at": "2026-07-28T00:00:01Z",
        "terminal": None,
        "audit_event_id": update_apply._audit_event_id(REQUEST_ID),
    }


def _journal(
    phase: str,
    *,
    failure_category: str | None = None,
    rollback_trigger: str | None = None,
) -> dict:
    previous_slot = "adopted-" + ("7" * 64)
    target_slot = "release-" + TARGET_COMMIT
    pointer_slot = (
        previous_slot if phase == "failed_rolled_back" else target_slot
    )
    return {
        "schema_version": 1,
        "document_type": "release_slot_activation",
        "request_id": REQUEST_ID,
        "phase": phase,
        "previous": {
            "slot_id": previous_slot,
            "version": "0.8.2",
            "commit": "8" * 40,
        },
        "target": {
            "slot_id": target_slot,
            "version": "0.8.3",
            "commit": TARGET_COMMIT,
        },
        "failure_category": failure_category,
        "rollback_trigger": rollback_trigger,
        "pointer_slot_id": pointer_slot,
        "target_verified": phase
        in {"committing_target", "completed", "failed_rolled_back"},
        "previous_verified": phase == "failed_rolled_back",
        "updated_at": "2026-07-28T00:00:02Z",
    }


def test_restart_gap_projects_nonterminal_journal_without_false_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control = tmp_path / "control"
    monkeypatch.setattr(settings, "update_control_root", str(control))
    _write(control / "update-request.json", _request())
    _write(
        control / "activation-journal.json",
        _journal("verifying_target"),
    )

    status = update_apply.read_update_apply_status()

    assert status["status"] == "reconnecting"
    assert status["phase"] == "verifying_target"
    assert status["admission"]["active"] is True
    persisted = json.loads(
        (control / "update-request.json").read_text(encoding="utf-8")
    )
    assert persisted["state"] == "claimed"


def test_rolled_back_journal_projects_distinct_terminal_truth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control = tmp_path / "control"
    monkeypatch.setattr(settings, "update_control_root", str(control))
    _write(control / "update-request.json", _request())
    _write(
        control / "activation-journal.json",
        _journal(
            "failed_rolled_back",
            failure_category="target_health_failed",
            rollback_trigger="target_health_failed",
        ),
    )

    status = update_apply.read_update_apply_status()

    assert status["status"] == "failed_rolled_back"
    assert status["rollback"] == {
        "status": "completed",
        "trigger": "target_health_failed",
        "restored_version": "0.8.2",
    }
    assert status["error"]["category"] == "target_health_failed"
    assert status["admission"]["active"] is True
    persisted = json.loads(
        (control / "update-request.json").read_text(encoding="utf-8")
    )
    assert persisted["state"] == "claimed"
    assert persisted["terminal"] is None
