from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, Integer, JSON, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SchemaMigrationControl(Base):
    __tablename__ = "schema_migration_control"
    __table_args__ = (
        CheckConstraint(
            "char_length(registry_fingerprint) = 64 "
            "AND char_length(plan_fingerprint) = 64 "
            "AND char_length(source_shape_fingerprint) = 64 "
            "AND char_length(control_definition_fingerprint) = 64",
            name="ck_schema_migration_control_fingerprints",
        ),
        CheckConstraint(
            "state IN ('prepared','recovering','migrating','completed','failed')",
            name="ck_schema_migration_control_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="current")
    fencing_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False)
    installed_version: Mapped[str] = mapped_column(String(80), nullable=False)
    installed_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    source_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    target_release: Mapped[str] = mapped_column(String(80), nullable=False)
    target_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_shape_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    control_definition_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SchemaMigrationAttempt(Base):
    __tablename__ = "schema_migration_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started','applied','failed','blocked','interrupted')",
            name="ck_schema_migration_attempt_status",
        ),
        CheckConstraint(
            "char_length(registry_fingerprint) = 64 "
            "AND char_length(plan_fingerprint) = 64 "
            "AND char_length(definition_fingerprint) = 64 "
            "AND char_length(before_shape_fingerprint) = 64 "
            "AND (after_shape_fingerprint IS NULL "
            "OR char_length(after_shape_fingerprint) = 64)",
            name="ck_schema_migration_attempt_fingerprints",
        ),
        Index(
            "ix_schema_migration_attempt_request",
            "request_id",
            "started_at",
        ),
        Index(
            "ix_schema_migration_attempt_status",
            "status",
            "started_at",
        ),
        Index(
            "uq_schema_migration_attempt_applied_lineage",
            "migration_id",
            "definition_fingerprint",
            unique=True,
            postgresql_where=text("status = 'applied'"),
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    admission_attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False)
    migration_id: Mapped[str] = mapped_column(String(100), nullable=False)
    previous_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fencing_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    installed_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    installed_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_release: Mapped[str] = mapped_column(String(80), nullable=False)
    target_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    registry_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_shape_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    after_shape_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(96), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    resumable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    details: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
