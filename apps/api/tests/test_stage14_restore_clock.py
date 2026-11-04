"""Actual PostgreSQL coordination timestamps must not depend on session TZ."""
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from app.services import current_db_restore_executor as executor


@pytest.mark.parametrize("zone", ["UTC", "Asia/Yekaterinburg", "America/New_York"])
def test_recorder_proof_uses_utc_in_all_database_timezones(monkeypatch, zone):
    url = os.getenv("KMVMS_STAGE1378_POSTGRES_URL")
    if not url:
        pytest.skip("requires isolated PostgreSQL test service")
    schema = "stage14_clock_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-c search_path={schema} -c timezone={zone}"})
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE recorder_runtime_status (recorder_instance_id text, loop_state text, started_at timestamp, heartbeat_at timestamp)"))
            conn.execute(text("INSERT INTO recorder_runtime_status VALUES ('stage14', 'loop', timezone('UTC', NOW()) - interval '5 seconds', timezone('UTC', NOW()) - interval '3 seconds')"))
        monkeypatch.setattr(executor, "create_engine", lambda *args, **kwargs: engine)
        result = executor._recorder_runtime_proof(not_before_epoch=time.time() - 10, timeout_seconds=0)
        assert result["recorder_heartbeat_fresh"] is True
        with engine.begin() as conn:
            conn.execute(text("UPDATE recorder_runtime_status SET heartbeat_at=timezone('UTC', NOW()) - interval '1 hour'"))
        with pytest.raises(executor.RestoreExecutorBlocked, match="restore_recorder_heartbeat_timeout"):
            executor._recorder_runtime_proof(not_before_epoch=None, timeout_seconds=0)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
