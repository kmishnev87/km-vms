from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.session import Base
from app.models.schema_migration_control import SchemaMigrationAttempt
from app.models.schema_version import SchemaMigrationHistory
from app.services import schema_migration_gate as gate
from app.services import schema_update_pipeline as pipeline
from app.services.schema_migrations import (
    MIGRATION_SOURCE,
    PRODUCTION_MIGRATIONS,
    STAGE4101_STORAGE_FOUNDATION_MIGRATION,
    STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION,
    STAGE660128_UNIVERSAL_SCHEMA_MIGRATION,
    migration_definition_fingerprint,
)
from app.services import schema_update_control as control


REQUEST_ID = "update-" + ("1" * 32)
ADMISSION_ID = "migration-attempt-" + ("2" * 32)
TRANSITION_ID = "migration-attempt-" + ("3" * 32)
TARGET_COMMIT = "4" * 40
REGISTRY = "5" * 64
PLAN = "6" * 64


def test_published_universal_schema_v8_fingerprint_is_immutable() -> None:
    migration = STAGE660128_UNIVERSAL_SCHEMA_MIGRATION
    assert migration.migration_id == "stage660128_universal_skipped_release_schema_v8"
    # Do not update this value for a refactor; a changed definition needs a new migration id.
    assert migration_definition_fingerprint(migration) == (
        "997c36101dc217a0355e0308cf146b947feca0c882b7b07a3b912b4db982004a"
    )


def test_current_schema_v9_accepts_only_verified_exact_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for fingerprint in control.TARGET_SHAPE_FINGERPRINTS:
        monkeypatch.setattr(
            control,
            "database_shape_fingerprint",
            lambda _db, value=fingerprint: value,
        )
        assert control.target_shape_is_exact(object()) == (
            True,
            fingerprint,
        )

    unknown = "f" * 64
    assert unknown not in control.TARGET_SHAPE_FINGERPRINTS
    monkeypatch.setattr(
        control,
        "database_shape_fingerprint",
        lambda _db: unknown,
    )
    assert control.target_shape_is_exact(object()) == (False, unknown)


def test_schema_v9_exact_fingerprints_match_three_validated_paths() -> None:
    assert control.TARGET_SHAPE_FINGERPRINTS == {
        "ecc7ccf61e781477ceec853414d0b4ff90da7f45b4a73052885db4a57093fdcb",
        "5a0e8ab16dc61c99ed6a5842d54d4e6a0599bfcdaf7e03a24b30c51316060c7e",
        "16c571428596280b75779606006b2871af828b9d634c6a9faee6c41701892477",
    }


def test_same_target_lineage_includes_verified_schema_v9_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control,
        "_source_identity_payload",
        lambda: {
            "request_id": REQUEST_ID,
            "installed_version": "0.8.9",
            "installed_commit": TARGET_COMMIT,
        },
    )

    _version, _commit, schema_version, shapes = (
        control.expected_source_lineage(
            request_id=REQUEST_ID,
            target_release="0.8.9",
            target_commit=TARGET_COMMIT,
        )
    )

    assert schema_version == control.TARGET_SCHEMA_VERSION
    assert shapes == control.TARGET_SHAPE_FINGERPRINTS


def test_pipeline_holds_one_lock_while_running_all_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    lock_db = object()

    class LockSession:
        def __init__(self, _engine: object) -> None:
            pass

        def __enter__(self) -> object:
            events.append("session")
            return lock_db

        def __exit__(self, *_args: object) -> None:
            events.append("session_closed")

    monkeypatch.setattr(pipeline, "Session", LockSession)
    monkeypatch.setattr(
        pipeline,
        "acquire_schema_lock",
        lambda db: (events.append("lock") or 4242) if db is lock_db else None,
    )
    monkeypatch.setattr(
        pipeline,
        "release_schema_lock",
        lambda db: events.append("unlock") if db is lock_db else None,
    )
    monkeypatch.setattr(
        pipeline.schema_preparation,
        "main",
        lambda *,
        manage_lock,
        pipeline_lock_backend_pid,
        on_mutation_start: events.append(
            f"prepare:{manage_lock}:{pipeline_lock_backend_pid}"
        ),
    )
    monkeypatch.setattr(
        pipeline.operation_recovery,
        "main",
        lambda *,
        manage_lock,
        pipeline_lock_backend_pid,
        on_mutation_start: events.append(
            f"recover:{manage_lock}:{pipeline_lock_backend_pid}"
        ),
    )
    monkeypatch.setattr(
        pipeline.schema_migration_gate,
        "main",
        lambda *,
        manage_lock,
        pipeline_lock_backend_pid,
        on_mutation_start: events.append(
            f"migrate:{manage_lock}:{pipeline_lock_backend_pid}"
        ),
    )

    pipeline.main()

    assert events == [
        "session",
        "lock",
        "prepare:False:4242",
        "recover:False:4242",
        "migrate:False:4242",
        "unlock",
        "session_closed",
    ]


def test_exact_current_schema_preflight_is_read_only_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    context.source_schema_version = control.TARGET_SCHEMA_VERSION
    events: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "load_initial_update_context",
        lambda _db: context,
    )
    monkeypatch.setattr(
        pipeline,
        "validate_released_source_history",
        lambda *_args, **_kwargs: (
            events.append("history")
            or {
                "legacy_applied_count": 7,
                "legacy_null_checksum_adopted_count": 0,
                "legacy_exact_checksum_count": 7,
                "compatibility_preparation_count": 0,
            }
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "build_backup_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("no-op preflight requested a backup")
        ),
    )

    loaded, summary = pipeline.read_only_update_preflight(object())

    assert loaded is context
    assert summary["migration_required"] is False
    assert summary["migration_count"] == 0
    assert summary["backup_preflight"]["required"] is False
    assert events == ["history"]


def test_safe_backup_failure_retries_without_duplicate_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    summary = {
        "source_schema_version": 1,
        "target_schema_version": control.TARGET_SCHEMA_VERSION,
        "migration_required": True,
        "migration_count": len(PRODUCTION_MIGRATIONS.migrations),
        "migration_ids": [
            migration.migration_id
            for migration in PRODUCTION_MIGRATIONS.migrations
        ],
    }
    events: list[str] = []
    writes: list[dict] = []
    attempts = {"count": 0}

    class LockDb:
        def rollback(self) -> None:
            events.append("read_transaction_closed")

    lock_db = LockDb()

    class LockSession:
        def __init__(self, _engine: object) -> None:
            pass

        def __enter__(self) -> object:
            return lock_db

        def __exit__(self, *_args: object) -> None:
            pass

    def backup(*_args: object, **_kwargs: object) -> dict:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("safe pre-mutation backup failure")
        return {"backup_id": "backup-1"}

    monkeypatch.setattr(pipeline, "Session", LockSession)
    monkeypatch.setattr(pipeline, "acquire_schema_lock", lambda _db: 4242)
    monkeypatch.setattr(
        pipeline,
        "release_schema_lock",
        lambda _db: events.append("unlock"),
    )
    monkeypatch.setattr(
        pipeline,
        "read_only_update_preflight",
        lambda _db: (context, summary),
    )
    monkeypatch.setattr(pipeline, "create_backup_before_upgrade", backup)
    monkeypatch.setattr(
        pipeline,
        "atomic_write",
        lambda _path, payload: writes.append(dict(payload)),
    )
    def run_phases(
        backend_pid: int,
        *,
        on_mutation_start,
    ) -> None:
        events.append(f"phases:{backend_pid}")
        on_mutation_start()

    monkeypatch.setattr(pipeline, "_run_phases", run_phases)

    with pytest.raises(
        RuntimeError,
        match="safe pre-mutation backup failure",
    ):
        pipeline.run_update_migration()
    assert [item["mutation_started"] for item in writes] == [False]
    assert not any(item.startswith("phases:") for item in events)

    pipeline.run_update_migration()

    assert [item["mutation_started"] for item in writes] == [
        False,
        False,
        True,
        True,
    ]
    assert [item["state"] for item in writes] == [
        "backup_pending",
        "backup_pending",
        "migrating",
        "completed",
    ]
    assert events.count("phases:4242") == 1
    assert events.index("read_transaction_closed") < events.index(
        "phases:4242"
    )


def test_failure_before_first_real_mutation_keeps_marker_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    summary = {
        "source_schema_version": 1,
        "target_schema_version": control.TARGET_SCHEMA_VERSION,
        "migration_required": True,
        "migration_count": len(PRODUCTION_MIGRATIONS.migrations),
        "migration_ids": [
            migration.migration_id
            for migration in PRODUCTION_MIGRATIONS.migrations
        ],
    }
    writes: list[dict] = []
    events: list[str] = []

    class LockDb:
        def rollback(self) -> None:
            events.append("read_transaction_closed")

    lock_db = LockDb()

    class LockSession:
        def __init__(self, _engine: object) -> None:
            pass

        def __enter__(self) -> object:
            return lock_db

        def __exit__(self, *_args: object) -> None:
            pass

    def fail_before_mutation(
        backend_pid: int,
        *,
        on_mutation_start,
    ) -> None:
        assert backend_pid == 4242
        assert callable(on_mutation_start)
        events.append("phases_entered")
        raise RuntimeError("pre-mutation phase validation failed")

    monkeypatch.setattr(pipeline, "Session", LockSession)
    monkeypatch.setattr(
        pipeline,
        "acquire_schema_lock",
        lambda _db: 4242,
    )
    monkeypatch.setattr(
        pipeline,
        "release_schema_lock",
        lambda _db: events.append("unlock"),
    )
    monkeypatch.setattr(
        pipeline,
        "read_only_update_preflight",
        lambda _db: (context, summary),
    )
    monkeypatch.setattr(
        pipeline,
        "create_backup_before_upgrade",
        lambda *_args, **_kwargs: {"backup_id": "backup-1"},
    )
    monkeypatch.setattr(
        pipeline,
        "atomic_write",
        lambda _path, payload: writes.append(dict(payload)),
    )
    monkeypatch.setattr(
        pipeline,
        "_run_phases",
        fail_before_mutation,
    )

    with pytest.raises(
        RuntimeError,
        match="pre-mutation phase validation failed",
    ):
        pipeline.run_update_migration()

    assert [
        (item["state"], item["mutation_started"])
        for item in writes
    ] == [("backup_pending", False)]
    assert events == [
        "read_transaction_closed",
        "phases_entered",
        "unlock",
    ]


class FakeDb:
    def __init__(self, attempt: SimpleNamespace) -> None:
        self.attempt = attempt
        self.commits = 0
        self.rollbacks = 0

    def get(self, _model: object, attempt_id: str) -> SimpleNamespace:
        assert attempt_id == TRANSITION_ID
        return self.attempt

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        request_id=REQUEST_ID,
        admission_attempt_id=ADMISSION_ID,
        target_release="0.7.25",
        target_commit=TARGET_COMMIT,
        registry_fingerprint=REGISTRY,
        plan_fingerprint=PLAN,
        source_schema_version=1,
        installed_version="0.7.18",
        installed_commit="7" * 40,
    )


def _attempt(*, resumable: bool) -> SimpleNamespace:
    evidence = _retry_evidence()
    return SimpleNamespace(
        status="failed",
        admission_attempt_id=ADMISSION_ID,
        request_id=REQUEST_ID,
        target_commit=TARGET_COMMIT,
        registry_fingerprint=REGISTRY,
        plan_fingerprint=PLAN,
        fencing_generation=7,
        completed_at=object(),
        failure_class="bounded_failure",
        failure_summary="Bounded failure.",
        resumable=resumable,
        details={"bounded": True, "retry_evidence": evidence},
    )


def _retry_evidence() -> dict:
    return {
        "schema_version": 1,
        "mutation_started": False,
        "physical_mutation_possible": False,
        "transaction_rolled_back": True,
        "rollback_verified": True,
        "schema_shape_unchanged": True,
        "history_unchanged": True,
        "canonical_transition_committed": False,
        "foreign_state_detected": False,
    }


def _receipt(*, resumable: bool) -> dict:
    return {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "admission_attempt_id": ADMISSION_ID,
        "target_version": "0.7.25",
        "target_commit": TARGET_COMMIT,
        "target_schema_version": control.TARGET_SCHEMA_VERSION,
        "registry_fingerprint": REGISTRY,
        "plan_fingerprint": PLAN,
        "fencing_generation": 7,
        "attempt_id": TRANSITION_ID,
        "state": "failed" if resumable else "recovery_required",
        "phase": "preparing_database",
        "retryable": resumable,
        "error_code": (
            "test_injected_retryable_schema_failure"
            if resumable
            else "bounded_failure"
        ),
        "summary": "Bounded failure.",
        "operator_action": "Retry." if resumable else "Review.",
        "details": {
            "bounded": True,
            "retry_evidence": _retry_evidence(),
        },
        "updated_at": "2026-07-24T13:00:00Z",
    }


@pytest.mark.parametrize(
    ("resumable", "expected_exit"),
    ((True, 42), (False, 43)),
)
def test_repeated_legacy_compose_replays_exact_terminal_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resumable: bool,
    expected_exit: int,
) -> None:
    receipt_path = tmp_path / "gate.signed.json"
    monkeypatch.setattr(gate, "GATE_RECEIPT_PATH", receipt_path)
    control.write_signed(receipt_path, _receipt(resumable=resumable))
    original = receipt_path.read_bytes()
    state_calls: list[str] = []

    def update_state(
        _db: object,
        *,
        context: object,
        generation: int,
        state: str,
    ) -> None:
        assert context is not None
        assert generation == 7
        state_calls.append(state)

    monkeypatch.setattr(gate, "update_control_state", update_state)
    db = FakeDb(_attempt(resumable=resumable))
    with pytest.raises(SystemExit) as raised:
        gate._replay_existing_failed_attempt(
            db,
            context=_context(),
            generation=7,
            attempt_id=TRANSITION_ID,
        )

    assert raised.value.code == expected_exit
    assert state_calls == ["failed"]
    assert db.commits == 1
    assert receipt_path.read_bytes() == original


def test_terminal_replay_receipt_mismatch_fails_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = tmp_path / "gate.signed.json"
    monkeypatch.setattr(gate, "GATE_RECEIPT_PATH", receipt_path)
    payload = _receipt(resumable=True)
    payload["request_id"] = "update-" + ("9" * 32)
    control.write_signed(receipt_path, payload)
    original = receipt_path.read_bytes()
    state_calls: list[str] = []
    monkeypatch.setattr(
        gate,
        "update_control_state",
        lambda *_args, **_kwargs: state_calls.append("called"),
    )
    db = FakeDb(_attempt(resumable=True))

    with pytest.raises(
        gate.TerminalAttemptReplayEvidenceError,
        match="schema_gate_terminal_receipt_request_id_mismatch",
    ):
        gate._replay_existing_failed_attempt(
            db,
            context=_context(),
            generation=7,
            attempt_id=TRANSITION_ID,
        )

    assert state_calls == []
    assert db.commits == 0
    assert receipt_path.read_bytes() == original


def _lineage_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            SchemaMigrationHistory.__table__,
            SchemaMigrationAttempt.__table__,
        ],
    )
    return Session(engine)


def _record_target_history(db: Session) -> None:
    migration = STAGE4101_STORAGE_FOUNDATION_MIGRATION
    db.add(
        SchemaMigrationHistory(
            migration_id=migration.migration_id,
            previous_version=migration.from_version,
            target_version=migration.to_version,
            schema_version=migration.to_version,
            baseline_id="chapter06_stage4_baseline",
            app_version="0.7.25",
            app_build_version="test",
            status="applied",
            checksum=migration_definition_fingerprint(migration),
            source=MIGRATION_SOURCE,
            service_name="schema_migration_gate",
            details={},
        )
    )
def _record_target_attempt(
    db: Session,
    *,
    status: str = "applied",
) -> None:
    context = _context()
    migration = STAGE4101_STORAGE_FOUNDATION_MIGRATION
    completed = datetime.now() if status == "applied" else None
    after_shape = ("9" * 64) if status == "applied" else None
    db.add(
        SchemaMigrationAttempt(
            attempt_id=control.transition_attempt_id(
                context.admission_attempt_id,
                migration.migration_id,
            ),
            admission_attempt_id=context.admission_attempt_id,
            request_id=context.request_id,
            migration_id=migration.migration_id,
            previous_version=migration.from_version,
            target_version=migration.to_version,
            status=status,
            started_at=datetime.now(),
            completed_at=completed,
            fencing_generation=7,
            installed_version=context.installed_version,
            installed_commit=context.installed_commit,
            target_release=context.target_release,
            target_commit=context.target_commit,
            registry_fingerprint=context.registry_fingerprint,
            plan_fingerprint=context.plan_fingerprint,
            definition_fingerprint=(
                migration_definition_fingerprint(migration)
            ),
            before_shape_fingerprint="8" * 64,
            after_shape_fingerprint=after_shape,
            resumable=False,
            details={},
        )
    )


@pytest.mark.parametrize("status", ("started", "applied"))
def test_restart_accepts_only_exact_target_history_bound_to_attempt(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "current_schema_version", lambda _db: 2)
    with _lineage_session() as db:
        _record_target_history(db)
        _record_target_attempt(db, status=status)
        db.commit()

        evidence = gate._validate_released_history_lineage(
            db,
            _context(),
            generation=7,
        )

    assert evidence[f"target_{status}_attempt_count"] == 1
    assert evidence["legacy_applied_count"] == 0


def test_restart_rejects_target_history_without_bound_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "current_schema_version", lambda _db: 2)
    with _lineage_session() as db:
        _record_target_history(db)
        db.commit()

        with pytest.raises(
            gate.SchemaControlError,
            match="target_migration_attempt_evidence_invalid",
        ):
            gate._validate_released_history_lineage(
                db,
                _context(),
                generation=7,
            )


def test_source_history_keeps_strict_legacy_null_checksum_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "current_schema_version", lambda _db: 2)
    context = _context()
    context.source_schema_version = 2
    migration = STAGE4101_STORAGE_FOUNDATION_MIGRATION
    with _lineage_session() as db:
        db.add(
            SchemaMigrationHistory(
                migration_id=migration.migration_id,
                previous_version=migration.from_version,
                target_version=migration.to_version,
                schema_version=migration.to_version,
                baseline_id="chapter06_stage4_baseline",
                app_version="0.7.18",
                app_build_version="test",
                status="applied",
                checksum=None,
                source=MIGRATION_SOURCE,
                service_name="schema_migration_gate",
                details={},
            )
        )
        db.commit()

        evidence = gate._validate_released_history_lineage(
            db,
            context,
            generation=7,
        )

    assert evidence["legacy_applied_count"] == 1
    assert evidence["legacy_null_checksum_adopted_count"] == 1
    assert evidence["target_applied_attempt_count"] == 0


@pytest.mark.parametrize("source_schema_version", range(1, 9))
def test_each_canonical_schema_prefix_is_registry_driven(
    source_schema_version: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate,
        "current_schema_version",
        lambda _db: source_schema_version,
    )
    context = _context()
    context.source_schema_version = source_schema_version

    with _lineage_session() as db:
        for migration in PRODUCTION_MIGRATIONS.path(
            1,
            source_schema_version,
        ):
            db.add(
                SchemaMigrationHistory(
                    migration_id=migration.migration_id,
                    previous_version=migration.from_version,
                    target_version=migration.to_version,
                    schema_version=migration.to_version,
                    baseline_id="chapter06_stage4_baseline",
                    app_version="0.7.27",
                    app_build_version="test",
                    status="applied",
                    checksum=migration_definition_fingerprint(migration),
                    source=MIGRATION_SOURCE,
                    service_name="schema_migration_gate",
                    details={},
                )
            )
        db.commit()

        evidence = gate._validate_released_history_lineage(
            db,
            context,
            generation=7,
        )

    expected_count = max(source_schema_version - 1, 0)
    assert evidence["legacy_applied_count"] == expected_count
    assert evidence["legacy_exact_checksum_count"] == expected_count
    assert evidence["target_applied_attempt_count"] == 0


def test_published_schema_v8_history_from_v0727_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "current_schema_version", lambda _db: 8)
    context = _context()
    context.source_schema_version = 8
    context.installed_version = "0.7.27"

    with _lineage_session() as db:
        for migration in PRODUCTION_MIGRATIONS.path(1, 8):
            db.add(
                SchemaMigrationHistory(
                    migration_id=migration.migration_id,
                    previous_version=migration.from_version,
                    target_version=migration.to_version,
                    schema_version=migration.to_version,
                    baseline_id="chapter06_stage4_baseline",
                    app_version="0.7.27",
                    app_build_version="test",
                    status="applied",
                    checksum=migration_definition_fingerprint(migration),
                    source=MIGRATION_SOURCE,
                    service_name="schema_migration_gate",
                    details={},
                )
            )
        db.commit()

        evidence = gate._validate_released_history_lineage(
            db,
            context,
            generation=7,
        )

    assert evidence["legacy_applied_count"] == 7
    assert evidence["legacy_exact_checksum_count"] == 7
    assert evidence["target_applied_attempt_count"] == 0


def test_published_historical_v6_to_v7_variant_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "current_schema_version", lambda _db: 7)
    context = _context()
    context.source_schema_version = 7
    path_to_six = PRODUCTION_MIGRATIONS.path(1, 6)

    with _lineage_session() as db:
        for migration in (
            *path_to_six,
            STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION,
        ):
            db.add(
                SchemaMigrationHistory(
                    migration_id=migration.migration_id,
                    previous_version=migration.from_version,
                    target_version=migration.to_version,
                    schema_version=migration.to_version,
                    baseline_id="chapter06_stage4_baseline",
                    app_version="0.7.23",
                    app_build_version="test",
                    status="applied",
                    checksum=migration_definition_fingerprint(migration),
                    source=MIGRATION_SOURCE,
                    service_name="schema_migration_gate",
                    details={},
                )
            )
        db.commit()

        evidence = gate._validate_released_history_lineage(
            db,
            context,
            generation=7,
        )

    assert evidence["legacy_applied_count"] == 6


def test_two_published_ids_for_one_edge_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "current_schema_version", lambda _db: 7)
    context = _context()
    context.source_schema_version = 7
    canonical = PRODUCTION_MIGRATIONS.path(6, 7)[0]

    with _lineage_session() as db:
        for migration in (
            *PRODUCTION_MIGRATIONS.path(1, 6),
            STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION,
            canonical,
        ):
            db.add(
                SchemaMigrationHistory(
                    migration_id=migration.migration_id,
                    previous_version=migration.from_version,
                    target_version=migration.to_version,
                    schema_version=migration.to_version,
                    baseline_id="chapter06_stage4_baseline",
                    app_version="0.7.27",
                    app_build_version="test",
                    status="applied",
                    checksum=migration_definition_fingerprint(migration),
                    source=MIGRATION_SOURCE,
                    service_name="schema_migration_gate",
                    details={},
                )
            )
        db.commit()

        with pytest.raises(
            gate.SchemaControlError,
            match="legacy_migration_history_duplicate_edge",
        ):
            gate._validate_released_history_lineage(
                db,
                context,
                generation=7,
            )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("checksum", "f" * 64, "definition_mismatch"),
        ("previous_version", 0, "lineage_invalid"),
        ("target_version", 3, "lineage_invalid"),
    ),
)
def test_known_history_with_tampered_definition_or_edge_is_rejected(
    field: str,
    value: object,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "current_schema_version", lambda _db: 2)
    context = _context()
    context.source_schema_version = 2
    migration = STAGE4101_STORAGE_FOUNDATION_MIGRATION
    values = {
        "migration_id": migration.migration_id,
        "previous_version": migration.from_version,
        "target_version": migration.to_version,
        "schema_version": migration.to_version,
        "baseline_id": "chapter06_stage4_baseline",
        "app_version": "0.7.18",
        "app_build_version": "test",
        "status": "applied",
        "checksum": migration_definition_fingerprint(migration),
        "source": MIGRATION_SOURCE,
        "service_name": "schema_migration_gate",
        "details": {},
    }
    values[field] = value

    with _lineage_session() as db:
        db.add(SchemaMigrationHistory(**values))
        db.commit()
        with pytest.raises(gate.SchemaControlError, match=error):
            gate._validate_released_history_lineage(
                db,
                context,
                generation=7,
            )


def test_schema_v8_history_still_rejects_an_unpublished_migration_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "current_schema_version", lambda _db: 8)
    context = _context()
    context.source_schema_version = 8

    with _lineage_session() as db:
        db.add(
            SchemaMigrationHistory(
                migration_id="unpublished_schema_transition_v99",
                previous_version=8,
                target_version=8,
                schema_version=8,
                baseline_id="chapter06_stage4_baseline",
                app_version="0.7.27",
                app_build_version="test",
                status="applied",
                checksum="f" * 64,
                source=MIGRATION_SOURCE,
                service_name="schema_migration_gate",
                details={},
            )
        )
        db.commit()

        with pytest.raises(
            gate.SchemaControlError,
            match="legacy_migration_history_unknown_id",
        ):
            gate._validate_released_history_lineage(
                db,
                context,
                generation=7,
            )


def test_declared_v0724_shape_alternate_is_a_bounded_source_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alternate = next(
        iter(control.SOURCE_SHAPE_FINGERPRINT_ALTERNATES["0.7.24"])
    )
    monkeypatch.setattr(
        control,
        "_source_identity_payload",
        lambda: {
            "schema_version": 1,
            "request_id": REQUEST_ID,
            "installed_version": "0.7.24",
            "installed_commit": control.SOURCE_TAG_COMMITS["0.7.24"],
            "recorded_at": "2026-07-25T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        control,
        "current_schema_version",
        lambda _db: 7,
    )
    monkeypatch.setattr(
        control,
        "database_shape_fingerprint",
        lambda _db: alternate,
    )

    lineage = control.validate_source_lineage(
        object(),
        request_id=REQUEST_ID,
        target_release="0.7.25",
        target_commit=TARGET_COMMIT,
    )

    assert lineage == (
        "0.7.24",
        control.SOURCE_TAG_COMMITS["0.7.24"],
        7,
        alternate,
    )
    assert control.SOURCE_SHAPE_FINGERPRINT_ALTERNATES == {
        "0.7.24": frozenset(
            {
                alternate
            }
        )
    }


def test_unknown_v0724_shape_remains_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control,
        "_source_identity_payload",
        lambda: {
            "schema_version": 1,
            "request_id": REQUEST_ID,
            "installed_version": "0.7.24",
            "installed_commit": control.SOURCE_TAG_COMMITS["0.7.24"],
            "recorded_at": "2026-07-25T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        control,
        "current_schema_version",
        lambda _db: 7,
    )
    monkeypatch.setattr(
        control,
        "database_shape_fingerprint",
        lambda _db: "f" * 64,
    )

    with pytest.raises(
        control.SchemaControlError,
        match="installed_source_shape_mismatch",
    ):
        control.validate_source_lineage(
            object(),
            request_id=REQUEST_ID,
            target_release="0.7.25",
            target_commit=TARGET_COMMIT,
        )
