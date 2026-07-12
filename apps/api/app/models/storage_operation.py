from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class StorageOperation(Base):
    __tablename__ = "storage_operations"
    __table_args__ = (
        UniqueConstraint(
            "actor_key",
            "operation_type",
            "idempotency_key",
            name="uq_storage_operation_idempotency",
        ),
        Index("ix_storage_operations_status_updated", "status", "updated_at"),
        Index("ix_storage_operations_type_status", "operation_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    system_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    progress: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(96), nullable=True)
    retry_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancel_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    parent_operation_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    parent_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    retry_depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    owner_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    queued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class StorageWorkerLease(Base):
    __tablename__ = "storage_worker_leases"

    worker_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    owner_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class StorageWorkSignal(Base):
    __tablename__ = "storage_work_signals"
    __table_args__ = (
        UniqueConstraint("signal_type", "scope_key", name="uq_storage_work_signal_scope"),
        Index("ix_storage_work_signals_status_updated", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="idle", nullable=False, index=True)
    requested_watermark: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    consumed_watermark: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    claimed_watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
