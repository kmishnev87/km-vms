import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import * as facade from "../lib/settingsPageHelpers.js";
import * as owner from "../lib/settingsUpdateApplyHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const facadeSource = fs.readFileSync(resolve(__dirname, "../lib/settingsPageHelpers.js"), "utf8");
const ownerSource = fs.readFileSync(resolve(__dirname, "../lib/settingsUpdateApplyHelpers.js"), "utf8");
const sharedSource = fs.readFileSync(resolve(__dirname, "../lib/settingsPageSharedHelpers.js"), "utf8");

const movedExports = [
  "UPDATE_APPLY_POLL_INTERVAL_MS",
  "UPDATE_APPLY_RUNNING_STATUSES",
  "buildUpdateApplyConfirmation",
  "formatDurationSeconds",
  "formatUpdateNotice",
  "shortCommit",
  "updateApplyButtonText",
  "updateApplyCandidateSnapshot",
  "updateApplyEffectiveStatus",
  "updateApplyErrorMessages",
  "updateApplyFactRows",
  "updateApplyIsRunning",
  "updateApplyOperatorModel",
  "updateApplyProgressText",
  "updateApplyReconnectTiming",
  "updateApplyRecoveryText",
  "updateApplyStepRows",
  "updateApplyTechnicalRows",
  "updateApplyTransportPhase",
  "updateApplyTrustedCandidateRelease",
];

assert.equal(facadeSource.includes('from "./settingsUpdateApplyHelpers.js"'), true);
assert.equal(facadeSource.includes('from "./settingsPageSharedHelpers.js"'), true);
assert.equal(ownerSource.includes("settingsPageHelpers"), false, "update owner must not import its facade");
assert.equal(sharedSource.includes("settingsPageHelpers"), false, "shared leaf must not import the facade");

for (const name of movedExports) {
  assert.equal(name in facade, true, `${name} must remain exported by the facade`);
  assert.equal(name in owner, true, `${name} must be owned by the update module`);
  assert.equal(facade[name], owner[name], `${name} facade binding must be the owner binding`);
  const declaration = new RegExp(`(?:export\\s+)?(?:function|const)\\s+${name}\\b`);
  assert.equal(declaration.test(facadeSource), false, `${name} implementation must not remain in the facade`);
  assert.equal(declaration.test(ownerSource), true, `${name} implementation must exist in the update owner`);
}

assert.deepEqual(owner.UPDATE_APPLY_RUNNING_STATUSES, [
  "queued", "starting_helper", "preflight", "acquire_source", "downloading", "extracting",
  "validating_source", "overlay", "applying", "compose_config", "rebuilding", "restarting",
  "health_check", "commit_verification", "preparing", "staging", "activating", "reconnecting",
  "rolling_back",
]);
assert.equal(owner.UPDATE_APPLY_POLL_INTERVAL_MS, 5000);

for (const pendingName of [
  "createUpdateApplyPending",
  "sanitizeUpdateApplyPending",
  "updateApplyPendingExactMatch",
  "restoreUpdateApplyPending",
  "reconcileUpdateApplyPending",
]) {
  assert.match(facadeSource, new RegExp(`export function ${pendingName}\\b`));
  assert.equal(ownerSource.includes(pendingName), false, `${pendingName} must stay outside presentation owner`);
}

assert.equal(facade.maintenanceStatusText("ok", { maintenanceStatuses: { ok: "OK" } }), "OK");
assert.equal(typeof facade.formatMaintenanceMessage, "function");
