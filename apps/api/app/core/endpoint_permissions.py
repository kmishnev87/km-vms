from __future__ import annotations

from dataclasses import dataclass

from app.core.permissions import (
    PERMISSION_DELETE_RECORDINGS,
    PERMISSION_MANAGE_CAMERAS,
    PERMISSION_MANAGE_SETTINGS,
    PERMISSION_MANAGE_USERS,
    PERMISSION_RUN_DIAGNOSTICS,
    PERMISSION_VIEW_LIVE,
    PERMISSION_VIEW_RECORDINGS,
    PERMISSION_VIEW_TIMELINE,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_OWNER,
    ROLE_PERMISSIONS,
    ROLE_VIEWER,
)


PUBLIC = "public"
AUTHENTICATED = "authenticated"


@dataclass(frozen=True)
class EndpointPermission:
    group: str
    method: str
    path: str
    decision: str
    enforcement: str
    notes: str = ""

    @property
    def allowed_roles(self) -> tuple[str, ...]:
        if self.decision == PUBLIC:
            return (PUBLIC,)
        if self.decision == AUTHENTICATED:
            return (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)
        return tuple(role for role, permissions in ROLE_PERMISSIONS.items() if self.decision in permissions)


ENDPOINT_PERMISSIONS: tuple[EndpointPermission, ...] = (
    EndpointPermission("auth", "POST", "/auth/login", PUBLIC, "public route", "Requires username/password."),
    EndpointPermission("auth", "GET", "/auth/me", AUTHENTICATED, "get_current_user"),
    EndpointPermission("audit", "GET", "/audit/events", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("users", "GET", "/users/me", AUTHENTICATED, "get_current_user"),
    EndpointPermission("users", "GET", "/users", PERMISSION_MANAGE_USERS, "require_permission"),
    EndpointPermission("users", "POST", "/users", PERMISSION_MANAGE_USERS, "require_permission"),
    EndpointPermission("users", "PATCH", "/users/{user_id}", PERMISSION_MANAGE_USERS, "require_permission"),
    EndpointPermission("users", "DELETE", "/users/{user_id}", PERMISSION_MANAGE_USERS, "require_permission"),
    EndpointPermission("settings", "GET", "/system/status", PUBLIC, "public route", "First-run setup status only."),
    EndpointPermission("settings", "POST", "/setup", PUBLIC, "public route", "Allowed only before initialization."),
    EndpointPermission("settings", "GET", "/settings", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("settings", "PATCH", "/settings", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("settings", "POST", "/settings/storage/validate", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("diagnostics", "GET", "/settings/logs/archive", PERMISSION_RUN_DIAGNOSTICS, "require_permission"),
    EndpointPermission("diagnostics", "POST", "/settings/bug-report", PERMISSION_RUN_DIAGNOSTICS, "require_permission"),
    EndpointPermission("hardware", "GET", "/hardware/capabilities", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("hardware", "POST", "/hardware/rescan", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("storage", "GET", "/storage/status", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only Storage Monitoring summary; owner/admin only."),
    EndpointPermission("storage", "GET", "/storage/reconciliation/summary", PERMISSION_RUN_DIAGNOSTICS, "require_permission"),
    EndpointPermission("storage", "POST", "/storage/reconcile", PERMISSION_MANAGE_SETTINGS, "require_permission", "Non-destructive dry_run/apply_safe reconciliation; deletion is forbidden."),
    EndpointPermission("system", "GET", "/", PUBLIC, "public route", "Minimal service name/status."),
    EndpointPermission("system", "GET", "/health", PUBLIC, "public route", "Minimal health data."),
    EndpointPermission("system", "GET", "/system/info", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("cameras", "GET", "/cameras", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "GET", "/cameras/{camera_id}", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "PUT", "/cameras/{camera_id}", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/{camera_id}/enable", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/{camera_id}/disable", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "DELETE", "/cameras/{camera_id}", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/test", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/onvif/profiles", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/onvif/profile_config", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/onvif/update_profile", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("viewer-cameras", "GET", "/viewer/cameras", PERMISSION_VIEW_LIVE, "require_permission", "Viewer-safe camera fields only."),
    EndpointPermission("live", "GET", "/live/status", PERMISSION_VIEW_LIVE, "require_permission"),
    EndpointPermission("live", "POST", "/live/viewers", PERMISSION_VIEW_LIVE, "require_permission"),
    EndpointPermission("live", "DELETE", "/live/viewers/{viewer_id}", PERMISSION_VIEW_LIVE, "require_permission"),
    EndpointPermission("live", "POST", "/live/viewers/{viewer_id}/touch", PERMISSION_VIEW_LIVE, "require_permission"),
    EndpointPermission("live", "GET", "/live/{camera_id}/{stream}/index.m3u8", PERMISSION_VIEW_LIVE, "token user permission check"),
    EndpointPermission("live", "GET", "/live/{camera_id}/{stream}/{filename}", PERMISSION_VIEW_LIVE, "token user permission check"),
    EndpointPermission("live", "GET", "/live/debug", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("live", "GET", "/live/debug/{camera_id}/{stream}", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("live", "POST", "/live/stop", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("live", "POST", "/live/stop-all", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("recordings", "GET", "/recordings/cameras", PERMISSION_VIEW_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "GET", "/recordings", PERMISSION_VIEW_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "GET", "/recordings/download", PERMISSION_VIEW_RECORDINGS, "token/header user permission check"),
    EndpointPermission("recordings", "GET", "/recordings/stream", PERMISSION_VIEW_RECORDINGS, "token user permission check"),
    EndpointPermission("recordings", "POST", "/recordings/retention/dry-run", PERMISSION_DELETE_RECORDINGS, "require_permission", "Read-only retention plan; no file or metadata mutation."),
    EndpointPermission("recordings", "GET", "/recordings/retention/plan", PERMISSION_DELETE_RECORDINGS, "require_permission", "Read-only retention plan; no file or metadata mutation."),
    EndpointPermission("recordings", "POST", "/recordings/retention/run", PERMISSION_DELETE_RECORDINGS, "require_permission", "Destructive retention apply with explicit confirm, safety limits, and lock."),
    EndpointPermission("recordings", "DELETE", "/recordings", PERMISSION_DELETE_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "POST", "/recordings/bulk-delete", PERMISSION_DELETE_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "DELETE", "/recordings/by-camera", PERMISSION_DELETE_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "DELETE", "/recordings/all", PERMISSION_DELETE_RECORDINGS, "require_permission", "Dry-run preview supported; destructive delete-all requires confirm=true and confirmation_text=DELETE_ALL_RECORDINGS."),
    EndpointPermission("chronology", "GET", "/chronology/playback", PERMISSION_VIEW_TIMELINE, "require_permission"),
    EndpointPermission("chronology", "GET", "/chronology/ranges", PERMISSION_VIEW_TIMELINE, "require_permission"),
    EndpointPermission("chronology", "GET", "/chronology/file", PERMISSION_VIEW_TIMELINE, "token user permission check"),
)


def endpoint_permission_rows() -> list[dict]:
    return [
        {
            "group": item.group,
            "method": item.method,
            "path": item.path,
            "decision": item.decision,
            "allowed_roles": item.allowed_roles,
            "enforcement": item.enforcement,
            "notes": item.notes,
        }
        for item in ENDPOINT_PERMISSIONS
    ]
