import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { updateApplyFactRows, updateApplyOperatorModel } from "../lib/settingsUpdateApplyHelpers.js";
import { retainUpdateApplyOperation, updateApplyOperationView } from "../lib/settingsUpdateOperation.js";

const commit = "a".repeat(40);
const steps = ["request", "preflight", "applying", "health_check", "commit_verification"]
  .map((name) => ({ name, status: "completed" }));
const completed = {
  request_id: "update-example-a", target_version: "0.9.0", status: "completed",
  expected_commit: commit, installed_commit: commit, commit_verified: true,
  started_at: "2026-11-04T15:11:12Z", finished_at: "2026-11-04T15:12:52Z", steps,
};
const update = {
  status: "known", check_state: "not_checked",
  installed_release: {
    version: "0.9.0", commit_sha: commit, metadata_status: "complete", identity_validity: "valid",
    installed_at: completed.started_at,
  },
};
const t = {
  updateApplyHeadlines: { completed: "Completed", not_checked: "Not checked", blocked: "Attention", available: "Available", running: "Running" },
  updateApplySummaries: { completed: "Installed and verified" },
  updateCommitVerified: "Verified", updateCommitPending: "Pending", updateCommitUnavailable: "Unavailable",
  maintenanceLabels: { status: "Status", elapsed: "Elapsed" },
  maintenanceStatuses: { unknown: "Unknown", idle: "Idle" },
};
const detail = (status, current = update, context = {}) => updateApplyOperatorModel(current, status, t, "en", { operationResult: true, ...context });

test("completed operation keeps its steps when they are read from matching history", () => {
  const live = detail(completed);
  const historySteps = detail({ ...completed, steps: [], last_apply_summary: completed });
  assert.equal(historySteps.resultVerified, true);
  assert.deepEqual(historySteps.timeline, live.timeline);
  assert.equal(historySteps.timeline.every((step) => step.status === "completed"), true);
  assert.equal(historySteps.elapsed, "1m 40s");
  assert.notEqual(historySteps.finishedAt, historySteps.installedAt);
  const facts = new Map(updateApplyFactRows(update, completed, t, historySteps));
  assert.equal(facts.get("Status"), "Completed");
  assert.equal(facts.get("Elapsed"), "1m 40s");
});

test("history-only detail is successful without turning the overview into old success", () => {
  const historyOnly = { status: "idle", last_apply_summary: completed };
  assert.equal(detail(historyOnly).headline, "Completed");
  assert.equal(detail(historyOnly).timeline.every((step) => step.status === "completed"), true);
  const overview = updateApplyOperatorModel(update, historyOnly, t, "en");
  assert.equal(overview.headline, "Not checked");
  assert.equal(overview.timeline.every((step) => step.status === "idle"), true);
});

test("a completed result is independent of subsequent release checks and transport failures", () => {
  for (const status of ["known", "check_failed", "update_available"]) {
    const model = detail(completed, {
      ...update, status, comparison: { status }, can_apply_from_ui: status === "update_available",
      available_release: { version: "0.9.1", commit: "b".repeat(40) },
    }, { applyError: { category: "network_unavailable" } });
    assert.equal(model.headline, "Completed");
    assert.equal(model.elapsed, "1m 40s");
    assert.equal(model.resultVerified, true);
  }
});

test("history of another attempt never supplies steps or verification", () => {
  const next = { ...completed, request_id: "update-example-b", steps: [], last_apply_summary: completed };
  assert.equal(updateApplyOperationView(next).steps.length, 0);
  assert.equal(detail(next).timeline.every((step) => step.status === "idle"), true);
  const running = detail({ ...next, status: "preflight", commit_verified: false });
  assert.equal(running.resultVerified, false);
  assert.equal(running.headline, "Running");
});

test("unverified, mismatched, incomplete and rolled-back results are not successful", () => {
  for (const change of [
    { commit_verified: false }, { commit_verified: undefined }, { installed_commit: "b".repeat(40) },
    { expected_commit: "a".repeat(12) }, { status: "failed" }, { status: "failed_rolled_back" },
  ]) {
    const model = detail({ ...completed, ...change });
    assert.equal(model.resultVerified, false);
    assert.equal(model.severity, "blocked");
    if (!change.status) assert.notEqual(model.timeline.at(-1).status, "completed");
  }
  assert.equal(detail(completed, { ...update, installed_release: { ...update.installed_release, metadata_status: "incomplete" } }).resultVerified, false);
  const unverified = detail({ ...completed, commit_verified: false, last_apply_summary: completed });
  assert.equal(unverified.commitVerified, false);
  assert.equal(unverified.commitStatus, "Pending");
});

test("an open result survives empty status, not a new attempt or explicit failure", () => {
  assert.equal(retainUpdateApplyOperation(completed, { status: "idle" }), completed);
  assert.equal(retainUpdateApplyOperation(completed, { status: "idle", last_apply_summary: completed }), completed);
  const next = { request_id: "update-example-b", status: "preflight", steps: [], last_apply_summary: completed };
  assert.equal(retainUpdateApplyOperation(completed, next).request_id, next.request_id);
  const lost = retainUpdateApplyOperation(next, { status: "idle", last_apply_summary: completed });
  assert.equal(lost.request_id, next.request_id);
  assert.equal(lost.status, "unknown");
  assert.equal(detail(lost).stateUnknown, true);
  assert.equal(detail(lost).resultVerified, false);
  const failed = { ...completed, status: "failed", commit_verified: false };
  assert.equal(retainUpdateApplyOperation(completed, failed).status, "failed");
});

const controllerSource = readFileSync(new URL("../lib/settingsMaintenanceController.js", import.meta.url), "utf8");
test("stalled heartbeat projection can recover without losing the real final result", () => {
  const stalled = { request_id: "update-stalled-recovery", status: "rebuilding", effective_status: "stalled",
    phase: "building_images", steps: [{ name: "applying", status: "running" }] };
  const recovered = { ...stalled, effective_status: "rebuilding", phase: "verifying_images" };
  const next = retainUpdateApplyOperation(stalled, recovered);
  assert.equal(next.effective_status, "rebuilding");
  assert.equal(next.phase, "verifying_images");
  const finished = { ...completed, request_id: stalled.request_id };
  const final = retainUpdateApplyOperation(next, finished);
  assert.equal(final.status, "completed");
  assert.equal(retainUpdateApplyOperation(final, { status: "idle" }), final);
  assert.equal(retainUpdateApplyOperation(final, recovered), final);
  assert.equal(final.steps.every(step => step.status === "completed"), true);
  assert.equal(detail(final).elapsed, "1m 40s");
});

function overviewLoader() {
  const start = controllerSource.indexOf("async function loadMaintenanceOverview(");
  const end = controllerSource.indexOf("async function loadMaintenanceBackupPage(", start);
  assert.ok(start >= 0 && end > start);
  const requests = [];
  const state = { overview: null, error: "", loading: [], toasts: [], requestRef: { current: 0 } };
  const apiFetch = () => new Promise((resolve, reject) => requests.push({ resolve, reject }));
  const load = new Function(
    "canManageMaintenance", "maintenanceOverviewRequestRef", "apiFetch", "setMaintenanceLoading",
    "setMaintenanceOverview", "setMaintenanceError", "humanErrorText", "t", "showToast",
    `return (${controllerSource.slice(start, end).trim()});`,
  )(
    true, state.requestRef, apiFetch, (value) => state.loading.push(value),
    (value) => { state.overview = value; }, (value) => { state.error = value; },
    (message) => message, { maintenanceLoadError: "Failed" }, (value) => state.toasts.push(value),
  );
  return { load, requests, state };
}

test("late overview response cannot overwrite the newer snapshot", async () => {
  const { load, requests, state } = overviewLoader();
  const old = load();
  const fresh = load({ silent: true });
  requests[1].resolve({ version: "new", backups: 2 });
  await fresh;
  requests[0].resolve({ version: "old", backups: 1 });
  await old;
  assert.deepEqual(state.overview, { version: "new", backups: 2 });
  assert.deepEqual(state.loading, [true, false]);
});

test("late overview error does not replace a newer success or emit a stale toast", async () => {
  const { load, requests, state } = overviewLoader();
  const old = load();
  const fresh = load({ silent: true });
  requests[1].resolve({ version: "new" });
  await fresh;
  requests[0].reject(new Error("old error"));
  await old;
  assert.equal(state.error, "");
  assert.equal(state.toasts.length, 0);
});

test("invalidated overview request is ignored after access changes or unmount", async () => {
  const { load, requests, state } = overviewLoader();
  const result = load({ silent: true });
  state.requestRef.current += 1;
  requests[0].resolve({ version: "stale" });
  await result;
  assert.equal(state.overview, null);
});

test("terminal and detail-close events refresh the whole surface without a new polling loop", () => {
  assert.match(controllerSource, /updateTerminalRefreshRef\.current === resultKey/);
  assert.match(controllerSource, /updateTerminalRefreshRef\.current = resultKey;\s*void refreshMaintenanceSurface\(\{ silent: true \}\)/);
  assert.match(controllerSource, /function closeMaintenanceDetail\(\)[\s\S]*?if \(refreshUpdate\) void refreshMaintenanceSurface\(\{ silent: true \}\)/);
});
