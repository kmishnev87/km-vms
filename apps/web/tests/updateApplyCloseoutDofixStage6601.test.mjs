import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  UPDATE_APPLY_RECONCILIATION_STORAGE_KEY,
  createUpdateApplyReconciliation,
  humanErrorText,
  reconcileUpdateApplySubmission,
  restoreUpdateApplyReconciliation,
  sanitizeUpdateApplyReconciliation,
  updateApplyEffectiveStatus,
  updateApplyErrorMessages,
  updateApplyOperatorModel,
  updateApplyRecheckCanClear,
  updateApplyReconnectTiming,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pageSource = fs.readFileSync(resolve(__dirname, "../app/settings/page.js"), "utf8");

const targetCommit = "b".repeat(40);
const installedCommit = "a".repeat(40);
const submissionId = "11111111-1111-4111-8111-111111111111";
const foreignSubmissionId = "22222222-2222-4222-8222-222222222222";
const submissionProof = "eyJhbGciOiJIUzI1NiJ9.eyJ0eXAiOiJ0ZXN0In0.signature";
const runningApply = {
  request_id: "request-running",
  status: "rebuilding",
  expected_commit: targetCommit,
  stale_after_seconds: 20,
  last_progress_age_seconds: 5,
  updated_at: "2026-07-19T00:00:00Z",
};
const transportError = { category: "temporarily_unavailable", status: 502 };
const timing = updateApplyReconnectTiming(runningApply, 1000);

assert.equal(updateApplyEffectiveStatus({}, runningApply, {
  applyError: transportError,
  reconnectTiming: timing,
  nowMs: timing.deadlineMs - 1,
}), "reconnecting");
assert.equal(updateApplyEffectiveStatus({}, runningApply, {
  applyError: transportError,
  reconnectTiming: timing,
  nowMs: timing.deadlineMs + 1,
}), "unknown");

for (const status of ["failed", "blocked", "cancelled", "canceled"]) {
  assert.equal(updateApplyEffectiveStatus({}, { ...runningApply, status }, {
    applyError: transportError,
    reconnectTiming: timing,
    nowMs: timing.deadlineMs + 1,
  }), status);
}
assert.equal(updateApplyEffectiveStatus({}, { ...runningApply, status: "failed", is_stale: true }, {
  applyError: transportError,
}), "stalled");
assert.equal(updateApplyEffectiveStatus({}, {
  status: "completed",
  expected_commit: targetCommit,
  commit_verified: true,
}, { applyError: transportError }), "completed");
assert.equal(updateApplyEffectiveStatus({}, {
  status: "completed",
  expected_commit: targetCommit,
  commit_verified: false,
}, { applyError: transportError }), "failed");
assert.equal(updateApplyEffectiveStatus({}, {}, { applyError: transportError }), "unknown");

const operatorText = {
  yes: "Yes",
  updateApplyStart: "Apply update",
  updateApplyHeadlines: { current: "Current", available: "Available", running: "Running", completed: "Completed", blocked: "Blocked", unknown: "Unknown" },
  updateApplySummaries: { current: "Current", available: "Available", running: "Running", completed: "Completed", blocked: "Blocked", unknown: "Unknown" },
  updateApplyResults: { current: "Current", available: "Available", running: "Running", completedVerified: "Verified", blocked: "Blocked", unknown: "Unknown" },
  updateApplyRecoveryRunning: "Running",
  updateApplyRecoveryStalled: "Stalled",
  updateApplyRecoveryUnknown: "Unknown",
  updateApplyRecoveryIdentity: "Identity",
  updateApplyRecoveryFailed: "Failed",
  updateApplyRecoveryBlocked: "Blocked",
  updateApplyRecoveryAvailable: "Available",
  updateApplyRecoveryCurrent: "Current",
  updateApplyRecoveryCompleted: "Completed",
  updateApplyRecoveryCommitMismatch: "Mismatch",
  updateApplyReleaseTitleFallback: "Release",
  updateApplyReleaseSummaryFallback: "Summary",
  updateCommitPending: "Pending",
  updateCommitUnavailable: "Unavailable",
  updateCommitVerified: "Verified",
  maintenanceStatuses: { unknown: "Unknown", failed: "Failed", blocked: "Blocked", pending: "Pending", completed: "Completed", idle: "Idle" },
  maintenanceMessageLabels: { helper_failed: "Helper failed", check_status: "Check status" },
  maintenanceMessageFallback: "Safe fallback",
  maintenanceActionFallback: "Safe action",
};
const failedModel = updateApplyOperatorModel({ status: "identity_incomplete" }, {
  ...runningApply,
  status: "failed",
  error: { message: "helper_failed", operator_action: "check_status" },
}, operatorText, "en", { applyError: transportError, reconnectTiming: timing, nowMs: timing.deadlineMs + 1 });
assert.equal(failedModel.status, "failed");
assert.equal(failedModel.severity, "blocked");
assert.equal(failedModel.stateUnknown, false);
assert.deepEqual(
  updateApplyErrorMessages({ message: "helper_failed", operator_action: "check_status" }, operatorText, "en"),
  ["Helper failed", "Check status"],
);

const candidate = { version: "0.7.24", commit: targetCommit, title: "Closeout" };
const preSubmit = {
  request_id: "request-old",
  status: "completed",
  expected_commit: installedCommit,
  updated_at: "2026-07-18T23:00:00Z",
};
const submittedAt = 100000;
const unresolved = createUpdateApplyReconciliation({
  submission_id: submissionId,
  submission_proof: submissionProof,
  target_version: candidate.version,
  target_commit: candidate.commit,
  expires_at: "2026-07-19T00:15:00Z",
}, candidate, preSubmit, submittedAt);
const foreignActive = {
  schema_version: 1,
  request_id: "request-foreign",
  submission_id: foreignSubmissionId,
  status: "rebuilding",
  effective_status: "rebuilding",
  phase: "rebuilding",
  current_step: "rebuilding",
  expected_commit: "c".repeat(40),
  commit_verified: false,
  error: null,
  is_stale: false,
  updated_at: "2026-07-19T00:01:00Z",
  admission: {
    schema_version: 2,
    authority: "active",
    state: "operation_active",
    linearizable: true,
    active: true,
    submission_id: foreignSubmissionId,
    request_id: "request-foreign",
    target_commit: "c".repeat(40),
  },
};
const conflict = reconcileUpdateApplySubmission(unresolved, foreignActive, submittedAt + 1000);
assert.equal(conflict.outcome, "conflict");
assert.equal(conflict.record.conflictRequestId, "request-foreign");
assert.equal(reconcileUpdateApplySubmission(conflict.record, foreignActive, submittedAt + 2000).outcome, "conflict");

const lateMatching = reconcileUpdateApplySubmission(conflict.record, {
  schema_version: 1,
  request_id: "request-matching",
  submission_id: submissionId,
  status: "queued",
  effective_status: "queued",
  phase: "queued",
  current_step: "queued",
  expected_commit: targetCommit,
  commit_verified: false,
  error: null,
  is_stale: false,
  updated_at: "2026-07-19T00:02:00Z",
  admission: {
    schema_version: 2,
    authority: "active",
    state: "operation_active",
    linearizable: true,
    active: true,
    submission_id: submissionId,
    request_id: "request-matching",
    target_commit: targetCommit,
  },
}, submittedAt + 3000);
assert.equal(lateMatching.outcome, "accepted");

const foreignTerminal = {
  ...foreignActive,
  status: "completed",
  effective_status: "completed",
  phase: "completed",
  current_step: "completed",
  installed_commit: "c".repeat(40),
  commit_verified: true,
  error: null,
  updated_at: "2026-07-19T00:03:00Z",
  admission: {
    schema_version: 2,
    authority: "inactive",
    state: "exact_terminal",
    linearizable: true,
    active: false,
    submission_id: foreignSubmissionId,
    request_id: "request-foreign",
    target_commit: "c".repeat(40),
  },
};
const recheck = reconcileUpdateApplySubmission(conflict.record, foreignTerminal, submittedAt + 4000);
assert.equal(recheck.outcome, "recheck_required");
assert.equal(recheck.record.state, "recheck_required");
assert.equal(reconcileUpdateApplySubmission(recheck.record, foreignTerminal, submittedAt + 5000).outcome, "recheck_required");
assert.equal(updateApplyRecheckCanClear(recheck.record, foreignActive), false);
assert.equal(updateApplyRecheckCanClear(recheck.record, { ...foreignTerminal, is_stale: true }), false);
assert.equal(updateApplyRecheckCanClear(recheck.record, { status: "unknown" }), false);
assert.equal(updateApplyRecheckCanClear(recheck.record, foreignTerminal), true);
assert.equal(updateApplyRecheckCanClear(recheck.record, {
  ...foreignActive,
  status: "failed",
  effective_status: "failed",
  phase: "health_check",
  current_step: "health_check",
  error: { category: "helper_failed" },
  admission: {
    schema_version: 2,
    authority: "inactive",
    state: "exact_terminal",
    linearizable: true,
    active: false,
    submission_id: foreignSubmissionId,
    request_id: "request-foreign",
    target_commit: "c".repeat(40),
  },
}), true);

const legacyConflict = { ...conflict.record };
delete legacyConflict.conflictRequestId;
delete legacyConflict.conflictUpdatedAt;
const restoredLegacy = sanitizeUpdateApplyReconciliation(legacyConflict, submittedAt + 5000);
assert.equal(restoredLegacy.state, "conflict");
const reboundLegacy = reconcileUpdateApplySubmission(restoredLegacy, foreignActive, submittedAt + 6000);
assert.equal(reboundLegacy.outcome, "conflict");
assert.equal(reboundLegacy.record.conflictRequestId, "request-foreign");
assert.equal(reconcileUpdateApplySubmission(restoredLegacy, foreignTerminal, submittedAt + 7000).outcome, "recheck_required");

assert.equal(restoreUpdateApplyReconciliation(null, submittedAt), null);
for (const raw of [
  "{broken-json",
  JSON.stringify({ schema: 999, state: "unresolved" }),
  JSON.stringify({ schema: 1, state: "unresolved" }),
  JSON.stringify(null),
]) {
  const corrupt = restoreUpdateApplyReconciliation(raw, submittedAt);
  assert.equal(corrupt.state, "reconciliation_corrupt");
  assert.equal(corrupt.targetCommit, "");
  assert.equal(JSON.stringify(corrupt).includes("broken-json"), false);
  assert.equal(JSON.stringify(corrupt).toLowerCase().includes("token"), false);
  assert.equal(updateApplyRecheckCanClear(corrupt, { status: "unknown" }), false);
  const observed = reconcileUpdateApplySubmission(corrupt, foreignActive, submittedAt + 1000);
  assert.equal(observed.outcome, "reconciliation_corrupt");
  assert.equal(updateApplyRecheckCanClear(observed.record, foreignActive), false);
  assert.equal(updateApplyRecheckCanClear(observed.record, foreignTerminal), true);
}

const safeFallback = "Safe localized fallback";
assert.equal(humanErrorText("Bad input", safeFallback), "Bad input");
assert.equal(humanErrorText("Password must be at least 8 characters.", safeFallback), "Password must be at least 8 characters.");
assert.equal(humanErrorText(JSON.stringify({ detail: [{ msg: "Bad input" }] }), safeFallback), "Bad input");
assert.equal(humanErrorText(JSON.stringify({ detail: { message: "Readable validation message" } }), safeFallback), "Readable validation message");
assert.equal(humanErrorText(JSON.stringify({ retry_after_seconds: 30 }), safeFallback), `${safeFallback} (30s)`);

for (const fallback of [
  "Не удалось выполнить действие.",
  "The action could not be completed.",
  "无法完成此操作。",
  "User management is unavailable.",
  "Event journal is unavailable.",
  "Backup operation is unavailable.",
  "Diagnostic report is unavailable.",
]) {
  assert.equal(humanErrorText("<html>proxy failure</html>", fallback), fallback);
  assert.equal(humanErrorText("Bad input", fallback), "Bad input");
}

for (const unsafe of [
  "<!doctype html><html><body>502 Bad Gateway</body></html>",
  "502 Bad Gateway",
  "TypeError: Failed to fetch",
  "<?xml version=\"1.0\"?><error>proxy failed</error>",
  "Traceback (most recent call last): File \"/app/main.py\", line 10",
  "Error at worker (/app/main.js:12:4)",
  "Authorization: Bearer abcdefghijklmnop",
  "password=super-secret-value",
  "C:\\km-vms\\data\\secret.txt",
  "\\\\nas-server\\private\\secret.txt",
  "file:///var/lib/km-vms/state.json",
  "/Volume3/docker/vms/data/update-control/status.json",
  "http://api:8000/internal/status",
  "https://user:pass@10.0.0.4:8443/private",
  "10.0.0.4:8000 unavailable",
  "[fd00::10]:8000 unavailable",
  "api:8000 unavailable",
  "unknown_backend_error_code",
]) {
  assert.equal(humanErrorText(unsafe, safeFallback), safeFallback, unsafe);
}
assert.equal(humanErrorText("x".repeat(5000), safeFallback), safeFallback);
assert.equal(humanErrorText(JSON.stringify({ detail: "x".repeat(500) }), safeFallback), safeFallback);
assert.ok(humanErrorText("Readable message", "f".repeat(500)).length <= 240);

assert.equal(UPDATE_APPLY_RECONCILIATION_STORAGE_KEY, "km_vms_update_apply_reconciliation_v1");
assert.equal(pageSource.includes("restoreUpdateApplyReconciliation(raw, Date.now())"), true);
assert.equal(pageSource.includes("createCorruptUpdateApplyReconciliation(Date.now())"), false);
assert.equal(pageSource.includes("updateApplyRecheckCanClear(pending, surface.applyResult.value)"), true);
assert.equal(pageSource.includes("reconcilePendingUpdateApply(exact.apply_status"), true);
assert.equal(pageSource.includes("JSON.parse(raw)"), false);
assert.equal((pageSource.match(/apiFetch\("\/system\/update\/apply"/g) || []).length, 1);

console.log("Stage 6.6.0.1 closeout dofix contract tests passed");
