import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  createCorruptUpdateApplyReconciliation,
  createUpdateApplyReconciliation,
  reconcileUpdateApplySubmission,
  restoreUpdateApplyReconciliation,
  updateApplyRecheckCanClear,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pageSource = fs.readFileSync(resolve(__dirname, "../app/settings/page.js"), "utf8");
const cssSource = fs.readFileSync(resolve(__dirname, "../app/styles/20-settings-maintenance.css"), "utf8");
const targetCommit = "b".repeat(40);
const foreignCommit = "c".repeat(40);
const submissionId = "11111111-1111-4111-8111-111111111111";
const foreignSubmissionId = "22222222-2222-4222-8222-222222222222";
const submittedAt = 100000;
const preRequestId = "update-" + "a".repeat(32);
const submissionProof = "eyJhbGciOiJIUzI1NiJ9.eyJ0eXAiOiJ0ZXN0In0.signature";

function ticket(overrides = {}) {
  return {
    submission_id: submissionId,
    submission_proof: submissionProof,
    target_version: "0.8.0",
    target_commit: targetCommit,
    expires_at: "2026-07-19T00:15:00Z",
    ...overrides,
  };
}

const record = createUpdateApplyReconciliation(
  ticket(),
  { version: "0.8.0", commit: targetCommit },
  {
    request_id: preRequestId,
    status: "completed",
    expected_commit: "a".repeat(40),
    updated_at: "2026-07-19T00:00:00Z",
  },
  submittedAt,
);
assert.equal(record.schema, 3);
assert.equal(record.submissionId, submissionId);
assert.equal(record.submissionProof, submissionProof);

function admission({ authority = "active", state = "operation_active", requestId, submission = submissionId, commit = targetCommit } = {}) {
  return {
    schema_version: 2,
    authority,
    state,
    linearizable: authority !== "unknown",
    active: authority === "active" ? true : authority === "inactive" ? false : null,
    submission_id: submission || null,
    request_id: requestId || null,
    target_version: commit ? "0.8.0" : null,
    target_commit: commit || null,
    generation: requestId || null,
    reason_code: null,
    retry_allowed: authority === "inactive",
    next_action: authority === "active" ? "wait_for_status" : "confirm_apply",
  };
}

function active(overrides = {}) {
  const requestId = overrides.request_id === undefined ? "update-" + "d".repeat(32) : overrides.request_id;
  const submission = overrides.submission_id === undefined ? submissionId : overrides.submission_id;
  const commit = overrides.expected_commit === undefined ? targetCommit : overrides.expected_commit;
  return {
    schema_version: 1,
    request_id: requestId,
    submission_id: submission,
    status: "queued",
    effective_status: "queued",
    phase: "queued",
    current_step: "queued",
    expected_commit: commit,
    commit_verified: false,
    error: null,
    is_stale: false,
    updated_at: "2026-07-19T00:01:00Z",
    admission: admission({ requestId, submission, commit }),
    ...overrides,
  };
}

function terminal(status, overrides = {}) {
  const requestId = overrides.request_id || "update-" + "e".repeat(32);
  const phase = status === "completed" ? "completed" : status === "cancelled" ? "cancelled" : "health_check";
  return {
    schema_version: 1,
    request_id: requestId,
    submission_id: submissionId,
    status,
    effective_status: status,
    phase,
    current_step: phase,
    expected_commit: targetCommit,
    installed_commit: status === "completed" ? targetCommit : null,
    commit_verified: status === "completed",
    error: status === "completed" ? null : status === "cancelled" ? { category: "cancelled_before_start" } : { category: "health_check_failed" },
    is_stale: false,
    updated_at: "2026-07-19T00:02:00Z",
    admission: admission({ authority: "inactive", state: "exact_terminal", requestId, submission: submissionId, commit: targetCommit }),
    ...overrides,
  };
}

assert.equal(reconcileUpdateApplySubmission(record, active(), submittedAt + 1000).outcome, "accepted");
assert.equal(reconcileUpdateApplySubmission(record, active({ request_id: "" }), submittedAt + 1000).outcome, "unresolved");
assert.equal(reconcileUpdateApplySubmission(record, active({ request_id: preRequestId }), submittedAt + 1000).outcome, "unresolved");
assert.equal(reconcileUpdateApplySubmission(record, active({ submission_id: null }), submittedAt + 1000).outcome, "unresolved");
for (const status of ["failed", "cancelled", "completed"]) {
  assert.equal(reconcileUpdateApplySubmission(record, terminal(status), submittedAt + 1000).outcome, "accepted", status);
}

const foreignActive = active({
  request_id: "update-" + "f".repeat(32),
  submission_id: foreignSubmissionId,
  expected_commit: foreignCommit,
  admission: admission({ requestId: "update-" + "f".repeat(32), submission: foreignSubmissionId, commit: foreignCommit }),
});
const conflict = reconcileUpdateApplySubmission(record, foreignActive, submittedAt + 2000);
assert.equal(conflict.outcome, "conflict");
assert.equal(conflict.record.conflictSubmissionId, foreignSubmissionId);
assert.equal(reconcileUpdateApplySubmission(conflict.record, active(), submittedAt + 3000).outcome, "accepted");

const mutatedForeign = active({
  request_id: foreignActive.request_id,
  submission_id: foreignSubmissionId,
  expected_commit: targetCommit,
  admission: admission({ requestId: foreignActive.request_id, submission: foreignSubmissionId, commit: targetCommit }),
});
assert.equal(reconcileUpdateApplySubmission(conflict.record, mutatedForeign, submittedAt + 3000).outcome, "conflict");

const foreignTerminal = terminal("completed", {
  request_id: foreignActive.request_id,
  submission_id: foreignSubmissionId,
  expected_commit: foreignCommit,
  installed_commit: foreignCommit,
  admission: admission({ authority: "inactive", state: "exact_terminal", requestId: foreignActive.request_id, submission: foreignSubmissionId, commit: foreignCommit }),
});
assert.equal(reconcileUpdateApplySubmission(conflict.record, foreignTerminal, submittedAt + 4000).outcome, "recheck_required");

const unknown = active({
  status: "blocked",
  effective_status: "blocked",
  phase: "status_read",
  current_step: "status_read",
  admission: admission({ authority: "unknown", state: "request_invalid", requestId: null, submission: null, commit: null }),
});
assert.equal(reconcileUpdateApplySubmission(record, unknown, submittedAt + 5000).outcome, "unresolved");

const inactive = {
  schema_version: 1,
  request_id: null,
  submission_id: null,
  status: "idle",
  effective_status: "idle",
  phase: "idle",
  current_step: "idle",
  expected_commit: null,
  installed_commit: null,
  commit_verified: false,
  error: null,
  is_stale: false,
  admission: admission({ authority: "inactive", state: "no_active_admission", requestId: null, submission: null, commit: null }),
};
assert.equal(reconcileUpdateApplySubmission(record, inactive, submittedAt + 6000).outcome, "not_accepted");

const corrupt = createCorruptUpdateApplyReconciliation(submittedAt);
assert.equal(reconcileUpdateApplySubmission(corrupt, active(), submittedAt + 7000).outcome, "reconciliation_corrupt");
assert.equal(updateApplyRecheckCanClear(corrupt, terminal("failed")), true);
assert.equal(updateApplyRecheckCanClear(corrupt, unknown), false);
assert.equal(updateApplyRecheckCanClear(corrupt, { ...inactive, admission: undefined }), false);
assert.equal(updateApplyRecheckCanClear(corrupt, inactive), true);

const legacyRaw = JSON.stringify({
  schema: 1,
  state: "unresolved",
  targetVersion: "0.7.9",
  targetCommit,
  submittedAtMs: submittedAt,
});
const legacy = restoreUpdateApplyReconciliation(legacyRaw, submittedAt + 1000);
assert.equal(legacy.state, "legacy_uncorrelated");
assert.equal(reconcileUpdateApplySubmission(legacy, active(), submittedAt + 8000).outcome, "legacy_uncorrelated");
assert.equal(updateApplyRecheckCanClear(legacy, inactive), true);

assert.equal(pageSource.includes("submission_id: persisted.submissionId"), true);
assert.equal(pageSource.includes("submission_proof: persisted.submissionProof"), true);
assert.equal(pageSource.includes('apiFetch("/system/update/apply/submission-ticket"'), true);
assert.equal(pageSource.includes('"X-KM-VMS-Update-Submission-Proof": persisted.submissionProof'), false);
assert.equal(pageSource.includes("commitUpdateApplyReconciliation(reconciliation)"), true);
assert.equal(pageSource.includes("setToast(null);"), true);
assert.equal(pageSource.includes("toast && !updateApplyDialog"), true);
assert.equal((pageSource.match(/apiFetch\("\/system\/update\/apply"/g) || []).length, 1);
assert.match(cssSource, /\.settingsUpdateApplyDialogOverlay\s*\{[^}]*z-index:\s*9600/s);
assert.match(cssSource, /\.settingsToast\s*\{[^}]*z-index:\s*9500/s);

console.log("Stage 6.6.0.1.2 atomic admission and exact frontend correlation tests passed");
