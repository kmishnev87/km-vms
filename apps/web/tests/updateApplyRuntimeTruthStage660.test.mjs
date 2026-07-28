import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  UPDATE_APPLY_MODAL_GRACE_MS,
  UPDATE_APPLY_PENDING_STORAGE_KEY,
  createUpdateApplyPending,
  reconcileUpdateApplyPending,
  sanitizeUpdateApplyPending,
  updateApplyCandidateSnapshot,
  updateApplyErrorMessages,
  updateApplyOperatorModel,
  updateApplyReconnectTiming,
  updateApplyTransportPhase,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pageSource = fs.readFileSync(resolve(__dirname, "../app/settings/page.js"), "utf8");
const cssSource = fs.readFileSync(resolve(__dirname, "../app/styles/20-settings-maintenance.css"), "utf8");
assert.equal(UPDATE_APPLY_PENDING_STORAGE_KEY, "km_vms_update_apply_pending_v1");

const t = {
  yes: "Yes",
  updateApplyStart: "Apply update",
  updateApplyHeadlines: {
    current: "Current",
    available: "Available",
    running: "Running",
    completed: "Completed",
    blocked: "Blocked",
    unknown: "Unknown",
  },
  updateApplySummaries: {
    current: "Current summary",
    available: "Available summary",
    running: "Running summary",
    completed: "Completed summary",
    blocked: "Blocked summary",
    unknown: "Unknown summary",
  },
  updateApplyResults: {
    current: "Current",
    available: "Available",
    running: "Running",
    completedVerified: "Verified",
    blocked: "Blocked",
    unknown: "Unknown",
  },
  updateApplyRecoveryRunning: "Running recovery",
  updateApplyRecoveryStalled: "Stalled recovery",
  updateApplyRecoveryUnknown: "Unknown recovery",
  updateApplyRecoveryIdentity: "Identity recovery",
  updateApplyRecoveryFailed: "Failed recovery",
  updateApplyRecoveryBlocked: "Blocked recovery",
  updateApplyRecoveryAvailable: "Available recovery",
  updateApplyRecoveryCurrent: "Current recovery",
  updateApplyRecoveryCompleted: "Completed recovery",
  updateApplyRecoveryCommitMismatch: "Commit mismatch",
  updateApplyReleaseTitleFallback: "Release",
  updateApplyReleaseSummaryFallback: "Summary",
  updateCommitPending: "Pending",
  updateCommitUnavailable: "Unavailable",
  updateCommitVerified: "Verified",
  maintenanceStatuses: {
    unknown: "Unknown",
    request: "Request",
    preflight: "Preflight",
    applying: "Applying",
    health_check: "Health",
    commit_verification: "Verification",
    pending: "Pending",
    completed: "Completed",
    running: "Running",
    failed: "Failed",
    idle: "Idle",
    identity_incomplete: "Identity incomplete",
  },
};

const installedCommit = "a".repeat(40);
const targetCommit = "b".repeat(40);
const submissionId = "11111111-1111-4111-8111-111111111111";
const runningApply = {
  request_id: "request-new",
  status: "rebuilding",
  expected_commit: targetCommit,
  stale_after_seconds: 180,
  last_progress_age_seconds: 5,
  updated_at: "2026-07-18T10:00:00Z",
  steps: [{ name: "rebuilding", status: "running" }],
};

function updateState(status, metadataStatus = "complete") {
  return {
    status,
    comparison: { status },
    can_apply_from_ui: status === "update_available",
    installed_release: { version: "0.7.23", commit: installedCommit, metadata_status: metadataStatus },
    available_release: { version: "0.7.24", commit: targetCommit, title: "Stage 6.6.0", summary: "Runtime truth" },
  };
}

for (const transitionalStatus of ["identity_incomplete", "metadata_stale"]) {
  const model = updateApplyOperatorModel(updateState(transitionalStatus, "precompose"), runningApply, t, "en");
  assert.equal(model.headline, "Running");
  assert.equal(model.severity, "warning");
  assert.notEqual(model.status, "blocked");
}

const idleIdentity = updateApplyOperatorModel(updateState("identity_incomplete", "precompose"), { status: "idle" }, t, "en");
assert.equal(idleIdentity.severity, "blocked");
assert.equal(idleIdentity.headline, "Blocked");

const failedApply = updateApplyOperatorModel(
  updateState("identity_incomplete", "precompose"),
  { ...runningApply, status: "failed", error: { code: "helper_failed" } },
  t,
  "en",
);
assert.equal(failedApply.severity, "blocked");
assert.equal(failedApply.status, "failed");

const maintenanceMessages = {
  maintenanceMessageLabels: {},
  maintenanceActionFallback: "Safe fallback",
  maintenanceMessageFallback: "Status fallback",
  maintenanceStatuses: { unknown: "Unknown" },
};
assert.deepEqual(
  updateApplyErrorMessages({ message: "helper_failed", operator_action: "check_update_status" }, maintenanceMessages, "ru"),
  ["Safe fallback"],
  "equivalent sanitized reason/action messages must render once",
);
assert.deepEqual(
  updateApplyErrorMessages({ message: "failed", operator_action: "retry" }, {
    ...maintenanceMessages,
    maintenanceMessageLabels: { failed: "Failed safely", retry: "Retry safely" },
  }, "ru"),
  ["Failed safely", "Retry safely"],
  "distinct sanitized reason and next action must both remain visible",
);

const receivedAtMs = 1000;
const timing = updateApplyReconnectTiming({ ...runningApply, stale_after_seconds: 20, last_progress_age_seconds: 5 }, receivedAtMs);
assert.equal(timing.deadlineMs, 26000);
assert.equal(updateApplyTransportPhase(runningApply, { category: "temporarily_unavailable" }, timing, 25000), "reconnecting");
assert.equal(updateApplyTransportPhase(runningApply, { category: "temporarily_unavailable" }, timing, 26001), "unknown");
assert.equal(updateApplyTransportPhase(runningApply, { category: "temporarily_unavailable" }, timing, 36001), "unknown", "failed polls must not extend the original deadline");

const reconnecting = updateApplyOperatorModel(updateState("identity_incomplete", "precompose"), runningApply, t, "en", {
  applyError: { category: "temporarily_unavailable" },
  reconnectTiming: timing,
  nowMs: 25000,
});
assert.equal(reconnecting.headline, "Running");
assert.equal(reconnecting.reconnecting, true);

const expired = updateApplyOperatorModel(updateState("identity_incomplete", "precompose"), runningApply, t, "en", {
  applyError: { category: "temporarily_unavailable" },
  reconnectTiming: timing,
  nowMs: 26001,
});
assert.equal(expired.headline, "Unknown");
assert.equal(expired.stateUnknown, true);
assert.equal(expired.severity, "warning");
assert.equal(expired.timeline.every((step) => step.status === "idle"), true);

const incompleteTerminal = updateApplyOperatorModel(
  updateState("current", "precompose"),
  { status: "completed", expected_commit: targetCommit, installed_commit: targetCommit, commit_verified: true, release_identity: { metadata_status: "precompose" } },
  t,
  "en",
);
assert.equal(incompleteTerminal.severity, "blocked");
assert.notEqual(incompleteTerminal.headline, "Completed");

const completeTerminal = updateApplyOperatorModel(
  updateState("current", "complete"),
  { status: "completed", expected_commit: targetCommit, installed_commit: targetCommit, commit_verified: true, release_identity: { metadata_status: "complete" } },
  t,
  "en",
);
assert.equal(completeTerminal.headline, "Completed");
assert.equal(completeTerminal.commitVerified, true);

const unresolvedParent = updateApplyOperatorModel(
  updateState("update_available", "complete"),
  {
    request_id: "request-old",
    status: "completed",
    expected_commit: installedCommit,
    installed_commit: installedCommit,
    commit_verified: true,
    release_identity: { metadata_status: "complete" },
  },
  t,
  "en",
  { unresolvedSubmission: true },
);
assert.equal(unresolvedParent.headline, "Unknown");
assert.equal(unresolvedParent.canApply, false);

const candidateSource = {
  trusted_apply_candidate: {
    fresh: true,
    latest: { version: "0.7.24", commit: targetCommit, title: "Immutable release" },
  },
};
const candidate = updateApplyCandidateSnapshot(candidateSource);
assert.equal(candidate.version, "0.7.24");
assert.equal(candidate.commit, targetCommit);
assert.equal(Object.isFrozen(candidate), true);

const submittedAtMs = 100000;
const record = createUpdateApplyPending(submissionId, candidate, submittedAtMs);
assert.equal(record.submissionId, submissionId);
assert.equal(record.targetCommit, targetCommit);

const accepted = reconcileUpdateApplyPending(record, {
  schema_version: 1,
  request_id: "request-new",
  submission_id: submissionId,
  status: "queued",
  effective_status: "queued",
  phase: "queued",
  current_step: "queued",
  expected_commit: targetCommit,
  commit_verified: false,
  error: null,
  is_stale: false,
  updated_at: "2026-07-18T10:00:01Z",
  admission: {
    schema_version: 3,
    authority: "active",
    state: "admitted",
    active: true,
    submission_id: submissionId,
    request_id: "request-new",
    target_commit: targetCommit,
  },
}, submittedAtMs + 1000);
assert.equal(accepted.outcome, "accepted");

const conflict = reconcileUpdateApplyPending(record, {
  schema_version: 1,
  request_id: "request-other",
  submission_id: "22222222-2222-4222-8222-222222222222",
  status: "queued",
  effective_status: "queued",
  phase: "queued",
  current_step: "queued",
  expected_commit: "c".repeat(40),
  commit_verified: false,
  error: null,
  is_stale: false,
  updated_at: "2026-07-18T10:00:01Z",
  admission: {
    schema_version: 3,
    authority: "active",
    state: "admitted",
    active: true,
    submission_id: "22222222-2222-4222-8222-222222222222",
    request_id: "request-other",
    target_commit: "c".repeat(40),
  },
}, submittedAtMs + 1000);
assert.equal(conflict.outcome, "conflict");

const inactive = {
  schema_version: 1,
  request_id: null,
  submission_id: null,
  status: "idle",
  admission: {
    schema_version: 3,
    authority: "inactive",
    state: "idle",
    active: false,
    submission_id: null,
    request_id: null,
    target_commit: null,
  },
};
assert.equal(
  reconcileUpdateApplyPending(record, inactive, submittedAtMs + UPDATE_APPLY_MODAL_GRACE_MS - 1).outcome,
  "pending",
);
assert.equal(
  reconcileUpdateApplyPending(record, inactive, submittedAtMs + UPDATE_APPLY_MODAL_GRACE_MS).outcome,
  "not_accepted",
);

const restored = sanitizeUpdateApplyPending(record, submittedAtMs + 5000);
assert.equal(JSON.stringify(restored).includes("token"), false);
assert.equal(JSON.stringify(restored).includes("Authorization"), false);

assert.equal(pageSource.includes("window.confirm"), false);
assert.equal(pageSource.includes("Promise.allSettled"), true);
assert.equal(pageSource.includes("UPDATE_APPLY_PENDING_STORAGE_KEY"), true);
assert.equal(pageSource.includes("submission-ticket"), false);
assert.equal(pageSource.includes("submission_proof"), false);
assert.equal(pageSource.includes("/system/update/apply/reconciliation/"), false);
assert.equal((pageSource.match(/apiFetch\("\/system\/update\/apply"/g) || []).length, 1);
assert.equal(pageSource.includes("settingsUpdateApplySupport"), false);
assert.equal(pageSource.includes('inert={updateApplyDialog ? true : undefined}'), true);
assert.match(pageSource, /className="settingsUpdateApplyDialog"\s+role="dialog"\s+tabIndex=\{-1\}/);
assert.equal(pageSource.includes('(focusableElements(container)[0] || container)?.focus()'), true);
assert.equal(cssSource.includes(".settingsUpdateApplyDialogOverlay"), true);
assert.equal(cssSource.includes("z-index: 9600"), true);
assert.equal(pageSource.includes("updateApplyModalTitle"), true);
assert.equal((pageSource.match(/updateApplyLaunchChecking:/g) || []).length, 3);
