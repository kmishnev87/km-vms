from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.session import engine
from app.services import (
    operation_recovery,
    schema_migration_gate,
    schema_preparation,
)
from app.services.schema_update_control import (
    acquire_schema_lock,
    release_schema_lock,
)


def main() -> None:
    """Run every schema-update phase under one process-wide advisory lock."""
    with Session(engine) as lock_db:
        lock_backend_pid = acquire_schema_lock(lock_db)
        try:
            schema_preparation.main(
                manage_lock=False,
                pipeline_lock_backend_pid=lock_backend_pid,
            )
            operation_recovery.main(
                manage_lock=False,
                pipeline_lock_backend_pid=lock_backend_pid,
            )
            schema_migration_gate.main(
                manage_lock=False,
                pipeline_lock_backend_pid=lock_backend_pid,
            )
        finally:
            release_schema_lock(lock_db)


if __name__ == "__main__":
    main()
