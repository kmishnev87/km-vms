import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  createUpdateApplyPending,
  reconcileUpdateApplyPending,
  restoreUpdateApplyPending,
} from "../lib/settingsPageHelpers.js";


const here = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(here, "..");
const controllerSource = fs.readFileSync(
  resolve(webRoot, "lib/settingsMaintenanceController.js"),
  "utf8",
);
const surfaceSource = fs.readFileSync(
  resolve(webRoot, "components/SettingsMaintenanceSurface.js"),
  "utf8",
);

const modalStart = surfaceSource.indexOf(
  "export function SettingsMaintenanceModal",
);
const modalEnd = surfaceSource.indexOf(
  "export function SettingsUpdateApplyDialog",
);
assert.ok(modalStart >= 0 && modalEnd > modalStart);
const modalSource = surfaceSource.slice(modalStart, modalEnd);
const controllerReturnStart = controllerSource.lastIndexOf("\n  return {");
assert.ok(controllerReturnStart >= 0);
const controllerReturn = controllerSource.slice(controllerReturnStart);

assert.match(
  controllerSource,
  /const updateApplyHasUnknownLaunch = Boolean\(updateApplyPending\);/,
);
assert.match(controllerReturn, /\bupdateApplyHasUnknownLaunch,\s*\n/);
assert.match(
  modalSource,
  /updateApplyOperator,\s*\n\s*updateApplyHasUnknownLaunch,\s*\n\s*updateApplyRunning,/,
);
assert.match(
  modalSource,
  /updateApplyRunning \|\| updateApplyHasUnknownLaunch \|\| updateApplyOperator\.stateUnknown/,
);
assert.doesNotMatch(modalSource, /\bupdateApplyPending\b/);
assert.doesNotMatch(surfaceSource, /sessionStorage/);

const submittedAtMs = 100_000;
const submissionId = "11111111-1111-4111-8111-111111111111";
const targetCommit = "b".repeat(40);
const pending = createUpdateApplyPending(
  submissionId,
  { version: "0.8.17", commit: targetCommit },
  submittedAtMs,
);
assert.ok(pending);

const noPending = restoreUpdateApplyPending(null, submittedAtMs + 500);
assert.equal(Boolean(noPending), false, "no pending request must render normally");

const unresolved = restoreUpdateApplyPending(
  JSON.stringify(pending),
  submittedAtMs + 500,
);
assert.equal(
  Boolean(unresolved),
  true,
  "a restored unresolved submission must expose only the derived unknown-launch fact",
);

const terminalFailed = reconcileUpdateApplyPending(unresolved, {
  schema_version: 1,
  request_id: "request-failed",
  submission_id: submissionId,
  status: "failed_rolled_back",
  effective_status: "failed_rolled_back",
  phase: "failed_rolled_back",
  current_step: "rollback_verified",
  expected_commit: targetCommit,
  commit_verified: false,
  error: { category: "target_identity_mismatch" },
  is_stale: false,
  updated_at: "2026-08-15T07:00:00Z",
  admission: {
    schema_version: 3,
    authority: "active",
    state: "admitted",
    active: true,
    submission_id: submissionId,
    request_id: "request-failed",
    target_commit: targetCommit,
  },
}, submittedAtMs + 1_000);
assert.equal(terminalFailed.outcome, "accepted");
const pendingAfterTerminal = terminalFailed.outcome === "accepted"
  ? null
  : terminalFailed.record;
assert.equal(
  Boolean(pendingAfterTerminal),
  false,
  "a stored terminal failure must clear the transient pending presentation fact",
);

console.log("settings maintenance pending presentation reliability: PASS");
