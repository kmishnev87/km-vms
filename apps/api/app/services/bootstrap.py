from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import Base, engine
from app.models import User


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def ensure_admin(db: Session) -> None:
    existing = db.query(User).filter(User.username == settings.admin_username).first()
    if existing:
        return

    admin = User(
        username=settings.admin_username,
        full_name=settings.admin_full_name,
        password_hash=hash_password(settings.admin_password),
        role="admin",
    )
    db.add(admin)
    db.commit()
