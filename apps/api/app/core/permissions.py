ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

PERMISSIONS = {
    "view_live",
    "view_recordings",
    "view_timeline",
    "manage_cameras",
    "delete_recordings",
    "admin_access",
    "manage_settings",
}

ROLE_PERMISSIONS = {
    ROLE_OWNER: PERMISSIONS,
    ROLE_ADMIN: PERMISSIONS,
    ROLE_OPERATOR: {
        "view_live",
        "view_recordings",
        "view_timeline",
    },
    ROLE_VIEWER: {
        "view_live",
    },
}


def user_has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())
