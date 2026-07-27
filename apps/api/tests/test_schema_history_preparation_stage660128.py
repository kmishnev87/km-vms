from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.session import Base
from app.models.schema_version import SchemaMigrationHistory
from app.services.schema_migrations import (
    MIGRATION_SOURCE,
    _current_history_count,
)


BASELINE_ID = "chapter06_stage4_baseline"


def _history_row(
    *,
    migration_id: str,
    previous_version: int | None,
) -> SchemaMigrationHistory:
    return SchemaMigrationHistory(
        migration_id=migration_id,
        previous_version=previous_version,
        target_version=7,
        schema_version=7,
        baseline_id=BASELINE_ID,
        app_version="0.7.25",
        app_build_version="test",
        status="applied",
        checksum="1" * 64,
        source=MIGRATION_SOURCE,
        service_name="schema_migration_gate",
        details={},
    )


def test_same_version_preparation_is_not_a_second_current_history_row() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[SchemaMigrationHistory.__table__],
    )
    state = SimpleNamespace(
        baseline_id=BASELINE_ID,
        source=MIGRATION_SOURCE,
        schema_version=7,
    )

    with Session(engine) as db:
        db.add(
            _history_row(
                migration_id="canonical-v7",
                previous_version=6,
            )
        )
        db.add(
            _history_row(
                migration_id=(
                    "stage660128_remediation_state_width_compatibility"
                ),
                previous_version=7,
            )
        )
        db.commit()

        assert _current_history_count(db, state) == 1


def test_duplicate_version_transition_history_remains_blocking() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[SchemaMigrationHistory.__table__],
    )
    state = SimpleNamespace(
        baseline_id=BASELINE_ID,
        source=MIGRATION_SOURCE,
        schema_version=7,
    )

    with Session(engine) as db:
        db.add(
            _history_row(
                migration_id="canonical-v7-a",
                previous_version=6,
            )
        )
        db.add(
            _history_row(
                migration_id="canonical-v7-b",
                previous_version=6,
            )
        )
        db.commit()

        assert _current_history_count(db, state) == 2
