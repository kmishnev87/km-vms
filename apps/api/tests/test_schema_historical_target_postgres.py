"""Two real schema generations, actual signed receipts and database attempts."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import text

from app.models.schema_migration_control import SchemaMigrationControl
from app.services import schema_update_control as control
from app.services.schema_migrations import PRODUCTION_MIGRATIONS, execute_migration_plan, migration_definition_fingerprint
from test_format_schema_stage1375_postgres import format_database, business_snapshot, pytestmark
from test_schema_historical_target_update import legacy_context


def complete_transition(db, context, generation, target):
    migration = PRODUCTION_MIGRATIONS.canonical_for_edge(target - 1, target)
    attempt, status = control.start_attempt(db, context=context, generation=generation,
        transition_id=migration.migration_id, previous_version=target-1, target_version=target,
        definition_fingerprint=migration_definition_fingerprint(migration),
        before_shape_fingerprint=control.database_shape_fingerprint(db))
    assert status == "started"
    db.commit()
    execute_migration_plan(db, target_version=target)
    control.finish_attempt(db, attempt_id=attempt, status="applied",
        after_shape_fingerprint=control.database_shape_fingerprint(db), details={"schema_verified": True})
    control.update_control_state(db, context=context, generation=generation, state="completed")
    db.commit()


def historical_install(db, monkeypatch, tmp_path):
    # The shared formula fixture uses a deliberately synthetic seed_v9 row.
    # A real installation has a proper fresh/adopted baseline, not that marker.
    db.execute(text("UPDATE schema_migration_history SET migration_id=:migration, source='fresh_create_all' WHERE migration_id='seed_v9'"),
        {"migration": f"{control.CURRENT_BASELINE_ID}_schema_v9"})
    db.execute(text("UPDATE schema_version_state SET source='fresh_create_all' WHERE id='current'"))
    db.commit()
    for name in ("CONTROL_BOOTSTRAP_RECEIPT_PATH", "AUTH_PATH", "PRE_OVERLAY_IDENTITY_PATH"):
        monkeypatch.setattr(control, name, tmp_path / getattr(control, name).name)
    old = legacy_context(source_shape=control.database_shape_fingerprint(db))
    with monkeypatch.context() as previous:
        previous.setattr(control, "TARGET_SCHEMA_VERSION", 10)
        generation = control.bootstrap_or_resume_control(db, context=old,
            actor_user_id=1, actor_subject="preserved-test-user", actor_role="owner")
        assert generation == 1
        complete_transition(db, old, generation, 10)
    control.validate_exact_target_noop(db, expected_schema_version=10)
    receipt = control.CONTROL_BOOTSTRAP_RECEIPT_PATH.read_bytes()
    shape = control.database_shape_fingerprint(db)
    values = dict(installed_version="0.9.11", installed_commit=control.SOURCE_TAG_COMMITS["0.9.11"],
        source_schema_version=10, source_shape_fingerprint=shape, target_release="0.9.14", target_commit="f"*40)
    new = control.UpdateContext(request={}, request_id="update-" + "3"*32,
        admission_attempt_id="migration-attempt-" + "4"*32, registry_fingerprint=control.REGISTRY_FINGERPRINT,
        plan_fingerprint=control.plan_fingerprint(**values), **values)
    control.atomic_write(control.PRE_OVERLAY_IDENTITY_PATH, {"schema_version": 1, "request_id": new.request_id,
        "installed_version": new.installed_version, "installed_commit": new.installed_commit,
        "identity_mode": "trusted_release", "slot_id": None, "inventory_sha256": None,
        "recorded_at": control.utc_now()})
    return old, new, receipt


@pytest.mark.parametrize("prepared_receipt", [False, True])
def test_real_bootstrap_9_to_10_then_10_to_11_preserves_signed_history(format_database, monkeypatch, tmp_path, prepared_receipt):
    _engine, db = format_database
    before = business_snapshot(db)
    old, new, original_receipt = historical_install(db, monkeypatch, tmp_path)
    if prepared_receipt:
        proof = control.read_signed(control.CONTROL_BOOTSTRAP_RECEIPT_PATH)
        proof.update(state="prepared", control_shape_fingerprint=None)
        control.write_signed(control.CONTROL_BOOTSTRAP_RECEIPT_PATH, proof)
    generation = control.bootstrap_or_resume_control(db, context=new,
        actor_user_id=1, actor_subject="preserved-test-user", actor_role="owner")
    assert generation == 2
    proof = control.read_signed(control.CONTROL_BOOTSTRAP_RECEIPT_PATH)
    assert proof["target_schema_version"] == 10 and proof["target_release"] == old.target_release
    if not prepared_receipt:
        assert control.CONTROL_BOOTSTRAP_RECEIPT_PATH.read_bytes() == original_receipt
    # Idempotent same-admission restart does not create another generation.
    assert control.bootstrap_or_resume_control(db, context=new,
        actor_user_id=1, actor_subject="preserved-test-user", actor_role="owner") == 2
    complete_transition(db, new, generation, 11)
    control.validate_exact_target_noop(db)
    control._validate_persistent_bootstrap_evidence(db, proof, current_generation=2)
    assert business_snapshot(db) == before
    row = db.get(SchemaMigrationControl, "current")
    assert row.target_schema_version == 11 and row.target_release == new.target_release
    assert db.execute(text("SELECT events_enabled FROM cameras WHERE id=1")).scalar_one() is False


def test_tampered_old_signature_blocks_rollover_before_any_schema_change(format_database, monkeypatch, tmp_path):
    _engine, db = format_database
    _old, new, _receipt = historical_install(db, monkeypatch, tmp_path)
    before = business_snapshot(db)
    envelope = json.loads(control.CONTROL_BOOTSTRAP_RECEIPT_PATH.read_text())
    envelope["payload"]["target_schema_version"] = 11
    control.atomic_write(control.CONTROL_BOOTSTRAP_RECEIPT_PATH, envelope)
    with pytest.raises(control.SchemaControlError, match="signature_invalid"):
        control.bootstrap_or_resume_control(db, context=new,
            actor_user_id=1, actor_subject="preserved-test-user", actor_role="owner")
    db.rollback()
    assert db.get(SchemaMigrationControl, "current").fencing_generation == 1
    assert db.execute(text("SELECT schema_version FROM schema_version_state WHERE id='current'")).scalar_one() == 10
    assert business_snapshot(db) == before
