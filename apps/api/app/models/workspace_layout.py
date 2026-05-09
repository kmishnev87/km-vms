from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class UserWorkspaceLayout(Base):
    __tablename__ = "user_workspace_layouts"
    __table_args__ = (
        UniqueConstraint("user_id", "workspace_key", name="uq_user_workspace_layout_user_workspace"),
        Index("ix_user_workspace_layout_user_workspace", "user_id", "workspace_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_key: Mapped[str] = mapped_column(String(50), nullable=False)
    layout_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    layout: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
