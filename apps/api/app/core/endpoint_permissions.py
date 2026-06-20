from __future__ import annotations

from dataclasses import dataclass

from app.core.permissions import (
    PERMISSION_DELETE_RECORDINGS,
    PERMISSION_EXPORT_RECORDINGS,
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
    EndpointPermission("setup-storage", "GET", "/setup/storage/discovery", PUBLIC, "public route", "Allowed only before initialization; sanitized host snapshot only."),
    EndpointPermission("setup-storage", "GET", "/setup/storage/status", PUBLIC, "public route", "Allowed only before initialization; selected host path status only."),
    EndpointPermission("setup-storage", "POST", "/setup/storage/preview", PUBLIC, "public route", "Allowed only before initialization; revalidates candidate server-side."),
    EndpointPermission("setup-storage", "POST", "/setup/storage/validate", PUBLIC, "public route", "Allowed only before initialization; creates selected child folder and marker only."),
    EndpointPermission("setup-storage", "POST", "/setup/storage/apply", PUBLIC, "public route", "Allowed only before initialization; writes non-secret pending selection for host helper."),
    EndpointPermission("settings", "GET", "/settings", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("settings", "PATCH", "/settings", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("settings", "POST", "/settings/storage/validate", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("diagnostics", "GET", "/system/runtime/status", PERMISSION_RUN_DIAGNOSTICS, "require_permission", "Operator camera/live/recorder runtime status aggregate."),
    EndpointPermission("diagnostics", "GET", "/system/recorder/status", PERMISSION_RUN_DIAGNOSTICS, "require_permission", "Recorder liveness, jobs, storage, retention, and segment diagnostics."),
    EndpointPermission("diagnostics", "GET", "/system/recorder/summary", PERMISSION_RUN_DIAGNOSTICS, "require_permission", "Lightweight recorder UI status without storage or retention diagnostics."),
    EndpointPermission("system", "GET", "/system/schema/status", PERMISSION_MANAGE_SETTINGS, "require_permission", "Database schema version status; owner/admin only."),
    EndpointPermission("system", "GET", "/system/schema/plan", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only deterministic migration plan; no migration execution."),
    EndpointPermission("system", "GET", "/system/backup/plan", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only backup-before-upgrade plan; no backup files created."),
    EndpointPermission("system", "POST", "/system/backup/create", PERMISSION_MANAGE_SETTINGS, "require_permission", "Creates a sensitive DB backup artifact; no backup download endpoint."),
    EndpointPermission("system", "GET", "/system/db-adoption/status", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only DB adoption status; no mutation."),
    EndpointPermission("system", "POST", "/system/db-adoption/dry-run", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only DB adoption dry-run report; no backup or metadata write."),
    EndpointPermission("system", "POST", "/system/db-adoption/apply", PERMISSION_MANAGE_SETTINGS, "require_permission", "Explicit DB adoption apply; requires backup first and writes schema metadata only."),
    EndpointPermission("system", "GET", "/system/migrations/status", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only migration maintenance status; no mutation."),
    EndpointPermission("system", "POST", "/system/migrations/dry-run", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only migration maintenance preflight; no backup or migration execution."),
    EndpointPermission("system", "POST", "/system/migrations/apply", PERMISSION_MANAGE_SETTINGS, "require_permission", "Explicit migration apply; requires confirmation and backup first."),
    EndpointPermission("system", "GET", "/system/restore/status", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only restore artifact status; no restore or backup."),
    EndpointPermission("system", "POST", "/system/restore/dry-run", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only restore preflight; no current backup or restore."),
    EndpointPermission("system", "POST", "/system/restore/apply", PERMISSION_MANAGE_SETTINGS, "require_permission", "Explicit restore apply; protected, confirmed, and safety-gated."),
    EndpointPermission("system", "GET", "/system/maintenance/overview", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only maintenance UX overview for adoption, migration, restore and report summaries."),
    EndpointPermission("diagnostics", "GET", "/system/upgrade/report", PERMISSION_RUN_DIAGNOSTICS, "require_permission", "Read-only sanitized upgrade/backup/restore report; no backup, restore or migration execution."),
    EndpointPermission("system", "GET", "/system/update/status", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only safe update status; no network check or update apply."),
    EndpointPermission("system", "POST", "/system/update/check", PERMISSION_MANAGE_SETTINGS, "require_permission", "Manual safe update check against trusted configured source only; no arbitrary URL, download, restart, migration, backup or restore."),
    EndpointPermission("system", "POST", "/system/update/apply", PERMISSION_MANAGE_SETTINGS, "require_permission", "Confirmed update apply request writer; API remains socket-free and does not run Docker or update.sh."),
    EndpointPermission("system", "GET", "/system/update/apply/status", PERMISSION_MANAGE_SETTINGS, "require_permission", "Reads sanitized update helper status from controlled update-control files."),
    EndpointPermission("system", "POST", "/system/update/apply/cancel", PERMISSION_MANAGE_SETTINGS, "require_permission", "Cancels only queued update apply before helper starts; no rollback or container killing."),
    EndpointPermission("diagnostics", "GET", "/settings/logs/archive", PERMISSION_RUN_DIAGNOSTICS, "require_permission"),
    EndpointPermission("diagnostics", "POST", "/settings/bug-report", PERMISSION_RUN_DIAGNOSTICS, "require_permission"),
    EndpointPermission("hardware", "GET", "/hardware/capabilities", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("hardware", "POST", "/hardware/rescan", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("storage", "GET", "/storage/status", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only Storage Monitoring summary; owner/admin only."),
    EndpointPermission("storage", "GET", "/storage/archive-roots", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("storage", "POST", "/storage/archive-roots/validate", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("storage", "POST", "/storage/archive-roots", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("storage", "POST", "/storage/archive-roots/{root_id}/activate", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("storage", "POST", "/storage/migration/preview", PERMISSION_MANAGE_SETTINGS, "require_permission", "Read-only migration preview; no file move/copy/delete."),
    EndpointPermission("storage", "POST", "/storage/migration/apply", PERMISSION_MANAGE_SETTINGS, "require_permission", "Explicit copy-only migration apply; confirm required, server-side configured roots only, source preserved."),
    EndpointPermission("storage", "GET", "/storage/reconciliation/summary", PERMISSION_RUN_DIAGNOSTICS, "require_permission"),
    EndpointPermission("storage", "POST", "/storage/reconcile", PERMISSION_MANAGE_SETTINGS, "require_permission", "Non-destructive dry_run/apply_safe reconciliation; deletion is forbidden."),
    EndpointPermission("system", "GET", "/", PUBLIC, "public route", "Minimal service name/status."),
    EndpointPermission("system", "GET", "/health", PUBLIC, "public route", "Minimal health data."),
    EndpointPermission("system", "GET", "/system/info", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("previews", "GET", "/previews/camera-previews/{camera_id}.jpg", PUBLIC, "public route", "Validated preview file response under configured previews root."),
    EndpointPermission("previews", "GET", "/previews/camera-tests/{token}.jpg", PUBLIC, "public route", "Validated camera-test preview file response under configured previews root."),
    EndpointPermission("cameras", "GET", "/cameras", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "GET", "/cameras/{camera_id}", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "PUT", "/cameras/{camera_id}", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/{camera_id}/enable", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/{camera_id}/disable", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "GET", "/cameras/{camera_id}/delete-preview", PERMISSION_MANAGE_CAMERAS, "require_permission", "delete_files=true additionally requires delete_recordings in route logic."),
    EndpointPermission("cameras", "DELETE", "/cameras/{camera_id}", PERMISSION_MANAGE_CAMERAS, "require_permission", "delete_files=true additionally requires delete_recordings in route logic and returns JSON summary."),
    EndpointPermission("cameras", "POST", "/cameras/test", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/onvif/discover", PERMISSION_MANAGE_CAMERAS, "require_permission", "Bounded WS-Discovery only; no broad subnet scans and no secrets."),
    EndpointPermission("cameras", "POST", "/cameras/onvif/probe", PERMISSION_MANAGE_CAMERAS, "require_permission", "Manual ONVIF reachability probe; sanitized response only."),
    EndpointPermission("cameras", "POST", "/cameras/onvif/profiles", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/onvif/profile_config", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "POST", "/cameras/onvif/update_profile", PERMISSION_MANAGE_CAMERAS, "require_permission"),
    EndpointPermission("cameras", "GET", "/cameras/{camera_id}/onvif/ptz/capabilities", PERMISSION_MANAGE_CAMERAS, "require_permission", "Read-only sanitized ONVIF PTZ capability summary."),
    EndpointPermission("cameras", "POST", "/cameras/{camera_id}/onvif/ptz/command", PERMISSION_MANAGE_CAMERAS, "require_permission", "Allowlisted bounded PTZ command contract; dry-run/validation-only by default."),
    EndpointPermission("cameras", "GET", "/cameras/{camera_id}/onvif/health", PERMISSION_MANAGE_CAMERAS, "require_permission", "Cached/static ONVIF health and compatibility summary; no camera network probe."),
    EndpointPermission("cameras", "POST", "/cameras/{camera_id}/onvif/health/check", PERMISSION_MANAGE_CAMERAS, "require_permission", "Explicit bounded read-only ONVIF health, profile, PTZ and events feasibility check."),
    EndpointPermission("viewer-cameras", "GET", "/viewer/cameras", AUTHENTICATED, "get_current_user + view_live/view_timeline route check", "Viewer-safe camera fields only; requires either view_live or view_timeline."),
    EndpointPermission("live", "GET", "/live/status", PERMISSION_VIEW_LIVE, "require_permission"),
    EndpointPermission("live", "POST", "/live/media-token", PERMISSION_VIEW_LIVE, "require_permission", "Issues short-lived scoped media_token for live HLS."),
    EndpointPermission("live", "POST", "/live/viewers", PERMISSION_VIEW_LIVE, "require_permission"),
    EndpointPermission("live", "DELETE", "/live/viewers/{viewer_id}", PERMISSION_VIEW_LIVE, "require_permission"),
    EndpointPermission("live", "POST", "/live/viewers/{viewer_id}/touch", PERMISSION_VIEW_LIVE, "require_permission"),
    EndpointPermission("live", "GET", "/live/{camera_id}/{stream}/index.m3u8", PERMISSION_VIEW_LIVE, "scoped media_token check"),
    EndpointPermission("live", "GET", "/live/{camera_id}/{stream}/{filename}", PERMISSION_VIEW_LIVE, "scoped media_token check"),
    EndpointPermission("live", "GET", "/live/debug", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("live", "GET", "/live/debug/{camera_id}/{stream}", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("live", "POST", "/live/stop", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("live", "POST", "/live/stop-all", PERMISSION_MANAGE_SETTINGS, "require_permission"),
    EndpointPermission("recordings", "GET", "/recordings/cameras", PERMISSION_VIEW_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "GET", "/recordings", PERMISSION_VIEW_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "POST", "/recordings/media-token", PERMISSION_VIEW_RECORDINGS, "require_permission", "Issues short-lived scoped media_token for recording stream/download."),
    EndpointPermission("recordings", "GET", "/recordings/download", PERMISSION_VIEW_RECORDINGS, "scoped media_token check"),
    EndpointPermission("recordings", "GET", "/recordings/stream", PERMISSION_VIEW_RECORDINGS, "scoped media_token check"),
    EndpointPermission("recordings", "POST", "/recordings/retention/dry-run", PERMISSION_DELETE_RECORDINGS, "require_permission", "Read-only retention plan; no file or metadata mutation."),
    EndpointPermission("recordings", "GET", "/recordings/retention/plan", PERMISSION_DELETE_RECORDINGS, "require_permission", "Read-only retention plan; no file or metadata mutation."),
    EndpointPermission("recordings", "POST", "/recordings/retention/run", PERMISSION_DELETE_RECORDINGS, "require_permission", "Destructive retention apply with explicit confirm, safety limits, and lock."),
    EndpointPermission("recordings", "DELETE", "/recordings", PERMISSION_DELETE_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "POST", "/recordings/bulk-delete", PERMISSION_DELETE_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "DELETE", "/recordings/by-camera", PERMISSION_DELETE_RECORDINGS, "require_permission"),
    EndpointPermission("recordings", "DELETE", "/recordings/all", PERMISSION_DELETE_RECORDINGS, "require_permission", "Dry-run preview supported; destructive delete-all requires confirm=true and confirmation_text=DELETE_ALL_RECORDINGS."),
    EndpointPermission("chronology", "GET", "/chronology/playback", PERMISSION_VIEW_TIMELINE, "require_permission"),
    EndpointPermission("chronology", "GET", "/chronology/ranges", PERMISSION_VIEW_TIMELINE, "require_permission"),
    EndpointPermission("chronology", "POST", "/chronology/download-token", PERMISSION_VIEW_TIMELINE, "require_permission", "Issues short-lived scoped token for native source recording download by camera_id + timestamp."),
    EndpointPermission("chronology", "GET", "/chronology/download", PERMISSION_VIEW_TIMELINE, "scoped media_token check", "Downloads finalized source recording covering camera_id + timestamp; client never supplies a path."),
    EndpointPermission("chronology", "POST", "/chronology/media-token", PERMISSION_VIEW_TIMELINE, "require_permission", "Issues short-lived scoped media_token for chronology file."),
    EndpointPermission("chronology", "GET", "/chronology/file", PERMISSION_VIEW_TIMELINE, "scoped media_token check"),
    EndpointPermission("archive-exports", "POST", "/archive/exports", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Creates metadata-only queued archive export jobs; no clip generation."),
    EndpointPermission("archive-exports", "GET", "/archive/exports", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Lists safe archive export job metadata."),
    EndpointPermission("archive-exports", "GET", "/archive/exports/limits", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Reads server-side evidence export UI limits."),
    EndpointPermission("archive-exports", "POST", "/archive/exports/cleanup", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Runs bounded cleanup for expired export-owned artifacts under export root."),
    EndpointPermission("archive-exports", "GET", "/archive/exports/{export_id}", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Reads safe archive export job metadata."),
    EndpointPermission("archive-exports", "POST", "/archive/exports/{export_id}/generate", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Generates bounded evidence clip for queued export job; no download endpoint."),
    EndpointPermission("archive-exports", "POST", "/archive/exports/{export_id}/manifest", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Creates or refreshes protected evidence metadata manifest; no download endpoint."),
    EndpointPermission("archive-exports", "GET", "/archive/exports/{export_id}/manifest", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Reads protected sanitized evidence metadata manifest; no file attachment."),
    EndpointPermission("archive-exports", "GET", "/archive/exports/{export_id}/download", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Downloads completed evidence clip through authenticated request only."),
    EndpointPermission("archive-exports", "GET", "/archive/exports/{export_id}/manifest/download", PERMISSION_EXPORT_RECORDINGS, "require_permission", "Downloads validated evidence manifest JSON through authenticated request only."),
    EndpointPermission("users", "GET", "/users/me/workspaces/{workspace_key}/layout", AUTHENTICATED, "get_current_user + workspace permission route check", "Per-user workspace layout. live requires view_live; chronology requires view_timeline."),
    EndpointPermission("users", "PUT", "/users/me/workspaces/{workspace_key}/layout", AUTHENTICATED, "get_current_user + workspace permission route check", "Per-user workspace layout. live requires view_live; chronology requires view_timeline."),
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
