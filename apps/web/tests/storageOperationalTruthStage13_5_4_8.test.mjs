import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import * as storageOperations from "../lib/storageOperations.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const context = storageOperations;

const completedWithoutEvidence = context.activationProgressModel({ status: "completed", completed_steps: [] });
assert.equal(completedWithoutEvidence.steps.every((step) => step.done === false), true, "overall completed must not fabricate step completion");

const running = context.activationProgressModel({
  status: "running",
  current_step: "runtime_applied",
  completed_steps: ["snapshot_created", "recordings_stopping", "recordings_stopped", "root_preflight_checked", "runtime_activation_requested"],
});
assert.equal(running.steps.find((step) => step.key === "recordings_stopped").done, true);
assert.equal(running.steps.find((step) => step.key === "runtime_applied").active, true);
assert.equal(running.steps.find((step) => step.key === "cameras_restored").done, false);

const recovery = context.activationProgressModel({
  status: "failed_recovery_required",
  current_step: "failed_recovery_required",
  rollback_status: "failed",
  effective_active_root_label: "Archive A",
  completed_steps: ["runtime_activation_requested"],
});
assert.equal(recovery.recoveryRequired, true);
assert.equal(recovery.rollback.failed, true);
assert.equal(recovery.effectiveRootLabel, "Archive A");

const staleDiscovery = context.discoveryStateModel({
  freshness: "stale",
  available: false,
  snapshot_id: "old",
  candidates: [{ id: "must-not-be-selectable" }],
});
assert.equal(staleDiscovery.stale, true);
assert.equal(staleDiscovery.current, false);
assert.equal(staleDiscovery.candidates.length, 0);
assert.equal(staleDiscovery.snapshotId, null);

const currentDiscovery = context.discoveryStateModel({
  freshness: "current",
  available: true,
  snapshot_id: "snapshot-current",
  candidates: [{ id: "volume-2", free_bytes: 10, total_bytes: 100 }],
});
assert.equal(currentDiscovery.current, true);
assert.equal(currentDiscovery.candidates.length, 1);
assert.equal(currentDiscovery.snapshotId, "snapshot-current");

const storagePage = fs.readFileSync(resolve(__dirname, "../app/storage/page.js"), "utf8");
const storageCss = fs.readFileSync(resolve(__dirname, "../app/styles/40-storage-records-shared.css"), "utf8");
const camerasPage = fs.readFileSync(resolve(__dirname, "../app/cameras/page.js"), "utf8");
const loadStatusBlock = storagePage.slice(storagePage.indexOf("const loadStatus"), storagePage.indexOf("const loadArchiveRootDiscovery"));
const activateBlock = storagePage.slice(storagePage.indexOf("async function activateRoot"), storagePage.indexOf("function showRootProblems"));

assert.doesNotMatch(loadStatusBlock, /archive-roots\/discovery/, "normal status polling must not trigger host discovery");
assert.match(storagePage, /discovery_snapshot_id: archiveRootDiscoveryModel\.snapshotId/);
assert.match(storagePage, /onToggle=.*loadArchiveRootDiscovery/s, "opening add-root flow must refresh discovery");
assert.doesNotMatch(activateBlock, /completed_steps:\s*\["recordings_stopped"\]/, "frontend must not invent completed activation steps");
assert.doesNotMatch(activateBlock, /message:\s*copy\.rootSwitched/, "frontend must not claim switch success before backend completion");
assert.match(storagePage, /failed_recovery_required/);
assert.match(storagePage, /activationRetryRecovery/);
assert.match(storagePage, /archiveRootDeletePartialTitle/);
assert.match(camerasPage, /runtime\?\.confirmed_recording === true/);
assert.match(camerasPage, /jobState === "recording" && !confirmedRecording/);
assert.match(storageCss, /\.storageOpsProblemList div\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto;/s);
assert.match(storageCss, /\.storageOpsProblemList small,\s*\.storageOpsProblemList p\s*\{[^}]*grid-column:\s*1 \/ -1;[^}]*overflow-wrap:\s*anywhere;/s);
