from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


BRIDGE_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "km-vms-update-helper-bridge.py"
)
SPEC = importlib.util.spec_from_file_location(
    "stage660128_update_helper_bridge",
    BRIDGE_PATH,
)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

TARGET_COMMIT = "5" * 40


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _fixture(root: Path, request_id: str) -> None:
    _write_json(
        root / "data/update-control/update-request.json",
        {
            "schema_version": 1,
            "request_id": request_id,
            "requested_at": "2026-07-24T00:00:00Z",
            "requested_by": {"user_id": "1", "role": "owner"},
            "intent": "apply_update",
            "source": {
                "kind": "github-tarball",
                "repo": "kmishnev87/km-vms",
                "ref": TARGET_COMMIT,
                "commit": TARGET_COMMIT,
                "apply_ref": TARGET_COMMIT,
            },
            "confirmed": True,
        },
    )
    _write_json(
        root / ".km-vms-source.json",
        {
            "schema_version": 1,
            "source_kind": "github-tarball",
            "github_repo": "kmishnev87/km-vms",
            "ref": "v0.7.18",
            "commit_sha": bridge.SOURCE_TAG_COMMITS["0.7.18"],
            "recorded_at": "2026-07-23T00:00:00Z",
        },
    )
    _write_json(
        root / "release/km-vms-release.json",
        {
            "schema_version": 1,
            "product": "KM VMS",
            "version": "0.7.25",
            "tag": "v0.7.25",
            "source_kind": "github-release",
            "source_repo": "kmishnev87/km-vms",
            "source_ref": "v0.7.25",
            "evidence_model": "semver_tag_resolves_to_commit",
            "commit_sha": None,
        },
    )


def _v2_admission(request_id: str) -> tuple[dict, dict]:
    submission_id = "fd52d9de-8df1-4902-af6c-371618e50469"
    request = {
        "schema_version": 2,
        "request_id": request_id,
        "submission_id": submission_id,
        "requested_at": "2026-07-25T07:31:22Z",
        "requested_by": {
            "user_id": 1,
            "username": "matrix_owner",
            "role": "owner",
            "ip_address": "192.0.2.1",
            "user_agent": "matrix",
        },
        "intent": "apply_update",
        "source": {
            "kind": "trusted_manifest",
            "channel": "stable",
            "version": "0.7.25",
            "commit": TARGET_COMMIT,
            "apply_ref": TARGET_COMMIT,
            "ref": TARGET_COMMIT,
            "repo": "kmishnev87/km-vms",
            "source_type": "github_tarball",
        },
        "apply_candidate": {
            "source": "trusted_snapshot",
            "snapshot": {
                "available": True,
                "fresh": True,
                "age_seconds": 0,
                "fresh_for_seconds": 900,
                "version": "0.7.25",
                "commit_short": TARGET_COMMIT[:12],
                "provider": "local_static_manifest",
            },
        },
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
    }
    admission = {
        "schema_version": 2,
        "document_type": "update_apply_admission",
        "current_submission_id": submission_id,
        "entries": [
            {
                "submission_id": submission_id,
                "request_id": request_id,
                "target_version": "0.7.25",
                "target_commit": TARGET_COMMIT,
                "requested_at": "2026-07-25T07:31:22Z",
                "updated_at": "2026-07-25T07:31:23Z",
                "state": "claimed",
                "request": request,
                "audit": {
                    "state": "confirmed",
                    "event_id": "event",
                    "confirmed_at": "2026-07-25T07:31:22Z",
                },
                "claimed_at": "2026-07-25T07:31:23Z",
                "terminal": None,
            }
        ],
        "updated_at": "2026-07-25T07:31:23Z",
    }
    return admission, request


def test_bridge_accepts_exact_v0724_claimed_admission_document() -> None:
    request_id = "update-" + ("6" * 32)
    admission, request = _v2_admission(request_id)

    assert (
        bridge.extract_active_request(
            admission,
            request_id=request_id,
        )
        == request
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("document_type", "update_apply_admission_lineage"),
        ("schema_version", True),
        ("state", "admitted_unclaimed"),
    ),
)
def test_bridge_rejects_wrong_v2_admission_discriminator_or_state(
    field: str,
    value: object,
) -> None:
    request_id = "update-" + ("6" * 32)
    admission, _request = _v2_admission(request_id)
    if field == "state":
        admission["entries"][0]["state"] = value
    else:
        admission[field] = value

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.extract_active_request(
            admission,
            request_id=request_id,
        )
    assert captured.value.code == "source_handoff_authority_invalid"


def test_post_overlay_bootstrap_reuses_valid_pre_overlay_identity(
    tmp_path: Path,
) -> None:
    request_id = "update-" + ("7" * 32)
    _fixture(tmp_path, request_id)
    bridge.capture_installed_source_identity(
        tmp_path,
        request_id=request_id,
    )
    identity_path = (
        tmp_path
        / "data/update-control/pre-overlay-source-identity.json"
    )
    original_identity = identity_path.read_bytes()

    source_path = tmp_path / ".km-vms-source.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["ref"] = TARGET_COMMIT
    source["commit_sha"] = TARGET_COMMIT
    _write_json(source_path, source)

    bridge.capture_installed_source_identity(
        tmp_path,
        request_id=request_id,
    )
    assert identity_path.read_bytes() == original_identity


def test_post_overlay_bootstrap_rejects_tampered_pre_overlay_identity(
    tmp_path: Path,
) -> None:
    request_id = "update-" + ("8" * 32)
    _fixture(tmp_path, request_id)
    bridge.capture_installed_source_identity(
        tmp_path,
        request_id=request_id,
    )
    identity_path = (
        tmp_path
        / "data/update-control/pre-overlay-source-identity.json"
    )
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["installed_commit"] = "9" * 40
    _write_json(identity_path, identity)

    with pytest.raises(
        bridge.BridgeError,
        match="Installed source handoff evidence is contradictory",
    ) as captured:
        bridge.capture_installed_source_identity(
            tmp_path,
            request_id=request_id,
        )
    assert captured.value.code == "source_handoff_conflict"


def test_legacy_completed_status_uses_bounded_updated_at_timestamp() -> None:
    request_id = "update-" + ("9" * 32)
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "status": "completed",
        "expected_commit": TARGET_COMMIT,
        "installed_commit": TARGET_COMMIT,
        "commit_verified": True,
        "updated_at": "2026-07-24T15:35:03.020000Z",
    }

    bridge.validate_completed_status(payload, request_id)

    payload["updated_at"] = "not-a-timestamp"
    with pytest.raises(bridge.BridgeError) as captured:
        bridge.validate_completed_status(payload, request_id)
    assert captured.value.code == "terminal_timestamp_invalid"


def test_archive_override_moves_recovery_mount_to_single_schema_runner(
    tmp_path: Path,
) -> None:
    manifest = {
        "schema_version": 1,
        "runtime_base": "/storage/archive-roots",
        "compose_override_file": "docker-compose.archive-roots.yml",
        "items": [
            {
                "root_id": "root-a",
                "user_display_path": "/Volume1/archive/root-a",
                "backend_runtime_path": "/storage/archive-roots/root-a",
                "physical_volume_id": "volume-a",
                "storage_namespace": "recordings",
                "active_write_target": True,
            }
        ],
        "raw_runtime_paths_user_visible": False,
    }
    control = tmp_path / "data/install-control"
    _write_json(control / "archive-roots-runtime.json", manifest)
    volume_lines = bridge._archive_volume_lines(manifest)
    intermediate = "\n".join(
        [
            "# Generated by KM VMS. Do not edit manually.",
            "services:",
            "  api:",
            "    volumes:",
            *volume_lines,
            "  operation-recovery:",
            "    volumes:",
            *volume_lines,
            "",
        ]
    )
    override = control / "docker-compose.archive-roots.yml"
    override.write_text(intermediate, encoding="utf-8")

    assert bridge.normalize_archive_roots_override(tmp_path) is True
    normalized = override.read_text(encoding="utf-8")
    assert "  schema-update:" in normalized
    assert "  operation-recovery:" not in normalized
    assert bridge.normalize_archive_roots_override(tmp_path) is False
