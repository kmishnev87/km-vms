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
      "/storage/operations/history",
      "/settings",
      "/storage/archive-roots",
      "/storage/archive-roots/discovery",
      "/storage/archive-roots/{root_id}/activate",
      "/storage/archive-roots/{root_id}",
      "/storage/migration/preview",
      "/storage/migration/plans/{plan_id}",
      "/storage/migration/plans/{plan_id}/items",
      "/storage/migration/plans/{plan_id}/cancel",
      "/storage/migration/apply",
      "/storage/migration/operations/active",
      "/storage/migration/operations/{operation_id}",
      "/storage/migration/operations/{operation_id}/cancel",
      "/storage/migration/operations/{operation_id}/retry",
      "/storage/migration/operations/{operation_id}/cleanup-takeover",
      "/storage/reconciliation/summary",
      "/storage/reconcile",
      "/storage/integrity/scans",
      "/storage/integrity/scans/latest",
      "/storage/integrity/scans/{scan_id}",
      "/storage/integrity/scans/{scan_id}/cancel",
      "/storage/integrity/scans/{scan_id}/findings",
      "/storage/integrity/findings/{finding_id}/metadata-plan",
      "/storage/integrity/findings/{finding_id}/deletion-plan",
      "/storage/integrity/remediation-plans/{plan_id}",
      "/storage/integrity/remediation-plans/{plan_id}/apply-metadata",
      "/storage/integrity/remediation-plans/{plan_id}/apply-deletion",
      "/recordings/retention/dry-run",
      "/recordings/retention/run",
    ]),
    backendEndpoints: Object.freeze([
      Object.freeze({ method: "GET", path: "/storage/status", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/storage/operations/history", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/settings", permission: "manage_settings" }),
      Object.freeze({ method: "PATCH", path: "/settings", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/storage/archive-roots", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/storage/archive-roots/discovery", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/archive-roots", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/archive-roots/{root_id}/activate", permission: "manage_settings" }),
      Object.freeze({ method: "DELETE", path: "/storage/archive-roots/{root_id}", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/migration/preview", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/storage/migration/plans/{plan_id}", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/storage/migration/plans/{plan_id}/items", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/migration/plans/{plan_id}/cancel", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/migration/apply", permission: "manage_settings+delete_recordings" }),
      Object.freeze({ method: "GET", path: "/storage/migration/operations/active", permission: "manage_settings" }),
      Object.freeze({ method: "GET", path: "/storage/migration/operations/{operation_id}", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/migration/operations/{operation_id}/cancel", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/migration/operations/{operation_id}/retry", permission: "manage_settings+delete_recordings" }),
      Object.freeze({ method: "POST", path: "/storage/migration/operations/{operation_id}/cleanup-takeover", permission: "manage_settings+delete_recordings" }),
      Object.freeze({ method: "GET", path: "/storage/reconciliation/summary", permission: "run_diagnostics" }),
      Object.freeze({ method: "POST", path: "/storage/reconcile", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/integrity/scans", permission: "run_diagnostics" }),
      Object.freeze({ method: "GET", path: "/storage/integrity/scans/latest", permission: "run_diagnostics" }),
      Object.freeze({ method: "GET", path: "/storage/integrity/scans/{scan_id}", permission: "run_diagnostics" }),
      Object.freeze({ method: "POST", path: "/storage/integrity/scans/{scan_id}/cancel", permission: "run_diagnostics" }),
      Object.freeze({ method: "GET", path: "/storage/integrity/scans/{scan_id}/findings", permission: "run_diagnostics" }),
      Object.freeze({ method: "POST", path: "/storage/integrity/findings/{finding_id}/metadata-plan", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/integrity/findings/{finding_id}/deletion-plan", permission: "delete_recordings" }),
      Object.freeze({ method: "GET", path: "/storage/integrity/remediation-plans/{plan_id}", permission: "run_diagnostics" }),
      Object.freeze({ method: "POST", path: "/storage/integrity/remediation-plans/{plan_id}/apply-metadata", permission: "manage_settings" }),
      Object.freeze({ method: "POST", path: "/storage/integrity/remediation-plans/{plan_id}/apply-deletion", permission: "delete_recordings" }),
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
