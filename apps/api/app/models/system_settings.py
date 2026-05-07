from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    system_initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    system_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str] = mapped_column(String(100), default="UTC")
    language: Mapped[str] = mapped_column(String(2), default="ru")
    storage_path: Mapped[str] = mapped_column(String(1024), default="/storage/archive")
    recording_format: Mapped[str] = mapped_column(String(16), default="mkv")
    hardware_preferred_backend: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auto_free_space_cleanup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_suspended_by_low_disk: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
