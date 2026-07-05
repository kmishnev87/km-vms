import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  maintenanceBackupCheckResultText,
  maintenanceBackupOperationResultText,
  maintenanceBackupManagerModel,
  maintenanceWarningModel,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");

assert.equal(settingsPage.includes("settingsMaintenanceBackupManager"), true);
assert.equal(settingsPage.includes("settingsMaintenanceBackupCreate"), false);
assert.equal(settingsPage.includes("maintenanceConfirm"), true);
assert.equal(settingsPage.includes("/system/restore/artifacts/${encodeURIComponent(artifact.id)}/delete"), true);
assert.equal(settingsPage.includes("/system/restore/apply"), false);
assert.equal(settingsPage.includes("window.confirm(t.maintenanceBackupCreateConfirm)"), false);
assert.equal(settingsPage.includes("window.confirm(maintenanceBackupDeleteConfirm)"), false);
assert.equal(settingsPage.includes("maintenanceWarningsOpen"), true);

const t = {
  maintenanceBackupNoCopies: "No backups",
  maintenanceBackupCopyOne: "{count} backup",
  maintenanceBackupCopyMany: "{count} backups",
  maintenanceBackupStatusEmpty: "No backups yet.",
  maintenanceBackupStatusReady: "Backups: {count}",
  maintenanceBackupRestoreAvailable: "Restore is available",
  maintenanceBackupRestoreUnavailable: "Restore is unavailable",
  maintenanceBackupRestoreUnavailableReason: "Only safe checks are available.",
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

const model = maintenanceBackupManagerModel({
  flows: {
    restore: {
      status: "available",
      details: {
        valid_artifact_count: 1,
        artifact_count: 2,
        current_product_restore_supported: false,
        artifacts: [
          {
            artifact_id: "kmvms-db-20260704T010203Z-abcdef123456",
            artifact_created_at: "2026-07-04T01:02:03Z",
            artifact_schema_version: 7,
            db_backend: "sqlite",
            file_size: 4096,
            validation_status: "verified",
            valid: true,
            deletable: true,
            delete_supported: true,
          },
          {
            artifact_id: "broken.manifest.json",
            artifact_created_at: "",
            validation_status: "invalid",
            valid: false,
            deletable: false,
            delete_supported: false,
          },
        ],
      },
    },
  },
}, t, "en");

assert.equal(model.total, 2);
assert.equal(model.valid, 1);
assert.equal(model.problem, 1);
assert.equal(model.countText, "2 backups");
assert.equal(model.statusText, "Backups: 2");
assert.equal(model.restoreSupported, false);
assert.equal(model.artifacts[0].canDelete, true);
assert.equal(model.artifacts[1].canDelete, false);
assert.equal(model.artifacts[0].sizeText, "4.0 KB");
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
