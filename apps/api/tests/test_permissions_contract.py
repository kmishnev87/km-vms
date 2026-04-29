from pathlib import Path

from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER, ROLE_PERMISSIONS, ROLE_VIEWER


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def read_router(name: str) -> str:
    return (APP_ROOT / "routers" / name).read_text(encoding="utf-8")


def test_role_permissions_matrix():
    assert "manage_settings" in ROLE_PERMISSIONS[ROLE_OWNER]
    assert "manage_cameras" in ROLE_PERMISSIONS[ROLE_OWNER]
    assert "delete_recordings" in ROLE_PERMISSIONS[ROLE_OWNER]

    assert "manage_settings" in ROLE_PERMISSIONS[ROLE_ADMIN]
    assert "manage_cameras" in ROLE_PERMISSIONS[ROLE_ADMIN]
    assert "delete_recordings" in ROLE_PERMISSIONS[ROLE_ADMIN]

    assert ROLE_PERMISSIONS[ROLE_OPERATOR] == {
        "view_live",
        "view_recordings",
        "view_timeline",
    }
    assert ROLE_PERMISSIONS[ROLE_VIEWER] == {"view_live"}


def test_management_cameras_are_permission_protected():
    source = read_router("cameras.py")
    assert 'APIRouter(prefix="/cameras"' in source
    assert 'APIRouter(prefix="/viewer/cameras"' in source
    assert 'Depends(require_permission("manage_cameras"))' in source
    assert 'Depends(require_permission("view_live"))' in source


def test_recording_delete_routes_are_permission_protected():
    source = read_router("recordings.py")
    assert 'Depends(require_permission("view_recordings"))' in source
    assert source.count('Depends(require_permission("delete_recordings"))') >= 4


def test_settings_hardware_storage_are_permission_protected():
    assert 'Depends(require_permission("manage_settings"))' in read_router("settings.py")
    assert 'Depends(require_permission("manage_settings"))' in read_router("hardware.py")
    assert 'Depends(require_permission("manage_settings"))' in read_router("storage.py")
