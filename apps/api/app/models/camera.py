from datetime import datetime

from sqlalchemy import String, Integer, Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.core.config import settings


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    storage_folder_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    protocol: Mapped[str] = mapped_column(String(20))  # rtsp | onvif

    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)

    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    rtsp_main_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    rtsp_sub_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    rtsp_transport: Mapped[str | None] = mapped_column(String(20), nullable=True)

    onvif_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    onvif_profile_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    onvif_channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    recording_mode: Mapped[str] = mapped_column(String(50), default="always")
    default_live_stream: Mapped[str] = mapped_column(String(20), default="sub")
    default_record_stream: Mapped[str] = mapped_column(String(20), default="main")

    segment_minutes: Mapped[int] = mapped_column(Integer, default=5)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    storage_quota_gb: Mapped[int] = mapped_column(Integer, default=50)

    status: Mapped[str] = mapped_column(String(50), default="new")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def preview_url(self) -> str | None:
        preview_path = settings.camera_preview_path(self.id)
        if preview_path.exists():
            return settings.camera_preview_url(self.id)
        return None
