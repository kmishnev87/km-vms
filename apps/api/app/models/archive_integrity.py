from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ArchiveIntegrityScan(Base):
    __tablename__ = "archive_integrity_scans"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_archive_integrity_scan_operation"),
        UniqueConstraint("active_slot", name="uq_archive_integrity_scan_active_slot"),
        Index("ix_archive_integrity_scans_status_created", "status", "created_at"),
        Index("ix_archive_integrity_scans_finished_id", "finished_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("storage_operations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_key: Mapped[str] = mapped_column(String(128), nullable=False)
    active_slot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")

    root_snapshot: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    root_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_high_watermark: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    scan_cutoff_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    metadata_cursor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    planned_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checked_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    found_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checked_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    category_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    root_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    impact_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(96), nullable=True)
    retry_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ArchiveIntegrityFinding(Base):
    __tablename__ = "archive_integrity_findings"
    __table_args__ = (
        Index(
            "uq_archive_integrity_active_metadata_finding",
            "scan_id",
            "segment_id",
            unique=True,
            postgresql_where=text("is_active = TRUE AND segment_id IS NOT NULL"),
            sqlite_where=text("is_active = 1 AND segment_id IS NOT NULL"),
        ),
        Index(
            "uq_archive_integrity_active_file_finding",
            "scan_id",
            "root_id",
            "stable_object_key",
            unique=True,
            postgresql_where=text("is_active = TRUE AND segment_id IS NULL AND stable_object_key IS NOT NULL"),
            sqlite_where=text("is_active = 1 AND segment_id IS NULL AND stable_object_key IS NOT NULL"),
        ),
        Index("ix_archive_integrity_findings_scan_id", "scan_id", "id"),
        Index("ix_archive_integrity_findings_scan_category_id", "scan_id", "category", "id"),
        Index("ix_archive_integrity_findings_scan_root_id", "scan_id", "root_id", "id"),
        Index("ix_archive_integrity_findings_scan_camera_id", "scan_id", "camera_id", "id"),
        Index("ix_archive_integrity_findings_state_updated", "state", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scan_id: Mapped[str] = mapped_column(
        ForeignKey("archive_integrity_scans.id", ondelete="CASCADE"),
        nullable=False,
    )
    finding_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    impact_key: Mapped[str] = mapped_column(String(64), nullable=False)

    root_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    root_label_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    physical_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    camera_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    camera_name_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    segment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stable_object_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    relative_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    observed_facts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metadata_version: Mapped[str | None] = mapped_column(String(96), nullable=True)
    first_observed_scan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    first_observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    action_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    required_permission: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmation_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    no_action_reason: Mapped[str | None] = mapped_column(String(96), nullable=True)
    retry_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(96), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    superseded_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ArchiveIntegrityDirectoryWork(Base):
    __tablename__ = "archive_integrity_directory_work"
    __table_args__ = (
        UniqueConstraint("scan_id", "root_id", "relative_directory", name="uq_archive_integrity_directory_work"),
        Index("ix_archive_integrity_directory_queue", "scan_id", "status", "root_id", "id"),
        Index("ix_archive_integrity_directory_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scan_id: Mapped[str] = mapped_column(
        ForeignKey("archive_integrity_scans.id", ondelete="CASCADE"),
        nullable=False,
    )
    root_id: Mapped[str] = mapped_column(String(36), nullable=False)
    root_snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    physical_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    relative_directory: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    discovered_directory_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class RecorderFileReceipt(Base):
    __tablename__ = "recorder_file_receipts"
    __table_args__ = (
        UniqueConstraint("segment_id", name="uq_recorder_file_receipt_segment"),
        Index("ix_recorder_file_receipts_root_relative", "root_id", "relative_path"),
        Index("ix_recorder_file_receipts_root_object", "root_id", "object_identity"),
        Index("ix_recorder_file_receipts_state_finalized", "state", "finalized_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    segment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    camera_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    root_id: Mapped[str] = mapped_column(String(36), nullable=False)
    physical_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    object_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ArchiveIntegrityRemediationPlan(Base):
    __tablename__ = "archive_integrity_remediation_plans"
    __table_args__ = (
        UniqueConstraint("actor_key", "idempotency_key", name="uq_archive_integrity_plan_idempotency"),
        Index("ix_archive_integrity_plans_scan_state", "scan_id", "state", "created_at"),
        Index("ix_archive_integrity_plans_operation", "operation_id"),
        Index("ix_archive_integrity_plans_apply_operation", "apply_operation_id"),
        Index("ix_archive_integrity_plans_expiry", "state", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scan_id: Mapped[str] = mapped_column(
        ForeignKey("archive_integrity_scans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    finding_id: Mapped[str] = mapped_column(
        ForeignKey("archive_integrity_findings.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("storage_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    apply_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("storage_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    required_permission: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_level: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="prepared", nullable=False)
    result_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    retry_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ArchiveIntegrityRemediationItem(Base):
    __tablename__ = "archive_integrity_remediation_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "item_index", name="uq_archive_integrity_plan_item_index"),
        UniqueConstraint("plan_id", "finding_id", name="uq_archive_integrity_plan_item_finding"),
        Index("ix_archive_integrity_items_plan_state", "plan_id", "state", "item_index"),
        Index("ix_archive_integrity_items_segment", "segment_id"),
        Index("ix_archive_integrity_items_root_object", "root_id", "stable_object_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("archive_integrity_remediation_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    finding_id: Mapped[str] = mapped_column(
        ForeignKey("archive_integrity_findings.id", ondelete="RESTRICT"),
        nullable=False,
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    root_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    relative_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    stable_object_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    intended_mutation: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="prepared", nullable=False)
    result_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    quarantine_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
