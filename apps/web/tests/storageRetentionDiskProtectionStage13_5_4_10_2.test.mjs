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
assert.match(page, /retention\.active_camera_count/);
assert.match(page, /retention\.disabled_camera_count/);
assert.match(page, /retention\.retained_deleted_camera_count/);
assert.match(page, /retention\.missing_or_invalid_rule_camera_count/);
assert.match(page, /retention\.next_due_at/);
assert.match(page, /function operationReasonText\(operation, copy, language\)/);
assert.match(page, /operation\?\.last_error \? humanBlockerReason\(operation\.last_error, language\)/);
assert.match(page, /autoFreeConfigured && autoFreeAcknowledgementRequired/);
assert.match(page, /onClick=\{\(\) => requestAutoFreeSpace\(false\)\}/);
assert.doesNotMatch(page, /autoFreeMessage/);
assert.doesNotMatch(page, /setAutoFreeMessage/);
assert.doesNotMatch(page, /runRetentionPreview/);
assert.doesNotMatch(page, /applyRetentionPlan/);
assert.doesNotMatch(page, /\/recordings\/retention\/dry-run/);
assert.doesNotMatch(page, /\/recordings\/retention\/run/);
assert.match(page, /recentOperationPresentations\(recent\.items, 5\)/);
assert.match(page, /<details className="storageOpsSection storageOpsSection-secondary storageOpsSection-recent">/);
assert.doesNotMatch(page, /item\.title\s*\|\|/);
assert.doesNotMatch(page, /item\.summary\s*\|\|/);
assert.doesNotMatch(page, /statusLabel\(item\.type/);

function lastCssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [...css.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "g"))];
  assert.ok(matches.length > 0, `missing CSS rule for ${selector}`);
  return matches.at(-1)[1];
}

assert.match(lastCssRule(".storageOpsSection-recent"), /padding:\s*0/);
assert.match(lastCssRule(".storageOpsSection-recent > .storageOpsSectionHead"), /width:\s*100%/);
assert.match(lastCssRule(".storageOpsRecentSummary"), /display:\s*grid/);
assert.match(lastCssRule(".storageOpsRecentSummary"), /grid-template-columns:\s*auto minmax\(0, 1fr\) auto/);
assert.match(lastCssRule(".storageOpsRecentSummary > h2"), /width:\s*auto/);
assert.match(lastCssRule(".storageOpsSection-recent > .storageOpsRecent"), /margin:\s*12px 14px 14px/);
assert.match(lastCssRule(".storageOpsRecentTitle"), /font-weight:\s*400/);
assert.match(lastCssRule(".storageOpsRecentPrimary .storageOpsStatusPill"), /font-weight:\s*500/);
assert.match(lastCssRule(".storageOpsRecentItem"), /border-radius:\s*0/);

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

for (const forbidden of [
  "storageOpsArchiveOperationsV2",
  "storageOpsRetentionScenarioRow",
  "storageOpsAutoFreeScenarioRow",
]) {
  assert.equal(page.includes(forbidden), false, `${forbidden} would be a premature Stage 4.10.5 skeleton`);
}
