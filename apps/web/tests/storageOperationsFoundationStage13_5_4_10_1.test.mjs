import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (relative) => fs.readFileSync(resolve(__dirname, "..", relative), "utf8");
const helpers = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${helpers}
this.storageTopHealthModel = storageTopHealthModel;`,
  context
);

const healthyFacts = {
  operations: { status: "available" },
  pathHealth: { available: true, readable: true, writable: true },
  capacity: { total_bytes: 1000, free_bytes: 800, free_percent: 80 },
  policy: { state: "ok", warning_threshold_percent: 10 },
  retention: { last_status: "ok" },
};

const activeRecordingMigrationPreview = {
  blockers: [{ reason: "active_recording_jobs", count: 1 }],
};
const activeRecording = context.storageTopHealthModel({
  ...healthyFacts,
  reconciliation: { evidence_status: "fresh", problem_file_count: 0 },
  migrationPreview: activeRecordingMigrationPreview,
});
assert.equal(activeRecording.status, "ok");
assert.notEqual(activeRecording.status, "migration_blocked");

for (const evidence_status of ["missing", "unknown", "not_checked", "stale", "metadata_only"]) {
  const state = context.storageTopHealthModel({
    ...healthyFacts,
    reconciliation: { evidence_status, problem_file_count: 0 },
  });
  assert.equal(state.status, "unknown", `${evidence_status} must not be rendered as confirmed OK`);
  assert.equal(state.tone, "unknown");
}

const knownProblem = context.storageTopHealthModel({
  ...healthyFacts,
  reconciliation: { evidence_status: "metadata_only", problem_file_count: 2 },
});
assert.equal(knownProblem.status, "reconciliation");
assert.equal(knownProblem.tone, "warning");

const interruptedOperation = context.storageTopHealthModel({
  ...healthyFacts,
  operations: {
    status: "available",
    interrupted_operations: [{ operation_id: "safe-public-id", status: "interrupted" }],
  },
  reconciliation: { evidence_status: "fresh", problem_file_count: 0 },
});
assert.equal(interruptedOperation.status, "operation_interrupted");
assert.equal(interruptedOperation.tone, "warning");
assert.match(interruptedOperation.reason, /прервалась/);

const page = read("app/storage/page.js");
const storageStyles = read("app/styles/40-storage-records-shared.css");
assert.match(page, /apiFetch\("\/storage\/status"\)/);
assert.match(page, /refreshMigrationPreview/);
assert.match(page, /apiFetch\("\/storage\/migration\/preview"/);
assert.doesNotMatch(page, /operations\.migration_preview/);
assert.doesNotMatch(page, /status\?\.migration_preview/);
assert.match(page, /const migrationPreview = migrationPreviewState \|\| \{\};/);
assert.match(page, /if \(silent && statusRef\.current\)/, "silent refresh retains the last valid status");

for (const forbidden of [
  "storageOpsArchiveOperationsV2",
  "storageOpsRetentionScenarioRow",
  "storageOpsIntegrityScenarioRow",
  "storageOpsMigrationScenarioRow",
]) {
  assert.equal(page.includes(forbidden), false, `${forbidden} would be a premature Stage 4.10.5 skeleton`);
}

assert.match(page, /className="storageOpsCheckIcon">✓<\/span>/);
assert.match(page, /className="recordingsUiIcon recordingsTrashIcon recordingsRowSvgIcon storageOpsTrashIcon" viewBox="0 1 24 24"/);
assert.doesNotMatch(page, /settingsUpdateApplyTimelineDot/);
assert.match(storageStyles, /\.storageOpsRootActivateButton\s*\{[\s\S]*?border-width:\s*2px;[\s\S]*?border-color:\s*#cbd5e1;[\s\S]*?color:\s*#94a3b8;/);
assert.match(storageStyles, /\.storageOpsRootActivateButton\.isActive\s*\{[\s\S]*?border-color:\s*#9ee4ba;[\s\S]*?background:\s*#ecfdf3;[\s\S]*?color:\s*#178449;/);
assert.match(storageStyles, /\.storageOpsRootActivateButton,[\s\S]*?\.storageOpsRootDeleteButton\s*\{[\s\S]*?display:\s*inline-flex;[\s\S]*?align-items:\s*center;[\s\S]*?justify-content:\s*center;[\s\S]*?width:\s*30px;[\s\S]*?height:\s*30px;[\s\S]*?padding:\s*0;/);
assert.match(storageStyles, /\.storageOpsSection-roots\s*\{[\s\S]*?--storage-root-spacing:\s*10px;/);
assert.match(storageStyles, /\.storageOpsRootList\s*\{[\s\S]*?gap:\s*var\(--storage-root-spacing,\s*10px\);/);
assert.match(storageStyles, /\.storageOpsSection-roots\s*>\s*\.storageOpsAdvancedRoot\s*\{[\s\S]*?margin-top:\s*var\(--storage-root-spacing\);[\s\S]*?margin-bottom:\s*var\(--storage-root-spacing\);[\s\S]*?padding-top:\s*var\(--storage-root-spacing\);/);
assert.match(storageStyles, /\.storageOpsRootListRow\s*>\s*div:not\(\.storageOpsRootPath\):not\(\.storageOpsRootActionsCell\):not\(\.storageOpsRootDeleteCell\)\s*\{[\s\S]*?grid-template-rows:\s*14px 30px;[\s\S]*?align-content:\s*center;[\s\S]*?align-items:\s*center;/);
