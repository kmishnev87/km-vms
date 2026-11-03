from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from test_stage661_activation_convergence import bridge, FakeEngine, _journal, PREVIOUS, TARGET, REQUEST_ID, runtime


def blocked_journal():
    journal = _journal('blocked', migration_required=True, migration_invoked=True)
    journal['failure_category'] = 'schema_update_failed'
    return journal


def test_blocked_recovery_is_verified_and_idempotent(runtime):
    engine = FakeEngine(blocked_journal())
    first = bridge.converge_activation(engine, Path('/app'), 'tnas-vms', REQUEST_ID)
    assert first['phase'] == 'failed_rolled_back'
    assert first['failure_category'] == 'schema_update_failed'
    assert first['previous_verified'] is True
    assert first['schema']['migration_invoked'] is True
    assert bridge.converge_activation(engine, Path('/app'), 'tnas-vms', REQUEST_ID) == first
    assert runtime['cleanup'] == ['failed_rolled_back']


def test_unknown_database_never_restarts_previous_writers(monkeypatch, runtime):
    engine = FakeEngine(blocked_journal())
    def refuse(*_args, **_kwargs):
        raise bridge.BridgeError('rollback_database_unverified', 'Restore verified data first')
    monkeypatch.setattr(bridge, 'verify_rollback_database', refuse)
    result = bridge.converge_activation(engine, Path('/app'), 'tnas-vms', REQUEST_ID)
    assert result['phase'] == 'blocked'
    assert result['failure_category'] == 'schema_update_failed'
    assert result['previous_verified'] is False
    assert runtime['reconcile'] == []
    assert runtime['writer_fence'] == []


def test_recovery_cannot_guess_foreign_pointer(runtime):
    engine = FakeEngine(blocked_journal())
    engine.pointer = 'release-' + 'f' * 40
    result = bridge.converge_activation(engine, Path('/app'), 'tnas-vms', REQUEST_ID)
    assert result['phase'] == 'blocked'
    assert runtime['reconcile'] == []


@pytest.mark.parametrize('output', [
    '', '{}', '{"mode":"verified_original_schema"}',
    '{"request_id":"foreign","mode":"verified_original_schema"}',
])
def test_database_proof_requires_exact_request_and_previous_commit(monkeypatch, output):
    monkeypatch.setattr(bridge, 'slot_compose', lambda *_args, **_kwargs: (['compose'], {}, None, {}))
    monkeypatch.setattr(bridge, 'run_command', lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=''))
    with pytest.raises(bridge.BridgeError, match='Database state is not safe'):
        bridge.verify_rollback_database(object(), Path('/app'), 'tnas-vms', blocked_journal())


def test_read_only_probe_is_not_a_schema_repair():
    compile(bridge.ROLLBACK_DATABASE_PROBE, '<rollback-proof>', 'exec')
    assert 'SET TRANSACTION READ ONLY' in bridge.ROLLBACK_DATABASE_PROBE
    assert 'validate_exact_target_noop' in bridge.ROLLBACK_DATABASE_PROBE
    assert 'classify_retry' in bridge.ROLLBACK_DATABASE_PROBE
    assert 'db.commit()' not in bridge.ROLLBACK_DATABASE_PROBE
    assert 'ALTER TABLE' not in bridge.ROLLBACK_DATABASE_PROBE


def test_compose_fingerprint_ignores_serialization_and_explicit_defaults_but_not_security(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, 'mapped_compose_project_directory', lambda _app, source: source)
    plan = {'services': {'api': {'image': 'immutable-image', 'environment': {'KMVMS_UPDATE_SOURCE_PRIVATE': '0'}, 'volumes': [{'type':'bind','source':str(tmp_path/'data'),'target':'/storage','read_only':True}]},
                         'update-helper-bootstrap': {'environment': {'KM_VMS_UPDATE_CONTROL_REQUEST_ID': REQUEST_ID}}}}
    def digest(value):
        return bridge._normalized_compose_digest(json.dumps(value, indent=2), app_dir=tmp_path, source_dir=tmp_path/'source')
    original = digest(plan)
    equivalent = deepcopy(plan)
    equivalent['services']['api']['volumes'][0]['bind'] = {'create_host_path':True}
    equivalent['services']['api']['environment']['KMVMS_UPDATE_SOURCE_PRIVATE'] = 'false'
    equivalent['services']['update-helper-bootstrap']['environment']['KM_VMS_UPDATE_CONTROL_REQUEST_ID'] = 'update-'+'4'*32
    assert digest(equivalent) == original
    for mutation in ('read_only','source','image','create_host_path','privileged'):
        changed = deepcopy(equivalent)
        api = changed['services']['api']
        if mutation == 'create_host_path': api['volumes'][0]['bind']['create_host_path'] = False
        elif mutation == 'read_only': api['volumes'][0]['read_only'] = False
        elif mutation == 'source': api['volumes'][0]['source'] = '/unapproved/archive'
        elif mutation == 'image': api['image'] = 'other-image'
        else: api['privileged'] = True
        assert digest(changed) != original


def test_verified_healthy_previous_runtime_is_not_recreated(monkeypatch, runtime):
    monkeypatch.setattr(bridge, 'previous_runtime_ready', lambda *_args: True)
    engine = FakeEngine(blocked_journal())
    result = bridge.converge_activation(engine, Path('/app'), 'tnas-vms', REQUEST_ID)
    assert result['phase'] == 'failed_rolled_back'
    assert runtime['reconcile'] == []


def test_configuration_mismatch_blocks_without_three_minute_retry_or_restarts(monkeypatch, runtime):
    def refuse(*_args):
        raise bridge.BridgeError('slot_runtime_evidence_mismatch', 'Different configuration')
    monkeypatch.setattr(bridge, 'previous_runtime_ready', refuse)
    engine = FakeEngine(blocked_journal())
    result = bridge.converge_activation(engine, Path('/app'), 'tnas-vms', REQUEST_ID)
    assert result['phase'] == 'blocked' and result['failure_category'] == 'schema_update_failed'
    assert runtime['reconcile'] == []
