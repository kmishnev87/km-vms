from pathlib import Path

import pytest
import jwt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.config import settings
from app.core.security import create_access_token, decode_access_token
from app.core.user_sessions import user_session_generation
from app.services.media_tokens import create_media_token, validate_media_token
from test_stage14_auth_identity import identity_db, user


def test_restored_numeric_id_cannot_inherit_access_or_media(identity_db):
    db = identity_db
    original = user(db, role="operator")
    access = create_access_token(original.username, user_id=original.id)
    media, _ = create_media_token(user=original, scope="recording", resource={"path": "safe"})
    old_generation = user_session_generation()
    secret = settings.jwt_secret
    user_session_generation(rotate=True)
    db.delete(original)
    db.commit()
    replacement = user(db, name="replacement", role="owner")
    assert replacement.id == original.id
    with pytest.raises(jwt.InvalidTokenError, match="session_expired"):
        decode_access_token(access)
    with pytest.raises(HTTPException) as error:
        validate_media_token(db, token=media, scope="recording", resource={"path": "safe"}, permission="view_recordings")
    assert error.value.status_code == 401
    assert decode_access_token(create_access_token(replacement.username, user_id=replacement.id))["uid"] == replacement.id
    assert user_session_generation() != old_generation
    assert settings.jwt_secret == secret


def test_generation_is_durable_and_missing_never_validates():
    token = create_access_token("same", user_id=1)
    generation = user_session_generation()
    assert user_session_generation(create=True) == generation
    path = Path(settings.maintenance_control_root) / "user-sessions" / "user-session-generation"
    path.unlink()
    with pytest.raises(jwt.InvalidTokenError, match="session_expired"):
        decode_access_token(token)
    assert not path.exists()
    assert user_session_generation(create=True) != generation


@pytest.mark.parametrize("mode", ["source", "rollback"])
def test_generation_rotates_before_database_mutation(monkeypatch, tmp_path, mode):
    from app.services import current_db_restore_executor as executor
    generation = user_session_generation()
    monkeypatch.setattr(executor, "_exact_artifact", lambda *a, **k: (tmp_path / "dump.json", {"backup_file_label": "dump"}, None))
    monkeypatch.setattr(executor, "_marker_path", lambda: tmp_path / "marker.json")
    def reset(url):
        assert user_session_generation() != generation
        raise RuntimeError("synthetic crash after revocation")
    monkeypatch.setattr(executor, "_reset_postgres_database", reset)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        executor._restore({"operation_id": "synthetic"}, artifact_id="synthetic", mode=mode)
