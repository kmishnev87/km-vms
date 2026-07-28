import assert from "node:assert/strict";
import fs from "node:fs";
import path, { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  maintenanceStatusClass,
  updateApplyButtonText,
  updateApplyErrorMessages,
  updateApplyEffectiveStatus,
  updateApplyFactRows,
  updateApplyRecoveryText,
  updateApplyStepRows,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const repoRoot = resolve(webRoot, "../..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");
const settingsHelpers = fs.readFileSync(resolve(webRoot, "lib/settingsPageHelpers.js"), "utf8");
const css = fs.readFileSync(resolve(webRoot, "app/styles/20-settings-maintenance.css"), "utf8");

function walkFiles(root) {
  const result = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) result.push(...walkFiles(full));
    else result.push(full);
  }
  return result;
}

const t = {
  updateApplyStart: "Start update",
  updateApplyButtonRunning: "Updating",
  updateApplyButtonRebuilding: "Rebuilding",
  updateApplyButtonHealth: "Health check",
  updateApplyButtonVerification: "Commit check",
  updateApplyRecoveryRunning: "Running",
  updateApplyRecoveryStalled: "Stalled action",
  updateApplyRecoveryUnknown: "Unknown",
  updateCommitPending: "Pending",
  updateCommitUnavailable: "Unavailable",
  updateCommitVerified: "Verified",
  maintenanceLabels: {
    current: "Current",
    available: "Available",
    releaseTitle: "Release",
    releaseSummary: "Changes",
    status: "Status",
    verification: "Commit check",
    currentStep: "Current step",
    lastProgress: "Last progress",
    elapsed: "Elapsed",
  },
  maintenanceStatuses: {
    update_available: "Update available",
    request: "Request",
    preflight: "Preflight",
    applying: "Updating",
    rebuilding: "Rebuilding",
    health_check: "Health check",
    commit_verification: "Commit check",
    running: "Running",
    pending: "Pending",
    completed: "Completed",
    failed: "Failed",
    stalled: "Stalled",
    unknown: "Unknown",
  },
};

const appRoutes = walkFiles(resolve(webRoot, "app"))
  .map((file) => path.relative(resolve(webRoot, "app"), file).replaceAll(path.sep, "/"));

assert.equal(appRoutes.some((file) => file === "update/page.js" || file.startsWith("update/")), false);
assert.equal(settingsPage.includes("settingsUpdateApplyTimeline"), true);
assert.equal(settingsPage.includes("updateApplyOperatorModel(updateStatus, updateApplyStatus, t, lang"), true);
assert.equal(settingsPage.includes("updateApplyButtonText(updateApplyStatus, t)"), true);
assert.equal(settingsPage.includes("updateApplyCandidateSnapshot(updateStatus)"), true);
assert.equal(settingsHelpers.includes("applyStatus?.is_stale"), true);
assert.equal(settingsPage.includes("updateApplyErrorMessages(updateApplyStatus?.error, t, lang)"), true);
assert.equal(css.includes(".settingsUpdateApplyTimeline li.is-running"), true);
assert.equal(css.includes(".settingsUpdateApplyTimeline li.is-failed"), true);

assert.equal(settingsPage.includes("raw JSON"), false);
assert.equal(settingsPage.includes("helper logs"), false);
assert.equal(settingsPage.includes("localStorage"), false);
assert.equal(settingsPage.includes("UPDATE_APPLY_PENDING_STORAGE_KEY"), true);
assert.equal(settingsPage.includes("sessionStorage.setItem(TOKEN_KEY"), false);
assert.equal(settingsPage.includes('name="token"'), false);
assert.equal(settingsPage.includes('name="url"'), false);
assert.equal(settingsPage.includes('name="repo"'), false);
assert.equal(settingsHelpers.includes("raw stderr"), false);

assert.equal(updateApplyEffectiveStatus({ status: "update_available" }, { status: "rebuilding", is_stale: true }, ""), "stalled");
assert.equal(updateApplyEffectiveStatus({ status: "update_available" }, { status: "commit_verification", effective_status: "commit_verification" }, ""), "commit_verification");
assert.equal(maintenanceStatusClass("stalled"), "blocked");
assert.equal(maintenanceStatusClass("commit_verification"), "warning");
assert.equal(updateApplyButtonText({ status: "rebuilding", current_step: "rebuilding" }, t), "Rebuilding");
assert.equal(updateApplyButtonText({ status: "health_check", current_step: "health_check" }, t), "Health check");
assert.equal(updateApplyButtonText({ status: "commit_verification", current_step: "commit_verification" }, t), "Commit check");
assert.equal(updateApplyRecoveryText("stalled", { error: { operator_action: "Check server status." } }, t), "Check server status.");
assert.deepEqual(updateApplyErrorMessages({ message: "failed", operator_action: "retry" }, {
  maintenanceMessageLabels: { failed: "Failure reason", retry: "Retry action" },
  maintenanceMessageFallback: "Safe fallback",
  maintenanceActionFallback: "Safe action fallback",
  maintenanceStatuses: { unknown: "Unknown" },
}, "en"), ["Failure reason", "Retry action"]);

assert.deepEqual(updateApplyStepRows({ steps: [{ name: "preflight", status: "completed" }, { name: "rebuilding", status: "running" }] }, t), [
  { name: "request", label: "Request", status: "pending", statusLabel: "Pending" },
  { name: "preflight", label: "Preflight", status: "completed", statusLabel: "Completed" },
  { name: "applying", label: "Updating", status: "running", statusLabel: "Running" },
  { name: "health_check", label: "Health check", status: "pending", statusLabel: "Pending" },
  { name: "commit_verification", label: "Commit check", status: "pending", statusLabel: "Pending" },
]);

assert.deepEqual(updateApplyFactRows(
  { status: "update_available", available_release: { version: "0.7.4", title: "Reliability", summary: "Progress visibility" } },
  { expected_commit: "a".repeat(40), commit_verified: false, current_step: "rebuilding", last_progress_age_seconds: 185, elapsed_seconds: 3661 },
  t,
), [
  ["Current", "-"],
  ["Available", "0.7.4"],
  ["Release", "Reliability"],
  ["Changes", "Progress visibility"],
  ["Status", "Update available"],
  ["Commit check", "Pending"],
  ["Current step", "Rebuilding"],
  ["Last progress", "3m 5s"],
  ["Elapsed", "1h 1m"],
]);

const productFiles = walkFiles(repoRoot)
  .filter((file) => !file.includes(`${path.sep}.git${path.sep}`))
  .filter((file) => !file.includes(`${path.sep}node_modules${path.sep}`))
  .filter((file) => !file.includes(`${path.sep}Working folder${path.sep}`))
  .map((file) => path.relative(repoRoot, file).replaceAll(path.sep, "/"));

assert.equal(productFiles.some((file) => file === "apps/web/app/update/page.js"), false);
