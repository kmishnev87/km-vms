import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  BACKUP_OPERATION_PENDING_STORAGE_KEY,
  createBackupOperationPending,
  maintenanceBackupCheckResultText,
  maintenanceBackupOperationResultText,
  maintenanceBackupManagerModel,
  maintenanceWarningModel,
  restoreBackupOperationPending,
  sanitizeBackupOperationPending,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");
const settingsCss = fs.readFileSync(resolve(webRoot, "app/styles/20-settings-maintenance.css"), "utf8");

assert.equal(settingsPage.includes("settingsMaintenanceBackupManager"), true);
assert.equal(settingsPage.includes('className="settingsMaintenanceBackupCreate"'), false);
assert.equal(settingsPage.includes("maintenanceConfirm"), true);
assert.equal(settingsPage.includes("/system/restore/artifacts/${encodeURIComponent(artifact.id)}/delete"), true);
assert.equal(settingsPage.includes("/system/restore/apply"), true);
assert.equal(settingsPage.includes("/system/restore/dry-run"), false);
assert.equal(settingsPage.includes("/system/backup/operations/${encodeURIComponent(pending.submissionId)}"), true);
assert.equal(settingsPage.includes("BACKUP_OPERATION_PENDING_STORAGE_KEY"), true);
assert.equal(BACKUP_OPERATION_PENDING_STORAGE_KEY, "km_vms_backup_operation_pending_v1");
assert.equal(settingsPage.includes("window.confirm(t.maintenanceBackupCreateConfirm)"), false);
assert.equal(settingsPage.includes("window.confirm(maintenanceBackupDeleteConfirm)"), false);
assert.equal(settingsPage.includes("maintenanceWarningsOpen"), true);
assert.equal(settingsPage.includes("const MAINTENANCE_BACKUP_PAGE_SIZE = 5;"), true);
assert.equal(settingsPage.includes('className="storageOpsCheckIcon">✓</span>'), true);
assert.equal(settingsPage.includes('className="recordingsUiIcon recordingsTrashIcon recordingsRowSvgIcon storageOpsTrashIcon"'), true);
assert.equal(settingsPage.includes('className="settingsMaintenanceMiniButton danger"'), true);
assert.equal(settingsPage.includes("<strong>{artifact.createdAt}</strong>"), false);
assert.equal(settingsPage.includes('className="settingsMaintenanceBackupCreatedAt"'), true);
assert.equal(settingsPage.includes('className="settingsMaintenanceBackupMeta"'), true);
assert.equal(settingsPage.includes('className="settingsMaintenanceBackupDetailRow"'), true);
assert.equal(settingsPage.includes('className="settingsMaintenanceIconAction"'), true);
assert.equal(settingsPage.includes('aria-hidden="true">←</span>'), true);
assert.equal(settingsPage.includes('aria-hidden="true">→</span>'), true);
assert.match(settingsCss, /\.settingsMaintenanceBackupItemHead\s*\{[\s\S]*?display:\s*flex;[\s\S]*?align-items:\s*baseline;/);
assert.match(settingsCss, /\.settingsMaintenanceBackupDetailRow\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto;[\s\S]*?align-items:\s*center;/);
const pendingCommitBlock = settingsPage.slice(
  settingsPage.indexOf("function commitBackupOperationPending"),
  settingsPage.indexOf("function backupOperationFallback"),
);
assert.equal(pendingCommitBlock.includes("if (safeRecord) return null;"), true);
assert.equal(
  pendingCommitBlock.indexOf("window.sessionStorage.setItem") < pendingCommitBlock.indexOf("maintenanceBackupPendingRef.current"),
  true,
);
const operationBlock = settingsPage.slice(
  settingsPage.indexOf("async function performMaintenanceBackupOperation"),
  settingsPage.indexOf("async function runUpdateCheck"),
);
assert.notEqual(operationBlock.indexOf("commitBackupOperationPending(pending)"), -1);
assert.notEqual(operationBlock.indexOf("apiFetch(endpoint"), -1);
assert.equal(
  operationBlock.indexOf("commitBackupOperationPending(pending)") < operationBlock.indexOf("apiFetch(endpoint"),
  true,
);
const overviewLoadBlock = settingsPage.slice(
  settingsPage.indexOf("async function loadMaintenanceOverview"),
  settingsPage.indexOf("async function loadMaintenanceBackupPage"),
);
assert.equal(overviewLoadBlock.includes("setMaintenanceBackupResult(null)"), false);

const t = {
  maintenanceBackupNoCopies: "No backups",
  maintenanceBackupCopyOne: "{count} backup",
  maintenanceBackupCopyMany: "{count} backups",
  maintenanceBackupStatusEmpty: "No backups yet.",
  maintenanceBackupStatusReady: "Backups: {count}",
  maintenanceBackupRestoreAvailable: "Restore is available",
  maintenanceBackupRestoreUnavailable: "Restore is unavailable",
  maintenanceBackupRestoreUnavailableReason: "Only safe checks are available.",
  maintenanceBackupAvailabilityStatuses: {
    available: "Available",
    incomplete: "Incomplete",
    missing: "Missing",
    unsafe: "Unsafe",
    unknown: "Unknown",
  },
  maintenanceBackupIntegrityStatuses: {
    not_checked: "Not checked",
    verified: "Verified",
    failed: "Failed",
    stale_evidence: "Needs recheck",
    unknown: "Unknown",
  },
  maintenanceBackupCompatibilityStatuses: {
    compatible: "Compatible",
    migration_required: "Migration required",
    newer_than_supported: "Newer than supported",
    unsupported_backend: "Unsupported backend",
    unknown: "Unknown",
  },
  maintenanceBackupValidationStatuses: {
    not_performed: "Not performed",
    passed: "Passed",
    failed: "Failed",
    stale_evidence: "Needs recheck",
    unknown: "Unknown",
  },
  maintenanceBackupStatuses: {
    verified: "Ready",
    valid: "Ready",
    available: "Available",
    no_artifacts: "No backups",
    blocked: "Check blocked",
    invalid: "Problem",
    check_failed: "Check failed",
    unknown: "Unknown",
  },
  maintenanceBackupCheckStatuses: {
    valid: "Check passed",
    verified: "Check passed",
    available: "Backup is available to check",
    no_artifacts: "Nothing to check yet",
    blocked: "Check is blocked",
    invalid: "Backup is damaged or incomplete",
    check_failed: "Check failed",
    fallback: "Check status received",
  },
  maintenanceBackupCheckOutcomes: {
    integrity_verified_migration_required: "Integrity is verified. Trial restore was not run because this backup requires a compatible migration.",
    integrity_failed: "Backup integrity could not be verified: the backup is damaged or incomplete.",
    restore_failed: "Backup integrity is verified, but the trial restore failed.",
  },
  maintenanceBackupOperationLabels: {
    check: "Check",
    create: "Create",
    delete: "Delete",
  },
  maintenanceBackupCreateStatuses: {
    verified: "Backup was created",
    blocked: "Create blocked",
    fallback: "Create status received",
  },
  maintenanceBackupDeleteStatuses: {
    deleted: "Backup was deleted",
    deleted_with_missing_files: "Backup was deleted with missing files",
    blocked: "Delete blocked",
    fallback: "Delete status received",
  },
  maintenanceWarningGroups: {
    actionable: "Need action",
    support: "Support",
    informational: "Information",
  },
  maintenanceWarningLabels: {
    video_archive_restore_not_covered: {
      title: "Video archive is not included",
      summary: "Only database metadata is backed up here.",
      action: "Keep video archive backup separate.",
    },
  },
  maintenanceWarningsFallback: {
    title: "Warning",
    summary: "A service warning is present.",
    action: "Open diagnostics if support asks.",
  },
};

const overview = {
  flows: {
    restore: {
      status: "available",
      details: {
        current_product_restore_supported: false,
        temporary_validation_restore_supported: true,
      },
    },
  },
};
const backupStatus = {
  total_count: 25,
  total_bytes: 8192,
  valid_artifact_count: 7,
  offset: 10,
  limit: 10,
  has_more: true,
  current_product_restore_supported: false,
  temporary_validation_restore_supported: true,
  artifacts: [
    {
      artifact_id: "kmvms-db-20260704T010203Z-abcdef123456",
      artifact_created_at: "2026-07-04T01:02:03Z",
      artifact_schema_version: 7,
      db_backend: "sqlite",
      file_size: 4096,
      availability_status: "available",
      integrity_status: "stale_evidence",
      compatibility_status: "migration_required",
      restore_validation_status: "not_performed",
      delete_status: "allowed",
      checked_at: "2026-07-04T02:02:03Z",
      delete_supported: true,
    },
    {
      artifact_id: "kmvms-db-20260703T010203Z-fedcba654321",
      artifact_created_at: "2026-07-03T01:02:03Z",
      artifact_schema_version: 7,
      db_backend: "sqlite",
      file_size: 4096,
      availability_status: "incomplete",
      integrity_status: "failed",
      compatibility_status: "unknown",
      restore_validation_status: "failed",
      delete_status: "blocked",
      validated_at: "2026-07-03T02:02:03Z",
      delete_supported: false,
    },
  ],
};
const model = maintenanceBackupManagerModel(overview, t, "en", backupStatus);

assert.equal(model.total, 25);
assert.equal(model.valid, 7);
assert.equal(model.countText, "25 backups");
assert.equal(model.statusText, "Backups: 25");
assert.equal(model.totalBytesText, "8.0 KB");
assert.equal(model.restoreSupported, false);
assert.equal(model.artifacts[0].canDelete, true);
assert.equal(model.artifacts[1].canDelete, false);
assert.equal(model.artifacts[0].canCheck, true);
assert.equal(model.artifacts[1].canCheck, true);
assert.equal(model.artifacts[0].integrity, "stale_evidence");
assert.equal(model.artifacts[0].integrityTone, "attention");
assert.equal(model.artifacts[0].hasProblem, false);
assert.equal(model.artifacts[1].hasProblem, true);
assert.notEqual(model.artifacts[0].checkedAt, "");
assert.notEqual(model.artifacts[1].validatedAt, "");
assert.equal(model.offset, 10);
assert.equal(model.limit, 10);
assert.equal(model.pageStart, 11);
assert.equal(model.pageEnd, 12);
assert.equal(model.hasPrevious, true);
assert.equal(model.hasMore, true);
assert.equal(model.artifacts.length, 2);
assert.equal(model.artifacts[0].sizeText, "4.0 KB");
const unownedProblem = maintenanceBackupManagerModel(overview, t, "en", {
  total_count: 1,
  temporary_validation_restore_supported: true,
  artifacts: [{
    artifact_id: "broken.manifest",
    availability_status: "unsafe",
    integrity_status: "failed",
    delete_status: "allowed",
    delete_supported: true,
  }],
});
assert.equal(unownedProblem.artifacts[0].canCheck, false);
assert.equal(unownedProblem.artifacts[0].canDelete, false);
assert.doesNotMatch(model.countText, /\d+\/\d+/);
assert.doesNotMatch(model.statusText, /\d+\/\d+/);
assert.equal(maintenanceBackupCheckResultText("valid", t), "Check passed");
assert.equal(maintenanceBackupCheckResultText("available", t), "Backup is available to check");
assert.equal(maintenanceBackupCheckResultText("no_artifacts", t), "Nothing to check yet");
assert.equal(maintenanceBackupCheckResultText("check_failed", t), "Check failed");
assert.notEqual(maintenanceBackupCheckResultText("valid", t), "Unknown");
assert.deepEqual(maintenanceBackupOperationResultText({ kind: "check", status: "valid" }, t), {
  kind: "check",
  label: "Check",
  text: "Check passed",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({ kind: "create", status: "verified" }, t), {
  kind: "create",
  label: "Create",
  text: "Backup was created",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({ kind: "delete", status: "deleted_with_missing_files" }, t), {
  kind: "delete",
  label: "Delete",
  text: "Backup was deleted with missing files",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({
  kind: "check",
  state: "completed",
  result: { status: "validated" },
}, t), {
  kind: "check",
  label: "Check",
  text: "Check status received",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({
  kind: "check",
  state: "failed",
  phase: "preflight_failed",
  reason: "migration_required",
  result: {
    status: "check_failed",
    integrity_status: "verified",
    compatibility_status: "migration_required",
    restore_validation_status: "not_performed",
  },
}, t), {
  kind: "check",
  label: "Check",
  text: "Integrity is verified. Trial restore was not run because this backup requires a compatible migration.",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({
  kind: "check",
  state: "failed",
  result: {
    status: "check_failed",
    integrity_status: "failed",
    compatibility_status: "unknown",
    restore_validation_status: "not_performed",
  },
}, t), {
  kind: "check",
  label: "Check",
  text: "Backup integrity could not be verified: the backup is damaged or incomplete.",
  showReason: true,
});
assert.deepEqual(maintenanceBackupOperationResultText({
  kind: "check",
  state: "failed",
  result: {
    status: "check_failed",
    integrity_status: "verified",
    compatibility_status: "compatible",
    restore_validation_status: "failed",
  },
}, t), {
  kind: "check",
  label: "Check",
  text: "Backup integrity is verified, but the trial restore failed.",
  showReason: true,
});

const nowMs = 2_000_000_000_000;
const submissionId = "123e4567-e89b-42d3-a456-426614174000";
const artifactId = "kmvms-db-20260704T010203Z-abcdef123456";
const pending = createBackupOperationPending("check", artifactId, submissionId, nowMs - 1000);
assert.deepEqual(pending, {
  schema: 1,
  submissionId,
  kind: "check",
  artifactId,
  createdAtMs: nowMs - 1000,
});
assert.deepEqual(sanitizeBackupOperationPending(pending, nowMs), pending);
assert.deepEqual(restoreBackupOperationPending(JSON.stringify(pending), nowMs), pending);
assert.equal(createBackupOperationPending("create", "", submissionId, nowMs)?.artifactId, null);
assert.equal(createBackupOperationPending("delete", "../foreign", submissionId, nowMs), null);
assert.equal(restoreBackupOperationPending("{broken", nowMs), null);
assert.equal(restoreBackupOperationPending("x".repeat(1025), nowMs), null);
assert.equal(restoreBackupOperationPending(JSON.stringify({ ...pending, schema: 2 }), nowMs), null);
assert.equal(restoreBackupOperationPending(JSON.stringify(pending), nowMs + (25 * 60 * 60 * 1000)), null);

const warnings = maintenanceWarningModel({
  upgrade_report: {
    warnings_count: 6,
    warning_groups: { actionable: 0, support: 2, informational: 4 },
    warnings: [
      { code: "video_archive_restore_not_covered", classification: "informational", severity: "warning" },
      { code: "unknown_future_warning", classification: "support", severity: "warning" },
    ],
  },
}, t);

assert.equal(warnings.total, 6);
assert.equal(warnings.groups.actionable, 0);
assert.equal(warnings.groups.support, 2);
assert.equal(warnings.groups.informational, 4);
assert.equal(warnings.items[0].title, "Video archive is not included");
assert.equal(warnings.items[1].title, "Warning");
assert.equal(settingsPage.includes("settingsMaintenanceSupportStatus"), true);
assert.equal(settingsPage.includes("maintenanceSupportStatusOk"), true);
assert.equal(settingsPage.includes("<dt>{t.maintenanceWarningActionable}</dt>"), false);
