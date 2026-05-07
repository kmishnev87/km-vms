from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SchemaVersionState(Base):
    __tablename__ = "schema_version_state"

    id: Mapped[str] = mapped_column(String(50), primary_key=True, default="current")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_id: Mapped[str] = mapped_column(String(100), nullable=False)
    app_version: Mapped[str] = mapped_column(String(50), nullable=False)
    app_build_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    metadata_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    drift_classification: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SchemaMigrationHistory(Base):
    __tablename__ = "schema_migration_history"
    __table_args__ = (UniqueConstraint("migration_id", "source", name="uq_schema_migration_history_event"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    migration_id: Mapped[str] = mapped_column(String(100), nullable=False)
    previous_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_id: Mapped[str] = mapped_column(String(100), nullable=False)
    app_version: Mapped[str] = mapped_column(String(50), nullable=False)
    app_build_version: Mapped[str] = mapped_column(String(100), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    service_name: Mapped[str] = mapped_column(String(100), default="api_bootstrap")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
