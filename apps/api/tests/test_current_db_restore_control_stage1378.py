from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "update-control-plane"
    / "control_plane.py"
)
SPEC = importlib.util.spec_from_file_location(
    "stage1378_restore_control_plane",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


ARTIFACT_A = "kmvms-db-20260729T120000Z-aaaaaaaaaaaa"
ARTIFACT_B = "kmvms-db-20260729T120100Z-bbbbbbbbbbbb"
SUBMISSION = "674f28e0-b8b9-4b59-a931-00da55df9e4d"
NOW = "2026-07-29T12:00:00Z"


def _public_status() -> dict:
    return {
        "schema": "stage13.7.8.current-restore-public.v1",
        "operation_id": "restore-" + ("a" * 32),
        "submission_id": SUBMISSION,
        "actor_subject": "owner",
        "status": "failed_rolled_back",
        "phase": "failed_rolled_back",
        "artifact": {
            "artifact_id": ARTIFACT_A,
            "artifact_created_at": NOW,
            "artifact_schema_version": 9,
            "db_backend": "postgresql",
        },
        "pre_restore_backup_id": ARTIFACT_B,
        "accepted_at": NOW,
        "started_at": NOW,
        "updated_at": NOW,
        "finished_at": NOW,
        "terminal_result": "failed_rolled_back",
        "reason_code": "source_restore_failed",
        "failed_phase": "restore_running",
        "next_action": "current_database_restored",
        "video_archive_modified": False,
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )


def test_reader_restore_status_is_actor_bound_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "restore-public" / "restore-status.json"
    monkeypatch.setattr(control, "RESTORE_PUBLIC_STATUS", status_path)
    monkeypatch.setattr(
        control,
        "bearer_subject",
        lambda headers: headers["subject"],
    )
    _write(status_path, _public_status())

    safe = control.restore_public_status({"subject": "owner"})

    assert safe["terminal_result"] == "failed_rolled_back"
    assert safe["failed_phase"] == "restore_running"
    assert safe["next_action"] == "current_database_restored"
    assert "actor_subject" not in safe
    assert "schema" not in safe
    assert "raw_path" not in json.dumps(safe)
    assert control.CURRENT_PRODUCT_DB_SCHEMA_VERSION == 9

    with pytest.raises(control.ContractError, match="foreign_actor_forbidden"):
        control.restore_public_status({"subject": "other"})

    _write(status_path, {**_public_status(), "raw_path": "/forbidden"})
    with pytest.raises(
        control.ContractError,
        match="restore_status_contract_invalid",
    ):
        control.restore_public_status({"subject": "owner"})

    legacy = _public_status()
    legacy.pop("failed_phase")
    _write(status_path, legacy)
    assert (
        control.restore_public_status({"subject": "owner"})[
            "terminal_result"
        ]
        == "failed_rolled_back"
    )


@pytest.mark.parametrize("schema_version", (8, 10))
def test_reader_restore_status_rejects_non_current_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int,
) -> None:
    status_path = tmp_path / "restore-public" / "restore-status.json"
    monkeypatch.setattr(control, "RESTORE_PUBLIC_STATUS", status_path)
    monkeypatch.setattr(
        control,
        "bearer_subject",
        lambda headers: headers["subject"],
    )
    payload = _public_status()
    payload["artifact"]["artifact_schema_version"] = schema_version
    _write(status_path, payload)

    with pytest.raises(
        control.ContractError,
        match="restore_status_contract_invalid",
    ):
        control.restore_public_status({"subject": "owner"})


def test_reader_restore_status_returns_authenticated_idle_before_first_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control,
        "RESTORE_PUBLIC_STATUS",
        tmp_path / "restore-public" / "restore-status.json",
    )
    monkeypatch.setattr(
        control,
        "bearer_subject",
        lambda headers: headers["subject"],
    )

    assert control.restore_public_status({"subject": "owner"}) == {
        "status": "idle",
        "phase": None,
        "terminal_result": None,
        "failed_phase": None,
        "video_archive_modified": False,
    }


def test_retry_admission_fails_closed_when_backup_authority_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control,
        "RESTORE_REQUEST",
        tmp_path / "restore" / "restore-request.json",
    )
    monkeypatch.setattr(
        control,
        "MANUAL_SCHEMA_OPERATION",
        tmp_path / "maintenance" / "manual-schema-operation.json",
    )
    monkeypatch.setattr(
        control,
        "BACKUP_OPERATION_ROOT",
        tmp_path / "missing-backup-authority",
    )

    with pytest.raises(control.ContractError, match="backup_state_unavailable"):
        control.assert_retry_maintenance_idle()


def test_compose_and_nginx_keep_restore_authority_bounded() -> None:
    root = Path(__file__).resolve().parents[3]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    nginx = (root / "deploy/nginx/default.conf").read_text(
        encoding="utf-8"
    )
    reader = compose[
        compose.index("  update-status-reader:")
        : compose.index("\n  update-retry-admission:")
    ]
    executor = compose[
        compose.index("  restore-executor:")
        : compose.index("\n  setup-helper:")
    ]
    route = nginx[
        nginx.index("location = /api/system/restore/current/status")
        : nginx.index(
            "location = /api/system/update/apply/status",
        )
    ]
    fallback = nginx[
        nginx.index("location @failure_restore_current_status")
        : nginx.index("location @failure_update_apply_status")
    ]

    assert "/data/restore-public:/restore-public:ro" in reader
    assert "restore-control" not in reader
    assert 'profiles: ["current-db-restore"]' in executor
    assert 'restart: "no"' in executor
    assert "/data/restore-control:/restore-control" in executor
    assert "/data/maintenance-control:/maintenance-control:ro" in executor
    assert "/var/run/docker.sock" not in executor
    assert "\n    ports:" not in executor
    assert "limit_except GET" in route
    assert "error_page 502 503 504" in route
    assert "proxy_pass_request_body off" in route
    assert "Authorization $http_authorization" in route
    assert "proxy_pass http://update-status-reader:8080" in fallback
    assert "proxy_pass_request_body off" in fallback
