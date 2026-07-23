import assert from "node:assert/strict";
import {
  createCorruptUpdateApplyReconciliation,
  createUpdateApplyReconciliation,
  humanErrorText,
  reconcileUpdateApplySubmission,
  restoreUpdateApplyReconciliation,
  updateApplyIsAuthoritativeInactiveSnapshot,
  updateApplyRecheckCanClear,
} from "../lib/settingsPageHelpers.js";

const submittedAt = 100000;
const targetCommit = "b".repeat(40);
const foreignCommit = "c".repeat(40);
const submissionId = "11111111-1111-4111-8111-111111111111";
const foreignSubmissionId = "22222222-2222-4222-8222-222222222222";
const submissionProof = "eyJhbGciOiJIUzI1NiJ9.eyJ0eXAiOiJ0ZXN0In0.signature";

function ticket(overrides = {}) {
  return {
    submission_id: submissionId,
    submission_proof: submissionProof,
    target_version: "0.7.25",
    target_commit: targetCommit,
    expires_at: "2026-07-19T00:15:00Z",
    ...overrides,
  };
}

function snapshot(overrides = {}) {
  const merged = {
    schema_version: 1,
    request_id: "request-terminal",
    submission_id: submissionId,
    status: "failed",
    effective_status: "failed",
    phase: "health_check",
    current_step: "health_check",
    expected_commit: targetCommit,
    installed_commit: null,
    commit_verified: false,
    error: { category: "helper_failed" },
    is_stale: false,
    ...overrides,
  };
  const running = ["queued", "rebuilding", "applying"].includes(merged.status);
  const inactive = ["idle", "failed", "cancelled", "canceled", "completed"].includes(merged.status);
  return {
    ...merged,
    admission: overrides.admission || {
      schema_version: 2,
      authority: running ? "active" : inactive ? "inactive" : "unknown",
      state: running ? "operation_active" : merged.status === "idle" ? "no_active_admission" : inactive ? "exact_terminal" : "status_non_authoritative",
      linearizable: running || inactive,
      active: running ? true : inactive ? false : null,
      submission_id: merged.submission_id || null,
      request_id: merged.request_id || null,
      target_commit: merged.expected_commit || null,
    },
  };
}

const validFailed = snapshot();
const validCancelled = snapshot({
  status: "cancelled",
  effective_status: "cancelled",
  phase: "cancelled",
  current_step: "cancelled",
  error: { category: "cancelled_before_start" },
});
const validCompleted = snapshot({
  status: "completed",
  effective_status: "completed",
  phase: "completed",
  current_step: "completed",
  installed_commit: targetCommit,
  commit_verified: true,
  error: null,
});
const cleanIdle = snapshot({
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
});

for (const authoritative of [validFailed, validCancelled, validCompleted, cleanIdle]) {
  assert.equal(updateApplyIsAuthoritativeInactiveSnapshot(authoritative), true);
}
assert.equal(updateApplyIsAuthoritativeInactiveSnapshot(snapshot({
  status: "canceled",
  effective_status: "canceled",
  phase: "canceled",
  current_step: "canceled",
  error: { category: "canceled_before_start" },
})), true);

const syntheticBlocked = [
  snapshot({ status: "blocked", effective_status: "blocked", phase: "status_read", current_step: "status_read", error: { category: "status_invalid_json" } }),
  snapshot({ status: "blocked", effective_status: "blocked", phase: "status_read", current_step: "status_read", error: { category: "status_too_large" } }),
  snapshot({ status: "blocked", effective_status: "blocked", phase: "status_read", current_step: "status_read", error: { category: "status_invalid_shape" } }),
  snapshot({ status: "blocked", effective_status: "blocked", phase: "status_redaction", current_step: "status_redaction", error: { category: "status_sensitive_content" } }),
  snapshot({ status: "blocked", effective_status: "blocked", phase: "preflight", current_step: "preflight", error: { category: "safe_looking_blocker" } }),
];
for (const blocked of syntheticBlocked) {
  assert.equal(updateApplyIsAuthoritativeInactiveSnapshot(blocked), false);
}

for (const nonAuthoritative of [
  { ...validFailed, schema_version: undefined },
  { ...validFailed, schema_version: "1" },
  { ...validFailed, schema_version: 2 },
  { ...validFailed, expected_commit: undefined },
  { ...validFailed, expected_commit: "not-a-sha" },
  { ...validFailed, phase: "unknown", current_step: "unknown" },
  { ...validFailed, phase: "idle", current_step: "idle" },
  { ...validFailed, effective_status: "completed" },
  { ...validFailed, is_stale: true },
  { ...validFailed, status: "unknown", effective_status: "unknown" },
  { ...validFailed, error: { category: "status_invalid_json" } },
  { ...validCompleted, commit_verified: false },
  { ...validCompleted, installed_commit: foreignCommit },
  { ...validCancelled, error: { category: "helper_failed" } },
  snapshot({ status: "rebuilding", effective_status: "rebuilding", phase: "rebuilding", current_step: "rebuilding", error: null }),
]) {
  assert.equal(updateApplyIsAuthoritativeInactiveSnapshot(nonAuthoritative), false);
}

const unresolved = createUpdateApplyReconciliation(
  ticket(),
  { version: "0.7.25", commit: targetCommit },
  {
    request_id: "request-before",
    status: "completed",
    expected_commit: "a".repeat(40),
    updated_at: "2026-07-19T00:00:00Z",
  },
  submittedAt,
);
const foreignRunning = snapshot({
  request_id: "request-foreign",
  submission_id: foreignSubmissionId,
  status: "rebuilding",
  effective_status: "rebuilding",
  phase: "rebuilding",
  current_step: "rebuilding",
  expected_commit: foreignCommit,
  error: null,
  updated_at: "2026-07-19T00:01:00Z",
});
const conflict = reconcileUpdateApplySubmission(unresolved, foreignRunning, submittedAt + 1000);
assert.equal(conflict.outcome, "conflict");
assert.equal(reconcileUpdateApplySubmission(conflict.record, foreignRunning, submittedAt + 2000).outcome, "conflict");

const matchingRunning = {
  ...foreignRunning,
  request_id: "request-matching",
  submission_id: submissionId,
  status: "queued",
  effective_status: "queued",
  phase: "queued",
  current_step: "queued",
  expected_commit: targetCommit,
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
};
assert.equal(reconcileUpdateApplySubmission(conflict.record, matchingRunning, submittedAt + 3000).outcome, "accepted");

for (const blocked of syntheticBlocked) {
  const observed = reconcileUpdateApplySubmission(conflict.record, {
    ...blocked,
    request_id: "request-blocked",
    expected_commit: targetCommit,
    updated_at: "2026-07-19T00:03:00Z",
  }, submittedAt + 4000);
  assert.equal(observed.outcome, "conflict");
  assert.equal(updateApplyRecheckCanClear(createCorruptUpdateApplyReconciliation(submittedAt), blocked), false);
}

for (const terminal of [validFailed, validCancelled, validCompleted]) {
  const observed = reconcileUpdateApplySubmission(conflict.record, {
    ...terminal,
    request_id: "request-terminal-foreign",
    expected_commit: foreignCommit,
    ...(terminal.status === "completed" ? { installed_commit: foreignCommit } : {}),
    updated_at: "2026-07-19T00:04:00Z",
  }, submittedAt + 5000);
  assert.equal(observed.outcome, "recheck_required");
  assert.equal(updateApplyRecheckCanClear(observed.record, terminal), true);
}

const corruptGate = createCorruptUpdateApplyReconciliation(submittedAt);
assert.equal(reconcileUpdateApplySubmission(corruptGate, syntheticBlocked[0], submittedAt + 6000).outcome, "reconciliation_corrupt");
assert.equal(updateApplyRecheckCanClear(corruptGate, cleanIdle), true);
assert.equal(updateApplyRecheckCanClear(corruptGate, foreignRunning), false);

const fallback = "Safe localized fallback";
for (const [input, expected] of [
  [{ detail: "Bad input" }, "Bad input"],
  [{ message: "Readable" }, "Readable"],
  [{ summary: "Readable" }, "Readable"],
  [{ error: "Readable" }, "Readable"],
  [{ msg: "Readable" }, "Readable"],
  [[{ msg: "Bad input" }], "Bad input"],
  [{ detail: [{ msg: "Bad input" }] }, "Bad input"],
  [JSON.stringify({ detail: [{ msg: "Bad input" }] }), "Bad input"],
  [JSON.stringify({ detail: { message: "Readable" } }), "Readable"],
]) {
  assert.equal(humanErrorText(input, fallback), expected);
}

for (const unsafe of [
  { detail: "<!doctype html><html>502 Bad Gateway</html>" },
  [{ msg: "Traceback: File \"/app/main.py\", line 10" }],
  { message: "password=secret-value" },
  { summary: "/Volume3/docker/vms/data/update-control/status.json" },
  { error: "http://api:8000/internal/status" },
  { unknown: "Readable but not allowlisted" },
  new Date(),
  new Error("Readable"),
  123,
  10n,
  Symbol("unsafe"),
  () => "unsafe",
]) {
  assert.equal(humanErrorText(unsafe, fallback), fallback);
}

let toStringCalls = 0;
let valueOfCalls = 0;
let toJsonCalls = 0;
const customConversion = {
  toString() {
    toStringCalls += 1;
    return "custom conversion text";
  },
  valueOf() {
    valueOfCalls += 1;
    return "custom value";
  },
  toJSON() {
    toJsonCalls += 1;
    return { detail: "custom JSON text" };
  },
};
assert.equal(humanErrorText(customConversion, fallback), fallback);
assert.equal(toStringCalls, 0);
assert.equal(valueOfCalls, 0);
assert.equal(toJsonCalls, 0);

const failingProxy = new Proxy({}, {
  getPrototypeOf() {
    throw new Error("proxy trap");
  },
});
assert.equal(humanErrorText(failingProxy, fallback), fallback);

let getterCalls = 0;
const accessorCandidate = {};
Object.defineProperty(accessorCandidate, "detail", {
  enumerable: true,
  get() {
    getterCalls += 1;
    return "getter text";
  },
});
assert.equal(humanErrorText(accessorCandidate, fallback), fallback);
assert.equal(getterCalls, 0);

assert.equal(humanErrorText({ detail: "x".repeat(500) }, fallback), fallback);
assert.equal(humanErrorText([{ msg: "a".repeat(121) }, { msg: "b".repeat(121) }], fallback), fallback);
assert.equal(humanErrorText({ retry_after_seconds: 30 }, fallback), `${fallback} (30s)`);
assert.equal(humanErrorText(JSON.stringify({ retry_after_seconds: "30" }), fallback), `${fallback} (30s)`);
assert.ok(humanErrorText({ detail: "Readable" }, "f".repeat(500)).length <= 240);

assert.equal(restoreUpdateApplyReconciliation(null, submittedAt), null);
assert.equal(restoreUpdateApplyReconciliation(undefined, submittedAt), null);
const restoredValid = restoreUpdateApplyReconciliation(JSON.stringify(unresolved), submittedAt + 1000);
assert.equal(restoredValid.state, "unresolved");
assert.equal(restoredValid.targetCommit, targetCommit);

for (const raw of [
  "{malformed-json",
  JSON.stringify({ schema: 999, state: "unresolved" }),
  JSON.stringify({ schema: 1, state: "unresolved" }),
  JSON.stringify(null),
  JSON.stringify([]),
]) {
  const marker = restoreUpdateApplyReconciliation(raw, submittedAt);
  assert.equal(marker.state, "reconciliation_corrupt");
}
const uniqueMalformedMarker = restoreUpdateApplyReconciliation("{malformed-UNIQUE-RAW-PAYLOAD", submittedAt);
assert.equal(JSON.stringify(uniqueMalformedMarker).includes("UNIQUE-RAW-PAYLOAD"), false);

const originalJsonParse = JSON.parse;
let jsonParseCalls = 0;
JSON.parse = (...args) => {
  jsonParseCalls += 1;
  return originalJsonParse(...args);
};
try {
  const oversizedRaw = `{"detail":"${"x".repeat(8200)}"}`;
  const oversizedMarker = restoreUpdateApplyReconciliation(oversizedRaw, submittedAt);
  assert.equal(oversizedMarker.state, "reconciliation_corrupt");
  assert.equal(jsonParseCalls, 0);
  assert.equal(JSON.stringify(oversizedMarker).includes("x".repeat(64)), false);
} finally {
  JSON.parse = originalJsonParse;
}

let storageConversionCalls = 0;
const nonStringStorage = {
  toString() {
    storageConversionCalls += 1;
    throw new Error("must not run");
  },
};
const nonStringMarker = restoreUpdateApplyReconciliation(nonStringStorage, submittedAt);
assert.equal(nonStringMarker.state, "reconciliation_corrupt");
assert.equal(storageConversionCalls, 0);
assert.ok(JSON.stringify(nonStringMarker).length < 8192);
const restoredMarker = restoreUpdateApplyReconciliation(JSON.stringify(nonStringMarker), submittedAt + 1000);
assert.equal(restoredMarker.state, "reconciliation_corrupt");
assert.equal(updateApplyRecheckCanClear(restoredMarker, foreignRunning), false);

console.log("Stage 6.6.0.1.1 authoritative status, object sanitizer, and bounded storage tests passed");
