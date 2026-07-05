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
  updateCurrent: "Current",
  updateCommitPending: "Pending",
  updateCommitUnavailable: "No data",
  updateCommitVerified: "Commit verified",
  updateApplyStepDone: "Done",
  updateApplyTimelineCurrent: "Current version",
  updateApplyHeadlines: {
    current: "System is current",
    available: "Update available",
    running: "Update is running",
    completed: "Completed successfully",
    blocked: "Attention required",
  },
  updateApplySummaries: {
    current: "Installed version matches the published release.",
    available: "Review the release.",
    running: "Running.",
    completed: "Installed and verified.",
    blocked: "Blocked.",
  },
  updateApplyReleaseTitleFallback: "Published release",
  updateApplyReleaseSummaryFallback: "Release notes are not localized.",
  updateApplyRecoveryUnknown: "Unknown",
  updateApplyRecoveryRunning: "Running",
  updateApplyRecoveryProvider: "Provider-specific recovery",
  updateApplyRecoveryIdentity: "Identity-specific recovery",
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
    releaseIdentity: "Release identity",
    provider: "Provider",
    verification: "Commit check",
    elapsed: "Elapsed",
  },
  maintenanceStatuses: {
    current: "Current",
    update_available: "Update available",
    completed: "Completed",
    failed: "Failed",
    pending: "Pending",
    request: "Request",
    preflight: "Preflight",
    applying: "Updating",
    health_check: "Testing",
    commit_verification: "Commit check",
    unknown: "Unknown",
  },
};

assert.equal(settingsPage.includes("settingsUpdateApplyHero"), true);
assert.equal(settingsPage.includes("settingsUpdateApplySummaryGrid"), true);
assert.equal(settingsPage.includes("settingsUpdateApplySupport"), true);
assert.equal(settingsPage.includes("settingsMaintenanceModalHeader"), true);
assert.equal(settingsPage.includes("settingsMaintenanceBackupManager"), true);
assert.equal(settingsPage.includes("settingsMaintenanceBackupCreate"), false);
assert.equal(settingsPage.includes("<details className=\"settingsUpdateApplyTechnical\">"), false);
assert.equal(settingsPage.includes("updateApplyOperator.showApplyButton"), true);
assert.equal(settingsPage.includes("raw JSON"), false);
assert.equal(settingsPage.includes("helper logs"), false);
assert.equal(settingsPage.includes('name="token"'), false);
assert.equal(settingsPage.includes('name="url"'), false);
assert.equal(settingsPage.includes('name="repo"'), false);

for (const label of ["updateApplyHeadlines", "updateApplySummaries", "updateApplyHistoryLimited", "updateApplySupportTitle", "updateApplySupportAction"]) {
  assert.equal(settingsPage.includes(label), true);
}

assert.equal(css.includes(".settingsUpdateApplyHero"), true);
assert.equal(css.includes(".settingsUpdateApplyTimelineDot"), true);
assert.equal(css.includes(".settingsUpdateApplyTimeline li.is-idle"), true);
assert.equal(css.includes(".settingsMaintenanceModalHeader"), true);
assert.equal(css.includes(".settingsMaintenanceBackupManager"), true);
assert.equal(css.includes("settingsUpdateApplyHero.is-blocked"), true);
assert.equal(css.includes("grid-template-columns: minmax(0, 1.75fr) minmax(150px, 0.75fr)"), true);
assert.equal(css.includes("grid-template-columns: max-content max-content"), true);
assert.equal(css.includes(".settingsUpdateApplyVersionRows div"), true);
assert.equal(css.includes("display: contents"), true);
assert.equal(css.includes("white-space: nowrap"), true);
assert.equal(cssRule(".settingsUpdateApplyVersionRows dd").includes("justify-self: end"), false);
assert.equal(css.includes(".settingsUpdateApplySupport"), true);
assert.equal(css.includes("overflow-wrap: anywhere"), true);

const current = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: {
      version: "0.7.4",
      title: "Stage 6.2.1 Targeted Timeout Progress Step Dofix",
      summary: "Public update foundation and reliability hardening.",
      commit: "a".repeat(40),
      installed_at: "2026-07-02T05:02:09Z",
      metadata_status: "adopted",
    },
    available_release: {
      version: "0.7.4",
      title: "Stage 6.2.1 Targeted Timeout Progress Step Dofix",
      summary: "Public update foundation and reliability hardening.",
      commit: "a".repeat(40),
    },
  },
  { status: "idle", apply_history: { state: "missing" } },
  t,
  "en",
);

assert.equal(current.headline, "System is current");
assert.equal(current.severity, "ok");
assert.equal(current.canApply, false);
assert.equal(current.showApplyButton, false);
assert.equal(current.commitStatus, "Commit verified");
assert.equal(current.timeline.length, 5);
assert.equal(current.timeline.every((step) => step.status === "idle" && step.timeLabel === ""), true);
assert.equal(current.detailUnavailable, true);

const available = updateApplyOperatorModel(
  {
    status: "update_available",
    can_apply_from_ui: true,
    comparison: { status: "update_available" },
    installed_release: { version: "0.7.3", commit: "a".repeat(40) },
    available_release: {
      version: "0.7.4",
      title: "Long release title that should wrap inside the panel without clipping or raw hash noise",
      summary: "Operator UX redesign.",
      commit: "b".repeat(40),
    },
  },
  { status: "idle" },
  t,
  "en",
);

assert.equal(available.headline, "Update available");
assert.equal(available.severity, "warning");
assert.equal(available.canApply, true);
assert.equal(available.showApplyButton, true);
assert.equal(available.targetCommitShort, "bbbbbbbbbbbb...");
assert.equal(available.timeline.every((step) => step.status === "idle"), true);

const providerUnavailable = updateApplyOperatorModel(
  {
    status: "provider_unavailable",
    can_apply_from_ui: false,
    comparison: { status: "provider_unavailable" },
    installed_release: { version: "0.7.6", commit: "a".repeat(40) },
    available_release: { version: "0.7.6", commit: "a".repeat(40) },
  },
  { status: "idle" },
  t,
  "en",
);

assert.equal(providerUnavailable.headline, "Attention required");
assert.equal(providerUnavailable.severity, "blocked");
assert.equal(providerUnavailable.summary, "Provider-specific recovery");
assert.notEqual(providerUnavailable.summary, t.updateApplySummaries.blocked);

const completedWithSteps = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: { version: "0.7.4", commit: "a".repeat(40) },
    available_release: { version: "0.7.4", commit: "a".repeat(40) },
  },
  {
    status: "completed",
    expected_commit: "a".repeat(40),
    installed_commit: "a".repeat(40),
    commit_verified: true,
    steps: [
      { name: "request", status: "completed" },
      { name: "preflight", status: "completed" },
      { name: "applying", status: "completed" },
      { name: "health_check", status: "completed" },
      { name: "commit_verification", status: "completed" },
    ],
  },
  t,
  "en",
);

assert.equal(completedWithSteps.headline, "Completed successfully");
assert.equal(completedWithSteps.timeline.every((step) => step.status === "idle" && step.timeLabel === ""), true);

const ruFallback = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: { version: "0.7.4", commit: "a".repeat(40) },
    available_release: {
      version: "0.7.4",
      title: "Stage 6.3.0 Settings Maintenance",
      summary: "English-only release notes.",
      commit: "a".repeat(40),
    },
  },
  { status: "idle" },
  t,
  "ru",
);

assert.equal(ruFallback.releaseTitle, "Published release");
assert.equal(ruFallback.releaseSummary, "Release notes are not localized.");
