"""Historical migration proof keeps its own target when the next target changes."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.models.schema_migration_control import SchemaMigrationAttempt
from app.services import schema_update_control as control
from app.services.schema_migrations import MigrationRegistry, PRODUCTION_MIGRATIONS, migration_registry_fingerprint


def legacy_context(*, source_shape=None):
    registry = MigrationRegistry(
        [m for m in PRODUCTION_MIGRATIONS.migrations if m.to_version <= 10],
        published_variants=[m for m in PRODUCTION_MIGRATIONS.published_migrations
                            if m not in PRODUCTION_MIGRATIONS.migrations and m.to_version <= 10],
    )
    fingerprint = migration_registry_fingerprint(registry)
    values = dict(installed_version="0.8.23", installed_commit=control.SOURCE_TAG_COMMITS["0.8.23"],
        source_schema_version=9, source_shape_fingerprint=source_shape or control.SOURCE_SHAPE_FINGERPRINTS["0.8.23"],
        target_release="0.8.25", target_commit=control.SOURCE_TAG_COMMITS["0.8.25"])
    old = replace(control._schema_transition_compatibility(), TARGET_SCHEMA_VERSION=10, PRODUCTION_MIGRATIONS=registry)
    return control.UpdateContext(request={}, request_id="update-" + "1" * 32,
        admission_attempt_id="migration-attempt-" + "2" * 32, registry_fingerprint=fingerprint,
        plan_fingerprint=old.plan_fingerprint(**values, registry_fingerprint_value=fingerprint), **values)


def legacy_proof(monkeypatch):
    context = legacy_context()
    shape = "a" * 64
    payload = control._bootstrap_receipt_payload(context=context, generation=1,
        state="adopted", control_shape_fingerprint=shape) | {"target_schema_version": 10}
    attempt = SimpleNamespace(status="applied", request_id=context.request_id,
        admission_attempt_id=context.admission_attempt_id, migration_id=control.CONTROL_BOOTSTRAP_MIGRATION_ID,
        fencing_generation=1, previous_version=9, target_version=9,
        installed_version=context.installed_version, installed_commit=context.installed_commit,
        target_release=context.target_release, target_commit=context.target_commit,
        registry_fingerprint=context.registry_fingerprint, plan_fingerprint=context.plan_fingerprint,
        definition_fingerprint=control.CONTROL_DEFINITION_FINGERPRINT,
        before_shape_fingerprint=context.source_shape_fingerprint, after_shape_fingerprint=shape,
        failure_class=None, failure_summary=None, resumable=False)
    def get(model, identifier):
        assert model is SchemaMigrationAttempt
        assert identifier == control.transition_attempt_id(context.admission_attempt_id, control.CONTROL_BOOTSTRAP_MIGRATION_ID)
        return attempt
    monkeypatch.setattr(control, "verify_control_shape", lambda db: shape)
    return context, payload, attempt, SimpleNamespace(get=get)


def test_previous_signed_bootstrap_is_not_reinterpreted_as_current_schema(monkeypatch):
    context, payload, _attempt, db = legacy_proof(monkeypatch)
    restored, shape = control._validate_persistent_bootstrap_evidence(db, payload, current_generation=2)
    assert restored == context
    assert shape == payload["control_shape_fingerprint"]
    assert payload["target_schema_version"] == 10
    assert control.TARGET_SCHEMA_VERSION == 11


def test_new_admission_does_not_accept_a_legacy_target(monkeypatch):
    context, payload, _attempt, _db = legacy_proof(monkeypatch)
    with pytest.raises(control.SchemaControlError, match="control_bootstrap_receipt_target_schema_version_mismatch"):
        control._validate_bootstrap_receipt(payload, context=context)


@pytest.mark.parametrize("field,value", [("target_schema_version", 12), ("target_schema_version", True),
    ("target_commit", "f" * 40), ("plan_fingerprint", "f" * 64), ("registry_fingerprint", "f" * 64)])
def test_historical_receipt_does_not_admit_changed_bindings(monkeypatch, field, value):
    _context, payload, _attempt, db = legacy_proof(monkeypatch)
    payload[field] = value
    with pytest.raises(control.SchemaControlError):
        control._validate_persistent_bootstrap_evidence(db, payload, current_generation=2)


def test_historical_receipt_still_requires_its_real_database_attempt(monkeypatch):
    _context, payload, attempt, db = legacy_proof(monkeypatch)
    attempt.target_commit = "f" * 40
    with pytest.raises(control.SchemaControlError, match="schema_execution_terminal_bootstrap_database_mismatch"):
        control._validate_persistent_bootstrap_evidence(db, payload, current_generation=2)
