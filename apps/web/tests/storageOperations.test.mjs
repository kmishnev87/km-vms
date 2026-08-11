import assert from "node:assert/strict";
import * as storageOperations from "../lib/storageOperations.js";

const context = storageOperations;

assert.equal(context.formatBytes(0), "0 B");
assert.equal(context.formatBytes(1536), "1.5 KB");
assert.equal(context.formatPercent(9.456), "9.46%");
assert.equal(context.statusLabel("critical"), "Критично");
assert.equal(context.statusLabel("critical", "zh-CN"), "严重");

const offText = context.lowDiskPolicyText({
  auto_free_space_cleanup_enabled: false,
  warning_threshold_percent: 10,
  cleanup_threshold_percent: 5,
  critical_threshold_percent: 1,
});
assert.equal(offText.includes("только предупреждает"), true);
assert.equal(offText.includes("Автоосвобождение выключено"), true);
assert.equal(offText.includes("не разрешает удаление без явного включения"), true);

const onText = context.lowDiskPolicyText({
  auto_free_space_cleanup_enabled: true,
  warning_threshold_percent: 10,
  cleanup_threshold_percent: 5,
  critical_threshold_percent: 1,
});
assert.equal(onText.includes("принадлежащие KM VMS"), true);
assert.equal(onText.includes("метаданными"), true);
assert.equal(onText.includes("owned"), false);
assert.equal(onText.includes("metadata-safe"), false);
assert.equal(onText.includes("приостановлена"), true);

assert.match(context.humanBlockerReason("active_recording_jobs"), /идет запись/);
assert.match(context.humanBlockerReason("active_recording_jobs", "en"), /recording is active/);
assert.match(context.humanBlockerReason("namespace_missing", "en"), /namespace folder is missing/);
assert.match(context.humanBlockerReason("archive_root_not_writable", "en"), /not writable/);
assert.equal(context.factLabel(undefined), "Не проверено");
assert.equal(context.factTone(undefined), "unknown");
assert.equal(context.factLabel(false), "Нет");
assert.equal(context.factTone(false), "error");
assert.equal(context.accessRightsModel({ readable: true, writable: true }).label, "Права на чтение и запись: есть");
assert.equal(context.accessRightsModel({ readable: true, writable: false }).label, "Чтение есть, запись недоступна");
assert.match(context.primaryStorageActionText({ pathHealth: { available: false } }), /не хватает фактов/);
assert.match(context.primaryStorageActionText({ pathHealth: { readable: false, writable: true, available: false } }), /Корень архива недоступен|права чтения/);
assert.doesNotMatch(context.primaryStorageActionText({ operations: { status: "available" }, capacity: { total_bytes: 1 }, pathHealth: { readable: true, writable: true, available: false } }), /недоступен/);
assert.equal(context.freeSpaceTone({ free_percent: 77 }, { state: "warning", warning_threshold_percent: 10 }), "warning");
assert.equal(context.freeSpaceTone({ free_percent: 77 }, { warning_threshold_percent: 10 }), "neutral");
assert.match(context.primaryStorageActionText({ pathHealth: { readable: true, writable: false, available: true } }), /права записи/);
assert.match(context.primaryStorageActionText({ capacity: { total_bytes: 100, free_percent: 2 }, pathHealth: { readable: true, writable: true, available: true }, policy: { warning_threshold_percent: 10 } }), /Освободите место/);
assert.match(context.primaryStorageActionText({ pathHealth: { readable: true, writable: true, available: true }, reconciliation: { problem_file_count: 1 } }), /целостности/);
const recurringStatusWithMigrationBlocker = context.primaryStorageActionText({
  pathHealth: { readable: true, writable: true, available: true },
  migrationPreview: { blockers: [{ reason: "active_recording_jobs" }] },
});
assert.doesNotMatch(recurringStatusWithMigrationBlocker, /активной записью/);
assert.match(recurringStatusWithMigrationBlocker, /не хватает фактов/);
assert.match(context.primaryStorageActionText({ operations: {}, pathHealth: {}, capacity: {} }), /не хватает фактов/);

const reconciliation = context.normalizeReconciliationSummary({
  classification_counts: { missing_file: 2, orphan_file: 4, ok_owned_finalized: 10 },
  cleanup_candidates: { count: 4, classification_counts: { orphan_file: 4 } },
  apply_safe_summary: { updated_metadata_count: 0 },
  total_metadata_rows_checked: 12,
});
assert.equal(reconciliation.problemCount, 6);
assert.equal(reconciliation.reviewOnlyCount, 4);
assert.equal(reconciliation.totalRows, 12);

const rows = context.cameraStorageRows([
  { camera_id: 1, camera_name: "A", size_bytes: 100, segment_count: 1 },
  { camera_id: 2, camera_name: "Long camera name", size_bytes: 500, segment_count: 2 },
]);
assert.equal(rows[0].camera_id, 2);
assert.equal(JSON.stringify(rows).includes("rtsp://"), false);
assert.equal(JSON.stringify(rows).includes("Authorization"), false);
