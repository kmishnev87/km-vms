from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.recording_storage import (
    archive_root_host_display_path,
    archive_root_runtime_mount_path,
    write_archive_roots_runtime_files,
)


class FakeQuery:
    def __init__(self, roots: list[SimpleNamespace]) -> None:
        self.roots = roots

    def filter(self, *_args: object) -> "FakeQuery":
        return self

    def order_by(self, *_args: object) -> "FakeQuery":
        return self

    def all(self) -> list[SimpleNamespace]:
        return self.roots


class FakeDb:
    def __init__(self, roots: list[SimpleNamespace]) -> None:
        self.roots = roots

    def query(self, _model: object) -> FakeQuery:
        return FakeQuery(self.roots)


def _root(
    root_id: str,
    runtime_path: str,
    *,
    active: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=root_id,
        root_path=runtime_path,
        storage_namespace="kmvms/recordings",
        is_active=active,
        created_at=None,
        retired_at=None,
    )


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "runtime_base": "/storage/archive-roots",
        "compose_override_file": "docker-compose.archive-roots.yml",
        "items": [
            {
                "root_id": "root-a",
                "user_display_path": "/Volume1/archive/root-a",
                "backend_runtime_path": (
                    "/storage/archive-roots/root-a-0123456789ab"
                ),
                "physical_volume_id": "/Volume1",
                "storage_namespace": "kmvms/recordings",
                "active_write_target": True,
            },
            {
                "root_id": "root-b",
                "user_display_path": "/Volume2/archive/root-b",
                "backend_runtime_path": (
                    "/storage/archive-roots/root-b-ba9876543210"
                ),
                "physical_volume_id": "/Volume2",
                "storage_namespace": "kmvms/recordings",
                "active_write_target": False,
            },
        ],
        "raw_runtime_paths_user_visible": False,
    }


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_target_restart_preserves_legacy_host_to_runtime_mount_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "install-control"
    control.mkdir()
    monkeypatch.setattr(settings, "storage_install_control", str(control))
    monkeypatch.setattr(settings, "storage_root", "/storage/archive")
    monkeypatch.delenv("KMVMS_ARCHIVE_ROOTS_RUNTIME_BASE", raising=False)
    manifest_path = control / "archive-roots-runtime.json"
    _write_manifest(manifest_path, _manifest())
    roots = [
        _root(
            "root-a",
            "/storage/archive-roots/root-a-0123456789ab",
            active=True,
        ),
        _root(
            "root-b",
            "/storage/archive-roots/root-b-ba9876543210",
            active=False,
        ),
    ]

    assert (
        archive_root_host_display_path(roots[0])
        == "/Volume1/archive/root-a"
    )
    assert archive_root_runtime_mount_path(roots[0]).as_posix() == (
        "/storage/archive-roots/root-a-0123456789ab"
    )

    result = write_archive_roots_runtime_files(FakeDb(roots))
    first_manifest = Path(result["manifest_path"]).read_bytes()
    first_compose = Path(result["compose_override_path"]).read_bytes()
    written = json.loads(first_manifest)
    by_id = {item["root_id"]: item for item in written["items"]}

    assert (
        by_id["root-a"]["user_display_path"]
        == "/Volume1/archive/root-a"
    )
    assert by_id["root-a"]["backend_runtime_path"] == (
        "/storage/archive-roots/root-a-0123456789ab"
    )
    assert (
        by_id["root-b"]["user_display_path"]
        == "/Volume2/archive/root-b"
    )
    compose = first_compose.decode("utf-8")
    assert 'source: "/Volume1/archive/root-a"' in compose
    assert (
        'target: "/storage/archive-roots/root-a-0123456789ab"'
        in compose
    )
    assert "  schema-update:" in compose

    write_archive_roots_runtime_files(FakeDb(roots))
    assert manifest_path.read_bytes() == first_manifest
    assert (
        Path(result["compose_override_path"]).read_bytes()
        == first_compose
    )


def test_invalid_runtime_manifest_fails_without_rewriting_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "install-control"
    control.mkdir()
    monkeypatch.setattr(settings, "storage_install_control", str(control))
    monkeypatch.setattr(settings, "storage_root", "/storage/archive")
    manifest_path = control / "archive-roots-runtime.json"
    payload = _manifest()
    payload["items"][0]["user_display_path"] = "relative/unsafe"
    _write_manifest(manifest_path, payload)
    compose_path = control / "docker-compose.archive-roots.yml"
    compose_path.write_text("preserve-me\n", encoding="utf-8")
    manifest_before = manifest_path.read_bytes()
    compose_before = compose_path.read_bytes()
    roots = [
        _root(
            "root-a",
            "/storage/archive-roots/root-a-0123456789ab",
            active=True,
        )
    ]

    with pytest.raises(
        ValueError,
        match="archive_roots_runtime_manifest_item_invalid",
    ):
        write_archive_roots_runtime_files(FakeDb(roots))

    assert manifest_path.read_bytes() == manifest_before
    assert compose_path.read_bytes() == compose_before


def test_initial_setup_changes_only_default_host_binding_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "install-control"
    control.mkdir()
    monkeypatch.setattr(settings, "storage_install_control", str(control))
    monkeypatch.setattr(settings, "storage_root", "/storage/archive")
    manifest_path = control / "archive-roots-runtime.json"
    payload = {
        "schema_version": 1,
        "runtime_base": "/storage/archive-roots",
        "compose_override_file": "docker-compose.archive-roots.yml",
        "items": [
            {
                "root_id": "root-b",
                "user_display_path": "/Volume2/archive/root-b",
                "backend_runtime_path": "/storage/archive-roots/root-b-stable",
                "physical_volume_id": "/Volume2",
                "storage_namespace": "kmvms/recordings",
                "active_write_target": False,
            },
            {
                "root_id": "default",
                "user_display_path": "/Volume1/bootstrap/archive",
                "backend_runtime_path": "/storage/archive-roots/default-stable",
                "physical_volume_id": "/Volume1",
                "storage_namespace": "kmvms/recordings",
                "active_write_target": True,
            },
            {
                "root_id": "root-c",
                "user_display_path": "/Volume3/archive/root-c",
                "backend_runtime_path": "/storage/archive-roots/root-c-stable",
                "physical_volume_id": "/Volume3",
                "storage_namespace": "kmvms/recordings",
                "active_write_target": False,
            },
        ],
        "raw_runtime_paths_user_visible": False,
    }
    _write_manifest(manifest_path, payload)
    roots = [
        _root("default", "/storage/archive", active=True),
        _root("root-c", "/storage/archive-roots/root-c-stable", active=False),
        _root("root-b", "/storage/archive-roots/root-b-stable", active=False),
    ]
    non_default_before = {
        item["root_id"]: dict(item)
        for item in payload["items"]
        if item["root_id"] != "default"
    }

    result = write_archive_roots_runtime_files(
        FakeDb(roots),
        initial_default_host_path="/Volume9/Selected Archive",
    )
    first_manifest = Path(result["manifest_path"]).read_bytes()
    first_compose = Path(result["compose_override_path"]).read_bytes()
    written = json.loads(first_manifest)
    by_id = {item["root_id"]: item for item in written["items"]}

    assert [item["root_id"] for item in written["items"]] == [
        "root-b",
        "default",
        "root-c",
    ]
    assert {
        root_id: by_id[root_id] for root_id in non_default_before
    } == non_default_before
    assert by_id["default"]["user_display_path"] == "/Volume9/Selected Archive"
    assert by_id["default"]["backend_runtime_path"] == "/storage/archive-roots/default-stable"
    assert by_id["default"]["physical_volume_id"] == "/Volume9"
    assert roots[0].root_path == "/storage/archive"

    write_archive_roots_runtime_files(
        FakeDb(roots),
        initial_default_host_path="/Volume9/Selected Archive",
    )
    assert manifest_path.read_bytes() == first_manifest
    assert Path(result["compose_override_path"]).read_bytes() == first_compose
