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
this.archiveIntegrityScanModel = archiveIntegrityScanModel;
this.archiveIntegrityFindingPresentation = archiveIntegrityFindingPresentation;
this.archiveIntegrityCategoryPresentations = archiveIntegrityCategoryPresentations;
this.archiveIntegrityActionContract = archiveIntegrityActionContract;
this.recentOperationPresentation = recentOperationPresentation;`,
  context
);

const allowed = { allowed: true, reason: "" };
for (const [status, expected] of Object.entries({
  not_run: ["integrityScanNotRunTitle", "unknown", false],
  queued: ["integrityScanQueuedTitle", "neutral", true],
  running: ["integrityScanRunningTitle", "neutral", true],
  cancel_requested: ["integrityScanCancelRequestedTitle", "warning", true],
  completed: ["integrityScanCompletedTitle", "ok", false],
  partial: ["integrityScanPartialTitle", "warning", false],
  failed: ["integrityScanFailedTitle", "error", false],
  cancelled: ["integrityScanCancelledTitle", "neutral", false],
  interrupted: ["integrityScanInterruptedTitle", "warning", true],
})) {
  const model = context.archiveIntegrityScanModel({ status, operation: { cancel_allowed: true } }, allowed);
  assert.equal(model.titleKey, expected[0], status);
  assert.equal(model.tone, expected[1], status);
  assert.equal(model.running, expected[2], status);
}

const progress = context.archiveIntegrityScanModel({
  status: "running",
  phase: "filesystem",
  progress: { planned_count: 200, checked_count: 50, found_count: 9, failed_count: 2 },
  operation: { cancel_allowed: true },
}, allowed);
assert.equal(progress.percent, 25);
assert.equal(progress.found, 9);
assert.equal(progress.failed, 2);
assert.equal(progress.phaseKey, "integrityPhaseFilesystem");
assert.equal(progress.canCancel, true);

const denied = context.archiveIntegrityScanModel(
  { status: "not_run" },
  { allowed: false, reason: "human permission text" }
);
assert.equal(denied.canStart, false);
assert.equal(denied.permissionReason, "human permission text");

const finding = context.archiveIntegrityFindingPresentation({
  finding_id: "opaque-internal-id",
  category: "missing_file",
  severity: "error",
  impact_key: "recording_unavailable",
  root_label: "Volume 3",
  camera_name: "Entrance",
  display_name: "/internal/container/kmvms/recordings/segment-1.mkv",
  action_key: "retire_missing_recording",
  action_allowed: true,
  confirmation_level: "destructive_catalog",
  state: "active",
});
assert.equal(finding.categoryKey, "integrityCategoryMissing");
assert.equal(finding.impactKey, "integrityImpactRecordingUnavailable");
assert.equal(finding.actionLabelKey, "integrityActionRetireMissing");
assert.equal(finding.actionAllowed, true);
assert.equal(finding.displayName, "segment-1.mkv");
assert.equal(finding.displayName.includes("/"), false);

const reviewOnly = context.archiveIntegrityFindingPresentation({
  category: "pre_metadata_km_vms_file",
  severity: "warning",
  impact_key: "unindexed_storage_usage",
  required_permission: null,
  no_action_reason: "legacy_file_review_required",
  action_allowed: false,
  state: "active",
});
assert.equal(reviewOnly.actionAllowed, false);
assert.equal(reviewOnly.noActionLabelKey, "integrityNoActionLegacyReview");

const permissionDenied = context.archiveIntegrityFindingPresentation({
  category: "zero_size_file",
  impact_key: "recording_unplayable",
  required_permission: "delete_recordings",
  action_allowed: false,
  state: "active",
});
assert.equal(permissionDenied.permissionDenied, true);
assert.equal(permissionDenied.noActionLabelKey, "integrityNoActionPermission");

const incomplete = context.archiveIntegrityFindingPresentation({
  category: "partial_file",
  impact_key: "recording_incomplete",
  no_action_reason: "incomplete_recording_review_required",
  action_allowed: false,
  state: "active",
});
assert.equal(incomplete.categoryKey, "integrityCategoryActivePartial");
assert.equal(incomplete.impactKey, "integrityImpactRecordingIncomplete");
assert.equal(incomplete.noActionLabelKey, "integrityNoActionIncompleteReview");

const categories = context.archiveIntegrityCategoryPresentations({
  missing_file: 2,
  orphan_file: 7,
  corrupted_file: 1,
  zero: 0,
});
assert.deepEqual(
  JSON.parse(JSON.stringify(categories.map((item) => [item.category, item.count]))),
  [["orphan_file", 7], ["missing_file", 2], ["corrupted_file", 1]]
);
assert.equal(context.archiveIntegrityCategoryPresentations({ missing_file: 14 })[0].count, 14);

assert.deepEqual(
  JSON.parse(JSON.stringify(context.archiveIntegrityActionContract("mark_stale_recording"))),
  { planKind: "metadata", confirmationKey: "integrityConfirmMarkStale", destructive: false }
);
assert.equal(context.archiveIntegrityActionContract("delete_proven_orphan").planKind, "deletion");
assert.equal(context.archiveIntegrityActionContract("delete_proven_orphan").destructive, true);

for (const [operationType, typeKey] of Object.entries({
  integrity_scan: "recentOperationIntegrityScan",
  integrity_metadata_repair: "recentOperationIntegrityRepair",
  integrity_catalog_retirement: "recentOperationIntegrityRetirement",
  integrity_recording_delete: "recentOperationIntegrityRecordingDelete",
  orphan_file_cleanup: "recentOperationOrphanCleanup",
})) {
  assert.equal(context.recentOperationPresentation({ operation_type: operationType }).typeKey, typeKey);
}

const page = read("app/storage/page.js");
const css = read("app/styles/40-storage-records-shared.css");
const i18n = read("lib/i18n.js");
const routes = read("lib/routePermissions.js");

assert.match(page, /function ArchiveIntegrityDialog\(/);
assert.match(page, /integrityOperationPresentation\(reconciliation\)/);
assert.match(page, /copy\.archiveManagementIntegrityFindingsText\.replace\("\{count\}", String\(model\.problemCount\)\)/);
assert.doesNotMatch(page, /normalized\.safeFixCount > 0\) return copy\.integrityProblemsSafe/);
assert.match(page, /apiFetch\("\/storage\/integrity\/scans\/latest"\)/);
assert.match(page, /apiFetch\("\/storage\/integrity\/scans",\s*\{/);
assert.match(page, /\/storage\/integrity\/scans\/\$\{encodeURIComponent\(scanId\)\}\/findings\?/);
assert.match(page, /\/storage\/integrity\/findings\/\$\{encodeURIComponent\(finding\.finding_id\)\}\/\$\{contract\.planKind\}-plan/);
assert.match(page, /\/storage\/integrity\/remediation-plans\/\$\{encodeURIComponent\(integrityPlan\.plan_id\)\}\/apply-\$\{contract\.planKind\}/);
assert.match(page, /setInterval\(poll, 1500\)/);
assert.match(page, /const page = await apiFetch\(`\/storage\/integrity\/scans\/\$\{encodeURIComponent\(scanId\)\}\/findings\?limit=50`\);[\s\S]*setIntegrityFindings\(page\.items \|\| \[\]\);[\s\S]*setIntegrityScan\(next\);[\s\S]*} else \{[\s\S]*setIntegrityScan\(next\);/);
assert.match(page, /setIntegrityFindings\(\(current\) => append \? \[\.\.\.current, \.\.\.\(page\.items \|\| \[\]\)\]/);
assert.match(page, /role="dialog"/);
assert.match(page, /aria-modal="true"/);
assert.match(page, /storageIntegrityConfirm/);
assert.match(page, /integrityConfirmationAcknowledge/);
assert.doesNotMatch(page, /runReconciliationPreview/);
assert.doesNotMatch(page, /applyReconciliationSafe/);
assert.doesNotMatch(page, /mode:\s*"apply_safe"/);
assert.doesNotMatch(page, />\s*6\s*</);

const integrityActions = page.slice(page.indexOf("async function prepareIntegrityAction"), page.indexOf("async function refreshMigrationPreview"));
assert.equal(integrityActions.includes("window.confirm"), false);

assert.match(css, /\.storageIntegrityDialog\s*\{/);
assert.match(css, /max-height:\s*min\(820px, calc\(100vh - 36px\)\)/);
assert.match(css, /\.storageIntegrityDialogBody[\s\S]*overflow-y:\s*auto/);
assert.match(css, /@media \(max-width: 760px\)[\s\S]*\.storageIntegrityDialog[\s\S]*height:\s*100dvh/);
assert.match(css, /\.storageIntegrityFindingAction[\s\S]*width:\s*min\(240px, 28vw\)/);

for (const endpoint of [
  "/storage/integrity/scans/{scan_id}/cancel",
  "/storage/integrity/remediation-plans/{plan_id}/apply-metadata",
  "/storage/integrity/remediation-plans/{plan_id}/apply-deletion",
]) {
  assert.equal(routes.includes(`\"${endpoint}\"`), true, endpoint);
}

for (const key of [
  "integrityModalTitle",
  "integrityScanNotRunTitle",
  "integrityScanRunningTitle",
  "integrityScanCompletedTitle",
  "integrityScanPartialTitle",
  "integrityScanFailedTitle",
  "integrityScanCancelledTitle",
  "integrityScanStaleText",
  "integrityCategoryMissing",
  "integrityCategoryProvenOrphan",
  "integrityImpactRecordingUnavailable",
  "integrityImpactRecordingIncomplete",
  "integrityActionRetireMissing",
  "integrityNoActionLegacyReview",
  "integrityNoActionPermission",
  "integrityNoActionIncompleteReview",
  "integrityConfirmRetireMissing",
  "integrityConfirmDeleteUnusable",
  "integrityResultCompleted",
  "recentOperationIntegrityScan",
  "recentOperationIntegrityRetirement",
  "recentOperationOrphanCleanup",
]) {
  assert.equal((i18n.match(new RegExp(`${key}:`, "g")) || []).length, 3, `${key} must exist in ru/en/zh-CN`);
}

assert.match(page, /<ArchiveManagementCenter/);
assert.match(page, /<OperationDialog dialog=\{historyDialog\}/);
assert.doesNotMatch(page, /storageOpsRetentionScenarioRow/);
assert.doesNotMatch(page, /storageOpsMigrationScenarioRow/);
