import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import Base
from app.models.system_settings import SystemSettings
from app.services.system_settings import (
    get_system_settings,
    serialize_settings,
    update_system_settings,
    validate_settings_payload,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.mark.parametrize("language", ["ru", "en", "zh-CN"])
def test_language_contract_accepts_supported_locales(db, language):
    row = update_system_settings(db, {"language": language})

    assert row.language == language
    assert serialize_settings(row)["language"] == language


@pytest.mark.parametrize("alias", ["zh", "zh-cn", "zh_CN", "cn", "chinese"])
def test_language_contract_normalizes_legacy_chinese_aliases(alias):
    assert validate_settings_payload({"language": alias}, partial=True)["language"] == "zh-CN"


def test_language_contract_rejects_unsupported_input():
    with pytest.raises(ValueError) as exc:
        validate_settings_payload({"language": "de"}, partial=True)

    assert "language" in str(exc.value)


def test_existing_bad_stored_language_falls_back_safely(db):
    db.add(
        SystemSettings(
            system_initialized=True,
            timezone="UTC",
            language="bad",
            storage_path="/storage/archive",
            recording_format="mkv",
        )
    )
    db.commit()

    row = get_system_settings(db)

    assert row.language == "ru"
    assert serialize_settings(row)["language"] == "ru"
