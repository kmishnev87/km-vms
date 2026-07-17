from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ArchiveMigrationPlan(Base):
    __tablename__ = "archive_migration_plans"
    __table_args__ = (
        UniqueConstraint("actor_key", "idempotency_key", name="uq_archive_migration_plan_idempotency"),
        Index("ix_archive_migration_plans_status_updated", "status", "updated_at"),
        Index("ix_archive_migration_plans_operation", "current_operation_id"),
        Index("ix_archive_migration_plans_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    source_root_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    target_root_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_label_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    target_label_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    source_physical_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    target_physical_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    source_snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    target_snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_access_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    target_access_identity: Mapped[str] = mapped_column(String(64), nullable=False)

    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    segment_high_watermark: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    inventory_cursor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    item_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    largest_item_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completed_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completed_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cancelled_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    excluded_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    excluded_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    blocker_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    same_physical_volume: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    capacity_total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    capacity_free_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserve_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    required_free_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    canonical_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="building", nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(32), default="inventory", nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("storage_operations.id", ondelete="SET NULL"),
        nullable=True,
    )
    required_prepare_permission: Mapped[str] = mapped_column(String(64), default="manage_settings", nullable=False)
    required_read_permission: Mapped[str] = mapped_column(String(64), default="manage_settings", nullable=False)
    required_apply_permissions: Mapped[str] = mapped_column(
        String(128),
        default="manage_settings,delete_recordings",
        nullable=False,
    )
    required_cancel_permission: Mapped[str] = mapped_column(String(64), default="manage_settings", nullable=False)
    required_retry_permissions: Mapped[str] = mapped_column(
        String(128),
        default="manage_settings,delete_recordings",
        nullable=False,
    )
    reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(96), nullable=True)
    retry_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cleanup_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    new_after_high_watermark_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    retained_source_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ArchiveMigrationItem(Base):
    __tablename__ = "archive_migration_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "item_index", name="uq_archive_migration_item_index"),
        UniqueConstraint("plan_id", "segment_id", name="uq_archive_migration_item_segment"),
        Index("ix_archive_migration_items_plan_phase", "plan_id", "phase", "item_index"),
        Index("ix_archive_migration_items_operation_phase", "operation_id", "phase", "item_index"),
        Index("ix_archive_migration_items_segment", "segment_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("archive_migration_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    segment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    camera_id: Mapped[int] = mapped_column(Integer, nullable=False)
    camera_name_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)

    source_root_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_root_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_physical_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    target_physical_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    source_snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    target_snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_access_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    target_access_identity: Mapped[str] = mapped_column(String(64), nullable=False)

    source_relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    target_final_relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    target_temp_relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_quarantine_relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_device: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_inode: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_mode: Mapped[int] = mapped_column(Integer, nullable=False)
    source_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_gid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_metadata_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_device: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_inode: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    transferred_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    quarantine_device: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quarantine_inode: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    intended_transition: Mapped[str] = mapped_column(String(64), default="source_to_target", nullable=False)

    phase: Mapped[str] = mapped_column(String(40), default="planned", nullable=False, index=True)
    operation_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    operation_fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    retry_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cleanup_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cleanup_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    target_finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_switched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_quarantined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
