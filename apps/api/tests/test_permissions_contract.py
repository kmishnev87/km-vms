from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS, PUBLIC
from app.core.permissions import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_OWNER,
    ROLE_PERMISSIONS,
    ROLE_VIEWER,
    get_permissions_for_role,
    user_has_permission,
)
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


def decisions_for(path: str, method: str | None = None):
    return [
        item
        for item in ENDPOINT_PERMISSIONS
        if item.path == path and (method is None or item.method == method)
    ]


def decision(path: str, method: str):
    matches = decisions_for(path, method)
    assert len(matches) == 1
    return matches[0]


def test_role_permissions_matrix():
    expected_admin = {
        "admin_access",
        "delete_recordings",
        "manage_cameras",
        "manage_settings",
        "manage_users",
        "run_diagnostics",
        "view_live",
        "view_recordings",
        "view_timeline",
    }
    assert ROLE_PERMISSIONS[ROLE_OWNER] == expected_admin
    assert ROLE_PERMISSIONS[ROLE_ADMIN] == expected_admin
    assert ROLE_PERMISSIONS[ROLE_OPERATOR] == {
        "view_live",
        "view_recordings",
        "view_timeline",
    }
    assert ROLE_PERMISSIONS[ROLE_VIEWER] == {"view_live"}
    assert get_permissions_for_role("unknown") == frozenset()
    assert not user_has_permission("unknown", "view_live")
    assert not user_has_permission(ROLE_OWNER, "invented_permission")


def test_users_role_management_rules():
    ensure_can_create_role(user(1, ROLE_ADMIN), ROLE_OPERATOR)
    ensure_can_create_role(user(1, ROLE_ADMIN), ROLE_VIEWER)
    ensure_can_create_role(user(1, ROLE_OWNER), ROLE_ADMIN)

    assert_forbidden(ensure_can_create_role, user(1, ROLE_ADMIN), ROLE_ADMIN)
    assert_forbidden(ensure_can_create_role, user(1, ROLE_ADMIN), ROLE_OWNER)
    assert_forbidden(ensure_can_create_role, user(1, ROLE_OPERATOR), ROLE_VIEWER)
    assert_forbidden(ensure_can_create_role, user(1, ROLE_VIEWER), ROLE_OPERATOR)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_ADMIN), user(2, ROLE_OWNER))
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_ADMIN), user(2, ROLE_OPERATOR), next_role=ROLE_ADMIN)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_ADMIN), user(2, ROLE_VIEWER), next_role=ROLE_OWNER)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_OWNER), user(1, ROLE_OWNER), next_active=False)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_OWNER), user(1, ROLE_OWNER), next_role=ROLE_ADMIN)
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_OPERATOR), user(2, ROLE_VIEWER))
    assert_forbidden(ensure_can_modify_user, user(1, ROLE_VIEWER), user(2, ROLE_OPERATOR))


def test_endpoint_permission_contract_is_explicit_and_reviewable():
    assert ENDPOINT_PERMISSIONS
    keys = [(item.method, item.path) for item in ENDPOINT_PERMISSIONS]
    assert len(keys) == len(set(keys))
    assert all(item.decision for item in ENDPOINT_PERMISSIONS)
    assert decision("/health", "GET").decision == PUBLIC
    assert decision("/system/status", "GET").decision == PUBLIC
    assert decision("/system/info", "GET").decision == "manage_settings"
    assert decision("/settings/logs/archive", "GET").decision == "run_diagnostics"
    assert decision("/settings/bug-report", "POST").decision == "run_diagnostics"


def test_endpoint_allowed_roles_contract():
    assert decision("/settings", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/hardware/rescan", "POST").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/storage/status", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/users", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/viewer/cameras", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)
    assert decision("/recordings", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR)
    assert decision("/recordings", "DELETE").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/chronology/ranges", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR)
    assert decision("/live/debug", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)


def test_management_cameras_are_separated_from_viewer_cameras():
    source = read_router("cameras.py")
    assert 'APIRouter(prefix="/cameras"' in source
    assert 'APIRouter(prefix="/viewer/cameras"' in source
    assert 'Depends(require_permission("manage_cameras"))' in source
    assert 'Depends(require_permission("view_live"))' in source
    viewer_body = source.split("def list_viewer_cameras", 1)[1].split("@router.get", 1)[0]
    assert '"password"' not in viewer_body
    assert "password_encrypted" not in viewer_body
    assert decision("/cameras", "GET").decision == "manage_cameras"
    assert decision("/viewer/cameras", "GET").decision == "view_live"


def test_recording_routes_are_permission_protected():
    source = read_router("recordings.py")
    assert source.count('Depends(require_permission("view_recordings"))') >= 3
    assert source.count('Depends(require_permission("delete_recordings"))') >= 4
    assert 'user_has_permission(user.role, "view_recordings")' in source
    assert decision("/recordings/stream", "GET").decision == "view_recordings"
    assert decision("/recordings/all", "DELETE").decision == "delete_recordings"


def test_chronology_routes_and_token_file_access_are_permission_protected():
    source = read_router("chronology.py")
    assert source.count('Depends(require_permission("view_timeline"))') >= 2
    assert 'user_has_permission(user.role, "view_timeline")' in source
    assert "FORBIDDEN_DETAIL" in source
    assert decision("/chronology/file", "GET").decision == "view_timeline"


def test_live_routes_are_permission_protected():
    source = read_router("live.py")
    assert source.count('Depends(require_permission("view_live"))') >= 4
    assert source.count('Depends(require_permission("manage_settings"))') >= 4
    assert 'user_has_permission(user.role, "view_live")' in source
    assert "FORBIDDEN_DETAIL" in source
    assert decision("/live/{camera_id}/{stream}/index.m3u8", "GET").decision == "view_live"
    assert decision("/live/debug", "GET").decision == "manage_settings"
    assert decision("/live/stop-all", "POST").decision == "manage_settings"


def test_settings_hardware_storage_users_and_system_info_are_permission_protected():
    assert 'Depends(require_permission("manage_settings"))' in read_router("settings.py")
    assert 'Depends(require_permission("run_diagnostics"))' in read_router("settings.py")
    assert 'Depends(require_permission("manage_settings"))' in read_router("hardware.py")
    assert 'Depends(require_permission("manage_settings"))' in read_router("storage.py")
    assert 'Depends(require_permission("manage_users"))' in read_router("users.py")
    assert 'Depends(require_permission("manage_settings"))' in read_app_file("main.py")
