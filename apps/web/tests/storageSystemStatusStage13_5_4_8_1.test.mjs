import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const warningSource = read("lib/operatorWarnings.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const warningContext = {};
vm.runInNewContext(
  `${warningSource}
this.systemHealthIndicatorModel = systemHealthIndicatorModel;
this.runtimeStatusUserIdentity = runtimeStatusUserIdentity;`,
  warningContext
);

const owner = { id: 1, role: "owner", permissions: ["run_diagnostics", "manage_settings"] };
const admin = { id: 2, role: "admin", permissions: ["run_diagnostics", "manage_settings"] };
const operator = { id: 3, role: "operator", permissions: ["view_live", "manage_cameras"] };
const viewer = { id: 4, role: "viewer", permissions: ["view_live"] };
const healthySummary = { severity: "ok" };
const warningSummary = { severity: "warning" };

assert.deepEqual(
  JSON.parse(JSON.stringify(warningContext.systemHealthIndicatorModel({ user: null }))),
  { visible: false, canRead: false, state: "hidden", hasProblems: false }
);
for (const user of [owner, admin]) {
  assert.equal(warningContext.systemHealthIndicatorModel({ user }).state, "unknown");
  assert.equal(warningContext.systemHealthIndicatorModel({ user }).visible, true);
  assert.equal(
    warningContext.systemHealthIndicatorModel({ user, runtimeStatusKnown: true, summary: healthySummary }).state,
    "healthy"
  );
  assert.equal(
    warningContext.systemHealthIndicatorModel({ user, runtimeStatusKnown: true, summary: warningSummary }).state,
    "problem"
  );
  assert.equal(
    warningContext.systemHealthIndicatorModel({ user, permissionDenied: true }).visible,
    false
  );
}
for (const user of [operator, viewer]) {
  assert.equal(warningContext.systemHealthIndicatorModel({ user }).visible, false);
}
assert.equal(warningContext.runtimeStatusUserIdentity({ permissions: ["run_diagnostics"] }), "authorized-user");

const storageHelperSource = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const storageContext = {};
vm.runInNewContext(
  `${storageHelperSource}
this.discoveryHeaderStatusModel = discoveryHeaderStatusModel;`,
  storageContext
);

assert.equal(storageContext.discoveryHeaderStatusModel(null).state, "not_checked");
assert.equal(storageContext.discoveryHeaderStatusModel({ freshness: "refreshing" }).state, "refreshing");
assert.equal(
  storageContext.discoveryHeaderStatusModel({ freshness: "current", available: true, snapshot_id: "snapshot" }).state,
  "current"
);
assert.equal(storageContext.discoveryHeaderStatusModel({ freshness: "stale" }).state, "stale");
assert.equal(storageContext.discoveryHeaderStatusModel({ freshness: "unavailable" }).state, "unavailable");

const indicator = read("components/SystemHealthIndicator.js");
const layout = read("components/Layout.js");
const navCss = read("app/styles/00-base-layout-nav.css");
assert.doesNotMatch(indicator, /setAccessDenied\(!userCanReadRuntimeStatus/);
assert.match(indicator, /systemHealthIndicatorModel/);
assert.match(indicator, /data-health-state=\{status\.state\}/);
assert.match(indicator, /aria-busy=\{status\.loading \|\| undefined\}/);
assert.match(navCss, /\.systemHealthNavItem-unknown/);
assert.equal(layout.indexOf('href="/storage"') < layout.indexOf("<SystemHealthIndicator"), true);
assert.equal(layout.indexOf("<SystemHealthIndicator") < layout.indexOf('href="/settings"'), true);

const storagePage = read("app/storage/page.js");
const storageCss = read("app/styles/40-storage-records-shared.css");
const responsiveCss = read("app/styles/60-responsive-shared.css");
const i18n = read("lib/i18n.js");

assert.doesNotMatch(storagePage, /storageOpsArchiveSummary|accessRightsSummary|copy\.archiveRootLocation|copy\.accessRights/);
assert.doesNotMatch(storageCss, /storageOpsArchiveSummary/);
assert.doesNotMatch(responsiveCss, /storageOpsArchiveSummary/);
for (const retained of ["archivePathText", "accessRightsModel", "pathHealth", "storageContract", "archiveRootPath"]) {
  assert.match(storagePage, new RegExp(`\\b${retained}\\b`), `${retained} remains an authoritative consumer`);
}

const formStart = storagePage.indexOf('<div className="storageOpsRootForm storageOpsRootForm-product">');
const feedbackStart = storagePage.indexOf('<div className="storageOpsDiscoveryFeedback">', formStart);
assert.ok(formStart >= 0 && feedbackStart > formStart);
const controlsGrid = storagePage.slice(formStart, feedbackStart);
assert.doesNotMatch(controlsGrid, /discoveryRefreshing|discoveryStale|discoveryUnavailableCurrent|refreshDiscovery/);
assert.match(controlsGrid, /storageOpsRootAddButton/);
assert.doesNotMatch(storagePage, /copy\.discoveryRefreshing|copy\.discoveryStale|copy\.discoveryUnavailableCurrent/);
const headerStatusPosition = storagePage.indexOf("storageOpsDiscoveryStatus", Math.max(0, formStart - 1000));
assert.ok(headerStatusPosition >= 0 && headerStatusPosition < formStart);
assert.match(storageCss, /\.storageOpsAdvancedRoot \.storageOpsRootForm-product\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) minmax\(0, 1fr\) auto;/s);
assert.match(storageCss, /\.storageOpsDiscoveryStatus\s*\{/);
assert.match(storageCss, /\.storageOpsVolumeGroup \.storageOpsCapacityHeader > div > span\s*\{[^}]*font-size:\s*12px;[^}]*line-height:\s*1\.25;/s);
assert.match(storageCss, /\.storageOpsVolumeGroups\s*\{[^}]*gap:\s*10px;[^}]*margin-top:\s*10px;[^}]*margin-bottom:\s*10px;/s);
assert.doesNotMatch(storageCss, /storageOpsVolumeGroup[^\n{]*:nth-/);

for (const key of [
  "discoveryStatus_not_checked",
  "discoveryStatus_refreshing",
  "discoveryStatus_current",
  "discoveryStatus_stale",
  "discoveryStatus_unavailable",
]) {
  assert.equal((i18n.match(new RegExp(`${key}:`, "g")) || []).length, 3, `${key} must exist in all locales`);
}
for (const removed of ["archiveRootLocation:", "accessValueOk:", "accessValueNone:", "accessValueUnknown:"]) {
  assert.equal(i18n.includes(removed), false, `${removed} must not remain after row cleanup`);
}
