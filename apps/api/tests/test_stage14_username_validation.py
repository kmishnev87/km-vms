import pytest
from unittest.mock import Mock
from pydantic import ValidationError
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from app.schemas.user import UserCreateRequest, UserUpdateRequest
from app.routers.settings import validate_setup_username
from app.routers.users import commit_user_change


@pytest.mark.parametrize("value", ["  ", "\t\n", "a", " a ", "bad name", "name/@host", "x"*65])
def test_user_names_are_rejected_before_storage(value):
    with pytest.raises(ValidationError):
        UserCreateRequest(username=value,password="test-password",role="viewer")
    with pytest.raises(ValidationError):
        UserUpdateRequest(username=value)
    with pytest.raises(ValueError):
        validate_setup_username(value)


def test_username_is_normalized_consistently():
    value="  stage14.name-1_2  "
    assert UserCreateRequest(username=value,password="test-password",role="viewer").username == "stage14.name-1_2"
    assert UserUpdateRequest(username=value).username == "stage14.name-1_2"
    assert validate_setup_username(value) == "stage14.name-1_2"
    assert UserUpdateRequest(display_name="unchanged login").username is None


def test_database_unique_race_is_409_and_session_is_rolled_back():
    db=Mock()
    db.commit.side_effect=IntegrityError("insert",{},Exception("UNIQUE constraint failed: users.username"))
    with pytest.raises(HTTPException) as caught:
        commit_user_change(db)
    assert caught.value.status_code == 409
    db.rollback.assert_called_once()
