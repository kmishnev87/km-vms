"""Regression: signed credentials belong to a user id, never a recycled name."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.user import User
from app.routers.deps import get_current_user
from app.services.media_tokens import create_media_token, validate_media_token


@pytest.fixture
def identity_db(monkeypatch):
    import app.routers.deps as deps
    import app.services.media_tokens as media
    monkeypatch.setattr(deps, "audit_security_denied", lambda **kwargs: None)
    monkeypatch.setattr(media, "audit_security_denied", lambda **kwargs: None)
    engine = create_engine("sqlite:///:memory:")
    User.__table__.create(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def user(db, name="identity_original", role="viewer"):
    row = User(username=name, password_hash="unused", role=role, is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def authenticate(db, token):
    return get_current_user(db=db, credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))


def test_access_keeps_identity_after_rename_and_recycled_name(identity_db):
    db = identity_db
    original = user(db)
    token = create_access_token(original.username, user_id=original.id)
    original.username = "identity_renamed"
    db.commit()
    replacement = user(db, role="owner")
    assert authenticate(db, token).id == original.id
    assert authenticate(db, token).id != replacement.id
    db.delete(original)
    db.commit()
    with pytest.raises(HTTPException) as error:
        authenticate(db, token)
    assert error.value.status_code == 401


def test_legacy_access_cannot_authenticate_even_if_name_exists(identity_db):
    row = user(identity_db)
    token = jwt.encode({"typ": "access", "sub": row.username,
                        "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                       settings.jwt_secret, algorithm="HS256")
    with pytest.raises(HTTPException) as error:
        authenticate(identity_db, token)
    assert error.value.status_code == 401


def test_access_uses_current_role_and_disabled_state(identity_db):
    row = user(identity_db)
    token = create_access_token(row.username, user_id=row.id)
    row.role = "operator"
    identity_db.commit()
    assert authenticate(identity_db, token).role == "operator"
    row.is_active = False
    identity_db.commit()
    with pytest.raises(HTTPException) as error:
        authenticate(identity_db, token)
    assert error.value.status_code == 401


def test_media_keeps_identity_and_rejects_missing_uid(identity_db):
    db = identity_db
    original = user(db, role="operator")
    token, _ = create_media_token(user=original, scope="recording", resource={"path": "safe"})
    original.username = "identity_renamed"
    db.commit()
    user(db, role="owner")
    kwargs = dict(scope="recording", resource={"path": "safe"}, permission="view_recordings")
    assert validate_media_token(db, token=token, **kwargs).id == original.id
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    payload.pop("uid")
    missing_uid = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(HTTPException) as error:
        validate_media_token(db, token=missing_uid, **kwargs)
    assert error.value.status_code == 401


def test_control_plane_binds_status_to_user_id_not_recycled_name(monkeypatch):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[3] / "apps/update-control-plane/control_plane.py"
    spec = importlib.util.spec_from_file_location("stage14_identity_control", path)
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    contract = {"request_id": "update-" + "a" * 32, "target_version": "0.8.28",
                "target_commit": "a" * 40, "fencing_generation": 1,
                "actor_subject": "identity_original"}
    auth = {**contract, "actor_user_id": "1"}
    monkeypatch.setattr(control, "JWT_SECRET", settings.jwt_secret)
    monkeypatch.setattr(control, "read_signed", lambda path: contract if path == control.FAILURE_PLANE else auth)
    monkeypatch.setattr(control, "validate_failure_contract", lambda payload, **kwargs: payload)
    headers = {"Authorization": "Bearer " + create_access_token("identity_original", user_id=1)}
    assert control.active_contract(headers)[0] == contract
    headers = {"Authorization": "Bearer " + create_access_token("identity_original", user_id=2)}
    with pytest.raises(control.ContractError, match="foreign_actor_forbidden"):
        control.active_contract(headers)
