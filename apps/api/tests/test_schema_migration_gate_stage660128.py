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
    STAGE4101_STORAGE_FOUNDATION_MIGRATION,
    migration_definition_fingerprint,
)
from app.services import schema_update_control as control


REQUEST_ID = "update-" + ("1" * 32)
ADMISSION_ID = "migration-attempt-" + ("2" * 32)
TRANSITION_ID = "migration-attempt-" + ("3" * 32)
TARGET_COMMIT = "4" * 40
REGISTRY = "5" * 64
PLAN = "6" * 64


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
        lambda db: events.append("lock") if db is lock_db else None,
    )
    monkeypatch.setattr(
        pipeline,
        "release_schema_lock",
        lambda db: events.append("unlock") if db is lock_db else None,
    )
    monkeypatch.setattr(
        pipeline.schema_preparation,
        "main",
        lambda *, manage_lock: events.append(f"prepare:{manage_lock}"),
    )
    monkeypatch.setattr(
        pipeline.operation_recovery,
        "main",
        lambda *, manage_lock: events.append(f"recover:{manage_lock}"),
    )
    monkeypatch.setattr(
        pipeline.schema_migration_gate,
        "main",
        lambda *, manage_lock: events.append(f"migrate:{manage_lock}"),
    )

    pipeline.main()

    assert events == [
        "session",
        "lock",
        "prepare:False",
        "recover:False",
        "migrate:False",
        "unlock",
        "session_closed",
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
        "target_schema_version": 8,
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


def test_exact_working_nas_v0724_shape_is_a_bounded_source_variant(
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
        lambda _db: (
            control.WORKING_NAS_V0724_SOURCE_SHAPE_FINGERPRINT
        ),
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
        control.WORKING_NAS_V0724_SOURCE_SHAPE_FINGERPRINT,
    )
    assert control.SOURCE_SHAPE_FINGERPRINT_ALTERNATES == {
        "0.7.24": frozenset(
            {
                control.WORKING_NAS_V0724_SOURCE_SHAPE_FINGERPRINT
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
