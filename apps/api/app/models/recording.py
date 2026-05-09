from datetime import datetime

from sqlalchemy import String, Integer, DateTime, BigInteger, Boolean, ForeignKey, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class RecordingJob(Base):
    __tablename__ = "recording_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)

    camera_name_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    camera_folder_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(50), index=True)
    source_stream: Mapped[str | None] = mapped_column(String(20), nullable=True)
    input_fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recorder_instance_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ffmpeg_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_by: Mapped[str] = mapped_column(String(50), default="KM VMS")
    ownership: Mapped[str] = mapped_column(String(50), default="KM VMS", index=True)
    source: Mapped[str] = mapped_column(String(50), default="recorder")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ArchiveRoot(Base):
    __tablename__ = "archive_roots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    storage_namespace: Mapped[str] = mapped_column(String(255), nullable=False, default="kmvms/recordings")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_readable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_writable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    problem: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RecordingSegment(Base):
    __tablename__ = "recording_segments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("recording_jobs.id", ondelete="SET NULL"), index=True, nullable=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)

    camera_name_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    camera_folder_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_path: Mapped[str] = mapped_column(String(1024), index=True)
    relative_path: Mapped[str | None] = mapped_column(String(1024), index=True, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    stream_type: Mapped[str] = mapped_column(String(20), default="main")
    status: Mapped[str] = mapped_column(String(50), default="ready", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    ownership: Mapped[str] = mapped_column(String(50), default="KM VMS", index=True)
    source: Mapped[str] = mapped_column(String(50), default="recorder")
    archive_root_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("archive_roots.id", ondelete="SET NULL"), index=True, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    storage_namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    container_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    file_extension: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    integrity_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    integrity_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_integrity_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_size_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_mtime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_probe_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cleanup_candidate: Mapped[bool | None] = mapped_column(Boolean, default=False, nullable=True)
    cleanup_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconciliation_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reconciliation_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deletion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deletion_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ArchiveExportJob(Base):
    __tablename__ = "archive_export_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), index=True, nullable=True)
    camera_label_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)

    start_ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress_percent: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)

    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    format_hint: Mapped[str | None] = mapped_column(String(20), nullable=True)

    source_segment_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_segment_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_source_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    gap_warnings: Mapped[list] = mapped_column(JSON, default=list)

    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sanitized_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    internal_manifest_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    internal_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
