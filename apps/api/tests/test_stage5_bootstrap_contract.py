import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER, ROLE_VIEWER
from app.db.session import Base
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services import bootstrap
from app.services.bootstrap import ensure_admin, ensure_owner_migration, ensure_system_settings


@pytest.fixture
def db():
    original_storage_root = settings.storage_root
    settings.storage_root = "/stage5/test/archive"
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root


def add_user(db, username, role=ROLE_VIEWER, active=True, full_name=""):
    user = User(
        username=username,
        full_name=full_name or username,
        password_hash="hash",
        role=role,
        is_active=active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def snapshot_users(db):
    return [
        (user.id, user.username, user.full_name, user.role, bool(user.is_active), user.password_hash)
        for user in db.query(User).order_by(User.id.asc()).all()
    ]


def test_first_run_without_users_keeps_setup_required_and_creates_no_hidden_admin(db):
    settings_row = ensure_system_settings(db)
    ensure_admin(db)

    assert settings_row.system_initialized is False
    assert db.query(User).count() == 0
    assert ensure_system_settings(db).id == settings_row.id


def test_existing_initialized_db_with_active_owner_is_noop_on_repeated_bootstrap(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    owner = add_user(db, "real_owner", role=ROLE_OWNER, active=True)
    admin = add_user(db, "real_admin", role=ROLE_ADMIN, active=True)
    before = snapshot_users(db)

    ensure_owner_migration(db)
    ensure_admin(db)
    ensure_owner_migration(db)
    ensure_admin(db)

    assert snapshot_users(db) == before
    assert db.get(User, owner.id).role == ROLE_OWNER
    assert db.get(User, admin.id).role == ROLE_ADMIN


def test_existing_users_without_owner_promotes_first_active_user_only_once(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    inactive_admin = add_user(db, "inactive_admin", role=ROLE_ADMIN, active=False)
    operator = add_user(db, "operator_user", role=ROLE_OPERATOR, active=True)
    viewer = add_user(db, "viewer_user", role=ROLE_VIEWER, active=True)

    ensure_owner_migration(db)
    after_first = snapshot_users(db)
    ensure_owner_migration(db)

    assert snapshot_users(db) == after_first
    assert db.get(User, inactive_admin.id).role == ROLE_ADMIN
    assert db.get(User, inactive_admin.id).is_active is False
    assert db.get(User, operator.id).role == ROLE_OWNER
    assert db.get(User, viewer.id).role == ROLE_VIEWER
    assert db.query(User).filter(User.role == ROLE_OWNER, User.is_active == True).count() == 1  # noqa: E712


def test_existing_initialized_db_with_no_active_users_does_not_reactivate_or_create_owner(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    inactive_admin = add_user(db, "inactive_admin", role=ROLE_ADMIN, active=False)
    inactive_viewer = add_user(db, "inactive_viewer", role=ROLE_VIEWER, active=False)
    before = snapshot_users(db)

    ensure_owner_migration(db)
    ensure_admin(db)

    assert snapshot_users(db) == before
    assert db.get(User, inactive_admin.id).is_active is False
    assert db.get(User, inactive_viewer.id).is_active is False
    assert db.query(User).filter(User.role == ROLE_OWNER).count() == 0


def test_uninitialized_system_with_existing_active_users_does_not_promote_before_setup(db):
    db.add(SystemSettings(system_initialized=False, timezone="UTC", language="ru", storage_path="/old"))
    user = add_user(db, "pre_setup_user", role=ROLE_VIEWER, active=True)
    before = snapshot_users(db)

    ensure_system_settings(db)
    ensure_owner_migration(db)
    ensure_admin(db)

    assert snapshot_users(db) == before
    assert db.get(User, user.id).role == ROLE_VIEWER
    assert db.query(User).filter(User.role == ROLE_OWNER).count() == 0


def test_real_startup_sequence_is_idempotent_for_initialized_owner(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    owner = add_user(db, "startup_owner", role=ROLE_OWNER, active=True)
    admin = add_user(db, "startup_admin", role=ROLE_ADMIN, active=True)
    before = snapshot_users(db)

    for _ in range(2):
      ensure_system_settings(db)
      ensure_owner_migration(db)
      ensure_admin(db)

    assert snapshot_users(db) == before
    assert db.get(User, owner.id).role == ROLE_OWNER
    assert db.get(User, admin.id).role == ROLE_ADMIN


def test_mojibake_full_name_is_not_privileged_by_default(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    neutral = add_user(db, "neutral_first", role=ROLE_VIEWER, active=True)
    mojibake_user = add_user(
        db,
        "legacy_display_name_user",
        role=ROLE_OPERATOR,
        active=True,
        full_name="РљРѕРЅСЃС‚Р°РЅС‚РёРЅ",
    )

    ensure_owner_migration(db)

    assert db.get(User, neutral.id).role == ROLE_OWNER
    assert db.get(User, mojibake_user.id).role == ROLE_OPERATOR
    assert db.get(User, mojibake_user.id).full_name == "РљРѕРЅСЃС‚Р°РЅС‚РёРЅ"


def test_personal_legacy_usernames_are_not_privileged_by_default(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    neutral = add_user(db, "neutral_first", role=ROLE_VIEWER, active=True)
    admin_kostya = add_user(db, "Admin_Kostya", role=ROLE_VIEWER, active=True, full_name="Konstantin")
    kostya = add_user(db, "Kostya", role=ROLE_OPERATOR, active=True, full_name="Konstantin")

    ensure_owner_migration(db)

    assert db.get(User, neutral.id).role == ROLE_OWNER
    assert db.get(User, admin_kostya.id).role == ROLE_VIEWER
    assert db.get(User, admin_kostya.id).full_name == "Konstantin"
    assert db.get(User, kostya.id).role == ROLE_OPERATOR
    assert db.get(User, kostya.id).full_name == "Konstantin"


def test_personal_names_do_not_override_existing_active_owner(db):
    db.add(SystemSettings(system_initialized=True, timezone="UTC", language="ru", storage_path="/old"))
    owner = add_user(db, "actual_owner", role=ROLE_OWNER, active=True)
    admin_kostya = add_user(db, "Admin_Kostya", role=ROLE_VIEWER, active=True)
    kostya = add_user(db, "Kostya", role=ROLE_OPERATOR, active=True)
    before = snapshot_users(db)

    ensure_owner_migration(db)
    ensure_admin(db)

    assert snapshot_users(db) == before
    assert db.get(User, owner.id).role == ROLE_OWNER
    assert db.get(User, admin_kostya.id).role == ROLE_VIEWER
    assert db.get(User, kostya.id).role == ROLE_OPERATOR


def test_ensure_system_settings_has_no_privilege_side_effects(db):
    user = add_user(db, "first_user", role=ROLE_VIEWER, active=True)

    row = ensure_system_settings(db)

    assert row.system_initialized is True
    assert row.storage_path == settings.storage_root
    assert db.get(User, user.id).role == ROLE_VIEWER
    assert db.get(User, user.id).is_active is True


def test_bootstrap_module_has_no_removed_legacy_helpers_or_personal_production_logic():
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")

    assert "normalize_stage_403" not in source
    assert "ensure_legacy_owner_migration" not in source
    assert "Admin_Kostya" not in source
    assert "Kostya" not in source
    assert "Константин" not in source
    assert "РљРѕРЅСЃС‚Р°РЅС‚РёРЅ" not in source
