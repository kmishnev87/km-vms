from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.schema_migration_control import (
    SchemaMigrationAttempt,
    SchemaMigrationControl,
)
from app.models.schema_version import SchemaMigrationHistory
from app.services import schema_update_control as control
from app.services.schema_migrations import (
    MIGRATION_SOURCE,
    PRODUCTION_MIGRATIONS,
    migration_definition_fingerprint,
)


ROOT = Path(__file__).resolve().parents[3]
OLD_REQUEST_ID = "update-" + "1" * 32
NEW_REQUEST_ID = "update-" + "2" * 32
OLD_ADMISSION_ID = "migration-attempt-" + "3" * 32
NEW_ADMISSION_ID = "migration-attempt-" + "4" * 32
TARGET_COMMIT = "e" * 40
POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL") or os.getenv(
    "KMVMS_STAGE3_POSTGRES_URL"
)


def load_bridge():
    path = ROOT / "scripts/km-vms-update-helper-bridge.py"
    spec = importlib.util.spec_from_file_location(
        f"pre_stage661_bridge_{uuid.uuid4().hex}",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def update_context(*, request: dict | None = None) -> control.UpdateContext:
    installed_version = "0.7.27"
    installed_commit = control.SOURCE_TAG_COMMITS[installed_version]
    source_shape = control.SOURCE_SHAPE_FINGERPRINTS[installed_version]
    return control.UpdateContext(
        request=request or {},
        request_id=NEW_REQUEST_ID,
        admission_attempt_id=NEW_ADMISSION_ID,
        target_release="0.7.28",
        target_commit=TARGET_COMMIT,
        installed_version=installed_version,
        installed_commit=installed_commit,
        source_schema_version=8,
        source_shape_fingerprint=source_shape,
        registry_fingerprint=control.REGISTRY_FINGERPRINT,
        plan_fingerprint=control.plan_fingerprint(
            installed_version=installed_version,
            installed_commit=installed_commit,
            source_schema_version=8,
            source_shape_fingerprint=source_shape,
            target_release="0.7.28",
            target_commit=TARGET_COMMIT,
        ),
    )


class _Result:
    def __init__(
        self,
        *,
        row: dict | None = None,
        rowcount: int = 1,
        scalar: int | None = None,
    ):
        self.row = row
        self.rowcount = rowcount
        self.scalar = scalar

    def mappings(self):
        return self

    def one(self):
        assert self.row is not None
        return self.row

    def one_or_none(self):
        return self.row

    def scalar_one(self):
        assert self.scalar is not None
        return self.scalar


class _RolloverDb:
    def __init__(self, row: dict):
        self.row = row
        self.updates: list[dict] = []
        self.commits = 0

    def execute(self, statement, parameters=None):
        sql = str(statement)
        if "SELECT" in sql and "FROM schema_migration_control" in sql:
            return _Result(row=self.row)
        if "UPDATE schema_migration_control" in sql:
            self.updates.append(dict(parameters or {}))
            return _Result(rowcount=1)
        raise AssertionError(sql)

    def commit(self):
        self.commits += 1


def test_one_canonical_lineage_drives_api_and_bridge():
    payload = json.loads(
        (ROOT / "release/km-vms-update-lineage.json").read_text(
            encoding="utf-8"
        )
    )
    bridge = load_bridge()

    assert control.SOURCE_TAG_COMMITS == payload["tag_commits"]
    assert bridge.SOURCE_TAG_COMMITS == payload["tag_commits"]
    assert control.SOURCE_SCHEMA_VERSIONS["0.7.25"] == 8
    assert control.SOURCE_SCHEMA_VERSIONS["0.7.26"] == 8
    assert control.SOURCE_SCHEMA_VERSIONS["0.7.27"] == 8
    for version in ("0.7.25", "0.7.26", "0.7.27"):
        assert (
            control.SOURCE_SHAPE_FINGERPRINTS[version]
            == payload["shape_fingerprints"][version]
        )
        commit = payload["tag_commits"][version]
        assert commit not in (
            ROOT
            / "apps/api/app/services/schema_update_control.py"
        ).read_text(encoding="utf-8")
        assert commit not in (
            ROOT / "scripts/km-vms-update-helper-bridge.py"
        ).read_text(encoding="utf-8")


def test_target_mismatch_can_only_load_a_completed_rollover(
    monkeypatch: pytest.MonkeyPatch,
):
    request = {"request_id": NEW_REQUEST_ID}
    monkeypatch.setattr(
        control,
        "read_regular_json",
        lambda path: request if path == control.REQUEST_PATH else None,
    )
    monkeypatch.setattr(control, "validate_update_request", lambda _value: "current")
    monkeypatch.setattr(
        control,
        "target_identity",
        lambda _request, **_kwargs: ("0.7.28", TARGET_COMMIT),
    )
    sentinel = update_context()
    monkeypatch.setattr(
        control,
        "load_prebootstrap_update_context",
        lambda _db: sentinel,
    )
    row = {
        "target_commit": control.SOURCE_TAG_COMMITS["0.7.27"],
        "target_release": "0.7.27",
        "target_schema_version": 8,
        "registry_fingerprint": control.REGISTRY_FINGERPRINT,
        "control_definition_fingerprint": (
            control.CONTROL_DEFINITION_FINGERPRINT
        ),
        "state": "completed",
    }
    db = _RolloverDb(row)

    assert (
        control.load_existing_update_context(
            db,
            allow_completed_rollover=True,
        )
        is sentinel
    )
    with pytest.raises(
        control.SchemaControlError,
        match="schema_migration_control_target_mismatch",
    ):
        control.load_existing_update_context(db)

    row["state"] = "failed"
    with pytest.raises(
        control.SchemaControlError,
        match="schema_migration_control_rollover_requires_completed",
    ):
        control.load_existing_update_context(
            db,
            allow_completed_rollover=True,
        )


def test_completed_rollover_rebinds_every_control_field_and_increments_generation(
    monkeypatch: pytest.MonkeyPatch,
):
    context = update_context()
    row = {
        "fencing_generation": 7,
        "owner_attempt_id": OLD_ADMISSION_ID,
        "request_id": OLD_REQUEST_ID,
        "state": "completed",
    }
    db = _RolloverDb(row)
    receipt_context = update_context()
    validated: list[object] = []
    auth: list[dict] = []
    monkeypatch.setattr(control, "_control_tables_present", lambda _db: (True, True))
    monkeypatch.setattr(
        control,
        "control_table_population",
        lambda _db: (1, 1),
    )
    monkeypatch.setattr(
        control,
        "read_signed",
        lambda _path, **_kwargs: {"state": "adopted"},
    )
    monkeypatch.setattr(
        control,
        "_validate_persistent_bootstrap_evidence",
        lambda _db, _payload, **_kwargs: (
            receipt_context,
            "6" * 64,
        ),
    )
    monkeypatch.setattr(
        control,
        "validate_exact_target_noop",
        lambda value: validated.append(value),
    )
    monkeypatch.setattr(
        control,
        "write_auth_snapshot",
        lambda **kwargs: auth.append(kwargs),
    )

    generation = control.bootstrap_or_resume_control(
        db,
        context=context,
        actor_user_id=1,
        actor_subject="owner",
        actor_role="owner",
    )

    assert generation == 8
    assert validated == [db]
    assert db.commits == 1
    assert len(db.updates) == 1
    update = db.updates[0]
    assert update["generation"] == 8
    assert update["owner_attempt_id"] == NEW_ADMISSION_ID
    assert update["request_id"] == NEW_REQUEST_ID
    assert update["installed_version"] == "0.7.27"
    assert (
        update["installed_commit"]
        == control.SOURCE_TAG_COMMITS["0.7.27"]
    )
    assert update["source_schema_version"] == 8
    assert update["target_release"] == "0.7.28"
    assert update["target_commit"] == TARGET_COMMIT
    assert (
        update["target_schema_version"]
        == control.TARGET_SCHEMA_VERSION
    )
    assert update["registry_fingerprint"] == control.REGISTRY_FINGERPRINT
    assert update["plan_fingerprint"] == context.plan_fingerprint
    assert (
        update["source_shape_fingerprint"]
        == control.SOURCE_SHAPE_FINGERPRINTS["0.7.27"]
    )
    assert (
        update["control_definition_fingerprint"]
        == control.CONTROL_DEFINITION_FINGERPRINT
    )
    assert auth[0]["generation"] == 8


def test_precreated_empty_control_tables_bootstrap_first_update(
    monkeypatch: pytest.MonkeyPatch,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            SchemaMigrationControl.__table__,
            SchemaMigrationAttempt.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    context = update_context()
    receipts: list[dict] = []
    auth: list[dict] = []
    monkeypatch.setattr(
        control,
        "_control_tables_present",
        lambda _db: (True, True),
    )
    monkeypatch.setattr(
        control,
        "read_signed",
        lambda _path, **_kwargs: None,
    )
    monkeypatch.setattr(
        control,
        "write_signed",
        lambda _path, payload: receipts.append(payload),
    )
    monkeypatch.setattr(
        control,
        "write_auth_snapshot",
        lambda **kwargs: auth.append(kwargs),
    )
    monkeypatch.setattr(
        control,
        "verify_control_shape",
        lambda _db: "9" * 64,
    )

    generation = control.bootstrap_or_resume_control(
        session,
        context=context,
        actor_user_id=1,
        actor_subject="owner",
        actor_role="owner",
    )

    current = session.get(
        SchemaMigrationControl,
        control.CURRENT_STATE_ID,
    )
    attempts = session.query(SchemaMigrationAttempt).all()
    assert generation == 1
    assert current is not None
    assert current.request_id == NEW_REQUEST_ID
    assert current.source_schema_version == 8
    assert len(attempts) == 1
    assert attempts[0].status == "applied"
    assert [payload["state"] for payload in receipts] == [
        "prepared",
        "adopted",
    ]
    assert auth[0]["generation"] == 1


@pytest.mark.parametrize(
    ("state", "error"),
    (
        ("prepared", "migration_control_active_rollover_forbidden"),
        ("recovering", "migration_control_active_rollover_forbidden"),
        ("migrating", "migration_control_active_rollover_forbidden"),
        ("failed", "migration_control_failed_rollover_forbidden"),
    ),
)
def test_noncompleted_foreign_control_cannot_roll_over(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    error: str,
):
    row = {
        "fencing_generation": 7,
        "owner_attempt_id": OLD_ADMISSION_ID,
        "request_id": OLD_REQUEST_ID,
        "state": state,
    }
    db = _RolloverDb(row)
    monkeypatch.setattr(control, "_control_tables_present", lambda _db: (True, True))
    monkeypatch.setattr(
        control,
        "control_table_population",
        lambda _db: (1, 1),
    )
    monkeypatch.setattr(
        control,
        "read_signed",
        lambda _path, **_kwargs: {"state": "adopted"},
    )
    monkeypatch.setattr(
        control,
        "_validate_persistent_bootstrap_evidence",
        lambda _db, _payload, **_kwargs: (
            update_context(),
            "6" * 64,
        ),
    )

    with pytest.raises(control.SchemaControlError, match=error):
        control.bootstrap_or_resume_control(
            db,
            context=update_context(),
            actor_user_id=1,
            actor_subject="owner",
            actor_role="owner",
        )
    assert db.updates == []
    assert db.commits == 0


def _stage_receipt(
    context: control.UpdateContext,
    *,
    generation: int,
    state: str,
    error_code: str = "",
    details: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "request_id": context.request_id,
        "admission_attempt_id": context.admission_attempt_id,
        "target_version": context.target_release,
        "target_commit": context.target_commit,
        "target_schema_version": control.TARGET_SCHEMA_VERSION,
        "registry_fingerprint": context.registry_fingerprint,
        "plan_fingerprint": context.plan_fingerprint,
        "fencing_generation": generation,
        "attempt_id": context.admission_attempt_id,
        "state": state,
        "phase": "preparing_database",
        "retryable": False,
        "error_code": error_code,
        "summary": "Bounded schema result.",
        "operator_action": "Continue only when the evidence is exact.",
        "details": details or {},
        "updated_at": "2026-07-28T12:00:00Z",
    }


def test_exact_known_false_legacy_gate_failure_is_reconciled_without_history_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    old_registry = hashlib.sha256(b"old-registry").hexdigest()
    old_target = "0.7.29"
    old_target_commit = control.SOURCE_TAG_COMMITS[old_target]
    old_source = "0.7.27"
    old_source_commit = control.SOURCE_TAG_COMMITS[old_source]
    old_plan = control.plan_fingerprint(
        installed_version=old_source,
        installed_commit=old_source_commit,
        source_schema_version=8,
        source_shape_fingerprint=control.TARGET_SHAPE_FINGERPRINT,
        target_release=old_target,
        target_commit=old_target_commit,
        registry_fingerprint_value=old_registry,
    )
    row = {
        "fencing_generation": 2,
        "owner_attempt_id": OLD_ADMISSION_ID,
        "request_id": OLD_REQUEST_ID,
        "installed_version": old_source,
        "installed_commit": old_source_commit,
        "source_schema_version": 8,
        "source_shape_fingerprint": control.TARGET_SHAPE_FINGERPRINT,
        "target_release": old_target,
        "target_commit": old_target_commit,
        "target_schema_version": 8,
        "registry_fingerprint": old_registry,
        "plan_fingerprint": old_plan,
        "control_definition_fingerprint": (
            control.CONTROL_DEFINITION_FINGERPRINT
        ),
        "state": "failed",
    }
    context = control._failed_control_context(row)
    preparation_path = tmp_path / "preparation.signed.json"
    recovery_path = tmp_path / "recovery.signed.json"
    gate_path = tmp_path / "gate.signed.json"
    mutation_path = tmp_path / "mutation-state.json"
    monkeypatch.setattr(
        control,
        "JWT_SECRET",
        "known-false-legacy-gate-test-secret",
    )
    monkeypatch.setattr(
        control,
        "PREPARATION_RECEIPT_PATH",
        preparation_path,
    )
    monkeypatch.setattr(
        control,
        "RECOVERY_RECEIPT_PATH",
        recovery_path,
    )
    monkeypatch.setattr(control, "GATE_RECEIPT_PATH", gate_path)
    monkeypatch.setattr(
        control,
        "SCHEMA_MUTATION_STATE_PATH",
        mutation_path,
    )
    control.write_signed(
        preparation_path,
        _stage_receipt(context, generation=2, state="completed"),
    )
    control.write_signed(
        recovery_path,
        _stage_receipt(context, generation=2, state="completed"),
    )
    retry_evidence = {
        "schema_version": 1,
        "mutation_started": False,
        "physical_mutation_possible": False,
        "transaction_rolled_back": True,
        "rollback_verified": False,
        "schema_shape_unchanged": False,
        "history_unchanged": False,
        "canonical_transition_committed": False,
        "foreign_state_detected": False,
    }
    control.write_signed(
        gate_path,
        _stage_receipt(
            context,
            generation=2,
            state="blocked",
            error_code="legacy_migration_history_unknown_id",
            details={"retry_evidence": retry_evidence},
        ),
    )
    validated: list[str] = []
    monkeypatch.setattr(
        control,
        "validate_exact_target_noop",
        lambda _db, *, expected_control_state: validated.append(
            expected_control_state
        ),
    )

    class ReconcileDb(_RolloverDb):
        def execute(self, statement, parameters=None):
            sql = str(statement)
            if (
                "SELECT COUNT(*)" in sql
                and "schema_migration_attempts" in sql
            ):
                return _Result(scalar=0)
            return super().execute(statement, parameters)

    db = ReconcileDb(row)
    assert control._reconcile_known_false_legacy_gate_failure(db, row)
    assert validated == ["failed"]
    assert len(db.updates) == 1
    assert db.updates[0]["generation"] == 2
    assert db.updates[0]["request_id"] == OLD_REQUEST_ID

    unsafe_evidence = dict(retry_evidence)
    unsafe_evidence["mutation_started"] = True
    control.write_signed(
        gate_path,
        _stage_receipt(
            context,
            generation=2,
            state="blocked",
            error_code="legacy_migration_history_unknown_id",
            details={"retry_evidence": unsafe_evidence},
        ),
    )
    unsafe_db = ReconcileDb(row)
    assert not control._reconcile_known_false_legacy_gate_failure(
        unsafe_db,
        row,
    )
    assert unsafe_db.updates == []


def _history_rows():
    rows = [
        SimpleNamespace(
            migration_id="chapter06_stage4_baseline_schema_v1",
            source="adopted_existing_db",
            baseline_id=control.CURRENT_BASELINE_ID,
            error_summary=None,
            previous_version=None,
            target_version=1,
            schema_version=1,
            status="adopted_baseline",
            checksum=None,
        )
    ]
    for migration in PRODUCTION_MIGRATIONS.migrations:
        rows.append(
            SimpleNamespace(
                migration_id=migration.migration_id,
                source=MIGRATION_SOURCE,
                baseline_id=control.CURRENT_BASELINE_ID,
                error_summary=None,
                previous_version=migration.from_version,
                target_version=migration.to_version,
                schema_version=migration.to_version,
                status="applied",
                checksum=migration_definition_fingerprint(migration),
            )
        )
    return rows


def _historical_attempts(*, registry_fingerprint: str | None = None):
    source_shape = control.SOURCE_SHAPE_FINGERPRINTS["0.7.18"]
    target_commit = control.SOURCE_TAG_COMMITS["0.7.27"]
    registry_fingerprint = (
        control.REGISTRY_FINGERPRINT
        if registry_fingerprint is None
        else registry_fingerprint
    )
    historical_plan = control.plan_fingerprint(
        installed_version="0.7.18",
        installed_commit=control.SOURCE_TAG_COMMITS["0.7.18"],
        source_schema_version=1,
        source_shape_fingerprint=source_shape,
        target_release="0.7.27",
        target_commit=target_commit,
        registry_fingerprint_value=registry_fingerprint,
    )
    common = {
        "admission_attempt_id": OLD_ADMISSION_ID,
        "request_id": OLD_REQUEST_ID,
        "fencing_generation": 1,
        "installed_version": "0.7.18",
        "installed_commit": control.SOURCE_TAG_COMMITS["0.7.18"],
        "target_release": "0.7.27",
        "target_commit": target_commit,
        "registry_fingerprint": registry_fingerprint,
        "plan_fingerprint": historical_plan,
        "status": "applied",
        "completed_at": object(),
        "after_shape_fingerprint": "9" * 64,
        "failure_class": None,
        "failure_summary": None,
        "resumable": False,
    }
    attempts = [
        SimpleNamespace(
            **common,
            attempt_id="migration-attempt-" + "a" * 32,
            migration_id=control.CONTROL_BOOTSTRAP_MIGRATION_ID,
            previous_version=1,
            target_version=1,
            definition_fingerprint=(
                control.CONTROL_DEFINITION_FINGERPRINT
            ),
            before_shape_fingerprint=source_shape,
        )
    ]
    for migration in PRODUCTION_MIGRATIONS.migrations:
        suffix = hashlib.sha256(migration.migration_id.encode()).hexdigest()[:32]
        attempts.append(
            SimpleNamespace(
                **common,
                attempt_id="migration-attempt-" + suffix,
                migration_id=migration.migration_id,
                previous_version=migration.from_version,
                target_version=migration.to_version,
                definition_fingerprint=migration_definition_fingerprint(
                    migration
                ),
                before_shape_fingerprint="b" * 64,
            )
        )
    return attempts


def test_completed_previous_generation_history_is_preserved_but_tampering_fails():
    current = SimpleNamespace(
        source_schema_version=8,
        fencing_generation=2,
        owner_attempt_id=NEW_ADMISSION_ID,
        request_id=NEW_REQUEST_ID,
        target_release="0.7.28",
        target_commit=TARGET_COMMIT,
        registry_fingerprint=control.REGISTRY_FINGERPRINT,
        plan_fingerprint="5" * 64,
    )
    histories = _history_rows()
    old_registry = hashlib.sha256(b"historical-registry").hexdigest()
    attempts = _historical_attempts(
        registry_fingerprint=old_registry,
    )

    control._validate_migrated_target_history(
        histories,
        current,
        attempts,
    )

    attempts[0].target_commit = "f" * 40
    with pytest.raises(
        control.SchemaControlError,
        match="no_active_historical_attempt_plan_invalid",
    ):
        control._validate_migrated_target_history(
            histories,
            current,
            attempts,
        )


def test_inventory_bound_migration_reaches_terminal_restart_validation():
    slot_id = "initial-" + ("a" * 64)
    inventory = "b" * 64
    installed_commit = control.inventory_bound_source_token(
        slot_id,
        inventory,
    )
    installed_version = "0.7.18"
    target_release = "0.7.27"
    target_commit = control.SOURCE_TAG_COMMITS[target_release]
    source_shape = control.SOURCE_SHAPE_FINGERPRINTS[installed_version]
    plan = control.plan_fingerprint(
        installed_version=installed_version,
        installed_commit=installed_commit,
        source_schema_version=1,
        source_shape_fingerprint=source_shape,
        target_release=target_release,
        target_commit=target_commit,
    )
    attempts = _historical_attempts()
    for attempt in attempts:
        attempt.installed_commit = installed_commit
        attempt.plan_fingerprint = plan
        attempt.details = {
            "source_identity": {
                "identity_mode": "inventory_bound",
                "slot_id": slot_id,
                "inventory_sha256": inventory,
            }
        }
    terminal = SimpleNamespace(
        id=control.CURRENT_STATE_ID,
        state="completed",
        target_schema_version=control.TARGET_SCHEMA_VERSION,
        registry_fingerprint=control.REGISTRY_FINGERPRINT,
        control_definition_fingerprint=(
            control.CONTROL_DEFINITION_FINGERPRINT
        ),
        installed_version=installed_version,
        installed_commit=installed_commit,
        source_schema_version=1,
        source_shape_fingerprint=source_shape,
        target_release=target_release,
        target_commit=target_commit,
        plan_fingerprint=plan,
        request_id=OLD_REQUEST_ID,
        owner_attempt_id=OLD_ADMISSION_ID,
        fencing_generation=1,
    )

    validated = control._validate_terminal_control(
        [terminal],
        attempts,
    )
    control._validate_migrated_target_history(
        _history_rows(),
        validated,
        attempts,
    )


@pytest.fixture
def rollover_pg_session():
    if not POSTGRES_URL:
        pytest.skip("A disposable PostgreSQL test URL is required")
    schema = f"pre661_{uuid.uuid4().hex}"
    admin_engine = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped_engine = create_engine(
        POSTGRES_URL,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(
        scoped_engine,
        tables=[
            SchemaMigrationControl.__table__,
            SchemaMigrationAttempt.__table__,
            SchemaMigrationHistory.__table__,
        ],
    )
    Session = sessionmaker(
        bind=scoped_engine,
        autoflush=False,
        autocommit=False,
    )
    session = Session()
    try:
        yield session
    finally:
        session.close()
        scoped_engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        admin_engine.dispose()


def test_postgres_same_schema_rollover_is_fenced_replay_safe_and_keeps_history(
    rollover_pg_session,
    monkeypatch: pytest.MonkeyPatch,
):
    db = rollover_pg_session
    now = datetime.utcnow()
    old_source_shape = control.SOURCE_SHAPE_FINGERPRINTS["0.7.18"]
    old_target_commit = control.SOURCE_TAG_COMMITS["0.7.27"]
    old_plan = control.plan_fingerprint(
        installed_version="0.7.18",
        installed_commit=control.SOURCE_TAG_COMMITS["0.7.18"],
        source_schema_version=1,
        source_shape_fingerprint=old_source_shape,
        target_release="0.7.27",
        target_commit=old_target_commit,
    )
    db.add(
        SchemaMigrationControl(
            id=control.CURRENT_STATE_ID,
            fencing_generation=1,
            owner_attempt_id=OLD_ADMISSION_ID,
            request_id=OLD_REQUEST_ID,
            installed_version="0.7.18",
            installed_commit=control.SOURCE_TAG_COMMITS["0.7.18"],
            source_schema_version=1,
            target_commit=old_target_commit,
            target_release="0.7.27",
            target_schema_version=8,
            registry_fingerprint=control.REGISTRY_FINGERPRINT,
            plan_fingerprint=old_plan,
            source_shape_fingerprint=old_source_shape,
            control_definition_fingerprint=(
                control.CONTROL_DEFINITION_FINGERPRINT
            ),
            state="completed",
            lease_expires_at=now + timedelta(minutes=15),
            updated_at=now,
        )
    )
    db.add(
        SchemaMigrationAttempt(
            attempt_id=control.transition_attempt_id(
                OLD_ADMISSION_ID,
                control.CONTROL_BOOTSTRAP_MIGRATION_ID,
            ),
            admission_attempt_id=OLD_ADMISSION_ID,
            request_id=OLD_REQUEST_ID,
            migration_id=control.CONTROL_BOOTSTRAP_MIGRATION_ID,
            previous_version=1,
            target_version=1,
            status="applied",
            started_at=now,
            completed_at=now,
            fencing_generation=1,
            installed_version="0.7.18",
            installed_commit=control.SOURCE_TAG_COMMITS["0.7.18"],
            target_release="0.7.27",
            target_commit=old_target_commit,
            registry_fingerprint=control.REGISTRY_FINGERPRINT,
            plan_fingerprint=old_plan,
            definition_fingerprint=control.CONTROL_DEFINITION_FINGERPRINT,
            before_shape_fingerprint=old_source_shape,
            after_shape_fingerprint="9" * 64,
            resumable=False,
            details={"control_shape_verified": True},
        )
    )
    migration = PRODUCTION_MIGRATIONS.migrations[-1]
    db.add(
        SchemaMigrationHistory(
            migration_id=migration.migration_id,
            previous_version=migration.from_version,
            target_version=migration.to_version,
            schema_version=migration.to_version,
            baseline_id=control.CURRENT_BASELINE_ID,
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

    context = update_context()
    monkeypatch.setattr(
        control,
        "read_signed",
        lambda _path, **_kwargs: {"state": "adopted"},
    )
    monkeypatch.setattr(
        control,
        "_validate_persistent_bootstrap_evidence",
        lambda _db, _payload, **_kwargs: (context, "9" * 64),
    )
    monkeypatch.setattr(control, "validate_exact_target_noop", lambda _db: None)
    monkeypatch.setattr(control, "write_auth_snapshot", lambda **_kwargs: None)

    history_before = db.scalar(
        select(func.count()).select_from(SchemaMigrationHistory)
    )
    attempt_before = db.scalar(
        select(func.count()).select_from(SchemaMigrationAttempt)
    )
    generation = control.bootstrap_or_resume_control(
        db,
        context=context,
        actor_user_id=1,
        actor_subject="owner",
        actor_role="owner",
    )
    assert generation == 2
    control.update_control_state(
        db,
        context=context,
        generation=generation,
        state="completed",
    )
    db.commit()

    replay_generation = control.bootstrap_or_resume_control(
        db,
        context=context,
        actor_user_id=1,
        actor_subject="owner",
        actor_role="owner",
    )
    db.commit()

    current = db.get(SchemaMigrationControl, control.CURRENT_STATE_ID)
    assert current is not None
    assert replay_generation == generation == 2
    assert current.request_id == NEW_REQUEST_ID
    assert current.owner_attempt_id == NEW_ADMISSION_ID
    assert current.target_release == "0.7.28"
    assert current.target_commit == TARGET_COMMIT
    assert current.source_schema_version == 8
    assert current.state == "completed"
    assert (
        db.scalar(select(func.count()).select_from(SchemaMigrationHistory))
        == history_before
    )
    assert (
        db.scalar(select(func.count()).select_from(SchemaMigrationAttempt))
        == attempt_before
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(SchemaMigrationAttempt)
            .where(
                SchemaMigrationAttempt.status.in_(
                    ("started", "interrupted")
                )
            )
        )
        == 0
    )
