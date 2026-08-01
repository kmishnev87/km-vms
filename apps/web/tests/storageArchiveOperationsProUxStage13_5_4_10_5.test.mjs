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
this.retentionOperationPresentation = retentionOperationPresentation;
this.autoFreeOperationPresentation = autoFreeOperationPresentation;
this.integrityOperationPresentation = integrityOperationPresentation;
this.migrationOperationPresentation = migrationOperationPresentation;
this.storageMigrationActivitySnapshot = storageMigrationActivitySnapshot;
this.recentOperationPresentation = recentOperationPresentation;`,
  context
);

assert.deepEqual(
  JSON.parse(JSON.stringify(context.retentionOperationPresentation({
    state: "idle",
    configured_camera_count: 4,
    meaningful_rule_camera_count: 4,
    active_camera_count: 3,
    disabled_camera_count: 1,
    last_status: "completed",
    last_finished_at: "2026-07-17T08:00:00Z",
    last_summary: { deleted_count: 2, bytes_freed: 4096 },
  }))),
  {
    status: "healthy",
    tone: "ok",
    configuredCount: 4,
    totalCameraCount: 4,
    incompleteCount: 0,
    nextDueAt: null,
    last: {
      status: "completed",
      at: "2026-07-17T08:00:00Z",
      deletedCount: 2,
      freedBytes: 4096,
      failedCount: 0,
      skippedCount: 0,
      reasonCode: null,
    },
  }
);

assert.equal(context.retentionOperationPresentation({ configured_camera_count: 0 }).status, "not_configured");
assert.equal(context.retentionOperationPresentation({
  configured_camera_count: 4,
  meaningful_rule_camera_count: 3,
  missing_or_invalid_rule_camera_count: 1,
}).status, "incomplete");
assert.equal(context.retentionOperationPresentation({ running: true }).status, "running");
assert.equal(context.retentionOperationPresentation({ state: "failed", configured_camera_count: 2 }).status, "needs_attention");

const autoFreeAcknowledgement = context.autoFreeOperationPresentation({
  configured: true,
  effective: false,
  acknowledgementRequired: true,
  policy: { warning_threshold_percent: 10, cleanup_threshold_percent: 5, recovery_threshold_percent: 9, critical_threshold_percent: 1 },
});
assert.equal(autoFreeAcknowledgement.status, "acknowledgement_required");
assert.equal(autoFreeAcknowledgement.effective, false);
assert.equal(autoFreeAcknowledgement.cleanupPercent, 5);
assert.equal(autoFreeAcknowledgement.recoveryPercent, 9);

assert.equal(context.autoFreeOperationPresentation({ effective: false }).status, "disabled");
assert.equal(context.autoFreeOperationPresentation({}).status, "unknown");
assert.equal(context.autoFreeOperationPresentation({ effective: false, policy: { state: "warning" } }).status, "warning");
assert.equal(context.autoFreeOperationPresentation({ effective: true, policy: { state: "warning" } }).status, "warning");
assert.equal(context.autoFreeOperationPresentation({ effective: true, cleanup: { running: true } }).status, "cleanup");
assert.equal(context.autoFreeOperationPresentation({ effective: true, policy: { state: "recovery" } }).status, "recovery");
assert.equal(context.autoFreeOperationPresentation({
  configured: true,
  effective: true,
  policy: { state: "normal" },
  cleanup: { last_status: "partial" },
}).status, "failed");
assert.equal(context.autoFreeOperationPresentation({
  effective: true,
  policy: { recording_suspended_by_low_disk: true },
}).status, "critical");
const disabledCritical = context.autoFreeOperationPresentation({
  configured: true,
  effective: false,
  policy: { recording_suspended_by_low_disk: true },
});
assert.equal(disabledCritical.status, "critical");
assert.equal(disabledCritical.tone, "error");
assert.equal(disabledCritical.effective, false);
assert.equal(context.autoFreeOperationPresentation({
  configured: false,
  effective: false,
  policy: { state: "critical" },
}).status, "critical");

assert.equal(context.integrityOperationPresentation({ status: "not_run" }).status, "not_run");
assert.equal(context.integrityOperationPresentation({ status: "running", active: true }).status, "running");
assert.equal(context.integrityOperationPresentation({ status: "completed", problem_file_count: 0 }).status, "clean");
assert.equal(context.integrityOperationPresentation({ status: "completed", problem_file_count: 6 }).status, "findings");
const staleClean = context.integrityOperationPresentation({ status: "completed", evidence_status: "stale", problem_file_count: 0 });
assert.equal(staleClean.status, "stale");
assert.equal(staleClean.tone, "warning");
const staleFindings = context.integrityOperationPresentation({ status: "completed", stale: true, problem_file_count: 6 });
assert.equal(staleFindings.status, "stale");
assert.equal(staleFindings.problemCount, 6);
assert.equal(context.integrityOperationPresentation({ status: "running", active: true, stale: true }).status, "running");
assert.equal(context.integrityOperationPresentation({ status: "partial", problem_file_count: 3 }).status, "partial");

assert.equal(context.migrationOperationPresentation({ status: "idle" }, 1).status, "needs_target");
assert.equal(context.migrationOperationPresentation({ status: "running", active: true, percent: 17 }, 2).status, "running");
assert.equal(context.migrationOperationPresentation({ status: "completed", completedProof: true }, 2).status, "completed");
assert.equal(context.migrationOperationPresentation({ status: "partial", cleanupPending: true }, 2).status, "needs_attention");

const activity = context.storageMigrationActivitySnapshot({
  active: true,
  operation: {
    operation_id: "operation-1",
    status: "running",
    progress: { completed_bytes: 50, current_item_bytes: 10, total_bytes: 100, internal_scope: "hidden" },
  },
  plan: { plan_id: "plan-1", status: "running", total_bytes: 100, raw_path: "/internal/archive" },
});
assert.deepEqual(JSON.parse(JSON.stringify(activity)), {
  active: true,
  status: "running",
  operationId: "operation-1",
  completedBytes: 50,
  currentItemBytes: 10,
  totalBytes: 100,
});
assert.equal(context.storageMigrationActivitySnapshot({ active: false }), null);
assert.equal(context.storageMigrationActivitySnapshot({ active: true, operation: { status: "completed" } }), null);

const integrityHistory = context.recentOperationPresentation({
  operation_id: "integrity-scan-operation-1",
  operation_type: "integrity_scan",
  domain_ref: "archive",
  status: "completed",
  progress: { scan_id: "scan-1", checked_count: 12 },
  retry_allowed: true,
  retry_mode: "immediate",
  next_action: "create_new_integrity_scan",
  cancel_allowed: false,
});
assert.equal(integrityHistory.operationId, "integrity-scan-operation-1");
assert.equal(integrityHistory.operationType, "integrity_scan");
assert.equal(integrityHistory.domainRef, "archive");
assert.equal(integrityHistory.scanId, "scan-1");
assert.equal(integrityHistory.actionKind, "integrity");
assert.equal(integrityHistory.retryAllowed, true);
assert.equal(integrityHistory.retryMode, "immediate");
assert.equal(integrityHistory.nextAction, "create_new_integrity_scan");
assert.equal(integrityHistory.cancelAllowed, false);

const migrationHistory = context.recentOperationPresentation({
  operation_id: "migration-operation-1",
  operation_type: "archive_migration_apply",
  status: "partial",
  retry_allowed: true,
});
assert.equal(migrationHistory.actionKind, "migration");
assert.equal(context.recentOperationPresentation({
  operation_id: "completed-migration-operation",
  operation_type: "archive_migration_apply",
  status: "completed",
  retry_allowed: false,
  cancel_allowed: false,
}).actionKind, null);
assert.equal(context.recentOperationPresentation({
  operation_id: "retention-operation-1",
  operation_type: "retention_auto_run",
  status: "completed",
}).actionKind, null);
assert.equal(context.recentOperationPresentation({
  operation_id: "integrity-operation-without-scan",
  operation_type: "integrity_scan",
  status: "completed",
}).actionKind, null);

const page = read("app/storage/page.js");
const center = read("components/storage/ArchiveManagementCenter.js");
const feedback = read("components/OperationFeedback.js");
const layout = read("components/Layout.js");
const css = read("app/styles/40-storage-records-shared.css");
const responsive = read("app/styles/60-responsive-shared.css");
const i18n = read("lib/i18n.js");
const routePermissions = read("lib/routePermissions.js");

assert.match(page, /<ArchiveManagementCenter/);
assert.match(page, /id="storage-archive-root-add"/);
assert.match(page, /<OperationDialog dialog=\{historyDialog\}/);
assert.doesNotMatch(page, /storageOpsSection-recent/);
assert.doesNotMatch(page, /<OperationRow/);
assert.doesNotMatch(page, /window\.(alert|confirm|prompt)/);
assert.match(page, /copy\.archiveManagementProtectionGroup/);
assert.match(page, /useModalBodyScrollLock\(open\)/);
assert.match(page, /const target = busy \? dialogRef\.current : dialogFocusableElements\(dialogRef\.current\)\[0\] \|\| dialogRef\.current/);
assert.match(page, /if \(!busy && activeInsideDialog && activeElement !== dialogRef\.current\) return/);
assert.match(page, /if \(!open\) return <OperationDialog dialog=\{null\} onClose=\{onClose\} \/>;/);
assert.match(page, /archiveManagementIntegrityTitle/);
assert.match(page, /archiveManagementMigrationTitle/);
assert.match(page, /openOperationHistory/);
assert.match(page, /apiFetch\("\/storage\/operations\/history"\)/);
assert.match(page, /storage\/integrity\/scans\/\$\{encodeURIComponent\(requestedScanId\)\}/);
assert.match(center, /groups\.map/);
assert.match(center, /role="switch"/);
assert.match(center, /ArchiveOperationHistoryContent/);
assert.match(center, /operationHistoryUnavailable/);
assert.match(center, /operationHistoryCameraRetention/);
assert.doesNotMatch(center, /onOpenItem\?\.\(item\)/);
assert.match(feedback, /useModalBodyScrollLock/);
assert.match(feedback, /bodyScrollLockCount/);
assert.match(feedback, /document\.body\.style\.overflow = "hidden"/);
assert.match(feedback, /document\.body\.style\.overflow = bodyOverflowBeforeLock/);
assert.match(feedback, /containerRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
assert.match(feedback, /if \(!dialog\?\.busy && activeInsideDialog && activeElement !== containerRef\.current\) return/);
assert.match(layout, /STORAGE_MIGRATION_ACTIVITY_EVENT/);
assert.match(layout, /if \(onStoragePage\)/);
assert.match(layout, /storageMigrationActivitySnapshot\(result\)/);
assert.match(page, /publishStorageMigrationActivity/);
assert.match(css, /"management management"/);
assert.match(css, /\.archiveManagementGroups\s*\{[\s\S]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
assert.match(css, /\.archiveManagementRows\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)[\s\S]*grid-template-rows:\s*repeat\(2, minmax\(0, 1fr\)\)/);
assert.match(css, /\.archiveManagementGroup \+ \.archiveManagementGroup\s*\{[\s\S]*border-left:/);
assert.match(responsive, /"management"/);
assert.match(responsive, /\.button\.archiveManagementHistoryButton,[\s\S]*\.archiveManagementRowAction \.button[\s\S]*width:\s*auto/);

for (const path of [
  "/storage/migration/plans/{plan_id}",
  "/storage/migration/plans/{plan_id}/items",
  "/storage/migration/plans/{plan_id}/cancel",
  "/storage/migration/operations/active",
  "/storage/migration/operations/{operation_id}",
  "/storage/migration/operations/{operation_id}/cancel",
  "/storage/migration/operations/{operation_id}/retry",
  "/storage/migration/operations/{operation_id}/cleanup-takeover",
]) {
  assert.match(routePermissions, new RegExp(path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
}
assert.match(routePermissions, /path: "\/storage\/migration\/apply", permission: "manage_settings\+delete_recordings"/);

for (const key of [
  "archiveManagementTitle",
  "archiveManagementSubtitle",
  "archiveManagementProtectionGroup",
  "archiveManagementMaintenanceGroup",
  "archiveManagementRetentionTitle",
  "archiveManagementAutoFreeTitle",
  "archiveManagementIntegrityTitle",
  "archiveManagementMigrationTitle",
  "operationHistory",
  "operationHistoryTitle",
  "operationHistoryEmpty",
  "operationHistoryUnavailable",
  "operationHistoryOpenIntegrityScan",
  "operationHistoryOpenMigration",
  "archiveManagementRetentionStatusHealthy",
  "archiveManagementAutoFreeStatusEnabled",
  "archiveManagementIntegrityStatusNotRun",
  "archiveManagementIntegrityStatusStale",
  "archiveManagementIntegrityStaleText",
  "archiveManagementMigrationStatusIdle",
]) {
  assert.equal((i18n.match(new RegExp(`${key}:`, "g")) || []).length, 3, `${key} must exist in all locales`);
}

console.log("Stage 13.5 / 4.10.5 archive operations presentation models: PASS");
