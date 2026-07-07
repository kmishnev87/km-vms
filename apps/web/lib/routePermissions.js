export const ROUTE_ACCESS_PUBLIC = "public";
export const ROUTE_ACCESS_PERMISSION = "permission";
export const ROUTE_ACCESS_DENIED = "denied";

export const FRONTEND_ROUTE_ACCESS = Object.freeze({
  "/": Object.freeze({
    access: ROUTE_ACCESS_PUBLIC,
    reason: "dashboard shell; protected tiles and actions are permission-filtered",
  }),
  "/login": Object.freeze({
    access: ROUTE_ACCESS_PUBLIC,
    reason: "authentication entry point",
  }),
  "/setup": Object.freeze({
    access: ROUTE_ACCESS_PUBLIC,
    reason: "first-run setup gate; setup API routes remain backend-gated",
  }),
  "/apk": Object.freeze({
    access: ROUTE_ACCESS_PUBLIC,
    placeholderOnly: true,
    reason: "placeholder-only client page; no backend APK download route is exposed",
  }),
  "/live": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "view_live",
    backend: Object.freeze(["/live/media-token", "/live/{camera_id}/{stream}/index.m3u8", "/viewer/cameras"]),
  }),
  "/recordings": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "view_recordings",
    backend: Object.freeze(["/recordings", "/recordings/media-token", "/recordings/stream"]),
  }),
  "/chronology": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "view_timeline",
    backend: Object.freeze(["/chronology/ranges", "/chronology/media-token", "/chronology/file"]),
  }),
  "/cameras": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "manage_cameras",
    backend: Object.freeze(["/cameras", "/cameras/test", "/cameras/onvif/discover"]),
  }),
  "/settings": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "manage_settings",
    backend: Object.freeze(["/settings", "/system/maintenance/overview", "/users"]),
  }),
  "/storage": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "manage_settings",
    backend: Object.freeze([
      "/storage/status",
      "/settings",
      "/storage/archive-roots",
      "/storage/archive-roots/validate",
      "/storage/archive-roots/{root_id}/activate",
      "/storage/migration/preview",
      "/storage/migration/apply",
      "/storage/reconciliation/summary",
      "/storage/reconcile",
      "/recordings/retention/dry-run",
      "/recordings/retention/run",
    ]),
    backendEndpoints: Object.freeze([
      Object.freeze({ method: "GET", path: "/storage/status", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/settings", permission: "manage_settings" }),
      Object.freeze({ method: "PATCH", path: "/settings", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/storage/archive-roots", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/archive-roots/validate", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/archive-roots", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/archive-roots/{root_id}/activate", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/migration/preview", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/migration/apply", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/storage/reconciliation/summary", permission: "run_diagnostics" }),
      Object.freeze({ method: "POST", path: "/storage/reconcile", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/recordings/retention/dry-run", permission: "delete_recordings" }),
      Object.freeze({ method: "POST", path: "/recordings/retention/run", permission: "delete_recordings" }),
    ]),
  }),
  "/system-status": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "run_diagnostics",
    backend: Object.freeze(["/system/runtime/status", "/system/recorder/status"]),
  }),
  "/security-journal": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "manage_settings",
    backend: Object.freeze(["/audit/events"]),
  }),
  "/diagnostics": Object.freeze({
    access: ROUTE_ACCESS_PERMISSION,
    permission: "run_diagnostics",
    backend: Object.freeze(["/settings/logs/archive", "/settings/bug-report", "/system/upgrade/report"]),
  }),
});

export function normalizeRoutePath(href) {
  const raw = String(href || "").trim();
  if (!raw) return "";
  const pathOnly = raw.split("#", 1)[0].split("?", 1)[0] || "/";
  if (pathOnly.length > 1 && pathOnly.endsWith("/")) return pathOnly.slice(0, -1);
  return pathOnly;
}

export function routeAccessEntry(href) {
  return FRONTEND_ROUTE_ACCESS[normalizeRoutePath(href)] || null;
}

export function canUserAccessRoute(user, href) {
  const entry = routeAccessEntry(href);
  if (!entry) return false;
  if (entry.access === ROUTE_ACCESS_PUBLIC) return true;
  if (entry.access !== ROUTE_ACCESS_PERMISSION || !entry.permission) return false;
  return Array.isArray(user?.permissions) && user.permissions.includes(entry.permission);
}
