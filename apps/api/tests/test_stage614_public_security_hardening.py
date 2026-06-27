from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[3]
PRODUCT_PATHS = [
    "apps/api/app",
    "apps/api/tests",
    "apps/web/app",
    "apps/web/lib",
    "apps/web/tests",
    "scripts",
    "docs",
    "release",
]
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".sh", ".md", ".txt", ".json", ".yml", ".yaml", ".example"}


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def product_text_files():
    for relative in PRODUCT_PATHS:
        root = ROOT / relative
        if root.is_file():
            candidates = [root]
        else:
            candidates = [path for path in root.rglob("*") if path.is_file()]
        for path in candidates:
            if path.suffix in TEXT_SUFFIXES or path.name == ".env.example":
                yield path


def test_public_product_paths_do_not_ship_legacy_fallback_admin_secret():
    forbidden = "_".join(["Admin", "Change", "Me", "2026"])
    hits = []
    for path in product_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if forbidden in text:
            hits.append(str(path.relative_to(ROOT)))

    assert hits == []


def test_api_config_has_no_admin_password_or_hidden_bootstrap_env_contract():
    config = read("apps/api/app/core/config.py")
    compose = read("docker-compose.yml")
    env_example = read(".env.example")
    install = read("scripts/install.sh")

    for text in (config, compose, env_example, install):
        assert "_".join(["ADMIN", "PASSWORD"]) not in text
        assert "_".join(["admin", "password"]) not in text

    assert "_".join(["admin", "username"]) not in config
    assert "_".join(["admin", "full", "name"]) not in config


def test_production_settings_reject_empty_placeholder_or_short_secrets():
    base = {
        "app_env": "production",
        "database_url": "sqlite:////tmp/kmvms-stage614.sqlite3",
        "jwt_secret": "stage614-valid-jwt-secret-32-bytes-minimum",
        "encryption_key": "stage614-valid-encryption-key-32-bytes-minimum",
    }

    for field, value in (
        ("jwt_secret", ""),
        ("encryption_key", ""),
        ("jwt_secret", "generate_with_scripts_install"),
        ("encryption_key", "generate_with_scripts_install"),
        ("jwt_secret", "short-secret"),
        ("encryption_key", "short-secret"),
    ):
        payload = {**base, field: value}
        with pytest.raises(ValidationError):
            Settings(**payload)


def test_explicit_test_only_settings_can_use_nonproduction_secrets():
    settings = Settings(
        app_env="test",
        database_url="sqlite:////tmp/kmvms-stage614.sqlite3",
        jwt_secret="explicit-test-secret",
        encryption_key="explicit-test-encryption",
    )

    assert settings.jwt_secret == "explicit-test-secret"
    assert settings.encryption_key == "explicit-test-encryption"


def test_installer_generates_secrets_without_overwriting_existing_env_or_admin_password():
    install = read("scripts/install.sh")

    assert '[ ! -e "$env_file" ] || fail ".env already exists; refusing to overwrite: $env_file"' in install
    assert "pg_secret=$(random_secret)" in install
    assert "jwt_secret=$(random_secret)" in install
    assert "enc_secret=$(random_secret)" in install
    assert "admin" + "_secret=$(random_secret)" not in install


def test_compose_requires_production_database_and_security_secrets():
    compose = read("docker-compose.yml")
    pytest_compose = read("docker-compose.pytest.yml")

    assert "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}" in compose
    assert "${JWT_SECRET:?JWT_SECRET is required}" in compose
    assert "${ENCRYPTION_KEY:?ENCRYPTION_KEY is required}" in compose
    assert "kmvms-pytest-jwt-secret-32-bytes-minimum" in pytest_compose
    assert "kmvms-pytest-encryption-key-32-bytes-minimum" in pytest_compose


def test_bootstrap_preserves_explicit_setup_without_hardcoded_privileged_password():
    bootstrap = read("apps/api/app/services/bootstrap.py")
    settings_router = read("apps/api/app/routers/settings.py")

    assert "hash_password(settings." + "_".join(["admin", "password"]) + ")" not in bootstrap
    assert "_".join(["Admin", "Change", "Me", "2026"]) not in bootstrap
    assert "ensure_admin" in bootstrap
    assert "if not system.system_initialized:" in bootstrap
    assert "return" in bootstrap
    assert "password_hash=hash_password(payload.password)" in settings_router
    assert "if payload.password != payload.password_confirm:" in settings_router
    assert "existing_user_count = db.query(User).count()" in settings_router
