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
this.retentionScenarioModel = retentionScenarioModel;
this.humanBlockerReason = humanBlockerReason;
this.policyStateLabel = policyStateLabel;
this.recentOperationPresentation = recentOperationPresentation;
this.recentOperationPresentations = recentOperationPresentations;`,
  context
);

assert.equal(
  context.retentionScenarioModel({ retention: { state: "pending" } }).status,
  "pending"
);
assert.equal(
  context.retentionScenarioModel({ retention: { state: "blocked" } }).status,
  "apply_failed"
);
assert.equal(
  context.retentionScenarioModel({ retention: { state: "interrupted" } }).status,
  "apply_failed"
);
assert.match(
  context.humanBlockerReason("auto_free_space_acknowledgement_required", "ru"),
  /подтверд/i
);
assert.match(
  context.humanBlockerReason("retention_no_safe_candidates", "ru"),
  /безопасно удалить/i
);
assert.match(
  context.humanBlockerReason("physical_volume_identity_unknown", "en"),
  /physical volume/i
);
assert.equal(
  context.policyStateLabel({ auto_free_space_cleanup_enabled: true, auto_free_space_cleanup_effective: false }, "ru"),
  "Выключено"
);

const completedRetention = context.recentOperationPresentation({
  operation_id: "retention-1",
  operation_type: "retention_auto_run",
  status: "completed",
  progress: {
    completed_count: 14,
    completed_bytes: 4096,
    failed_count: 0,
    skipped_count: 0,
  },
  finished_at: "2026-07-13T08:30:00",
});
assert.equal(completedRetention.typeKey, "recentOperationRetentionAutomatic");
assert.equal(completedRetention.statusKey, "recentOperationStatusCompleted");
assert.equal(completedRetention.tone, "ok");
assert.equal(completedRetention.timestamp, "2026-07-13T08:30:00");
assert.deepEqual(
  JSON.parse(JSON.stringify(completedRetention.facts)),
  [
    { labelKey: "recentOperationDeletedCount", value: 14 },
    { labelKey: "recentOperationFreedBytes", value: 4096, format: "bytes" },
  ]
);

const unknownOperation = context.recentOperationPresentation({
  operation_id: "future-1",
  operation_type: "future_archive_operation",
  status: "future_status",
});
assert.equal(unknownOperation.typeKey, "recentOperationGeneric");
assert.equal(unknownOperation.statusKey, "recentOperationStatusUnknown");
assert.equal(unknownOperation.reasonCode, null);
assert.equal(unknownOperation.nextActionKey, null);
assert.deepEqual(JSON.parse(JSON.stringify(unknownOperation.facts)), []);

const boundedRecent = context.recentOperationPresentations(
  Array.from({ length: 12 }, (_, index) => ({
    operation_id: `operation-${index}`,
    operation_type: "archive_root_activation",
    status: index === 0 ? "failed" : "completed",
    reason_code: index === 0 ? "storage_operation_conflict" : null,
    next_action: index === 0 ? "retry_operation" : null,
  })),
  20
);
assert.equal(boundedRecent.length, 5);
assert.equal(boundedRecent[0].statusKey, "recentOperationStatusFailed");
assert.equal(boundedRecent[0].nextActionKey, "recentOperationNextRetry");

const page = read("app/storage/page.js");
const archiveManagement = read("components/storage/ArchiveManagementCenter.js");
const i18n = read("lib/i18n.js");
const css = read("app/styles/40-storage-records-shared.css");

assert.match(page, /function requestAutoFreeSpace\(nextEnabled\)/);
assert.match(page, /autoFreeAcknowledgementRequired/);
assert.match(page, /requestBody\.auto_free_space_acknowledgement\s*=\s*\{/);
assert.match(page, /acknowledged:\s*true/);
assert.match(page, /terms_version:\s*autoFreeTermsVersion/);
assert.match(page, /busy:\s*true/);
assert.match(page, /dismissible:\s*false/);
assert.match(page, /await apiFetch\("\/settings"/);
assert.match(page, /await loadStatus\(\{ silent: true \}\)/);
assert.match(page, /href="\/cameras">\{copy\.configureCameras\}<\/a>/);
assert.match(page, /actionPermissionState\(currentUser, "manage_cameras", language\)/);
assert.match(page, /disabled title=\{manageCamerasPermission\.reason\}/);
assert.match(page, /retentionOperationPresentation\(retention\)/);
assert.match(helpers, /retention\?\.active_camera_count/);
assert.match(helpers, /retention\?\.disabled_camera_count/);
assert.match(helpers, /retention\?\.retained_deleted_camera_count/);
assert.match(helpers, /retention\?\.missing_or_invalid_rule_camera_count/);
assert.match(helpers, /retention\?\.next_due_at/);
assert.match(page, /nextEnabled && autoFreeAcknowledgementRequired/);
assert.match(page, /onChange=\{requestAutoFreeSpace\}/);
assert.doesNotMatch(page, /autoFreeMessage/);
assert.doesNotMatch(page, /setAutoFreeMessage/);
assert.doesNotMatch(page, /runRetentionPreview/);
assert.doesNotMatch(page, /applyRetentionPlan/);
assert.doesNotMatch(page, /\/recordings\/retention\/dry-run/);
assert.doesNotMatch(page, /\/recordings\/retention\/run/);
assert.match(page, /recentOperationPresentations\(recent\.items, 5\)/);
assert.match(page, /<ArchiveManagementCenter/);
assert.match(page, /<OperationDialog dialog=\{historyDialog\}/);
assert.match(archiveManagement, /export function ArchivePolicySwitch/);
assert.match(archiveManagement, /role="switch"/);
assert.match(archiveManagement, /export function ArchiveOperationHistoryContent/);
assert.doesNotMatch(page, /storageOpsSection-recent/);
assert.doesNotMatch(page, /item\.title\s*\|\|/);
assert.doesNotMatch(page, /item\.summary\s*\|\|/);
assert.doesNotMatch(page, /statusLabel\(item\.type/);

function lastCssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [...css.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "g"))];
  assert.ok(matches.length > 0, `missing CSS rule for ${selector}`);
  return matches.at(-1)[1];
}

assert.match(lastCssRule(".storageOpsSection-archiveManagement"), /grid-area:\s*management/);
assert.match(css, /\.archiveManagementRows\s*\{[\s\S]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
assert.match(lastCssRule(".archiveManagementRows"), /grid-template-columns:\s*1fr/);
assert.match(css, /\.archiveManagementRow\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\) auto/);
assert.match(lastCssRule(".archiveManagementRow"), /grid-template-columns:\s*1fr/);
assert.match(lastCssRule(".archiveManagementHistoryList"), /display:\s*grid/);
assert.match(lastCssRule(".archiveManagementHistoryItem"), /border-bottom:\s*1px solid/);

for (const key of [
  "autoFreeConfirmTitle",
  "autoFreeConfirmMessage",
  "autoFreeConfirmThresholds",
  "autoFreeConfirmIrreversible",
  "autoFreeThresholdSummary",
  "confirmationRequired",
  "configureCameras",
  "retentionScope",
  "retentionScopeValue",
  "retentionRulesMissing",
  "nextCheck",
  "recentOperationsCount",
  "recentOperationGeneric",
  "recentOperationRetentionAutomatic",
  "recentOperationAutoFree",
  "recentOperationRootActivation",
  "recentOperationRootDelete",
  "recentOperationStatusCompleted",
  "recentOperationStatusPartial",
  "recentOperationStatusBlocked",
  "recentOperationStatusFailed",
  "recentOperationStatusUnknown",
  "recentOperationDeletedCount",
  "recentOperationFreedBytes",
  "recentOperationNextRetry",
]) {
  assert.equal((i18n.match(new RegExp(`${key}:`, "g")) || []).length, 3, `${key} must exist in ru/en/zh-CN`);
}

assert.doesNotMatch(page, /storageOpsRetentionScenarioRow/);
assert.doesNotMatch(page, /storageOpsAutoFreeScenarioRow/);
