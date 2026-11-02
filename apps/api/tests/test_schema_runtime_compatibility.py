from __future__ import annotations

import copy
import dataclasses
from types import SimpleNamespace

import pytest

from app.services import schema_runtime_compatibility as compat
from app.services import schema_update_pipeline as pipeline
from app.services.schema_migrations import MigrationRegistry, STAGE13721_CAMERA_SUB_PROFILE_TOKEN_MIGRATION, MigrationRegistryError


def migration(start=9, end=10, floor=9):
    return dataclasses.replace(
        STAGE13721_CAMERA_SUB_PROFILE_TOKEN_MIGRATION,
        migration_id=f"test_compatibility_{start}_{end}", from_version=start, to_version=end,
        runtime_compatibility_floor=floor,
    )


def test_every_edge_must_explicitly_preserve_older_reader_and_writer():
    assert compat.target_compatibility_floor(MigrationRegistry([migration(), migration(10, 11)]), 11) == 9
    assert compat.target_compatibility_floor(MigrationRegistry([migration(), migration(10, 11, 10)]), 11) == 10
    assert compat.target_compatibility_floor(MigrationRegistry([migration(floor=None)]), 10) == 10
    assert compat.target_compatibility_floor(MigrationRegistry([]), 11) == 11


@pytest.mark.parametrize("floor", [True, 8, 10, "9"])
def test_invalid_floor_is_not_a_generic_additive_bypass(floor):
    with pytest.raises(MigrationRegistryError):
        migration(floor=floor)


def test_summary_uses_declaration_not_a_growing_list_of_future_ids(monkeypatch):
    monkeypatch.setattr(pipeline, "TARGET_SCHEMA_VERSION", 11)
    summary = pipeline._migration_summary([migration(), migration(10, 11)])
    assert summary["previous_runtime_compatibility"]["status"] == "compatible"
    assert summary["previous_runtime_compatibility"]["required_previous_runtime_protocol"] == 1
    assert pipeline._migration_summary([migration(floor=None)])["previous_runtime_compatibility"]["status"] == "blocked"


@pytest.fixture
def signed_future(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "isolated-test-schema-compatibility-secret")
    payload = {
        "purpose": "schema_runtime_read_write", "protocol": 1, "schema_version": 10,
        "baseline_id": "test", "minimum_reader_version": 9, "minimum_writer_version": 9,
        "shape_sha256": "a" * 64, "history_sha256": "b" * 64, "control_sha256": "c" * 64,
    }
    state = SimpleNamespace(schema_version=10, drift_classification={compat.METADATA_KEY: {
        "payload": copy.deepcopy(payload), "signature": compat._signature(payload),
    }})
    monkeypatch.setattr(compat, "_observed_payload", lambda _db, _state, _floor: copy.deepcopy(payload))
    return state, payload


def test_prepared_runtime_verifies_forward_schema_without_relabeling_it(signed_future):
    state, before = signed_future
    assert compat.validate_runtime_compatibility(None, state, 9) == before
    assert state.schema_version == 10


@pytest.mark.parametrize("kind", ["missing", "signature", "shape", "history", "control", "protocol", "floor", "unknown_field", "secret"])
def test_unknown_or_changed_evidence_does_not_admit_future_database(signed_future, monkeypatch, kind):
    state, observed = signed_future
    envelope = state.drift_classification[compat.METADATA_KEY]
    if kind == "missing":
        state.drift_classification = {}
    elif kind == "signature":
        envelope["signature"] = "0" * 64
    elif kind == "secret":
        monkeypatch.setenv("JWT_SECRET", "different-isolated-compatibility-secret")
    else:
        field = {"shape": "shape_sha256", "history": "history_sha256", "control": "control_sha256", "protocol": "protocol", "floor": "minimum_writer_version", "unknown_field": "extra"}[kind]
        envelope["payload"][field] = 10 if kind == "floor" else "changed"
        envelope["signature"] = compat._signature(envelope["payload"])
    with pytest.raises(compat.RuntimeCompatibilityBlocked):
        compat.validate_runtime_compatibility(None, state, 9)


def test_unprepared_runtime_and_lower_writer_contract_cannot_use_certificate(signed_future):
    state, _payload = signed_future
    assert compat.supports_future_schema(None, state, 8) is False
    assert compat.supports_future_schema(None, state, 10) is False
