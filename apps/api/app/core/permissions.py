ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

PERMISSION_VIEW_LIVE = "view_live"
PERMISSION_VIEW_RECORDINGS = "view_recordings"
PERMISSION_VIEW_TIMELINE = "view_timeline"
PERMISSION_MANAGE_CAMERAS = "manage_cameras"
PERMISSION_DELETE_RECORDINGS = "delete_recordings"
PERMISSION_ADMIN_ACCESS = "admin_access"
PERMISSION_MANAGE_SETTINGS = "manage_settings"
PERMISSION_MANAGE_USERS = "manage_users"
PERMISSION_RUN_DIAGNOSTICS = "run_diagnostics"

PERMISSIONS = frozenset(
    {
        PERMISSION_VIEW_LIVE,
        PERMISSION_VIEW_RECORDINGS,
        PERMISSION_VIEW_TIMELINE,
        PERMISSION_MANAGE_CAMERAS,
        PERMISSION_DELETE_RECORDINGS,
        PERMISSION_ADMIN_ACCESS,
        PERMISSION_MANAGE_SETTINGS,
        PERMISSION_MANAGE_USERS,
        PERMISSION_RUN_DIAGNOSTICS,
    }
)

ROLE_PERMISSIONS = {
    ROLE_OWNER: PERMISSIONS,
    ROLE_ADMIN: PERMISSIONS,
    ROLE_OPERATOR: frozenset(
        {
            PERMISSION_VIEW_LIVE,
            PERMISSION_VIEW_RECORDINGS,
            PERMISSION_VIEW_TIMELINE,
        }
    ),
    ROLE_VIEWER: frozenset({PERMISSION_VIEW_LIVE}),
}


def get_permissions_for_role(role: str) -> frozenset[str]:
    normalized = str(role or "").strip().lower()
    return frozenset(ROLE_PERMISSIONS.get(normalized, frozenset()))


def user_has_permission(role: str, permission: str) -> bool:
    if permission not in PERMISSIONS:
        return False
    return permission in get_permissions_for_role(role)
