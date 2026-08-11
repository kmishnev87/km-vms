import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import * as storageOperations from "../lib/storageOperations.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const routePermissionsSource = fs.readFileSync(resolve(__dirname, "../lib/routePermissions.js"), "utf8");
const storagePage = fs.readFileSync(resolve(__dirname, "../app/storage/page.js"), "utf8");
const storageContext = storageOperations;

const routeContext = {};
vm.runInNewContext(
  `${routePermissionsSource
    .replaceAll("export const ", "const ")
    .replaceAll("export function ", "function ")}
this.FRONTEND_ROUTE_ACCESS = FRONTEND_ROUTE_ACCESS;`,
  routeContext
);

assert.equal(storageContext.STORAGE_ACTION_PERMISSIONS.retentionPreview, "delete_recordings");
assert.equal(storageContext.STORAGE_ACTION_PERMISSIONS.retentionApply, "delete_recordings");
assert.equal(storageContext.STORAGE_ACTION_PERMISSIONS.reconciliationSummary, "run_diagnostics");
assert.equal(storageContext.STORAGE_ACTION_PERMISSIONS.reconciliationApply, "manage_settings");
assert.equal(storageContext.STORAGE_ACTION_PERMISSIONS.archiveRootActivate, "manage_settings");
assert.equal(storageContext.STORAGE_ACTION_PERMISSIONS.migrationApply, "manage_settings");
assert.equal(storageContext.STORAGE_ACTION_PERMISSIONS.autoFreeSpace, "manage_settings");

const denied = storageContext.actionPermissionState({ permissions: ["manage_settings"] }, "delete_recordings");
assert.equal(denied.allowed, false);
assert.match(denied.reason, /Недостаточно прав для удаления записей/);

const endpoints = routeContext.FRONTEND_ROUTE_ACCESS["/storage"].backendEndpoints;
for (const expected of [
  ["GET", "/storage/status"],
  ["GET", "/settings"],
  ["PATCH", "/settings"],
  ["GET", "/storage/archive-roots"],
  ["GET", "/storage/archive-roots/discovery"],
  ["POST", "/storage/archive-roots"],
  ["POST", "/storage/archive-roots/{root_id}/activate"],
  ["POST", "/storage/migration/preview"],
  ["POST", "/storage/migration/apply"],
  ["GET", "/storage/reconciliation/summary"],
  ["POST", "/storage/reconcile"],
  ["POST", "/recordings/retention/dry-run"],
  ["POST", "/recordings/retention/run"],
  ["POST", "/storage/integrity/scans"],
  ["GET", "/storage/integrity/scans/{scan_id}/findings"],
  ["POST", "/storage/integrity/findings/{finding_id}/metadata-plan"],
  ["POST", "/storage/integrity/findings/{finding_id}/deletion-plan"],
  ["POST", "/storage/integrity/remediation-plans/{plan_id}/apply-metadata"],
  ["POST", "/storage/integrity/remediation-plans/{plan_id}/apply-deletion"],
]) {
  assert.ok(
    endpoints.some((item) => item.method === expected[0] && item.path === expected[1]),
    `${expected[0]} ${expected[1]} must be listed for /storage`
  );
}

assert.match(storagePage, /const diagnosticsPermission = actionPermissionState\(currentUser, "run_diagnostics", language\)/, "integrity diagnostics permission is resolved before rendering");
assert.match(storagePage, /permission=\{diagnosticsPermission\}/, "integrity dialog receives the diagnostics permission gate");
assert.match(storagePage, /!retentionPermission\.allowed/, "destructive storage actions remain permission-gated before click");
assert.match(storagePage, /migrationScenario\.canApply/, "migration apply is permission-gated before click");
assert.match(storagePage, /manageSettingsPermission\.reason/, "manage-settings missing permission reason remains visible");
