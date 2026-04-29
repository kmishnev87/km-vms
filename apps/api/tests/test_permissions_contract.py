from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER, ROLE_PERMISSIONS, ROLE_VIEWER
from app.routers.users import ensure_can_create_role, ensure_can_modify_user


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def read_app_file(*parts: str) -> str:
    return (APP_ROOT / Path(*parts)).read_text(encoding="utf-8")


def read_router(name: str) -> str:
    return read_app_file("routers", name)


def user(user_id: int, role: str, is_active: bool = True):
    return SimpleNamespace(id=user_id, role=role, is_active=is_active)


def assert_forbidden(callable_obj, *args, **kwargs):
    with pytest.raises(HTTPException) as exc:
        callable_obj(*args, **kwargs)
    assert exc.value.status_code in {400, 403}


def test_role_permissions_matrix():
    assert ROLE_PERMISSIONS[ROLE_OWNER] == {
        "admin_access",
        "delete_recordings",
        "manage_cameras",
        "manage_settings",
        "view_live",
        "view_recordings",
        "view_timeline",
    }
    assert ROLE_PERMISSIONS[ROLE_ADMIN] == ROLE_PERMISSIONS[ROLE_OWNER]
    assert ROLE_PERMISSIONS[ROLE_OPERATOR] == {
        "view_live",
        "view_recordings",
        "view_timeline",
    }
    assert ROLE_PERMISSIONS[ROLE_VIEWER] == {"view_live"}


def test_users_role_management_rules():
    ensure_can_create_role(user(1, ROLE_ADMIN), ROLE_OPERATOR)
    ensure_can_create_role(user(1, ROLE_ADMIN), ROLE_VIEWER)

    assert_forbidden(ensure_can_create_role, user(1, ROLE_ADMIN), ROLE_ADMIN)
    assert_forbidden(ensure_can_create_role, user(1, ROLE_ADMIN), ROLE_OWNER)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_ADMIN), user(2, ROLE_OWNER))
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_ADMIN), user(2, ROLE_OPERATOR), next_role=ROLE_ADMIN)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_ADMIN), user(2, ROLE_VIEWER), next_role=ROLE_OWNER)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_OWNER), user(1, ROLE_OWNER), next_active=False)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_OWNER), user(1, ROLE_OWNER), next_role=ROLE_ADMIN)


def test_management_cameras_are_separated_from_viewer_cameras():
    source = read_router("cameras.py")
    assert 'APIRouter(prefix="/cameras"' in source
    assert 'APIRouter(prefix="/viewer/cameras"' in source
    assert 'Depends(require_permission("manage_cameras"))' in source
    assert 'Depends(require_permission("view_live"))' in source
    assert '"password"' not in source.split("def list_viewer_cameras", 1)[1].split("@router.get", 1)[0]


def test_recording_routes_are_permission_protected():
    source = read_router("recordings.py")
    assert source.count('Depends(require_permission("view_recordings"))') >= 3
    assert source.count('Depends(require_permission("delete_recordings"))') >= 4
    assert 'user_has_permission(user.role, "view_recordings")' in source


def test_chronology_routes_and_token_file_access_are_permission_protected():
    source = read_router("chronology.py")
    assert source.count('Depends(require_permission("view_timeline"))') >= 2
    assert 'user_has_permission(user.role, "view_timeline")' in source
    assert "FORBIDDEN_DETAIL" in source


def test_live_routes_are_permission_protected():
    source = read_router("live.py")
    assert source.count('Depends(require_permission("view_live"))') >= 4
    assert source.count('Depends(require_permission("manage_settings"))') >= 4
    assert 'user_has_permission(user.role, "view_live")' in source
    assert "FORBIDDEN_DETAIL" in source


def test_settings_hardware_storage_and_system_info_are_permission_protected():
    assert 'Depends(require_permission("manage_settings"))' in read_router("settings.py")
    assert 'Depends(require_permission("manage_settings"))' in read_router("hardware.py")
    assert 'Depends(require_permission("manage_settings"))' in read_router("storage.py")
    assert 'Depends(require_permission("manage_settings"))' in read_app_file("main.py")
