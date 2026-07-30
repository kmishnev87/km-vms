import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  maintenanceBackupOperationResultText,
  updateApplyOperatorModel,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");
const css = fs.readFileSync(resolve(webRoot, "app/styles/20-settings-maintenance.css"), "utf8");
const stageCss = css.slice(css.indexOf("/* Stage 13.7.11:"));

function cssRule(selector) {
  const start = stageCss.indexOf(`${selector} {`);
  assert.notEqual(start, -1, `${selector} rule not found`);
  const end = stageCss.indexOf("\n}", start);
  assert.notEqual(end, -1, `${selector} rule is not closed`);
  return stageCss.slice(start, end + 2);
}

const copy = {
  updateCommitPending: "Pending",
  updateCommitUnavailable: "No data",
  updateCommitVerified: "Commit verified",
  updateApplyStepDone: "Done",
  updateApplyTimelineCurrent: "Current version",
  updateApplyHeadlines: { blocked: "Attention required", current: "Current", available: "Available", running: "Running", completed: "Completed" },
  updateApplySummaries: { blocked: "Generic blocked summary" },
  updateApplyRecoveryProvider: "The public release descriptor is unavailable.",
  updateApplyRecoveryIdentity: "Release identity metadata is incomplete.",
  updateApplyRecoveryInstalledNewer: "Installed version is newer than available.",
  updateApplyRecoveryUnknown: "Unknown",
  maintenanceLabels: {
    current: "Current version",
    available: "Available version",
    releaseTitle: "Release",
    releaseSummary: "Changes",
    status: "Status",
    source: "Source",
    installedCommit: "Installed commit",
    targetCommit: "Target commit",
    gitHead: "Git HEAD",
    metadataSource: "Metadata",
    provider: "Provider",
    verification: "Commit check",
    elapsed: "Elapsed",
  },
  maintenanceStatuses: {
    provider_unavailable: "Source unavailable",
    current: "Current",
    pending: "Pending",
    request: "Request",
    preflight: "Preflight",
    applying: "Updating",
    health_check: "Testing",
    commit_verification: "Commit check",
    unknown: "Unknown",
  },
  maintenanceBackupCheck: "Check",
  maintenanceBackupCreate: "Create",
  maintenanceBackupDelete: "Delete",
  maintenanceBackupCreated: "Backup was created",
  maintenanceBackupDeleted: "Backup was deleted",
  maintenanceBackupCreateFailed: "Backup create failed",
  maintenanceBackupDeleteFailed: "Backup delete failed",
  maintenanceMessageFallback: "Fallback",
  maintenanceBackupOperationLabels: { check: "Check", create: "Create", delete: "Delete" },
  maintenanceBackupCheckStatuses: { valid: "Check passed", fallback: "Check status received" },
  maintenanceBackupCreateStatuses: { verified: "Backup was created", fallback: "Create status received" },
  maintenanceBackupDeleteStatuses: {
    deleted: "Backup was deleted",
    deleted_with_missing_files: "Backup was deleted; some files were already missing",
    fallback: "Delete status received",
  },
};

assert.equal(settingsPage.includes("settingsUpdateApplyHero"), false);
assert.equal(settingsPage.includes("settingsMaintenanceOverall"), true);
assert.equal(settingsPage.includes("settingsUpdateApplyCompact"), true);
assert.equal(settingsPage.includes("settingsMaintenanceCoreGrid"), true);
assert.equal(settingsPage.includes("settingsMaintenanceBackupDetail"), true);
assert.match(cssRule(".settingsMaintenanceModal"), /width:\s*min\(980px,\s*calc\(100vw - 32px\)\)/);
assert.match(cssRule(".settingsMaintenanceCoreGrid"), /grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
assert.match(cssRule(".settingsUpdateApplyCompact"), /grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto/);
assert.equal(settingsPage.includes("settingsMaintenanceSupportStatus"), false);
assert.equal(settingsPage.includes("maintenanceSupportStatusOk"), true);
assert.equal(settingsPage.includes("<dt>{t.maintenanceWarningActionable}</dt>"), false);
const supportStart = settingsPage.indexOf('<section className="settingsMaintenanceSupport">');
const supportEnd = settingsPage.indexOf("</section>", supportStart);
const supportActions = settingsPage.slice(supportStart, supportEnd);
assert.equal((supportActions.match(/settingsMaintenanceSupportActionButton/g) || []).length, 1);
assert.equal(supportActions.includes("setDiagnosticChoiceOpen(true)"), true);
assert.equal(supportActions.includes("setMaintenanceWarningsOpen"), false);

const providerBlocked = updateApplyOperatorModel(
  {
    status: "provider_unavailable",
    comparison: { status: "provider_unavailable" },
    can_apply_from_ui: false,
    installed_release: { version: "0.7.6", commit: "a".repeat(40) },
    available_release: { version: "0.7.6", commit: "a".repeat(40) },
  },
  { status: "idle" },
  copy,
  "en",
);
assert.equal(providerBlocked.summary, "The public release descriptor is unavailable.");
assert.notEqual(providerBlocked.summary, "Generic blocked summary");

assert.deepEqual(maintenanceBackupOperationResultText({ kind: "check", status: "valid" }, copy), {
  kind: "check",
  label: "Check",
  text: "Check passed",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({ kind: "create", status: "verified" }, copy), {
  kind: "create",
  label: "Create",
  text: "Backup was created",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({ kind: "delete", status: "deleted_with_missing_files" }, copy), {
  kind: "delete",
  label: "Delete",
  text: "Backup was deleted; some files were already missing",
  showReason: false,
});
