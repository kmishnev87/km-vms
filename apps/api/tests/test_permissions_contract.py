from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS, PUBLIC
from app.core.permissions import (
    PERMISSION_EXPORT_RECORDINGS,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_OWNER,
    ROLE_PERMISSIONS,
    ROLE_VIEWER,
    get_permissions_for_role,
    user_has_permission,
)
from app.main import app, health
from app.routers.deps import FORBIDDEN_DETAIL, require_permission
from app.routers.settings import redact_text as settings_redact_text
from app.routers.settings import safe_json
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


def actual_app_routes():
    routes = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or []:
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.add((method, route.path))
    return routes


def test_role_permissions_matrix():
    expected_admin = {
        "admin_access",
        "delete_recordings",
        "export_recordings",
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
    assert decision("/system/runtime/status", "GET").decision == "run_diagnostics"
    assert decision("/system/recorder/status", "GET").decision == "run_diagnostics"
    assert decision("/settings/logs/archive", "GET").decision == "run_diagnostics"
    assert decision("/settings/bug-report", "POST").decision == "run_diagnostics"


def test_endpoint_permission_registry_covers_actual_fastapi_routes():
    actual = actual_app_routes()
    registered = {(item.method, item.path) for item in ENDPOINT_PERMISSIONS}

    assert actual - registered == set()
    assert registered - actual == set()


def test_endpoint_allowed_roles_contract():
    assert decision("/audit/events", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/settings", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/hardware/rescan", "POST").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/storage/status", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/users", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/viewer/cameras", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)
    assert "view_live or view_timeline" in decision("/viewer/cameras", "GET").notes
    assert decision("/users/me/workspaces/{workspace_key}/layout", "GET").allowed_roles == (
        ROLE_OWNER,
        ROLE_ADMIN,
        ROLE_OPERATOR,
        ROLE_VIEWER,
    )
    assert decision("/users/me/workspaces/{workspace_key}/layout", "PUT").allowed_roles == (
        ROLE_OWNER,
        ROLE_ADMIN,
        ROLE_OPERATOR,
        ROLE_VIEWER,
    )
    assert decision("/recordings", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR)
    assert decision("/recordings", "DELETE").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/chronology/ranges", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR)
    assert decision("/archive/exports", "POST").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/archive/exports", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/archive/exports/{export_id}", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/live/debug", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/cameras/{camera_id}/enable", "POST").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/cameras/{camera_id}/disable", "POST").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)


def test_public_health_is_minimal_and_detailed_system_info_is_protected():
    assert health() == {"status": "ok"}
    assert "app_env" not in health()
    assert "storage_root" not in health()
    assert "storage_exists" not in health()
    assert decision("/health", "GET").decision == PUBLIC
    assert decision("/system/info", "GET").decision == "manage_settings"


def test_management_cameras_are_separated_from_viewer_cameras():
    source = read_router("cameras.py")
    assert 'APIRouter(prefix="/cameras"' in source
    assert 'APIRouter(prefix="/viewer/cameras"' in source
    assert 'Depends(require_permission("manage_cameras"))' in source
    assert 'Depends(get_current_user)' in source
    assert 'user_has_permission(role, "view_live") or user_has_permission(role, "view_timeline")' in source
    viewer_body = source.split("def list_viewer_cameras", 1)[1].split("@router.get", 1)[0]
    assert '"password"' not in viewer_body
    assert "password_encrypted" not in viewer_body
    assert '"rtsp_main_url": camera.rtsp_main_url' not in viewer_body
    assert '"rtsp_sub_url": camera.rtsp_sub_url' not in viewer_body
    assert '"rtsp_main_url": bool(camera.rtsp_main_url)' in source
    assert '"rtsp_sub_url": bool(camera.rtsp_sub_url)' in source
    assert decision("/cameras", "GET").decision == "manage_cameras"
    assert decision("/viewer/cameras", "GET").decision == "authenticated"


def test_recording_routes_are_permission_protected():
    source = read_router("recordings.py")
    assert source.count('Depends(require_permission("view_recordings"))') >= 3
    assert source.count('Depends(require_permission("delete_recordings"))') >= 4
    assert 'validate_media_token(' in source
    assert decision("/recordings/stream", "GET").decision == "view_recordings"
    assert decision("/recordings/media-token", "POST").decision == "view_recordings"
    assert decision("/recordings/all", "DELETE").decision == "delete_recordings"


def test_camera_delete_with_files_permission_contract_is_explicit():
    source = read_router("cameras.py")
    assert 'Depends(require_permission("manage_cameras"))' in source
    assert 'user_has_permission(getattr(current_user, "role", ""), "delete_recordings")' in source
    assert decision("/cameras/{camera_id}/delete-preview", "GET").decision == "manage_cameras"
    assert "delete_recordings" in decision("/cameras/{camera_id}/delete-preview", "GET").notes
    assert decision("/cameras/{camera_id}", "DELETE").decision == "manage_cameras"
    assert "delete_recordings" in decision("/cameras/{camera_id}", "DELETE").notes


def test_chronology_routes_and_token_file_access_are_permission_protected():
    source = read_router("chronology.py")
    assert source.count('Depends(require_permission("view_timeline"))') >= 3
    assert 'validate_media_token(' in source
    assert 'scope="chronology"' in source
    assert decision("/chronology/file", "GET").decision == "view_timeline"
    assert decision("/chronology/media-token", "POST").decision == "view_timeline"


def test_archive_export_routes_require_explicit_export_permission():
    source = read_router("archive_exports.py")
    assert "Depends(require_permission(PERMISSION_EXPORT_RECORDINGS))" in source
    assert decision("/archive/exports", "POST").decision == PERMISSION_EXPORT_RECORDINGS
    assert decision("/archive/exports", "GET").decision == PERMISSION_EXPORT_RECORDINGS
    assert decision("/archive/exports/{export_id}", "GET").decision == PERMISSION_EXPORT_RECORDINGS


def test_live_routes_are_permission_protected():
    source = read_router("live.py")
    assert source.count('Depends(require_permission("view_live"))') >= 5
    assert source.count('Depends(require_permission("manage_settings"))') >= 4
    assert 'validate_media_token(' in source
    assert 'scope="live"' in source
    assert decision("/live/{camera_id}/{stream}/index.m3u8", "GET").decision == "view_live"
    assert decision("/live/media-token", "POST").decision == "view_live"
    assert decision("/live/debug", "GET").decision == "manage_settings"
    assert decision("/live/stop-all", "POST").decision == "manage_settings"


def test_settings_hardware_storage_users_and_system_info_are_permission_protected():
    assert decision("/audit/events", "GET").decision == "manage_settings"
    assert 'Depends(require_permission("manage_settings"))' in read_router("settings.py")
    assert 'Depends(require_permission("run_diagnostics"))' in read_router("settings.py")
    assert decision("/system/runtime/status", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert 'Depends(require_permission("manage_settings"))' in read_router("hardware.py")
    assert 'Depends(require_permission("manage_settings"))' in read_router("storage.py")
    assert 'Depends(require_permission("manage_users"))' in read_router("users.py")
    assert 'Depends(require_permission("manage_settings"))' in read_app_file("main.py")


def test_permission_denial_detail_is_normalized():
    dependency = require_permission("manage_settings")
    with pytest.raises(HTTPException) as exc:
        dependency(user(10, ROLE_VIEWER))

    assert exc.value.status_code == 403
    assert exc.value.detail == FORBIDDEN_DETAIL
    assert exc.value.detail != "Insufficient permissions"


def test_diagnostic_archive_audit_contract_is_time_based():
    source = read_router("settings.py")
    assert '"audit/events_recent.json"' in source
    assert '"audit/events_recent.txt"' in source
    assert 'since_minutes=30 if mode == "extended" else 10' in source
    assert '"audit_event_rule": "last 30 minutes" if mode == "extended" else "last 10 minutes"' in source
    assert '"docker_log_rule": "--since=30m" if mode == "extended" else "--since=10m"' in source


def test_diagnostic_archive_permissions_are_limited_to_diagnostics_permission():
    assert decision("/system/recorder/status", "GET").decision == "run_diagnostics"
    assert decision("/settings/logs/archive", "GET").decision == "run_diagnostics"
    assert decision("/settings/bug-report", "POST").decision == "run_diagnostics"
    assert decision("/system/recorder/status", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/settings/logs/archive", "GET").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)
    assert decision("/settings/bug-report", "POST").allowed_roles == (ROLE_OWNER, ROLE_ADMIN)


def test_diagnostic_archive_redaction_helpers_mask_sensitive_values():
    text = settings_redact_text(
        "Authorization: Bearer archive-token rtsp://user:camera-pass@host/live?access_token=query-token"
    )
    assert "archive-token" not in text
    assert "camera-pass" not in text
    assert "query-token" not in text
    assert "Bearer ***" in text
    assert "rtsp://***@host/live" in text
    assert "access_token=***" in text

    payload = safe_json(
        {
            "password": "plain-secret",
            "JWT_SECRET": "jwt-secret",
            "camera": {"url": "rtsp://admin:rtsp-secret@camera/live"},
            "items": [{"access_token": "nested-token"}],
        }
    )
    rendered = str(payload)
    assert "plain-secret" not in rendered
    assert "jwt-secret" not in rendered
    assert "rtsp-secret" not in rendered
    assert "nested-token" not in rendered
    assert payload["password"] == "***"
    assert payload["JWT_SECRET"] == "***"
