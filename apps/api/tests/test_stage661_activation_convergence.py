from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from app.services import schema_update_pipeline
from app.services.schema_migrations import PRODUCTION_MIGRATIONS


ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _load(
    "stage661_activation_bridge",
    ROOT / "scripts/km-vms-update-helper-bridge.py",
)


PREVIOUS = {
    "slot_id": "adopted-" + ("a" * 64),
    "kind": "adopted_pre_update_snapshot",
    "official_source_match": False,
    "version": "0.8.2",
    "commit": "1" * 40,
}
TARGET = {
    "slot_id": "release-" + ("2" * 40),
    "kind": "trusted_release",
    "official_source_match": True,
    "version": "0.8.3",
    "commit": "2" * 40,
}
REQUEST_ID = "update-" + ("3" * 32)


def _journal(
    phase: str = "target_prepared",
    *,
    pointer: str | None = PREVIOUS["slot_id"],
    migration_required: bool = False,
    migration_invoked: bool = False,
    migration_completed: bool = False,
    target_verified: bool = False,
    rollback_trigger: str | None = None,
) -> dict:
    return {
        "request_id": REQUEST_ID,
        "phase": phase,
        "previous": deepcopy(PREVIOUS),
        "target": deepcopy(TARGET),
        "schema": {
            "migration_required": migration_required,
            "migration_invoked": migration_invoked,
            "migration_completed": migration_completed,
        },
        "pointer_slot_id": pointer,
        "target_verified": target_verified,
        "previous_verified": False,
        "rollback_trigger": rollback_trigger,
        "failure_category": None,
    }


class FakeEngine:
    def __init__(
        self,
        journal: dict,
        *,
        switch_failure: str | None = None,
    ):
        self.journal = deepcopy(journal)
        self.pointer = journal["pointer_slot_id"]
        self.switches: list[str] = []
        self.switch_failure = switch_failure

    def read_activation_journal(self, _app_dir: Path) -> dict:
        return deepcopy(self.journal)

    def read_active_slot(self, _app_dir: Path):
        if self.pointer is None:
            return None
        return self.pointer, Path("/slot/source")

    def atomic_switch_pointer(self, _app_dir: Path, slot_id: str):
        if self.switch_failure == "before_replace":
            raise OSError("injected pre-replace failure")
        self.pointer = slot_id
        self.switches.append(slot_id)
        if self.switch_failure == "after_replace":
            raise OSError("injected post-replace failure")
        return Path("/slot/source")

    def transition_activation_journal(
        self,
        _app_dir: Path,
        *,
        request_id: str,
        phase: str,
        pointer_slot_id: str | None,
        record_pointer: bool,
        **updates,
    ) -> dict:
        assert request_id == self.journal["request_id"]
        self.journal["phase"] = phase
        if record_pointer:
            assert pointer_slot_id == self.pointer
            self.journal["pointer_slot_id"] = pointer_slot_id
        for key, value in updates.items():
            if key in {"migration_invoked", "migration_completed"}:
                self.journal["schema"][key] = value
            else:
                self.journal[key] = value
        return deepcopy(self.journal)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch):
    calls = {
        "reconcile": [],
        "verify": [],
        "handoff": 0,
        "handoff_phases": [],
        "migration": 0,
        "cleanup": [],
    }

    monkeypatch.setattr(
        bridge,
        "write_activation_progress",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bridge,
        "reconcile_slot_runtime",
        lambda _app, _project, slot, **_kwargs: calls[
            "reconcile"
        ].append(slot),
    )
    monkeypatch.setattr(
        bridge,
        "verify_slot_runtime",
        lambda _app, _project, binding, **_kwargs: calls[
            "verify"
        ].append(binding["slot_id"]),
    )
    def schedule_handoff(engine, *_args, **_kwargs):
        calls["handoff"] += 1
        calls["handoff_phases"].append(engine.journal["phase"])

    monkeypatch.setattr(
        bridge,
        "schedule_target_helper_handoff",
        schedule_handoff,
    )
    monkeypatch.setattr(
        bridge,
        "stop_slot_schema_writers",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bridge,
        "run_target_schema_migration",
        lambda *_args, **_kwargs: calls.__setitem__(
            "migration",
            calls["migration"] + 1,
        ),
    )
    monkeypatch.setattr(
        bridge,
        "schema_mutation_completed",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        bridge,
        "attempt_terminal_release_cleanup",
        lambda _engine, _app, _project, journal: calls[
            "cleanup"
        ].append(journal["phase"]),
    )
    return calls


def test_successful_activation_commits_exact_target(runtime) -> None:
    engine = FakeEngine(_journal())
    result = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )
    assert result["phase"] == "completed"
    assert engine.pointer == TARGET["slot_id"]
    assert engine.switches == [TARGET["slot_id"]]
    assert runtime["handoff"] == 1
    assert runtime["handoff_phases"] == ["completed"]
    assert runtime["cleanup"] == ["completed"]


def test_pointer_exception_after_replace_continues_target_verification(
    runtime,
) -> None:
    engine = FakeEngine(
        _journal(),
        switch_failure="after_replace",
    )

    result = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )

    assert result["phase"] == "completed"
    assert engine.pointer == TARGET["slot_id"]
    assert engine.switches == [TARGET["slot_id"]]
    assert TARGET["slot_id"] in runtime["reconcile"]
    assert TARGET["slot_id"] in runtime["verify"]


def test_pointer_exception_before_replace_restores_previous_runtime(
    runtime,
) -> None:
    engine = FakeEngine(
        _journal(),
        switch_failure="before_replace",
    )

    result = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )

    assert result["phase"] == "blocked"
    assert result["failure_category"] == "active_pointer_switch_failed"
    assert engine.pointer == PREVIOUS["slot_id"]
    assert engine.switches == []
    assert runtime["reconcile"][-1] == PREVIOUS["slot_id"]
    assert runtime["verify"][-1] == PREVIOUS["slot_id"]


def test_terminal_cleanup_removes_only_unprotected_product_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_slot = "release-" + ("4" * 40)
    calls: dict[str, list] = {
        "slot_cleanup": [],
        "staging_cleanup": [],
        "image_rm": [],
    }

    class CleanupEngine:
        def cleanup_unprotected_slots(self, _app, **kwargs):
            calls["slot_cleanup"].append(kwargs)
            return [old_slot]

        def protected_slot_ids(self, _app):
            return {PREVIOUS["slot_id"], TARGET["slot_id"]}

        def cleanup_request_staging(self, _app, **kwargs):
            calls["staging_cleanup"].append(kwargs)
            return True

    image_refs = "\n".join(
        (
            f"km-vms-tnas-vms-slot-api:{old_slot}",
            f"tnas-vms-api:{old_slot}",
            f"km-vms-tnas-vms-slot-web:{TARGET['slot_id']}",
            f"tnas-vms-web:{TARGET['slot_id']}",
            f"km-vms-other-slot-api:{old_slot}",
            f"other-api:{old_slot}",
            "unrelated/image:latest",
        )
    )

    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "image", "ls"]:
            return SimpleNamespace(
                returncode=0,
                stdout=image_refs,
                stderr="",
            )
        assert args[:3] == ["docker", "image", "rm"]
        calls["image_rm"].append(args[3])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bridge, "run_command", fake_run)
    journal = _journal(
        "completed",
        pointer=TARGET["slot_id"],
        target_verified=True,
    )

    result = bridge.cleanup_terminal_release_artifacts(
        CleanupEngine(),
        Path("/app"),
        "tnas-vms",
        journal,
    )

    assert result == {
        "removed_slots": [old_slot],
        "removed_image_refs": [
            f"km-vms-tnas-vms-slot-api:{old_slot}",
            f"tnas-vms-api:{old_slot}",
        ],
        "request_staging_removed": True,
    }
    assert calls["slot_cleanup"] == [
        {
            "retain_slot_ids": set(),
            "maximum_unprotected": 0,
            "terminal_evidence": True,
        }
    ]
    assert calls["staging_cleanup"] == [
        {
            "request_id": REQUEST_ID,
            "terminal_evidence": True,
        }
    ]
    assert calls["image_rm"] == [
        f"km-vms-tnas-vms-slot-api:{old_slot}",
        f"tnas-vms-api:{old_slot}",
    ]


@pytest.mark.parametrize(
    "slot_id",
    [PREVIOUS["slot_id"], TARGET["slot_id"]],
)
def test_runtime_reconciliation_recreates_setup_helper_from_selected_slot(
    monkeypatch: pytest.MonkeyPatch,
    slot_id: str,
) -> None:
    selected_slots: list[str] = []
    commands: list[list[str]] = []
    services = [
        *bridge.ACTIVATION_RUNTIME_SERVICES,
        "postgres",
        "redis",
    ]

    def fake_slot_compose(
        _app,
        _project,
        selected_slot,
        **_kwargs,
    ):
        selected_slots.append(selected_slot)
        return (
            ["docker-compose", "-f", "/slot/docker-compose.yml"],
            {},
            Path("/slot/source"),
            {"compose_evidence": {"services": services}},
        )

    def fake_run(args, **_kwargs):
        commands.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bridge, "slot_compose", fake_slot_compose)
    monkeypatch.setattr(bridge, "run_command", fake_run)

    bridge.reconcile_slot_runtime(
        Path("/app"),
        "tnas-vms",
        slot_id,
        engine=object(),
    )

    assert selected_slots == [slot_id]
    assert len(commands) == 1
    command = commands[0]
    assert "--force-recreate" in command
    assert "setup-helper" in command
    assert command[-len(bridge.ACTIVATION_RUNTIME_SERVICES) :] == list(
        bridge.ACTIVATION_RUNTIME_SERVICES
    )


def test_terminal_activation_recreates_helper_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
) -> None:
    engine = FakeEngine(_journal())
    direct_phases: list[str] = []
    helper_evidence = {
        "image_evidence": {
            "services": {
                "update-helper": {
                    "immutable_image_ref": "km-vms-helper:target",
                    "image_id": "sha256:" + ("a" * 64),
                }
            }
        }
    }
    monkeypatch.setattr(
        bridge,
        "slot_record",
        lambda *_args, **_kwargs: (
            Path("/slot"),
            Path("/slot/source"),
            helper_evidence,
        ),
    )

    def recreate(**_kwargs):
        direct_phases.append(engine.journal["phase"])
        return "container"

    monkeypatch.setattr(
        bridge,
        "recreate_and_verify_helper",
        recreate,
    )

    result = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
        terminal_owner=True,
    )

    assert result["phase"] == "completed"
    assert direct_phases == ["completed"]
    assert runtime["handoff"] == 0


@pytest.mark.parametrize(
    ("code", "trigger"),
    [
        ("slot_runtime_unhealthy", "target_health_failed"),
        ("slot_runtime_identity_mismatch", "target_identity_mismatch"),
    ],
)
def test_target_failure_restores_exact_previous(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
    code: str,
    trigger: str,
) -> None:
    engine = FakeEngine(
        _journal(
            "verifying_target",
            pointer=TARGET["slot_id"],
        )
    )

    def verify(_app, _project, binding, **_kwargs):
        if binding["slot_id"] == TARGET["slot_id"]:
            raise bridge.BridgeError(code, "target failed")

    monkeypatch.setattr(bridge, "verify_slot_runtime", verify)
    result = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )
    assert result["phase"] == "failed_rolled_back"
    assert result["rollback_trigger"] == trigger
    assert result["previous_verified"] is True
    assert engine.pointer == PREVIOUS["slot_id"]
    assert PREVIOUS["official_source_match"] is False
    assert runtime["cleanup"] == ["failed_rolled_back"]


def test_failed_previous_verification_never_claims_rollback(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
) -> None:
    engine = FakeEngine(
        _journal(
            "rolling_back",
            pointer=TARGET["slot_id"],
            rollback_trigger="target_health_failed",
        )
    )

    def verify(_app, _project, binding, **_kwargs):
        if binding["slot_id"] == PREVIOUS["slot_id"]:
            raise bridge.BridgeError(
                "slot_runtime_unhealthy",
                "previous failed",
            )

    monkeypatch.setattr(bridge, "verify_slot_runtime", verify)
    result = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )
    assert result["phase"] == "blocked"
    assert result["failure_category"] == "rollback_verification_failed"
    assert result["previous_verified"] is False


@pytest.mark.parametrize(
    ("journal", "terminal"),
    [
        (_journal("target_prepared"), "completed"),
        (
            _journal(
                "verifying_target",
                pointer=TARGET["slot_id"],
            ),
            "completed",
        ),
        (
            _journal(
                "committing_target",
                pointer=TARGET["slot_id"],
                target_verified=True,
            ),
            "completed",
        ),
        (
            _journal(
                "rolling_back",
                pointer=TARGET["slot_id"],
                rollback_trigger="target_health_failed",
            ),
            "failed_rolled_back",
        ),
    ],
)
def test_representative_restart_phases_converge_idempotently(
    runtime,
    journal: dict,
    terminal: str,
) -> None:
    engine = FakeEngine(journal)
    first = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )
    switches = list(engine.switches)
    second = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )
    assert first["phase"] == terminal
    assert second == first
    assert engine.switches == switches
    assert runtime["cleanup"] == [terminal]


def test_completed_schema_marker_is_not_replayed(
    runtime,
) -> None:
    engine = FakeEngine(
        _journal(
            "schema_preparing",
            migration_required=True,
            migration_invoked=True,
        )
    )
    result = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )
    assert result["phase"] == "completed"
    assert runtime["migration"] == 0


def test_schema_failure_restores_previous_runtime_before_blocking(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
) -> None:
    engine = FakeEngine(
        _journal(
            "quiescing",
            migration_required=True,
        )
    )
    monkeypatch.setattr(
        bridge,
        "run_target_schema_migration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bridge.BridgeError(
                "schema_update_failed",
                "migration failed",
            )
        ),
    )
    monkeypatch.setattr(
        bridge,
        "schema_mutation_completed",
        lambda *_args, **_kwargs: False,
    )

    result = bridge.converge_activation(
        engine,
        Path("/app"),
        "tnas-vms",
        REQUEST_ID,
    )

    assert result["phase"] == "blocked"
    assert result["failure_category"] == "schema_update_failed"
    assert engine.pointer == PREVIOUS["slot_id"]
    assert runtime["reconcile"][-1] == PREVIOUS["slot_id"]


def test_current_canonical_schema_paths_keep_previous_runtime_compatible() -> None:
    migrations = list(PRODUCTION_MIGRATIONS.migrations)
    assert {
        migration.migration_id for migration in migrations
    }.issubset(
        schema_update_pipeline.PREVIOUS_RUNTIME_COMPATIBLE_MIGRATION_IDS
    )
    summary = schema_update_pipeline._migration_summary(migrations)
    assert summary["previous_runtime_compatibility"] == {
        "status": "compatible",
        "evidence_model": "explicit_migration_allowlist_v1",
        "compatible_migration_ids": sorted(
            migration.migration_id for migration in migrations
        ),
        "unsupported_migration_ids": [],
    }


def test_unknown_future_schema_change_blocks_before_mutation() -> None:
    class UnknownMigration:
        migration_id = "future_breaking_change_v9"
        from_version = 8

    summary = schema_update_pipeline._migration_summary(
        [UnknownMigration()]
    )
    assert summary["previous_runtime_compatibility"]["status"] == "blocked"
    assert summary["previous_runtime_compatibility"][
        "unsupported_migration_ids"
    ] == ["future_breaking_change_v9"]
