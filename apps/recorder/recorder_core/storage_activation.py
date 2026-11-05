"""Start admission during a physical archive binding transition (no polling owner)."""

from sqlalchemy import JSON, String, column, or_, select, table


def recording_start_allowed(engine) -> bool:
    operations = table(
        "storage_operations",
        column("id", String),
        column("operation_type", String),
        column("status", String),
        column("progress", JSON),
    )
    # A failed/partial transition must retain the fence until its existing
    # recovery owner has verified the physical binding, not just timed out.
    blocked = select(operations.c.id).where(
        operations.c.operation_type == "archive_root_activation",
        or_(
            operations.c.status.in_(("queued", "running", "cancel_requested")),
            operations.c.progress["activation_writer_fenced"].as_boolean().is_(True),
        ),
    ).limit(1)
    with engine.connect() as connection:
        return connection.execute(blocked).first() is None
