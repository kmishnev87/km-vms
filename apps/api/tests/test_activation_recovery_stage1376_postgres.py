import json
import os
import sys
import uuid
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db import session as session_module
from app.services import bootstrap, schema_update_control as control, schema_versioning, schema_migrations
from test_stage661_activation_convergence import bridge, _journal

TEST_URL = os.getenv('KMVMS_STAGE2_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not TEST_URL, reason='owned isolated PostgreSQL required')


@pytest.fixture
def recovery_database(monkeypatch):
    url = make_url(TEST_URL)
    name = 'stage1376_' + uuid.uuid4().hex
    admin = create_engine(url, isolation_level='AUTOCOMMIT')
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name))
    try:
        monkeypatch.setattr(bootstrap, 'engine', engine)
        monkeypatch.setattr(session_module, 'engine', engine)
        bootstrap.init_db()
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


def run_probe(monkeypatch, source=10, target=10):
    binding = _journal()
    binding['schema'].update(source_version=source, target_version=target)
    monkeypatch.setattr(sys, 'argv', ['rollback-proof', json.dumps(binding)])
    exec(compile(bridge.ROLLBACK_DATABASE_PROBE, '<real-pg-rollback-proof>', 'exec'), {})


def test_real_database_proof_is_read_only_and_releases_lock(recovery_database, monkeypatch, capsys):
    with Session(recovery_database) as db:
        before = control.database_shape_fingerprint(db), control.canonical_schema_history_fingerprint(db)
    run_probe(monkeypatch)
    assert json.loads(capsys.readouterr().out)['mode'] == 'verified_original_schema'
    with Session(recovery_database) as db:
        assert before == (control.database_shape_fingerprint(db), control.canonical_schema_history_fingerprint(db))
        assert db.execute(text('SELECT pg_try_advisory_lock(660128)')).scalar_one() is True
        db.execute(text('SELECT pg_advisory_unlock(660128)'))


@pytest.mark.parametrize('change', ['extra_column', 'partial_ddl', 'unknown_version'])
def test_unknown_and_partial_mutation_stays_blocked(recovery_database, monkeypatch, change):
    with recovery_database.begin() as connection:
        if change == 'extra_column':
            connection.execute(text('ALTER TABLE system_settings ADD COLUMN unregistered_change INTEGER NULL'))
        elif change == 'partial_ddl':
            connection.execute(text('ALTER TABLE system_settings DROP COLUMN recording_format_apply_mode'))
        else:
            connection.execute(text("UPDATE schema_version_state SET schema_version=999 WHERE id='current'"))
    with pytest.raises(RuntimeError):
        run_probe(monkeypatch)


def test_completed_signed_forward_schema_allows_previous_prepared_runtime(recovery_database, monkeypatch, capsys):
    monkeypatch.setattr(schema_versioning, 'CURRENT_SCHEMA_VERSION', 9)
    monkeypatch.setattr(schema_migrations, 'CURRENT_SCHEMA_VERSION', 9)
    monkeypatch.setattr(control, 'TARGET_SCHEMA_VERSION', 9)
    run_probe(monkeypatch, source=9, target=10)
    proof = json.loads(capsys.readouterr().out)
    assert proof['mode'] == 'verified_forward_compatible' and proof['schema_version'] == 10


def test_concurrent_schema_owner_is_not_interrupted_or_waited_forever(recovery_database, monkeypatch):
    with recovery_database.connect() as other:
        assert other.execute(text('SELECT pg_try_advisory_lock(660128)')).scalar_one() is True
        try:
            with pytest.raises(RuntimeError, match='schema_operation_still_active'):
                run_probe(monkeypatch)
        finally:
            other.execute(text('SELECT pg_advisory_unlock(660128)'))


def test_export_generation_lease_is_exclusive_and_disappears_with_process_connection(recovery_database):
    from app.services.archive_exports import _generation_database_lease, archive_export_generation_active, _generation_lease_key
    from fastapi import HTTPException
    with Session(recovery_database) as db:
        with _generation_database_lease(db, 'isolated-job'):
            assert archive_export_generation_active(db, 'isolated-job') is True
            with pytest.raises(HTTPException) as exc:
                with _generation_database_lease(db, 'isolated-job'):
                    pass
            assert exc.value.status_code == 409
        assert archive_export_generation_active(db, 'isolated-job') is False
        with recovery_database.connect() as lost_owner:
            lost_owner.execute(text('SELECT pg_advisory_lock(:key)'), {'key': _generation_lease_key('isolated-job')})
            assert archive_export_generation_active(db, 'isolated-job') is True
            lost_owner.invalidate()  # actual PG connection loss, not just a mock status
        assert archive_export_generation_active(db, 'isolated-job') is False
