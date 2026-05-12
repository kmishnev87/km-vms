import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.permissions import ROLE_OPERATOR, ROLE_OWNER
from app.core.security import create_access_token
from app.db.session import Base, get_db
from app.main import app
from app.models.user import User
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from test_schema_migration_runner_stage3 import seed_state


def auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.username)}"}


def add_user(db, *, role, username):
    user = User(username=username, full_name=username, password_hash="hash", role=role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def client_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage13_maintenance_overview.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    owner = add_user(db, role=ROLE_OWNER, username="stage13_overview_owner")
    operator = add_user(db, role=ROLE_OPERATOR, username="stage13_overview_operator")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), owner, operator
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_maintenance_overview_permissions_and_registry(client_db):
    client, owner, operator = client_db

    assert client.get("/system/maintenance/overview").status_code == 401
    assert client.get("/system/maintenance/overview", headers=auth_headers(operator)).status_code == 403
    assert client.get("/system/maintenance/overview", headers=auth_headers(owner)).status_code == 200

    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("GET", "/system/maintenance/overview", "manage_settings") in rows


def test_maintenance_overview_is_read_only_sanitized_and_complete(client_db):
    client, owner, _operator = client_db

    response = client.get("/system/maintenance/overview", headers=auth_headers(owner))
    assert response.status_code == 200
    payload = response.json()

    assert payload["read_only"] is True
    assert payload["side_effects"]["db_mutated"] is False
    assert set(payload["flows"]) == {"db_adoption", "migration", "restore", "update"}
    assert payload["upgrade_report"]["available"] is True
    assert payload["upgrade_report"]["download_endpoint"] == "/system/upgrade/report"
    assert payload["history"]["durable_history"] == "limited"

    rendered = json.dumps(payload, ensure_ascii=False).lower()
    forbidden = ["password", "authorization", "jwt", "rtsp://", "postgresql://", "sqlite:///", "backup_root", "raw_path"]
    assert not any(item in rendered for item in forbidden)


def test_maintenance_overview_router_does_not_call_apply_actions():
    source = (Path(__file__).resolve().parents[1] / "app" / "routers" / "maintenance.py").read_text(encoding="utf-8")
    overview_source = source.split("@overview_router.get", 1)[1].split("class DbAdoptionApplyRequest", 1)[0]

    assert "apply_db_adoption(" not in overview_source
    assert "apply_migration_maintenance(" not in overview_source
    assert "apply_restore_maintenance(" not in overview_source
    assert "apply_update_maintenance(" not in overview_source
