import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  updateApplyOperatorModel,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");
const css = fs.readFileSync(resolve(webRoot, "app/styles/20-settings-maintenance.css"), "utf8");

function cssRule(selector) {
  const start = css.indexOf(`${selector} {`);
  assert.notEqual(start, -1, `${selector} rule not found`);
  const end = css.indexOf("\n}", start);
  assert.notEqual(end, -1, `${selector} rule is not closed`);
  return css.slice(start, end + 2);
}

const t = {
  updateCommitPending: "Pending",
  updateCommitUnavailable: "No data",
  updateCommitVerified: "Commit verified",
  updateApplyStepDone: "Done",
  updateApplyTimelineCurrent: "Current version",
  updateApplyHeadlines: {
    blocked: "Attention required",
    current: "System is current",
    available: "Update available",
    running: "Update is running",
    completed: "Completed successfully",
  },
  updateApplySummaries: {
    blocked: "Generic blocked summary",
    current: "Installed version matches the published release.",
  },
  updateApplyRecoveryCurrent: "Current recovery",
  updateApplyRecoveryFailed: "Failed recovery",
  updateApplyRecoveryCheckFailed: "Check failed recovery",
  updateApplyRecoveryStalled: "Stalled recovery",
  updateApplyRecoveryProvider: "Provider recovery",
  updateApplyRecoveryBlocked: "Blocked recovery",
  updateApplyRecoveryUnknown: "Unknown recovery",
  updateApplyRecoveryRunning: "Running recovery",
  updateApplyRecoveryReconnecting: "Reconnecting recovery",
  updateApplyRecoveryIdentity: "Identity recovery",
  updateApplyRecoveryInstalledNewer: "Installed-newer recovery",
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
    current: "Current",
    update_available: "Update available",
    failed: "Failed",
    stalled: "Stalled",
    check_failed: "Check failed",
    provider_unavailable: "Provider unavailable",
    pending: "Pending",
    request: "Request",
    preflight: "Preflight",
    applying: "Updating",
    health_check: "Testing",
    commit_verification: "Commit check",
    unknown: "Unknown",
  },
};

function currentUpdateStatus(extra = {}) {
  return {
    status: "current",
    comparison: { status: "current" },
    can_apply_from_ui: false,
    installed_release: { version: "0.7.6", commit: "a".repeat(40) },
    available_release: { version: "0.7.6", commit: "a".repeat(40) },
    ...extra,
  };
}

const failedCurrent = updateApplyOperatorModel(
  currentUpdateStatus(),
  { status: "failed", error: { operator_action: "Do not use current copy" } },
  t,
  "en",
);

assert.equal(failedCurrent.headline, "Attention required");
assert.equal(failedCurrent.severity, "blocked");
assert.equal(failedCurrent.summary, "Failed recovery");
assert.notEqual(failedCurrent.summary, "Current recovery");
assert.notEqual(failedCurrent.summary, "Installed version matches the published release.");

const stalledCurrent = updateApplyOperatorModel(
  currentUpdateStatus(),
  { status: "stalled", error: { operator_action: "Stalled recovery" } },
  t,
  "en",
);

assert.equal(stalledCurrent.headline, "Attention required");
assert.equal(stalledCurrent.severity, "blocked");
assert.equal(stalledCurrent.summary, "Stalled recovery");
assert.notEqual(stalledCurrent.summary, "Current recovery");

const checkFailedCurrent = updateApplyOperatorModel(
  currentUpdateStatus({ status: "check_failed" }),
  { status: "idle" },
  t,
  "en",
);

assert.equal(checkFailedCurrent.headline, "Attention required");
assert.equal(checkFailedCurrent.severity, "blocked");
assert.equal(checkFailedCurrent.summary, "Check failed recovery");
assert.notEqual(checkFailedCurrent.summary, "Current recovery");
assert.notEqual(checkFailedCurrent.summary, "Unknown recovery");

const providerUnavailable = updateApplyOperatorModel(
  {
    status: "provider_unavailable",
    comparison: { status: "provider_unavailable" },
    can_apply_from_ui: false,
    installed_release: { version: "0.7.6", commit: "a".repeat(40) },
    available_release: { version: "0.7.6", commit: "a".repeat(40) },
  },
  { status: "idle" },
  t,
  "en",
);

assert.equal(providerUnavailable.headline, "Attention required");
assert.equal(providerUnavailable.summary, "Provider recovery");
assert.notEqual(providerUnavailable.summary, "Generic blocked summary");

const supportStart = settingsPage.indexOf('<section className="settingsMaintenanceSupport">');
assert.notEqual(supportStart, -1, "support diagnostics section missing");
const supportEnd = settingsPage.indexOf("{maintenanceWarningsOpen", supportStart);
const supportMarkup = settingsPage.slice(supportStart, supportEnd);
const supportButtonCount = (supportMarkup.match(/<button /g) || []).length;

assert.equal(supportButtonCount, 2);
assert.equal(supportMarkup.includes("downloadMaintenanceReport"), false);
assert.equal(supportMarkup.includes("setDiagnosticChoiceOpen(true)"), true);
assert.equal(supportMarkup.includes("viewMaintenanceReport"), false);
assert.equal(settingsPage.includes('id: "diagnostic-archive-choice"'), true);
assert.equal(settingsPage.includes('overlayClassName: "settingsDiagnosticDialogOverlay"'), true);
assert.equal(settingsPage.includes('diagnosticArchiveQuestion: "Выберите диагностический архив"'), true);
const diagnosticDialogStart = settingsPage.indexOf('id: "diagnostic-archive-choice"');
const diagnosticDialogEnd = settingsPage.indexOf("onClose={() => setDiagnosticChoiceOpen(false)}", diagnosticDialogStart);
const diagnosticDialog = settingsPage.slice(diagnosticDialogStart, diagnosticDialogEnd);
assert.equal(diagnosticDialog.includes("descriptions: ["), true);
assert.equal(diagnosticDialog.includes("summary:"), false);
assert.equal(diagnosticDialog.includes("showFooterClose: false"), true);
assert.match(cssRule(".settingsMaintenanceSupportActions"), /width:\s*auto;/);
assert.match(cssRule(".settingsMaintenanceSupportActions .button"), /white-space:\s*nowrap;/);
assert.equal(settingsPage.includes('downloadLogArchive("normal")'), true);
assert.equal(settingsPage.includes('downloadLogArchive("extended")'), true);
assert.equal(settingsPage.includes("/system/upgrade/report"), false);
assert.equal(css.includes(".settingsDiagnosticChoice"), false);
assert.match(cssRule(".operationFeedbackOverlay.settingsDiagnosticDialogOverlay"), /z-index: 9600/);
assert.equal(settingsPage.includes("maintenanceReportView"), false);
assert.equal(settingsPage.includes("Открыть отчёт"), false);
assert.equal(settingsPage.includes("Open report"), false);
assert.equal(settingsPage.includes("打开报告"), false);
assert.equal(settingsPage.includes("Показать детали"), true);
assert.equal(settingsPage.includes("Скрыть детали"), true);
assert.equal(settingsPage.includes("санитизирован"), false);
assert.equal(settingsPage.includes("Sanitized"), false);
assert.equal(settingsPage.includes("sanitized"), false);
assert.equal(settingsPage.includes("脱敏"), false);

assert.match(cssRule(".settingsMaintenanceSupportActions"), /display: grid/);
assert.match(cssRule(".settingsMaintenanceSupportActions"), /grid-template-columns: max-content/);
assert.match(cssRule(".settingsMaintenanceSupportActions"), /width: auto/);
assert.match(cssRule(".settingsMaintenanceSupportActions"), /min-width: 0/);
assert.match(cssRule(".settingsMaintenanceSupportActions .button"), /width: auto/);
assert.match(cssRule(".settingsMaintenanceWarningsList"), /position: absolute/);
assert.match(cssRule(".settingsMaintenanceWarningsList"), /max-height: min\(42vh, 340px\)/);
