from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts/km-vms-release-slots.py"
SPEC = importlib.util.spec_from_file_location(
    "stage661_release_slots",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
slots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(slots)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
LEGACY_COMMIT = "1" * 40
VERSION = "0.8.3"
SERVICES = sorted(
    [
        "api",
        "nginx",
        "postgres",
        "recorder",
        "redis",
        "setup-helper",
        "update-helper",
        "web",
    ]
)
TARGET_SERVICES = sorted(
    [
        *SERVICES,
        "schema-update",
        "update-helper-bootstrap",
        "update-retry-admission",
        "update-status-reader",
    ]
)


def _write(path: Path, value: str = "fixture\n", mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, mode)


def _source_fixture(
    root: Path,
    *,
    commit: str,
    version: str,
    trusted: bool,
) -> None:
    for relative in (
        "apps/api/app.py",
        "apps/web/page.js",
        "apps/recorder/main.py",
        "apps/update-helper/Dockerfile",
        "deploy/nginx/default.conf",
        "scripts/install.sh",
        "scripts/km-vms-compose-common.sh",
        "scripts/update.sh",
        "docker-compose.yml",
    ):
        _write(root / relative)
    os.chmod(root / "scripts/install.sh", 0o755)
    os.chmod(root / "scripts/update.sh", 0o755)
    os.chmod(root / "scripts/km-vms-compose-common.sh", 0o755)
    descriptor = {
        "schema_version": 1,
        "product": "KM VMS",
        "version": version,
        "tag": f"v{version}",
        "title": "Fixture",
        "summary": "Fixture",
        "release_channel": "stable",
        "source_kind": "github-release",
        "source_repo": "example/km-vms",
        "source_ref": f"v{version}",
        "commit_sha": commit if trusted else None,
    }
    _write(
        root / "release/km-vms-release.json",
        json.dumps(descriptor, sort_keys=True) + "\n",
    )
    if trusted:
        for relative in (
            "scripts/km-vms-permission-gate.sh",
            "scripts/km-vms-update-helper-bridge.py",
            "scripts/km-vms-release-slots.py",
        ):
            _write(root / relative, mode=0o755)


def _stable_app(tmp_path: Path, *, legacy: bool = False) -> Path:
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    _write(app / ".env", "COMPOSE_PROJECT_NAME=fixture\n")
    if legacy:
        _source_fixture(
            app,
            commit=LEGACY_COMMIT,
            version="0.7.18",
            trusted=False,
        )
        _write(
            app / ".km-vms-release.json",
            json.dumps({"commit_sha": LEGACY_COMMIT}) + "\n",
        )
        _write(
            app / ".km-vms-source.json",
            json.dumps({"commit_sha": LEGACY_COMMIT}) + "\n",
        )
    return app


def test_target_slot_excludes_runtime_secrets_but_keeps_product_code(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path)
    source = tmp_path / "source"
    _source_fixture(
        source,
        commit=COMMIT_A,
        version=VERSION,
        trusted=True,
    )
    blocked = (
        "apps/.env",
        "apps/.ssh/id_rsa",
        "apps/api/auth-token.txt",
        "docs/private.p12",
        "scripts/credentials.txt",
        "scripts/cache.sqlite",
        "apps/web/node_modules/package/index.js",
    )
    for relative in blocked:
        _write(source / relative, "do-not-stage\n")
    allowed = (
        "apps/api/secret_policy.py",
        "apps/api/token_serializer.py",
        "docs/images/update-flow.png",
        ".env.example",
    )
    for relative in allowed:
        _write(source / relative, "product\n")

    staged = slots.stage_target(
        app,
        source,
        request_id="update-" + ("9" * 32),
        trusted_commit=COMMIT_A,
        declared_version=VERSION,
    )
    staged_source = Path(staged["source_path"])

    for relative in blocked:
        assert not (staged_source / relative).exists()
    for relative in allowed:
        assert (staged_source / relative).is_file()


def _compose_evidence(
    *,
    services: list[str],
    runtime_digest: str | None,
    suffix: str,
) -> dict:
    return {
        "schema_version": 1,
        "project_name": "fixture",
        "project_directory": "source",
        "captured_plan_sha256": suffix * 64,
        "slot_plan_sha256": chr(ord(suffix) + 1) * 64,
        "archive_override_attached": True,
        "archive_override_sha256": "e" * 64,
        "runtime_override_sha256": runtime_digest,
        "shared_root_contract": "stable_app_dir_v1",
        "services": services,
    }


def _image_evidence(slot_id: str, *, target: bool) -> dict:
    required = (
        slots.TARGET_REQUIRED_IMAGE_SERVICES
        if target
        else slots.ADOPTED_REQUIRED_IMAGE_SERVICES
    )
    services = {}
    for index, service in enumerate(sorted(required), start=1):
        if target and service in slots.TARGET_BUILT_IMAGE_SERVICES:
            source_ref = f"fixture/{service}:{slot_id}"
        else:
            source_ref = f"fixture/{service}:current"
        services[service] = {
            "image_id": "sha256:" + f"{index:064x}",
            "source_image_ref": source_ref,
            "immutable_image_ref": f"km-vms-fixture-slot-{service}:{slot_id}",
        }
    return {"schema_version": 1, "services": services}


def _health() -> dict:
    return {
        "schema_version": 1,
        "status": "healthy",
        "api_visible_identity_sha256": "f" * 64,
        "core_services": ["api", "nginx", "recorder", "web"],
    }


def _finalize_target(
    app: Path,
    source: Path,
    *,
    request_id: str,
    commit: str,
) -> dict:
    staged = slots.stage_target(
        app,
        source,
        request_id=request_id,
        trusted_commit=commit,
        declared_version=VERSION,
    )
    return slots.finalize_candidate(
        app,
        request_id=request_id,
        compose_evidence=_compose_evidence(
            services=TARGET_SERVICES,
            runtime_digest=None,
            suffix="a",
        ),
        image_evidence=_image_evidence(staged["slot_id"], target=True),
    )


def _finalize_adopted(app: Path, *, request_id: str) -> dict:
    staged = slots.stage_adopted(
        app,
        request_id=request_id,
        declared_version="0.7.18",
        declared_commit=LEGACY_COMMIT,
    )
    runtime = slots.prepare_adopted_runtime_override(
        app,
        request_id=request_id,
        services=SERVICES,
    )
    return slots.finalize_candidate(
        app,
        request_id=request_id,
        compose_evidence=_compose_evidence(
            services=SERVICES,
            runtime_digest=runtime["sha256"],
            suffix="b",
        ),
        image_evidence=_image_evidence(staged["slot_id"], target=False),
        health_evidence=_health(),
    )


def test_trusted_target_id_is_server_derived_and_partial_never_publishes(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path)
    source = tmp_path / "target"
    _source_fixture(source, commit=COMMIT_A, version=VERSION, trusted=True)

    assert slots.trusted_slot_id(COMMIT_A) == f"release-{COMMIT_A}"
    with pytest.raises(slots.SlotError):
        slots.trusted_slot_id("../../target")
    with pytest.raises(slots.SlotError):
        slots.require_slot_id(
            f"adopted-{'2' * 64}",
            target=True,
        )

    staged = slots.stage_target(
        app,
        source,
        request_id="update-" + ("1" * 32),
        trusted_commit=COMMIT_A,
        declared_version=VERSION,
    )
    final_path = (
        app / "data/update-runtime/slots" / staged["slot_id"]
    )
    assert staged["status"] == "staged"
    assert not final_path.exists()
    assert not (
        Path(staged["candidate_path"]) / slots.MANIFEST_NAME
    ).exists()
    assert slots.read_active_slot(app) is None


def test_target_publication_is_immutable_and_has_no_mutable_role(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path)
    source = tmp_path / "target"
    _source_fixture(source, commit=COMMIT_A, version=VERSION, trusted=True)
    result = _finalize_target(
        app,
        source,
        request_id="update-" + ("2" * 32),
        commit=COMMIT_A,
    )

    slot_root = Path(result["slot_path"])
    manifest = result["manifest"]
    assert result["status"] == "published"
    assert manifest["kind"] == "trusted_release"
    assert manifest["official_source_match"] is True
    assert not {
        "status",
        "current",
        "previous",
        "candidate",
        "active",
    } & set(manifest)
    assert not (slot_root / slots.RUNTIME_OVERRIDE_NAME).exists()
    for current, dirnames, filenames in os.walk(slot_root):
        for name in [*dirnames, *filenames]:
            assert not (
                stat.S_IMODE((Path(current) / name).lstat().st_mode)
                & 0o222
            )
    assert slots.validate_slot(
        slot_root,
        expected_slot_id=f"release-{COMMIT_A}",
    ) == manifest


def test_legacy_source_without_new_module_becomes_exact_adopted_slot(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path, legacy=True)
    env_before = (app / ".env").read_bytes()
    data_sentinel = app / "data/sentinel"
    _write(data_sentinel, "unchanged\n")
    inventory_before = slots.product_inventory(
        app,
        required_paths=slots.LEGACY_REQUIRED_SOURCE_PATHS,
    )

    result = _finalize_adopted(
        app,
        request_id="update-" + ("3" * 32),
    )
    manifest = result["manifest"]
    slot_root = Path(result["slot_path"])
    runtime_override = (
        slot_root / slots.RUNTIME_OVERRIDE_NAME
    ).read_text(encoding="utf-8")

    assert manifest["kind"] == "adopted_pre_update_snapshot"
    assert manifest["official_source_match"] is False
    assert manifest["source_inventory"] == inventory_before
    assert (app / ".env").read_bytes() == env_before
    assert data_sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert not (slot_root / "source/.env").exists()
    assert str(app / "data/postgres") in runtime_override
    assert str(slot_root / "source/release") in runtime_override
    assert str(slot_root / "source/.km-vms-release.json") in runtime_override
    assert slots.read_active_slot(app) is None

    reused = slots.stage_adopted(
        app,
        request_id="update-" + ("4" * 32),
        declared_version="0.7.18",
        declared_commit=LEGACY_COMMIT,
    )
    assert reused["status"] == "reused"
    assert reused["slot_id"] == manifest["slot_id"]
    assert reused["manifest"] == manifest


def test_adoption_failure_leaves_legacy_source_and_pointer_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _stable_app(tmp_path, legacy=True)
    compose_before = (app / "docker-compose.yml").read_bytes()
    original_copy = slots._copy_product_source

    def mutate_after_copy(source: Path, destination: Path) -> None:
        original_copy(source, destination)
        _write(source / "apps/api/changed.py", "changed\n")

    monkeypatch.setattr(slots, "_copy_product_source", mutate_after_copy)
    with pytest.raises(
        slots.SlotError,
        match="not stable during snapshot materialization",
    ):
        slots.stage_adopted(
            app,
            request_id="update-" + ("5" * 32),
            declared_version="0.7.18",
            declared_commit=LEGACY_COMMIT,
        )
    assert slots.read_active_slot(app) is None
    assert (app / "docker-compose.yml").read_bytes() == compose_before
    assert not any(
        (app / "data/update-runtime/slots").iterdir()
    )


@pytest.mark.parametrize(
    ("images", "health"),
    (
        ({"schema_version": 1, "services": {}}, _health()),
        (None, {"schema_version": 1, "status": "failed"}),
    ),
)
def test_missing_image_or_health_evidence_blocks_before_pointer(
    tmp_path: Path,
    images: dict | None,
    health: dict,
) -> None:
    app = _stable_app(tmp_path, legacy=True)
    request_id = "update-" + ("6" * 32)
    staged = slots.stage_adopted(
        app,
        request_id=request_id,
        declared_version="0.7.18",
        declared_commit=LEGACY_COMMIT,
    )
    runtime = slots.prepare_adopted_runtime_override(
        app,
        request_id=request_id,
        services=SERVICES,
    )
    with pytest.raises(slots.SlotError):
        slots.finalize_candidate(
            app,
            request_id=request_id,
            compose_evidence=_compose_evidence(
                services=SERVICES,
                runtime_digest=runtime["sha256"],
                suffix="c",
            ),
            image_evidence=(
                images
                if images is not None
                else _image_evidence(staged["slot_id"], target=False)
            ),
            health_evidence=health,
        )
    assert slots.read_active_slot(app) is None
    assert not (
        app / "data/update-runtime/slots" / staged["slot_id"]
    ).exists()


def test_atomic_pointer_and_journal_roles_never_rewrite_slot_manifests(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path, legacy=True)
    adopted = _finalize_adopted(
        app,
        request_id="update-" + ("7" * 32),
    )
    target_source = tmp_path / "target"
    _source_fixture(
        target_source,
        commit=COMMIT_A,
        version=VERSION,
        trusted=True,
    )
    target = _finalize_target(
        app,
        target_source,
        request_id="update-" + ("8" * 32),
        commit=COMMIT_A,
    )
    adopted_manifest = (
        Path(adopted["slot_path"]) / slots.MANIFEST_NAME
    ).read_bytes()
    target_manifest = (
        Path(target["slot_path"]) / slots.MANIFEST_NAME
    ).read_bytes()

    slots.atomic_switch_pointer(app, adopted["slot_id"])
    assert slots.read_active_slot(app)[0] == adopted["slot_id"]
    request_id = "update-" + ("6" * 32)
    slots.initialize_activation_journal(
        app,
        request_id=request_id,
        previous=slots.build_activation_slot_binding(
            app,
            adopted["slot_id"],
        ),
        target=slots.build_activation_slot_binding(
            app,
            target["slot_id"],
        ),
        compatibility_sha256="0" * 64,
        source_schema_version=0,
        target_schema_version=0,
        migration_required=False,
    )
    slots.transition_activation_journal(
        app,
        request_id=request_id,
        phase="activating",
        pointer_slot_id=adopted["slot_id"],
        record_pointer=True,
    )
    slots.atomic_switch_pointer(app, target["slot_id"])
    assert slots.read_active_slot(app)[0] == target["slot_id"]
    slots.transition_activation_journal(
        app,
        request_id=request_id,
        phase="verifying_target",
        pointer_slot_id=target["slot_id"],
        record_pointer=True,
    )

    assert (
        Path(adopted["slot_path"]) / slots.MANIFEST_NAME
    ).read_bytes() == adopted_manifest
    assert (
        Path(target["slot_path"]) / slots.MANIFEST_NAME
    ).read_bytes() == target_manifest
    assert slots.protected_slot_ids(app) == {
        adopted["slot_id"],
        target["slot_id"],
    }


def test_cleanup_requires_terminal_evidence_and_preserves_pointer_journal_slots(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path)
    sources = []
    results = []
    for marker, commit in zip(("9", "a"), (COMMIT_A, COMMIT_B)):
        source = tmp_path / f"target-{marker}"
        _source_fixture(source, commit=commit, version=VERSION, trusted=True)
        sources.append(source)
        results.append(
            _finalize_target(
                app,
                source,
                request_id="update-" + (marker * 32),
                commit=commit,
            )
        )
    slots.atomic_switch_pointer(app, results[0]["slot_id"])
    slots.initialize_activation_journal(
        app,
        request_id="update-" + ("c" * 32),
        previous=slots.build_activation_slot_binding(
            app,
            results[0]["slot_id"],
        ),
        target=slots.build_activation_slot_binding(
            app,
            results[1]["slot_id"],
        ),
        compatibility_sha256="1" * 64,
        source_schema_version=0,
        target_schema_version=0,
        migration_required=False,
    )
    with pytest.raises(
        slots.SlotError,
        match="terminal operation evidence",
    ):
        slots.cleanup_unprotected_slots(
            app,
            retain_slot_ids=set(),
        )
    assert (
        slots.cleanup_unprotected_slots(
            app,
            retain_slot_ids=set(),
            terminal_evidence=True,
        )
        == []
    )
    assert all(Path(item["slot_path"]).is_dir() for item in results)

    slots.atomic_write_json(
        app / slots.JOURNAL_RELATIVE,
        {"target_slot_id": results[1]["slot_id"]},
    )
    with pytest.raises(
        slots.SlotError,
        match="Activation journal is invalid",
    ):
        slots.cleanup_unprotected_slots(
            app,
            retain_slot_ids=set(),
            terminal_evidence=True,
        )


def test_terminal_request_staging_cleanup_is_exact_and_bounded(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path)
    layout = slots.ensure_layout(app)
    request_id = "update-" + ("7" * 32)
    other_request_id = "update-" + ("8" * 32)
    request_root = layout["staging"] / request_id
    other_request_root = layout["staging"] / other_request_id
    _write(request_root / "candidate" / "partial.txt")
    _write(other_request_root / "candidate" / "keep.txt")

    with pytest.raises(
        slots.SlotError,
        match="terminal operation evidence",
    ):
        slots.cleanup_request_staging(
            app,
            request_id=request_id,
        )

    assert slots.cleanup_request_staging(
        app,
        request_id=request_id,
        terminal_evidence=True,
    )
    assert not request_root.exists()
    assert (other_request_root / "candidate" / "keep.txt").is_file()
    assert not slots.cleanup_request_staging(
        app,
        request_id=request_id,
        terminal_evidence=True,
    )


def test_terminal_activation_allows_one_later_generation(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path)
    results = []
    for marker, commit in (("d", COMMIT_A), ("e", COMMIT_B)):
        source = tmp_path / f"generation-target-{marker}"
        _source_fixture(
            source,
            commit=commit,
            version=VERSION,
            trusted=True,
        )
        results.append(
            _finalize_target(
                app,
                source,
                request_id="update-" + (marker * 32),
                commit=commit,
            )
        )
    slots.atomic_switch_pointer(app, results[0]["slot_id"])
    first_request = "update-" + ("f" * 32)
    first = slots.initialize_activation_journal(
        app,
        request_id=first_request,
        previous=slots.build_activation_slot_binding(
            app,
            results[0]["slot_id"],
        ),
        target=slots.build_activation_slot_binding(
            app,
            results[1]["slot_id"],
        ),
        compatibility_sha256="2" * 64,
        source_schema_version=8,
        target_schema_version=8,
        migration_required=False,
    )
    slots.transition_activation_journal(
        app,
        request_id=first_request,
        phase="activating",
        pointer_slot_id=results[0]["slot_id"],
        record_pointer=True,
    )
    slots.atomic_switch_pointer(app, results[1]["slot_id"])
    slots.transition_activation_journal(
        app,
        request_id=first_request,
        phase="verifying_target",
        pointer_slot_id=results[1]["slot_id"],
        record_pointer=True,
    )
    slots.transition_activation_journal(
        app,
        request_id=first_request,
        phase="committing_target",
        pointer_slot_id=results[1]["slot_id"],
        record_pointer=True,
        target_verified=True,
    )
    terminal = slots.transition_activation_journal(
        app,
        request_id=first_request,
        phase="completed",
        pointer_slot_id=results[1]["slot_id"],
        record_pointer=True,
        target_verified=True,
    )
    assert first["generation"] == terminal["generation"] == 1

    second = slots.initialize_activation_journal(
        app,
        request_id="update-" + ("0" * 32),
        previous=slots.build_activation_slot_binding(
            app,
            results[1]["slot_id"],
        ),
        target=slots.build_activation_slot_binding(
            app,
            results[0]["slot_id"],
        ),
        compatibility_sha256="3" * 64,
        source_schema_version=8,
        target_schema_version=8,
        migration_required=False,
    )
    assert second["generation"] == 2
    assert second["phase"] == "target_prepared"


def test_stage_b_exposes_no_activation_cli_and_stable_compose_paths() -> None:
    parser = slots._build_parser()
    subparser = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "activate" not in subparser.choices
    assert "rollback" not in subparser.choices
    assert "switch" not in subparser.choices

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    common = (
        ROOT / "scripts/km-vms-compose-common.sh"
    ).read_text(encoding="utf-8")
    install = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert "${KM_VMS_HOST_APP_DIR:?KM_VMS_HOST_APP_DIR is required}/data/postgres" in compose
    assert "${KM_VMS_HOST_APP_DIR:?KM_VMS_HOST_APP_DIR is required}/data/redis" in compose
    assert "docker-compose.runtime-override.yml" in common
    assert "python3 is required for the release-slot layout foundation" not in install
    assert "activation_cli_enabled" in MODULE_PATH.read_text(encoding="utf-8")


def test_posix_resolver_handles_legacy_root_and_one_complete_active_slot(
    tmp_path: Path,
) -> None:
    app = _stable_app(tmp_path)
    common = ROOT / "scripts/km-vms-compose-common.sh"

    def resolve() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "sh",
                "-c",
                '. "$1"; km_vms_resolve_product_source "$2"',
                "stage661-resolver",
                str(common),
                str(app),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    legacy = resolve()
    assert legacy.returncode == 0, legacy.stderr
    assert legacy.stdout.strip() == str(app)

    source = tmp_path / "target-resolver"
    _source_fixture(source, commit=COMMIT_A, version=VERSION, trusted=True)
    target = _finalize_target(
        app,
        source,
        request_id="update-" + ("d" * 32),
        commit=COMMIT_A,
    )
    slots.atomic_switch_pointer(app, target["slot_id"])

    active = resolve()
    assert active.returncode == 0, active.stderr
    assert active.stdout.strip() == str(Path(target["slot_path"]) / "source")
