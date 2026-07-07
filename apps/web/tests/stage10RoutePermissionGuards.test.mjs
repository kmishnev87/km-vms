import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const routePermissions = await import(pathToFileURL(resolve(root, "lib/routePermissions.js")));
const api = read("lib/api.js");
const layout = read("components/Layout.js");
const operatorBanners = read("components/OperatorProblemBanners.js");
const systemHealth = read("components/SystemHealthIndicator.js");
const apkPage = read("app/apk/page.js");

const entries = routePermissions.FRONTEND_ROUTE_ACCESS;

assert.equal(api.includes("canUserAccessRoute(user, href)"), true);
assert.equal(layout.includes("canAccessPath(currentUser, pathname)"), true);
assert.equal(operatorBanners.includes("canAccessPath(user, action.href)"), true);
assert.equal(systemHealth.includes("userCanReadRuntimeStatus(currentUser)"), true);
assert.equal(systemHealth.includes('apiFetch("/system/runtime/status")'), true);

for (const route of ["/system-status", "/security-journal", "/diagnostics", "/storage", "/apk"]) {
  assert.equal(route in entries, true, `${route} missing from frontend route access registry`);
}

assert.equal(entries["/system-status"].permission, "run_diagnostics");
assert.deepEqual(entries["/system-status"].backend, ["/system/runtime/status", "/system/recorder/status"]);
assert.equal(entries["/security-journal"].permission, "manage_settings");
assert.deepEqual(entries["/security-journal"].backend, ["/audit/events"]);
assert.equal(entries["/diagnostics"].permission, "run_diagnostics");
assert.equal(entries["/storage"].permission, "manage_settings");
for (const storagePath of [
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
]) {
  assert.equal(entries["/storage"].backend.includes(storagePath), true, `/storage backend metadata must include ${storagePath}`);
}
assert.equal(entries["/storage"].backendEndpoints.some((item) => item.method === "POST" && item.path === "/recordings/retention/dry-run" && item.permission === "delete_recordings"), true);
assert.equal(entries["/storage"].backendEndpoints.some((item) => item.method === "POST" && item.path === "/recordings/retention/run" && item.permission === "delete_recordings"), true);
assert.equal(entries["/storage"].backendEndpoints.some((item) => item.method === "GET" && item.path === "/storage/reconciliation/summary" && item.permission === "run_diagnostics"), true);
for (const [method, path] of [
  ["GET", "/storage/status"],
  ["GET", "/settings"],
  ["PATCH", "/settings"],
  ["POST", "/storage/reconcile"],
  ["POST", "/storage/archive-roots/validate"],
  ["POST", "/storage/archive-roots"],
  ["POST", "/storage/archive-roots/{root_id}/activate"],
  ["POST", "/storage/migration/preview"],
  ["POST", "/storage/migration/apply"],
]) {
  assert.equal(entries["/storage"].backendEndpoints.some((item) => item.method === method && item.path === path && item.permission === "manage_settings"), true, `${method} ${path} must require manage_settings`);
}
assert.equal(entries["/apk"].access, routePermissions.ROUTE_ACCESS_PUBLIC);
assert.equal(entries["/apk"].placeholderOnly, true);

for (const [route, entry] of Object.entries(entries)) {
  assert.equal(route.startsWith("/"), true, `${route} must be absolute`);
  if (entry.access === routePermissions.ROUTE_ACCESS_PUBLIC || entry.access === routePermissions.ROUTE_ACCESS_DENIED) {
    assert.equal(typeof entry.reason, "string", `${route} public/denied route needs a reason`);
    assert.notEqual(entry.reason.trim(), "", `${route} public/denied reason must be non-empty`);
  }
  if (entry.access === routePermissions.ROUTE_ACCESS_PERMISSION) {
    assert.equal(typeof entry.permission, "string", `${route} permission route needs a permission`);
    assert.notEqual(entry.permission.trim(), "", `${route} permission must be non-empty`);
    assert.equal(Array.isArray(entry.backend), true, `${route} permission route needs backend references`);
    assert.notEqual(entry.backend.length, 0, `${route} backend references must be non-empty`);
  }
}

const pageRoutes = fs
  .readdirSync(resolve(root, "app"), { withFileTypes: true })
  .filter((entry) => entry.isDirectory())
  .filter((entry) => fs.existsSync(resolve(root, "app", entry.name, "page.js")))
  .map((entry) => `/${entry.name}`)
  .sort();

for (const route of pageRoutes) {
  assert.equal(route in entries, true, `${route} page route must have explicit access metadata`);
}

const owner = {
  permissions: [
    "manage_cameras",
    "manage_settings",
    "run_diagnostics",
    "view_live",
    "view_recordings",
    "view_timeline",
  ],
};
const viewer = { permissions: ["view_live"] };
const operator = { permissions: ["view_live", "view_recordings", "view_timeline"] };
const anonymous = null;

assert.equal(routePermissions.canUserAccessRoute(owner, "/storage"), true);
assert.equal(routePermissions.canUserAccessRoute(viewer, "/storage"), false);
assert.equal(routePermissions.canUserAccessRoute(owner, "/system-status?tab=runtime"), true);
assert.equal(routePermissions.canUserAccessRoute(operator, "/system-status"), false);
assert.equal(routePermissions.canUserAccessRoute(owner, "/security-journal"), true);
assert.equal(routePermissions.canUserAccessRoute(operator, "/security-journal"), false);
assert.equal(routePermissions.canUserAccessRoute(owner, "/diagnostics"), true);
assert.equal(routePermissions.canUserAccessRoute(viewer, "/diagnostics"), false);
assert.equal(routePermissions.canUserAccessRoute(anonymous, "/apk"), true);
assert.equal(routePermissions.canUserAccessRoute(owner, "/apk"), true);
assert.equal(routePermissions.canUserAccessRoute(owner, "/unknown-stage-10-route"), false);
assert.equal(routePermissions.canUserAccessRoute(owner, "/timeline"), false);
assert.equal(routePermissions.canUserAccessRoute(owner, "/chronology2"), false);
assert.equal(routePermissions.canUserAccessRoute(viewer, "/timeline"), false);
assert.equal(routePermissions.canUserAccessRoute(viewer, "/chronology2"), false);

for (const forbidden of [
  "/apk/",
  "apk.zip",
  "application/vnd.android.package-archive",
  "download=",
  "href=\"#download\"",
  "apiFetch(\"/apk",
  "fetch(\"/api/apk",
]) {
  assert.equal(apkPage.includes(forbidden), false, `${forbidden} must stay absent from APK placeholder`);
}
