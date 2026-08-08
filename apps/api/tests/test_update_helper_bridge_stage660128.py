from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_refresh_records_target_validation_failure_before_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    (app_dir / "data/update-control").mkdir(parents=True)
    request_id = "update-" + ("a" * 32)
    expected_image_id = "sha256:" + ("b" * 64)
    args = SimpleNamespace(
        app_dir=str(app_dir),
        project_name="fixture",
        helper_image="fixture-helper:target",
        request_id=request_id,
        expected_image_id=expected_image_id,
        timeout_seconds=30,
        target_slot="release-" + TARGET_COMMIT,
    )
    monkeypatch.setattr(bridge, "require_app_dir", lambda _value: app_dir)
    monkeypatch.setattr(bridge, "bridge_source_root", lambda: app_dir)
    monkeypatch.setattr(
        bridge,
        "load_slot_engine",
        lambda _root: (_ for _ in ()).throw(
            bridge.BridgeError(
                "slot_mutable",
                "Published release slot is not immutable.",
            )
        ),
    )

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.refresh(args)

    assert captured.value.code == "slot_mutable"
    receipt = json.loads(
        (
            app_dir
            / "data/update-control/update-helper-refresh.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["request_id"] == request_id
    assert receipt["expected_image_id"] == expected_image_id
    assert receipt["status"] == "failed"
    assert receipt["message"] == "slot_mutable"


def test_reused_adopted_slot_ignores_only_historical_request_digests(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    slot_source = tmp_path / "slots" / ("adopted-" + ("a" * 64)) / "source"
    request_id = "update-" + ("b" * 32)
    current_contract = {
        "name": "fixture",
        "services": {
            "api": {
                "build": {"context": str(app)},
                "environment": {
                    "KM_VMS_UPDATE_CONTROL_REQUEST_ID": request_id,
                    "KEEP_SECURITY_RELEVANT": "yes",
                },
                "privileged": False,
                "volumes": [
                    {
                        "source": str(app / "data"),
                        "target": "/data",
                        "type": "bind",
                    },
                    {
                        "source": str(app / "release"),
                        "target": "/app/release",
                        "type": "bind",
                    },
                ],
            }
        },
    }
    slot_contract = json.loads(json.dumps(current_contract))
    slot_contract["services"]["api"]["build"]["context"] = str(slot_source)
    slot_contract["services"]["api"]["volumes"][1]["source"] = str(
        slot_source / "release"
    )
    normalized_current = bridge._normalize_current_compose_contract(
        current_contract,
        app_dir=app,
        source_dir=app,
        request_id=request_id,
    )
    normalized_slot = bridge._normalize_current_compose_contract(
        slot_contract,
        app_dir=app,
        source_dir=slot_source,
        request_id=request_id,
    )
    assert normalized_slot == normalized_current

    slot_contract["services"]["api"]["privileged"] = True
    assert (
        bridge._normalize_current_compose_contract(
            slot_contract,
            app_dir=app,
            source_dir=slot_source,
            request_id=request_id,
        )
        != normalized_current
    )

    current_compose = {
        "schema_version": 1,
        "project_name": "fixture",
        "project_directory": "source",
        "captured_plan_sha256": "1" * 64,
        "slot_plan_sha256": "2" * 64,
        "archive_override_attached": True,
        "archive_override_sha256": "3" * 64,
        "runtime_override_sha256": "4" * 64,
        "shared_root_contract": "stable_app_dir_v1",
        "services": ["api", "nginx", "recorder", "web"],
    }
    historical_compose = {
        **current_compose,
        "captured_plan_sha256": "5" * 64,
        "slot_plan_sha256": "6" * 64,
    }
    images = {
        "schema_version": 1,
        "services": {
            "api": {
                "image_id": "sha256:" + ("7" * 64),
                "source_image_ref": "fixture-api:latest",
                "immutable_image_ref": "fixture-api:adopted",
            }
        },
    }
    health = {
        "schema_version": 1,
        "status": "healthy",
        "api_visible_identity_sha256": "8" * 64,
        "core_services": ["api", "nginx", "recorder", "web"],
    }
    identity = {
        "installed_version": "0.8.3",
        "installed_commit": "9" * 40,
    }
    manifest = {
        "compose_evidence": historical_compose,
        "image_evidence": images,
        "pre_update_health": health,
        "declared_identity": {
            "version": identity["installed_version"],
            "commit": identity["installed_commit"],
        },
    }
    assert bridge._reused_adopted_evidence_matches(
        manifest,
        compose_evidence=current_compose,
        image_evidence=images,
        health_evidence=health,
        installed_identity=identity,
    )

    for field, replacement in (
        ("project_name", "other"),
        ("archive_override_sha256", "a" * 64),
        ("runtime_override_sha256", "b" * 64),
        ("services", ["api", "nginx", "web"]),
    ):
        changed = json.loads(json.dumps(current_compose))
        changed[field] = replacement
        assert not bridge._reused_adopted_evidence_matches(
            manifest,
            compose_evidence=changed,
            image_evidence=images,
            health_evidence=health,
            installed_identity=identity,
        )

    changed_images = json.loads(json.dumps(images))
    changed_images["services"]["api"]["image_id"] = "sha256:" + ("c" * 64)
    assert not bridge._reused_adopted_evidence_matches(
        manifest,
        compose_evidence=current_compose,
        image_evidence=changed_images,
        health_evidence=health,
        installed_identity=identity,
    )
    changed_health = {**health, "status": "unhealthy"}
    assert not bridge._reused_adopted_evidence_matches(
        manifest,
        compose_evidence=current_compose,
        image_evidence=images,
        health_evidence=changed_health,
        installed_identity=identity,
    )
    assert not bridge._reused_adopted_evidence_matches(
        manifest,
        compose_evidence=current_compose,
        image_evidence=images,
        health_evidence=health,
        installed_identity={**identity, "installed_commit": "d" * 40},
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
            "preflight_required": True,
            "status_path": "data/update-control/update-status.json",
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


def _v3_request(request_id: str) -> dict:
    _admission, request = _v2_admission(request_id)
    return {
        **request,
        "schema_version": 3,
        "document_type": "update_apply_request",
        "updated_at": "2026-07-25T07:31:23Z",
        "state": "claimed",
        "claimed_at": "2026-07-25T07:31:23Z",
        "terminal": None,
        "audit_event_id": "48e9d399-84a8-5385-b362-aac2101f3489",
    }


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


def test_bridge_projects_current_single_request_to_schema_control_shape() -> None:
    request_id = "update-" + ("a" * 32)
    current = _v3_request(request_id)

    projected = bridge.extract_active_request(current, request_id=request_id)

    assert projected["schema_version"] == 2
    assert set(projected) == bridge.NORMALIZED_REQUEST_FIELDS
    assert projected["request_id"] == request_id
    assert projected["submission_id"] == current["submission_id"]
    assert "state" not in projected
    assert "audit_event_id" not in projected


@pytest.mark.parametrize("with_snapshot", (False, True))
def test_bridge_accepts_only_published_schema_v1_request_shapes(
    with_snapshot: bool,
) -> None:
    request_id = "update-" + ("b" * 32)
    request = {
        "schema_version": 1,
        "request_id": request_id,
        "requested_at": "2026-07-24T00:00:00Z",
        "requested_by": {"user_id": "1", "role": "owner"},
        "intent": "apply_update",
        "source": _v2_admission(request_id)[1]["source"],
        "confirmed": True,
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
    }
    if with_snapshot:
        request["apply_candidate"] = _v2_admission(request_id)[1][
            "apply_candidate"
        ]

    assert (
        bridge.extract_active_request(request, request_id=request_id)
        == request
    )

    minimal = {
        key: value
        for key, value in request.items()
        if key not in {"requested_by", "preflight_required", "status_path", "apply_candidate"}
    }
    with pytest.raises(bridge.BridgeError) as captured:
        bridge.extract_active_request(minimal, request_id=request_id)
    assert captured.value.code == "source_handoff_authority_invalid"


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


def test_pre_overlay_handoff_uses_staged_target_descriptor(
    tmp_path: Path,
) -> None:
    request_id = "update-" + ("c" * 32)
    _fixture(tmp_path, request_id)
    staged = tmp_path / "staged-target"
    staged_release = json.loads(
        (tmp_path / "release/km-vms-release.json").read_text(
            encoding="utf-8"
        )
    )
    _write_json(
        staged / "release/km-vms-release.json",
        staged_release,
    )
    installed_release = dict(staged_release)
    installed_release["version"] = "0.7.24"
    installed_release["tag"] = "v0.7.24"
    installed_release["source_ref"] = "v0.7.24"
    _write_json(
        tmp_path / "release/km-vms-release.json",
        installed_release,
    )

    bridge.capture_installed_source_identity(
        tmp_path,
        request_id=request_id,
        target_source_dir=staged,
    )

    identity = json.loads(
        (
            tmp_path
            / "data/update-control/pre-overlay-source-identity.json"
        ).read_text(encoding="utf-8")
    )
    request = json.loads(
        (
            tmp_path
            / "data/update-control/schema-update-request.json"
        ).read_text(encoding="utf-8")
    )
    assert identity["installed_version"] == "0.7.18"
    assert request["request_id"] == request_id


def test_legacy_handoff_binds_adopted_slot_as_active_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_id = "update-" + ("e" * 32)
    _fixture(tmp_path, request_id)
    staged = tmp_path / "staged-target"
    _write_json(
        staged / "release/km-vms-release.json",
        json.loads(
            (tmp_path / "release/km-vms-release.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    adopted_slot = "adopted-" + ("f" * 64)

    class Engine:
        active = None

        def read_active_slot(self, app_dir: Path):
            if self.active is None:
                return None
            return (
                self.active,
                app_dir
                / "data/update-runtime/slots"
                / self.active
                / "source",
            )

        def atomic_switch_pointer(self, _app_dir: Path, slot_id: str):
            self.active = slot_id

    engine = Engine()
    monkeypatch.setattr(bridge, "require_app_dir", lambda _value: tmp_path)
    monkeypatch.setattr(
        bridge,
        "normalize_archive_roots_override",
        lambda _app_dir: False,
    )
    monkeypatch.setattr(bridge, "load_slot_engine", lambda _source: engine)
    monkeypatch.setattr(
        bridge,
        "capture_installed_source_identity",
        lambda *_args, **_kwargs: {
            "installed_version": "0.7.18",
            "installed_commit": "1" * 40,
        },
    )
    monkeypatch.setattr(
        bridge,
        "prepare_legacy_adopted_slot",
        lambda **_kwargs: adopted_slot,
    )

    result = bridge.handoff(
        SimpleNamespace(
            app_dir=str(tmp_path),
            target_source_dir=str(staged),
            request_id=request_id,
            project_name="fixture",
            terminal=False,
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert engine.active == adopted_slot
    assert "handoff_kind=legacy_adoption" in output
    assert f"previous_slot={adopted_slot}" in output


def test_legacy_adoption_does_not_overwrite_a_different_active_pointer(
    tmp_path: Path,
) -> None:
    adopted_slot = "adopted-" + ("a" * 64)
    different_slot = "release-" + ("b" * 40)

    class Engine:
        def read_active_slot(self, app_dir: Path):
            return different_slot, app_dir / "different-source"

        def atomic_switch_pointer(self, _app_dir: Path, _slot_id: str):
            raise AssertionError("a different active pointer must not be overwritten")

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.bind_legacy_adopted_slot_as_active(
            app_dir=tmp_path,
            slot_id=adopted_slot,
            engine=Engine(),
        )

    assert captured.value.code == "activation_previous_binding_mismatch"


def test_terminal_handoff_writes_bounded_schema_request_without_api_admission(
    tmp_path: Path,
) -> None:
    request_id = "update-" + ("d" * 32)
    _fixture(tmp_path, request_id)
    (tmp_path / "data/update-control/update-request.json").unlink()
    terminal_request = {
        "schema_version": 1,
        "request_id": request_id,
        "requested_at": "2026-07-28T00:00:00Z",
        "intent": "apply_update",
        "confirmed": True,
        "source": {
            "version": "0.7.25",
            "commit": TARGET_COMMIT,
        },
    }

    identity = bridge.capture_installed_source_identity(
        tmp_path,
        request_id=request_id,
        request_override=terminal_request,
    )

    written = json.loads(
        (
            tmp_path
            / "data/update-control/schema-update-request.json"
        ).read_text(encoding="utf-8")
    )
    assert identity["installed_version"] == "0.7.18"
    assert written == terminal_request
    assert not (
        tmp_path / "data/update-control/update-request.json"
    ).exists()


def test_bridge_prefers_candidate_lineage_over_stale_installed_cwd(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    candidate = tmp_path / "candidate"
    candidate_bridge = candidate / "scripts/km-vms-update-helper-bridge.py"
    candidate_bridge.parent.mkdir(parents=True)
    candidate_bridge.write_bytes(BRIDGE_PATH.read_bytes())

    lineage_path = BRIDGE_PATH.parents[1] / "release/km-vms-update-lineage.json"
    lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    latest_version = list(lineage["tag_commits"])[-1]
    _write_json(candidate / "release/km-vms-update-lineage.json", lineage)

    stale_lineage = json.loads(json.dumps(lineage))
    for field in ("tag_commits", "schema_versions", "shape_fingerprints"):
        stale_lineage[field].pop(latest_version)
    stale_lineage["shape_alternates"].pop(latest_version, None)
    _write_json(installed / "release/km-vms-update-lineage.json", stale_lineage)

    probe = "\n".join(
        [
            "import importlib.util",
            f"spec = importlib.util.spec_from_file_location('candidate_bridge', {str(candidate_bridge)!r})",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            f"print(module.SOURCE_TAG_COMMITS.get({latest_version!r}, 'MISSING'))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=installed,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == lineage["tag_commits"][latest_version]


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


def test_slot_compose_uses_stable_env_exact_source_and_both_overrides(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    # Use a normal bounded source path; the exact slot ID is validated by the
    # slot owner, while compose_base only assembles already-resolved paths.
    source = (
        app
        / "data/update-runtime/slots"
        / ("release-" + ("a" * 40))
        / "source"
    )
    source.mkdir(parents=True)
    (app / ".env").write_text("COMPOSE_PROJECT_NAME=fixture\n", encoding="utf-8")
    (source / "docker-compose.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    runtime_override = source.parent / "docker-compose.runtime-override.yml"
    runtime_override.write_text("services: {}\n", encoding="utf-8")
    archive_override = (
        app / "data/install-control/docker-compose.archive-roots.yml"
    )
    archive_override.parent.mkdir(parents=True)
    archive_override.write_text("services: {}\n", encoding="utf-8")

    command = bridge.compose_base(
        app,
        "fixture",
        source_dir=source,
    )

    assert command[:4] == [
        "docker",
        "compose",
        "--env-file",
        str(app / ".env"),
    ]
    assert command[command.index("--project-directory") + 1] == str(source)
    assert command.index(str(runtime_override)) < command.index(
        str(archive_override)
    )


def test_slot_image_alias_is_exact_and_conflict_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot_id = "adopted-" + ("b" * 64)
    image_id = "sha256:" + ("c" * 64)
    immutable_ref = f"km-vms-fixture-slot-api:{slot_id}"
    aliases: dict[str, str] = {}

    def fake_run(args, **kwargs):
        command = list(args)
        if command[:3] == ["docker", "image", "inspect"]:
            ref = command[-1]
            if ref in aliases:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=aliases[ref] + "\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="missing",
            )
        if command[:3] == ["docker", "image", "tag"]:
            aliases[command[-1]] = command[-2]
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="",
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr(bridge, "run_command", fake_run)
    evidence = bridge.preserve_slot_images(
        {
            "schema_version": 1,
            "services": {
                "api": {
                    "image_id": image_id,
                    "source_image_ref": "fixture-api:latest",
                }
            },
        },
        project_name="fixture",
        slot_id=slot_id,
    )
    assert aliases[immutable_ref] == image_id
    assert (
        evidence["services"]["api"]["immutable_image_ref"]
        == immutable_ref
    )

    aliases[immutable_ref] = "sha256:" + ("d" * 64)
    with pytest.raises(bridge.BridgeError) as captured:
        bridge.preserve_slot_images(
            {
                "schema_version": 1,
                "services": {
                    "api": {
                        "image_id": image_id,
                        "source_image_ref": "fixture-api:latest",
                    }
                },
            },
            project_name="fixture",
            slot_id=slot_id,
        )
    assert captured.value.code == "slot_image_alias_conflict"


def _activation_args(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "app_dir": str(tmp_path),
        "project_name": "fixture",
        "request_id": "update-" + ("a" * 32),
        "previous_slot": "adopted-" + ("b" * 64),
        "target_slot": "release-" + TARGET_COMMIT,
        "target_commit": TARGET_COMMIT,
        "target_version": "0.8.5",
        "terminal": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _ActivationEngine:
    def __init__(self, args: SimpleNamespace, events: list[str]):
        self.args = args
        self.events = events
        self.journal = None
        self.target_binding = {
            "slot_id": args.target_slot,
            "commit": args.target_commit,
            "version": args.target_version,
        }

    def read_activation_journal(self, _app_dir, *, missing_ok):
        assert missing_ok is True
        return self.journal

    def require_slot_id(self, value, *, target=False):
        self.events.append("normalize_target" if target else "normalize_previous")
        return str(value).lower()

    def read_active_slot(self, app_dir):
        self.events.append("read_active")
        return self.args.previous_slot, app_dir / "active-source"

    def build_activation_slot_binding(self, _app_dir, _slot_id):
        self.events.append("build_target_binding")
        return dict(self.target_binding)

    def initialize_activation_journal(self, _app_dir, **kwargs):
        self.events.append("initialize_journal")
        return {
            "phase": "target_prepared",
            "request_id": kwargs["request_id"],
            "previous": kwargs["previous"],
            "target": kwargs["target"],
            "failure_category": None,
        }


def _install_activation_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    engine: _ActivationEngine,
    events: list[str],
) -> None:
    monkeypatch.setattr(bridge, "require_app_dir", lambda _value: tmp_path)
    monkeypatch.setattr(bridge, "load_slot_engine", lambda _root: engine)
    monkeypatch.setattr(bridge, "bridge_source_root", lambda: tmp_path)
    monkeypatch.setattr(
        bridge,
        "capture_slot_runtime_binding",
        lambda _app, _project, slot_id, **_kwargs: (
            events.append("capture_previous")
            or {"slot_id": slot_id, "runtime": "verified"}
        ),
    )
    monkeypatch.setattr(
        bridge,
        "run_target_schema_preflight",
        lambda _app, _project, _slot, **_kwargs: (
            events.append("schema_preflight")
            or {
                "compatibility_sha256": "c" * 64,
                "source_schema_version": 7,
                "target_schema_version": 7,
                "migration_required": False,
            }
        ),
    )
    monkeypatch.setattr(
        bridge,
        "write_activation_progress",
        lambda *_args, **_kwargs: events.append("write_progress"),
    )
    monkeypatch.setattr(
        bridge,
        "converge_activation",
        lambda _engine, _app, _project, request_id, **_kwargs: (
            events.append("converge")
            or {
                "phase": "completed",
                "request_id": request_id,
                "previous": {
                    "slot_id": engine.args.previous_slot,
                },
                "target": {"slot_id": engine.args.target_slot},
                "failure_category": None,
            }
        ),
    )


def test_activation_rejects_target_identity_before_any_runtime_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    args = _activation_args(tmp_path)
    engine = _ActivationEngine(args, events)
    engine.target_binding["commit"] = "d" * 40
    _install_activation_mocks(monkeypatch, tmp_path, engine, events)

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.activate_or_resume(args)

    assert captured.value.code == "activation_target_identity_mismatch"
    assert "schema_preflight" not in events
    assert "capture_previous" not in events
    assert "initialize_journal" not in events


def test_activation_checks_bindings_before_preflight_and_journals_exact_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    args = _activation_args(tmp_path)
    engine = _ActivationEngine(args, events)
    _install_activation_mocks(monkeypatch, tmp_path, engine, events)

    result = bridge.activate_or_resume(args)

    assert result == 0
    assert events.index("build_target_binding") < events.index("schema_preflight")
    assert events.index("schema_preflight") < events.index("initialize_journal")
    assert events.count("capture_previous") == 2
    assert events.count("build_target_binding") == 2
    assert events[-1] == "converge"


def test_activation_blocks_if_binding_changes_during_schema_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    args = _activation_args(tmp_path)
    engine = _ActivationEngine(args, events)
    calls = {"count": 0}

    def changing_binding(_app_dir, _slot_id):
        events.append("build_target_binding")
        calls["count"] += 1
        binding = dict(engine.target_binding)
        if calls["count"] == 2:
            binding["version"] = "0.8.6"
        return binding

    engine.build_activation_slot_binding = changing_binding
    _install_activation_mocks(monkeypatch, tmp_path, engine, events)

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.activate_or_resume(args)

    assert captured.value.code == "activation_slot_binding_changed"
    assert "schema_preflight" in events
    assert "initialize_journal" not in events


def test_activation_resume_rejects_conflicting_supplied_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    args = _activation_args(tmp_path, target_commit="d" * 40)
    engine = _ActivationEngine(args, events)
    engine.journal = {
        "phase": "target_prepared",
        "request_id": args.request_id,
        "previous": {"slot_id": args.previous_slot},
        "target": {
            "slot_id": args.target_slot,
            "commit": TARGET_COMMIT,
            "version": args.target_version,
        },
    }
    _install_activation_mocks(monkeypatch, tmp_path, engine, events)

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.activate_or_resume(args)

    assert captured.value.code == "activation_journal_conflict"
    assert "schema_preflight" not in events
    assert "converge" not in events
