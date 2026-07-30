import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");
const settingsHelpers = fs.readFileSync(resolve(webRoot, "lib/settingsPageHelpers.js"), "utf8");
const operationFeedback = fs.readFileSync(resolve(webRoot, "components/OperationFeedback.js"), "utf8");
const css = fs.readFileSync(resolve(webRoot, "app/styles/20-settings-maintenance.css"), "utf8");

assert.equal(settingsPage.includes("settingsUpdateApplyPanel"), true);
assert.equal(settingsPage.includes("settingsMaintenanceOverall"), true);
assert.equal(settingsPage.includes("settingsMaintenanceCoreGrid"), true);
assert.equal(settingsPage.includes("settingsMaintenanceBackupManager"), true);
assert.equal(settingsPage.includes("settingsMaintenanceSupport"), true);
assert.equal(settingsPage.includes("settingsMaintenanceModalHeader"), true);
assert.equal(settingsPage.includes('className="settingsMaintenanceBackupCreate"'), false);
assert.equal(settingsPage.includes("maintenanceBackupScope"), true);
assert.equal(settingsPage.includes("maintenanceDatabaseOverviewModel(maintenanceOverview, t)"), true);
assert.equal(settingsPage.includes("maintenanceBackupOverviewModel(maintenanceOverview, t, lang)"), true);
assert.equal(settingsPage.includes("maintenanceBackupDetailModel(maintenanceBackupDetail, t, lang)"), true);
assert.equal(settingsPage.includes("maintenanceFlowRows(maintenanceOverview).map"), false);
assert.equal(settingsPage.includes("maintenanceDetailRows(flow, t)"), false);
assert.match(
  settingsPage,
  /maintenanceChildDialogOpen = Boolean\([\s\S]{0,180}maintenanceConfirm[\s\S]{0,180}currentRestoreDialog[\s\S]{0,180}updateApplyDialog[\s\S]{0,180}diagnosticChoiceOpen/,
);
assert.equal(settingsPage.includes("if (event.defaultPrevented || maintenanceChildDialogOpenRef.current) return;"), true);
assert.equal(settingsPage.includes('aria-hidden={maintenanceChildDialogOpen ? "true" : undefined}'), true);
assert.equal(settingsPage.includes("inert={maintenanceChildDialogOpen ? true : undefined}"), true);
assert.match(operationFeedback, /event\.key === "Escape"[\s\S]{0,100}event\.stopPropagation\(\)/);

const updateApplyIndex = settingsPage.indexOf('className="settingsUpdateApplyPanel"');
const coreGridIndex = settingsPage.indexOf('className="settingsMaintenanceCoreGrid"');
const backupManagerIndex = settingsPage.indexOf("settingsMaintenanceBackupManager settingsMaintenanceCoreCard");
const supportIndex = settingsPage.indexOf('className="settingsMaintenanceSupport"');
assert.ok(updateApplyIndex !== -1 && coreGridIndex !== -1 && backupManagerIndex !== -1 && supportIndex !== -1, "maintenance sections not found");
assert.ok(updateApplyIndex < coreGridIndex, "update apply section must be first in DOM order");
assert.ok(coreGridIndex < backupManagerIndex, "database card must be before backup manager");
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
assert.equal(css.includes(".settingsMaintenanceOverall"), true);
assert.equal(css.includes(".settingsMaintenanceCoreGrid"), true);
assert.equal(css.includes(".settingsMaintenanceBackupManager"), true);
assert.equal(css.includes(".settingsMaintenanceSupport"), true);
assert.equal(css.includes(".settingsMaintenanceModalHeader"), true);
assert.equal(css.includes("position: sticky"), true);
assert.equal(css.includes("/* Stage 13.7.11: final maintenance overview composition. */"), true);
assert.equal(css.includes("grid-template-columns: repeat(2, minmax(0, 1fr));"), true);

for (const forbidden of [
  "current_product_restore_not_enabled</",
  "temporary_validation_target</",
  "drift_known_safe</h",
]) {
  assert.equal(settingsPage.includes(forbidden), false, `${forbidden} should not be visible in main UI`);
}
