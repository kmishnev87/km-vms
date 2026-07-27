from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.services.schema_update_control import (
    SchemaControlError,
    acquire_schema_lock,
    release_schema_lock,
    wait_for_writer_quiescence,
)


POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL") or os.getenv(
    "KMVMS_STAGE3_POSTGRES_URL"
)

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="A disposable PostgreSQL URL is required",
)


def test_pipeline_lock_connection_is_excluded_but_external_writer_blocks() -> None:
    database_name = f"kmvms_quiescence_{uuid.uuid4().hex}"
    base_url = make_url(POSTGRES_URL)
    admin_engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
    database_engine = None
    lock_db = None
    probe_db = None
    writer_db = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        database_url = base_url.set(database=database_name)
        database_engine = create_engine(database_url, poolclass=NullPool)
        with database_engine.begin() as connection:
            connection.execute(text("CREATE TABLE writer_probe (id integer)"))

        lock_db = Session(database_engine)
        probe_db = Session(database_engine)
        lock_backend_pid = acquire_schema_lock(lock_db)

        wait_for_writer_quiescence(
            probe_db,
            owned_backend_pid=lock_backend_pid,
            timeout_seconds=0,
        )

        writer_db = Session(database_engine)
        writer_db.execute(text("INSERT INTO writer_probe (id) VALUES (1)"))
        probe_db.close()
        probe_db = Session(database_engine)
        with pytest.raises(
            SchemaControlError,
            match="product_database_writer_not_quiescent",
        ):
            wait_for_writer_quiescence(
                probe_db,
                owned_backend_pid=lock_backend_pid,
                timeout_seconds=0,
            )
    finally:
        if writer_db is not None:
            writer_db.rollback()
            writer_db.close()
        if probe_db is not None:
            probe_db.close()
        if lock_db is not None:
            release_schema_lock(lock_db)
            lock_db.close()
        if database_engine is not None:
            database_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :database_name "
                    "AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        admin_engine.dispose()
