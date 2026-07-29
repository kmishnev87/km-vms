import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");
const settingsHelpers = fs.readFileSync(resolve(webRoot, "lib/settingsPageHelpers.js"), "utf8");
const css = fs.readFileSync(resolve(webRoot, "app/styles/20-settings-maintenance.css"), "utf8");

assert.equal(settingsPage.includes("settingsUpdateApplyPanel"), true);
assert.equal(settingsPage.includes("settingsMaintenanceReadiness"), true);
assert.equal(settingsPage.includes("settingsMaintenanceBackupManager"), true);
assert.equal(settingsPage.includes("settingsMaintenanceSupport"), true);
assert.equal(settingsPage.includes("settingsMaintenanceModalHeader"), true);
assert.equal(settingsPage.includes("settingsMaintenanceBackupCreate"), false);
assert.equal(settingsPage.includes("maintenanceBackupScope"), true);
assert.equal(settingsPage.includes("maintenanceReadinessRows(maintenanceOverview, t)"), true);
assert.equal(settingsPage.includes("maintenanceBackupManagerModel(maintenanceOverview, t, lang, maintenanceBackupStatus)"), true);
assert.equal(settingsPage.includes("maintenanceFlowRows(maintenanceOverview).map"), false);
assert.equal(settingsPage.includes("maintenanceDetailRows(flow, t)"), false);

const updateApplyIndex = settingsPage.indexOf('className="settingsUpdateApplyPanel"');
const readinessIndex = settingsPage.indexOf('className="settingsMaintenanceReadiness"');
const backupManagerIndex = settingsPage.indexOf('className="settingsMaintenanceBackupManager"');
const supportIndex = settingsPage.indexOf('className="settingsMaintenanceSupport"');
assert.ok(updateApplyIndex !== -1 && readinessIndex !== -1 && backupManagerIndex !== -1 && supportIndex !== -1, "maintenance sections not found");
assert.ok(updateApplyIndex < readinessIndex, "update apply section must be first in DOM order");
assert.ok(readinessIndex < backupManagerIndex, "readiness section must be before backup manager");
assert.ok(backupManagerIndex < supportIndex, "backup manager must be before support diagnostics");

assert.equal(settingsPage.includes('maintenanceBackupsTitle: "Резервные копии"'), true);
assert.equal(settingsPage.includes('maintenanceBackupsTitle: "Backups"'), true);
assert.equal(settingsPage.includes('maintenanceBackupsTitle: "备份"'), true);

for (const key of [
  "maintenanceReadinessTitle",
  "maintenanceSupportTitle",
  "maintenanceOperatorSummaries",
  "maintenanceOperatorActions",
  "maintenanceCheckActions",
  "maintenanceFactLabels",
]) {
  assert.equal(settingsPage.includes(key), true, `${key} missing`);
}

assert.equal(settingsHelpers.includes("export function maintenanceReadinessRows"), true);
assert.equal(settingsHelpers.includes("maintenanceOperatorSummaries"), true);
assert.equal(settingsHelpers.includes("maintenanceFactLabels"), true);
assert.equal(settingsHelpers.includes("showCheck: Boolean(presentation.can_check && userStatus !== \"ok\" && !canApply)"), true);
assert.equal(settingsHelpers.includes("showApply: canApply"), true);

assert.equal(css.includes(".settingsMaintenanceReadiness"), true);
assert.equal(css.includes(".settingsMaintenanceReadinessItem"), true);
assert.equal(css.includes(".settingsMaintenanceBackupManager"), true);
assert.equal(css.includes(".settingsMaintenanceSupport"), true);
assert.equal(css.includes(".settingsMaintenanceModalHeader"), true);
assert.equal(css.includes("position: sticky"), true);
assert.equal(css.includes(".settingsUpdateApplyPanel {\n  order: 0;"), true);
assert.equal(css.includes(".settingsMaintenanceReadiness {\n  order: 1;"), true);
assert.equal(css.includes(".settingsMaintenanceBackupManager {\n  order: 2;"), true);
assert.equal(css.includes(".settingsMaintenanceSupport {\n  order: 3;"), true);

for (const forbidden of [
  "current_product_restore_not_enabled</",
  "temporary_validation_target</",
  "drift_known_safe</h",
]) {
  assert.equal(settingsPage.includes(forbidden), false, `${forbidden} should not be visible in main UI`);
}
