from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.core.permissions import ROLE_ADMIN, ROLE_OWNER
from app.db.session import Base, engine
from app.models import SystemSettings, User
from app.services.system_settings import default_timezone


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    migrate_user_table()


def migrate_user_table() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "is_active" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE NOT NULL"))
        if "updated_at" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL"))
        if "last_login_at" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP NULL"))


def ensure_system_settings(db: Session) -> SystemSettings:
    row = db.query(SystemSettings).order_by(SystemSettings.id.asc()).first()
    if row:
        return row

    has_users = db.query(User).count() > 0
    row = SystemSettings(
        system_initialized=has_users,
        timezone=default_timezone(),
        language="ru",
        storage_path=settings.storage_root,
        recording_format="mkv",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def ensure_owner_migration(db: Session) -> None:
    normalize_stage_403_users(db)


def normalize_stage_403_users(db: Session) -> None:
    admin_kostya = db.query(User).filter(User.username == "Admin_Kostya").first()
    kostya = db.query(User).filter(User.username == "Kostya").first()

    if admin_kostya:
        admin_kostya.role = ROLE_OWNER
        admin_kostya.is_active = True
        db.add(admin_kostya)

    if kostya:
        kostya.role = ROLE_ADMIN
        kostya.is_active = True
        db.add(kostya)

    db.commit()
    return

    legacy_admin = db.query(User).filter(User.username == "admin").first()
    kostya = db.query(User).filter(User.username == "Kostya").first()
    legacy_konstantin = (
        db.query(User)
        .filter(User.full_name == "Константин", User.username.notin_(["Admin_Kostya", "Kostya", "admin"]))
        .order_by(User.id.asc())
        .first()
    )

    if not admin_kostya and legacy_admin:
        legacy_admin.username = "Admin_Kostya"
        legacy_admin.full_name = "Константин"
        legacy_admin.role = ROLE_OWNER
        legacy_admin.is_active = True
        admin_kostya = legacy_admin
        legacy_admin = None
    elif not admin_kostya and legacy_konstantin:
        legacy_konstantin.username = "Admin_Kostya"
        legacy_konstantin.full_name = "Константин"
        legacy_konstantin.role = ROLE_OWNER
        legacy_konstantin.is_active = True
        admin_kostya = legacy_konstantin

    if admin_kostya:
        admin_kostya.full_name = "Константин"
        admin_kostya.role = ROLE_OWNER
        admin_kostya.is_active = True
        db.add(admin_kostya)

    if kostya:
        kostya.full_name = "Константин"
        kostya.role = ROLE_ADMIN
        kostya.is_active = True
        db.add(kostya)

    if legacy_admin and admin_kostya and legacy_admin.id != admin_kostya.id:
        legacy_admin.role = ROLE_ADMIN
        legacy_admin.is_active = False
        db.add(legacy_admin)

    preferred_owner = admin_kostya
    if not preferred_owner:
        preferred_owner = db.query(User).filter(User.role == ROLE_OWNER).order_by(User.id.asc()).first()
        if not preferred_owner:
            preferred_owner = db.query(User).order_by(User.id.asc()).first()
        if preferred_owner:
            preferred_owner.role = ROLE_OWNER
            preferred_owner.is_active = True
            db.add(preferred_owner)

    if preferred_owner:
        other_owners = db.query(User).filter(User.role == ROLE_OWNER, User.id != preferred_owner.id).all()
        for user in other_owners:
            user.role = ROLE_ADMIN
            if user.username == "admin":
                user.is_active = False
            db.add(user)

    db.commit()


def ensure_legacy_owner_migration(db: Session) -> None:
    existing_owner = db.query(User).filter(User.role == ROLE_OWNER).first()
    legacy_owner = (
        db.query(User)
        .filter((User.username == "admin") | (User.full_name == "Константин"))
        .order_by(User.id.asc())
        .first()
    )
    if legacy_owner:
        legacy_owner.role = ROLE_OWNER
        db.add(legacy_owner)
        db.commit()
        return
    if existing_owner:
        return
    first_user = db.query(User).order_by(User.id.asc()).first()
    if first_user:
        first_user.role = ROLE_OWNER
        db.add(first_user)
        db.commit()


def ensure_admin(db: Session) -> None:
    system = ensure_system_settings(db)
    if not system.system_initialized:
        return

    ensure_owner_migration(db)
    return

    existing = db.query(User).filter(User.username == settings.admin_username).first()
    if existing:
        ensure_owner_migration(db)
        return
    if db.query(User).filter(User.role == ROLE_OWNER, User.is_active == True).first():  # noqa: E712
        ensure_owner_migration(db)
        return

    admin = User(
        username=settings.admin_username,
        full_name=settings.admin_full_name,
        password_hash=hash_password(settings.admin_password),
        role=ROLE_OWNER,
        is_active=True,
    )
    db.add(admin)
    db.commit()
    ensure_legacy_owner_migration(db)
    ensure_owner_migration(db)
