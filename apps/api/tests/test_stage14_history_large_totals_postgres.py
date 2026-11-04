import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import app.models  # noqa: F401 - register the complete FK graph
from app.db.session import Base
from app.models.storage_operation import StorageOperation
from app.services.storage_monitoring import useful_storage_operation_history

TEST_URL = os.getenv('KMVMS_STAGE2_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not TEST_URL, reason='owned isolated PostgreSQL required')


@pytest.fixture
def history_database():
    url = make_url(TEST_URL)
    name = 'stage14_history_' + uuid.uuid4().hex
    admin = create_engine(url, isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name))
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


@pytest.mark.parametrize('bytes_freed', [213904547979, 4 * 1024**4, 2**31 + 1])
def test_real_postgres_history_accepts_large_archive_totals(history_database, bytes_freed):
    now = datetime.utcnow()
    with Session(history_database) as db:
        for index, result in enumerate([
            {'bytes_freed': bytes_freed, 'deleted_count': 12},
            {'bytes_freed': 0, 'deleted_count': 0},
            None,
        ]):
            db.add(StorageOperation(id=f'history-{index}', operation_type='retention_auto_run',
                actor_kind='system', actor_key='system:retention', idempotency_key=f'history-{index}',
                request_fingerprint='a' * 64, status='completed', scope={}, progress={},
                result=result, finished_at=now, retry_mode='none'))
        db.commit()
        result = useful_storage_operation_history(db)
        assert result['summary']['camera_retention']['bytes_freed'] == bytes_freed
        assert result['summary']['camera_retention']['deleted_count'] == 12
        assert sum(row['bytes_freed'] for row in result['daily_items']) == bytes_freed
