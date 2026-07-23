import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  UPDATE_APPLY_RECONCILIATION_STORAGE_KEY,
  createUpdateApplyReconciliation,
  reconcileUpdateApplySubmission,
  restoreUpdateApplyReconciliation,
  updateApplyReconciliationExactMatch,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pageSource = fs.readFileSync(resolve(__dirname, "../app/settings/page.js"), "utf8");
const targetCommit = "b".repeat(40);
const foreignCommit = "c".repeat(40);
const submissionId = "11111111-1111-4111-8111-111111111111";
const foreignSubmissionId = "22222222-2222-4222-8222-222222222222";
const submissionProof = "eyJhbGciOiJIUzI1NiJ9.eyJ0eXAiOiJ0ZXN0In0.signature";
const submittedAtMs = 100000;
const candidate = { version: "0.8.0", commit: targetCommit, title: "Durable apply" };
const preSubmitStatus = {
  request_id: "update-" + "a".repeat(32),
  status: "completed",
  expected_commit: "a".repeat(40),
  updated_at: "2026-07-19T00:00:00Z",
};

function ticket(overrides = {}) {
  return {
    submission_id: submissionId,
    submission_proof: submissionProof,
    target_version: candidate.version,
    target_commit: candidate.commit,
    expires_at: "2026-07-19T00:15:00Z",
    ...overrides,
  };
}

function admission({ authority, state, requestId, id, commit }) {
  return {
    schema_version: 2,
    authority,
    state,
    linearizable: true,
    active: authority === "active",
    submission_id: id,
    request_id: requestId,
    target_commit: commit,
  };
}

function status({ requestId, id, commit, status = "queued" }) {
  const terminal = status === "completed";
  return {
    schema_version: 1,
    request_id: requestId,
    submission_id: id,
    status,
    effective_status: status,
    phase: terminal ? "completed" : "queued",
    current_step: terminal ? "completed" : "queued",
    expected_commit: commit,
    installed_commit: terminal ? commit : null,
    commit_verified: terminal,
    error: null,
    is_stale: false,
    updated_at: "2026-07-19T00:01:00Z",
    admission: admission({
      authority: terminal ? "inactive" : "active",
      state: terminal ? "exact_terminal" : "operation_active",
      requestId,
      id,
      commit,
    }),
  };
}

const record = createUpdateApplyReconciliation(ticket(), candidate, preSubmitStatus, submittedAtMs);
assert.equal(record.schema, 3);
assert.equal(record.submissionId, submissionId);
assert.equal(record.submissionProof, submissionProof);
assert.equal(record.proofExpiresAt, ticket().expires_at);

for (const invalidTicket of [
  ticket({ submission_proof: "invalid" }),
  ticket({ submission_proof: "a".repeat(2049) }),
  ticket({ target_version: "0.8.1" }),
  ticket({ target_commit: foreignCommit }),
  ticket({ expires_at: "not-a-date" }),
]) {
  assert.equal(createUpdateApplyReconciliation(invalidTicket, candidate, preSubmitStatus, submittedAtMs), null);
}

function persistBeforeApply(storage, value) {
  let applyPosts = 0;
  try {
    const serialized = JSON.stringify(value);
    storage.setItem(UPDATE_APPLY_RECONCILIATION_STORAGE_KEY, serialized);
    const restored = restoreUpdateApplyReconciliation(
      storage.getItem(UPDATE_APPLY_RECONCILIATION_STORAGE_KEY),
      submittedAtMs,
    );
    if (!updateApplyReconciliationExactMatch(value, restored, submittedAtMs)) return { applyPosts, restored: null };
    applyPosts += 1;
    return { applyPosts, restored };
  } catch {
    return { applyPosts, restored: null };
  }
}

const storageFailure = persistBeforeApply({
  setItem() { throw new Error("blocked"); },
  getItem() { return null; },
}, record);
assert.equal(storageFailure.applyPosts, 0);

const readbackMismatch = persistBeforeApply({
  value: "",
  setItem(_key, value) { this.value = value; },
  getItem() {
    const parsed = JSON.parse(this.value);
    parsed.submissionId = foreignSubmissionId;
    return JSON.stringify(parsed);
  },
}, record);
assert.equal(readbackMismatch.applyPosts, 0);

const validStorage = persistBeforeApply({
  value: "",
  setItem(_key, value) { this.value = value; },
  getItem() { return this.value; },
}, record);
assert.equal(validStorage.applyPosts, 1);
assert.equal(updateApplyReconciliationExactMatch(record, validStorage.restored, submittedAtMs), true);
assert.equal(updateApplyReconciliationExactMatch(record, { ...record, submissionProof: "a.b.c" }, submittedAtMs), false);

const currentB = status({
  requestId: "update-" + "b".repeat(32),
  id: foreignSubmissionId,
  commit: foreignCommit,
});
assert.equal(reconcileUpdateApplySubmission(record, currentB, submittedAtMs + 1000).outcome, "conflict");

const canonicalA = status({
  requestId: "update-" + "c".repeat(32),
  id: submissionId,
  commit: targetCommit,
  status: "completed",
});
assert.equal(reconcileUpdateApplySubmission(record, canonicalA, submittedAtMs + 2000).outcome, "accepted");

const commitStart = pageSource.indexOf("function commitUpdateApplyReconciliation");
const commitEnd = pageSource.indexOf("function closeUpdateApplyDialog", commitStart);
const commitSource = pageSource.slice(commitStart, commitEnd);
assert.ok(commitStart >= 0 && commitEnd > commitStart);
assert.ok(commitSource.indexOf("sessionStorage.setItem") < commitSource.indexOf("sessionStorage.getItem"));
assert.ok(commitSource.indexOf("sessionStorage.getItem") < commitSource.indexOf("restoreUpdateApplyReconciliation"));
assert.ok(commitSource.indexOf("restoreUpdateApplyReconciliation") < commitSource.indexOf("updateApplyReconciliationExactMatch"));

const confirmStart = pageSource.indexOf("async function confirmUpdateApply");
const confirmEnd = pageSource.indexOf("async function downloadMaintenanceReport", confirmStart);
const confirmSource = pageSource.slice(confirmStart, confirmEnd);
assert.ok(confirmStart >= 0 && confirmEnd > confirmStart);
assert.ok(confirmSource.indexOf("/system/update/apply/submission-ticket") < confirmSource.indexOf("commitUpdateApplyReconciliation(reconciliation)"));
assert.ok(confirmSource.indexOf("commitUpdateApplyReconciliation(reconciliation)") < confirmSource.indexOf('apiFetch("/system/update/apply"'));
assert.equal((confirmSource.match(/apiFetch\("\/system\/update\/apply"/g) || []).length, 1);
assert.equal(confirmSource.includes("submission_proof: persisted.submissionProof"), true);
assert.equal(confirmSource.includes("X-KM-VMS-Update-Submission-Proof"), false);
assert.equal(confirmSource.includes("setTimeout(() => confirmUpdateApply"), false);

assert.equal(pageSource.includes("/system/update/apply/reconciliation/"), true);
assert.equal(pageSource.includes("reconcilePendingUpdateApply(exact.apply_status"), true);
assert.equal(pageSource.includes("reconcilePendingUpdateApply(applyResult.value"), false);
assert.equal(pageSource.includes("updateApplyPersistenceFailed"), true);
assert.equal(pageSource.includes("updateApplySubmissionExpired"), true);
assert.equal(pageSource.includes("window.confirm"), false);
assert.equal(pageSource.includes("{pending.submissionProof}"), false);

console.log("Stage 6.6.0.1.2.1 durable frontend admission tests passed");
