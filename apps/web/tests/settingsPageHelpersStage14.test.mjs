import assert from "node:assert/strict";
import {
  AUDIT_LIMIT,
  HARDWARE_OPTIONS,
  MAINTENANCE_DRY_RUN_ENDPOINTS,
  UTC_TIMEZONES,
  auditLabel,
  auditMessage,
  auditTarget,
  backendLabel,
  formatAuditTimestamp,
  formatMaintenanceMessage,
  formatUpdateNotice,
  formatBytes,
  hardwareOptionState,
  humanErrorText,
  languageOf,
  maintenanceBackupCheckResultText,
  maintenanceBackupOperationResultText,
  maintenanceDetailRows,
  maintenanceFlowRows,
  maintenanceBackupManagerModel,
  maintenanceReadinessRows,
  maintenanceWarningModel,
  maintenanceStatusClass,
  maintenanceStatusText,
  payloadFromDraft,
  profileFromFormat,
  recordingFormatForProfile,
  roleLabel,
  roleOptionsFor,
  safeMetadataRows,
  samePayload,
  settingsDraftFromApi,
  sortedUsersForTable,
  timezoneValueForSettings,
  buildUpdateApplyConfirmation,
  shortCommit,
  updateApplyEffectiveStatus,
  updateApplyFactRows,
  updateApplyTechnicalRows,
  updateApplyIsRunning,
  updateApplyRecoveryText,
  updateApplyStepRows,
  updateApplyButtonText,
  userCanBeDeleted,
  userCanBeManaged,
} from "../lib/settingsPageHelpers.js";

assert.equal(AUDIT_LIMIT, 50);
assert.deepEqual(HARDWARE_OPTIONS, ["auto", "qsv", "amf", "nvenc", "cpu", "vaapi"]);
assert.equal(UTC_TIMEZONES.some((zone) => zone.value === "UTC" && zone.offset === 0), true);

assert.equal(recordingFormatForProfile("compatibility"), "mp4");
assert.equal(recordingFormatForProfile("reliability"), "mkv");
assert.equal(profileFromFormat("mp4"), "compatibility");
assert.equal(profileFromFormat("mkv"), "reliability");
assert.equal(timezoneValueForSettings("Europe/Moscow"), "Europe/Moscow");
assert.equal(timezoneValueForSettings(""), "UTC");

const apiSettings = {
  system_name: "Demo",
  timezone: "Europe/Moscow",
  language: "en",
  recording_format: "mp4",
  hardware_preferred_backend: "qsv",
};
const draft = settingsDraftFromApi(apiSettings);
assert.equal(draft.system_name, "Demo");
assert.equal(Object.hasOwn(draft, "archive_primary_path"), false);
assert.equal(draft.recordingProfile, "compatibility");
assert.equal(languageOf(draft), "en");
assert.deepEqual(payloadFromDraft(draft), {
  system_name: "Demo",
  timezone: "Europe/Moscow",
  language: "en",
  recording_format: "mp4",
  hardware_preferred_backend: "qsv",
});
assert.equal(samePayload(draft, { ...draft }), true);
assert.equal(samePayload(draft, { ...draft, recordingProfile: "reliability" }), false);

assert.equal(formatBytes(-1), "-");
assert.equal(formatBytes(1024 ** 3), "1.0 GB");
assert.equal(formatBytes(2 * 1024 ** 4), "2.0 TB");
assert.equal(formatAuditTimestamp("not-a-date", "ru"), "not-a-date");

const maintenanceText = {
  maintenanceStatuses: {
    ok: "OK",
    blocked: "Blocked",
    complete: "Complete",
    valid: "Verified",
    verified: "Verified",
    drift_known_safe: "No critical issues",
    update_available: "Update available",
    identity_incomplete: "Identity incomplete",
    installed_identity_drift: "Install drift",
    provider_unavailable: "Provider unavailable",
    installed_newer_than_available: "Installed newer",
    rebuilding: "Rebuilding",
    health_check: "Health check",
    commit_verification: "Commit check",
    running: "Running",
    pending: "Pending",
    unknown: "Unknown",
  },
  maintenanceMessageFallback: "Safe fallback",
  maintenanceActionFallback: "Action fallback",
  maintenanceMessageLabels: {
    schema_metadata_valid: "Schema localized",
    schema_current_no_pending_migrations: "Migration localized",
    restore_no_valid_artifacts: "Restore localized",
    update_apply_not_available_for_release: "Update apply localized",
    maintenance_history_limited: "History localized",
    drift_known_safe: "No critical issues localized",
    draft_known_safe: "Draft localized",
  },
  maintenanceReadinessTitles: {
    db_identity: "Database identity",
    db_schema: "Database schema",
    backup_restore_check: "Backup restore check",
  },
  maintenanceOperatorStatuses: {
    ok: "Healthy",
    attention: "Check",
    blocked: "Needs attention",
    unavailable: "No data",
    action_available: "Action available",
  },
  maintenanceOperatorSummaries: {
    db_identity_ok: "Database is recognized.",
    db_schema_current: "Schema is current.",
    backup_restore_no_artifacts: "No backup copies.",
  },
  maintenanceOperatorActions: {
    db_identity_check_optional: "No action.",
    migration_check_optional: "No action.",
    backup_restore_create_backup_first: "Create backup first.",
    check_status: "Check status.",
  },
  maintenanceCheckActions: {
    db_adoption: "Check DB",
    migration: "Check migrations",
    restore: "Check backup",
  },
  maintenanceFactLabels: {
    metadata_present: "Metadata",
    current_version: "Current",
    target_version: "Target",
    pending_count: "Pending",
    valid_artifacts: "Copies",
    temporary_validation: "Temporary",
    current_product_restore: "Production restore",
  },
  maintenanceBackupsTitle: "Backups",
  maintenanceBackupCopyOne: "{count} copy",
  maintenanceBackupCopyMany: "{count} copies",
  maintenanceBackupNoCopies: "No copies",
  maintenanceBackupStatusEmpty: "No backups yet",
  maintenanceBackupStatusReady: "{count} backups are available.",
  maintenanceBackupRestoreAvailable: "Restore is available",
  maintenanceBackupRestoreUnavailable: "Restore is unavailable",
  maintenanceBackupRestoreUnavailableReason: "Production restore is not enabled.",
  maintenanceBackupStatuses: {
    valid: "Verified",
    verified: "Verified",
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
    available: "Backup is available",
    no_artifacts: "Nothing to check",
    blocked: "Check blocked",
    invalid: "Backup invalid",
    check_failed: "Check failed",
    fallback: "Check status received",
  },
  maintenanceBackupOperationLabels: {
    check: "Check",
    create: "Create",
    delete: "Delete",
  },
  maintenanceBackupCreateStatuses: {
    verified: "Backup created",
    blocked: "Create blocked",
    fallback: "Create status received",
  },
  maintenanceBackupDeleteStatuses: {
    deleted: "Backup deleted",
    deleted_with_missing_files: "Backup deleted with already-missing files",
    blocked: "Delete blocked",
    fallback: "Delete status received",
  },
  maintenanceWarningGroups: {
    actionable: "Need action",
    support: "Support",
    informational: "Information",
  },
  maintenanceWarningLabels: {
    backup_status_source_unavailable: {
      title: "Backup status unavailable",
      summary: "Status is not available.",
      action: "Open backups.",
    },
  },
  maintenanceWarningsFallback: {
    title: "Warning",
    summary: "Safe warning.",
    action: "Open diagnostics.",
  },
  maintenanceLabels: {
    pending: "Pending",
    artifacts: "Artifacts",
    current: "Current",
    target: "Target",
    available: "Available",
    backup: "Backup",
    confirm: "Confirm",
    apply: "Apply",
    installedCommit: "Installed commit",
    source: "Source",
    targetCommit: "Target commit",
    verification: "Commit check",
    releaseTitle: "Release",
    releaseSummary: "Changes",
    status: "Status",
    gitHead: "Git HEAD",
    metadataSource: "Metadata",
    provider: "Provider",
  },
  maintenanceBackupRequired: "Backup required",
  maintenanceBackupNotRequired: "Backup not required",
  maintenanceConfirmationRequired: "Confirmation required",
  maintenanceUnsupported: "Unsupported",
  updateApplyConfirm: "Start update?",
  updateApplyConfirmRestart: "Services may restart.",
  updateApplyRecoveryAvailable: "Available",
  updateApplyRecoveryBlocked: "Blocked",
  updateApplyRecoveryCommitMismatch: "Mismatch",
  updateApplyRecoveryCompleted: "Completed",
  updateApplyRecoveryCurrent: "Current",
  updateApplyRecoveryFailed: "Failed",
  updateApplyRecoveryReconnecting: "Reconnecting",
  updateApplyRecoveryRunning: "Running",
  updateApplyRecoveryUnknown: "Unknown",
  updateApplyRecoveryIdentity: "Identity",
  updateApplyRecoveryProvider: "Provider",
  updateApplyRecoveryInstalledNewer: "Installed newer",
  updateApplyButtonRebuilding: "Rebuilding",
  updateApplyButtonHealth: "Health check",
  updateApplyButtonVerification: "Commit check",
  updateCommitPending: "Pending",
  updateCommitUnavailable: "Unavailable",
  updateCommitVerified: "Verified",
  updateWarningGeneric: "Generic safe warning",
  updateWarningLabels: {
    source_metadata_invalid: "Source metadata localized",
    update_metadata_invalid: "Update metadata localized",
    requires_migration: "Migration localized",
    trusted_manifest_not_configured: "Manifest localized",
    commit_mismatch: "Commit mismatch localized",
    trusted_commit_missing: "Trusted commit localized",
  },
};
const overview = {
  flows: {
    db_adoption: { status: "ok" },
    migration: { status: "blocked" },
  },
};
assert.deepEqual(maintenanceFlowRows(overview).map((row) => row.key), ["db_adoption", "migration", "restore"]);
assert.equal(maintenanceStatusText("blocked", maintenanceText), "Blocked");
assert.equal(maintenanceStatusText("", maintenanceText), "Unknown");
assert.equal(maintenanceStatusText("complete", maintenanceText), "Complete");
assert.equal(maintenanceStatusText("valid", maintenanceText), "Verified");
assert.equal(maintenanceStatusText("drift_known_safe", maintenanceText), "No critical issues");
assert.equal(maintenanceStatusText("raw_unknown_code", maintenanceText), "Unknown");
assert.equal(maintenanceStatusClass("ok"), "ok");
assert.equal(maintenanceStatusClass("adoptable"), "warning");
assert.equal(maintenanceStatusClass("attention"), "warning");
assert.equal(maintenanceStatusClass("unavailable"), "warning");
assert.equal(maintenanceStatusClass("queued"), "warning");
assert.equal(maintenanceStatusClass("reconnecting"), "warning");
assert.equal(maintenanceStatusClass("completed"), "ok");
assert.equal(maintenanceStatusClass("valid"), "ok");
assert.equal(maintenanceStatusClass("verified"), "ok");
assert.equal(maintenanceStatusClass("failed"), "blocked");
assert.equal(maintenanceStatusClass("blocked"), "blocked");
assert.deepEqual(
  maintenanceDetailRows({
    backup_required: true,
    requires_confirmation: true,
    can_apply: false,
    details: { pending_count: 2, valid_artifact_count: 1, artifact_count: 3, current_version: "1", target_version: "2" },
  }, maintenanceText),
  [["Pending", 2], ["Artifacts", "1/3"], ["Current", "1"], ["Target", "2"], ["Backup", "Backup required"]]
);
assert.equal(MAINTENANCE_DRY_RUN_ENDPOINTS.update, undefined);
const readinessRows = maintenanceReadinessRows({
  flows: {
    db_adoption: {
      status: "already_adopted",
      presentation: {
        user_status: "ok",
        title_key: "db_identity",
        summary_key: "db_identity_ok",
        operator_action_key: "db_identity_check_optional",
        can_check: true,
        support_report_available: true,
        facts: [{ key: "metadata_present", value: true }],
      },
    },
    migration: {
      status: "current",
      presentation: {
        user_status: "ok",
        title_key: "db_schema",
        summary_key: "db_schema_current",
        operator_action_key: "migration_check_optional",
        can_check: true,
        facts: [{ key: "pending_count", value: 0 }],
      },
    },
    restore: {
      status: "no_artifacts",
      presentation: {
        user_status: "unavailable",
        title_key: "backup_restore_check",
        summary_key: "backup_restore_no_artifacts",
        operator_action_key: "backup_restore_create_backup_first",
        can_check: true,
        facts: [
          { key: "valid_artifacts", value: "0/0" },
          { key: "current_product_restore", value: false },
        ],
      },
    },
  },
}, maintenanceText);
assert.deepEqual(readinessRows.map((row) => row.key), ["db_adoption", "migration"]);
assert.equal(readinessRows[0].title, "Database identity");
assert.equal(readinessRows[0].summary, "Database is recognized.");
assert.equal(readinessRows[0].showCheck, false);
const backupManager = maintenanceBackupManagerModel({
  flows: {
    restore: {
      status: "available",
      details: {
        valid_artifact_count: 1,
        artifact_count: 1,
        current_product_restore_supported: false,
        artifacts: [
          {
            artifact_id: "kmvms-db-20260704T010203Z-abcdef123456",
            artifact_created_at: "2026-07-04T01:02:03Z",
            artifact_schema_version: 7,
            db_backend: "sqlite",
            file_size: 2048,
            validation_status: "verified",
            valid: true,
            deletable: true,
            delete_supported: true,
          },
        ],
      },
    },
  },
}, maintenanceText, "en");
assert.equal(backupManager.countText, "1 copy");
assert.equal(backupManager.statusText, "1 backups are available.");
assert.equal(backupManager.restoreSupported, false);
assert.equal(backupManager.artifacts[0].canDelete, true);
assert.equal(backupManager.artifacts[0].sizeText, "2.0 KB");
assert.doesNotMatch(backupManager.countText, /\/1/);
assert.equal(maintenanceBackupCheckResultText("valid", maintenanceText), "Check passed");
assert.equal(maintenanceBackupCheckResultText("verified", maintenanceText), "Check passed");
assert.equal(maintenanceBackupCheckResultText("available", maintenanceText), "Backup is available");
assert.equal(maintenanceBackupCheckResultText("no_artifacts", maintenanceText), "Nothing to check");
assert.equal(maintenanceBackupCheckResultText("check_failed", maintenanceText), "Check failed");
assert.equal(maintenanceBackupOperationResultText({ kind: "check", status: "valid" }, maintenanceText).text, "Check passed");
assert.deepEqual(maintenanceBackupOperationResultText({ kind: "create", status: "verified" }, maintenanceText), {
  kind: "create",
  label: "Create",
  text: "Backup created",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({ kind: "delete", status: "deleted" }, maintenanceText), {
  kind: "delete",
  label: "Delete",
  text: "Backup deleted",
  showReason: false,
});
assert.deepEqual(maintenanceBackupOperationResultText({ kind: "delete", status: "deleted_with_missing_files" }, maintenanceText), {
  kind: "delete",
  label: "Delete",
  text: "Backup deleted with already-missing files",
  showReason: false,
});
const warningModel = maintenanceWarningModel({
  upgrade_report: {
    warnings_count: 1,
    warning_groups: { actionable: 0, support: 0, informational: 1 },
    warnings: [{ code: "backup_status_source_unavailable", classification: "informational" }],
  },
}, maintenanceText);
assert.equal(warningModel.total, 1);
assert.equal(warningModel.groups.informational, 1);
assert.equal(warningModel.items[0].title, "Backup status unavailable");
assert.equal(updateApplyIsRunning("rebuilding"), true);
assert.equal(updateApplyEffectiveStatus({ status: "update_available" }, { status: "rebuilding" }, "fetch failed"), "reconnecting");
assert.equal(updateApplyEffectiveStatus({}, { status: "completed", expected_commit: "abc", commit_verified: false }, ""), "failed");
assert.equal(shortCommit("1234567890abcdef"), "1234567890ab...");
const updateStatus = {
  status: "update_available",
  installed_release: {
    version: "0.7.0",
    title: "Installed release",
    commit: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    metadata_source: "official_update",
  },
  available_release: {
    version: "0.7.1",
    title: "Public release identity",
    summary: "Readable update status",
    commit: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    provider: "public_github_release",
  },
  comparison: { status: "update_available" },
  evidence: { git_head: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" },
  installed_build: { app_version: "0.7.0", git_commit: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" },
  latest_release: { version: "0.7.1", commit: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", git_ref: "stable" },
};
assert.deepEqual(updateApplyFactRows(updateStatus, { expected_commit: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", commit_verified: false }, maintenanceText), [
  ["Current", "0.7.0"],
  ["Available", "0.7.1"],
  ["Release", "Public release identity"],
  ["Changes", "Readable update status"],
  ["Status", "Update available"],
  ["Commit check", "Pending"],
  ["Current step", "Update available"],
  ["Last progress", "-"],
  ["Elapsed", "-"],
]);
assert.deepEqual(updateApplyTechnicalRows(updateStatus, { expected_commit: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" }, maintenanceText), [
  ["Source", "stable"],
  ["Installed commit", "aaaaaaaaaaaa..."],
  ["Target commit", "bbbbbbbbbbbb..."],
  ["Git HEAD", "aaaaaaaaaaaa..."],
  ["Metadata", "official_update"],
  ["Provider", "public_github_release"],
]);
assert.equal(updateApplyRecoveryText("completed", { expected_commit: "b", commit_verified: true }, maintenanceText), "Completed");
assert.equal(updateApplyRecoveryText("blocked", {}, maintenanceText), "Blocked");
assert.equal(updateApplyRecoveryText("failed", {}, maintenanceText), "Failed");
assert.equal(updateApplyRecoveryText("rebuilding", {}, maintenanceText), "Running");
assert.equal(updateApplyRecoveryText("reconnecting", {}, maintenanceText), "Reconnecting");
assert.equal(updateApplyRecoveryText("current", {}, maintenanceText), "Current");
assert.equal(updateApplyRecoveryText("update_available", {}, maintenanceText), "Available");
assert.equal(updateApplyRecoveryText("identity_incomplete", {}, maintenanceText), "Identity");
assert.equal(updateApplyRecoveryText("provider_unavailable", {}, maintenanceText), "Provider");
assert.equal(updateApplyRecoveryText("installed_newer_than_available", {}, maintenanceText), "Installed newer");
assert.equal(updateApplyRecoveryText("not-a-real-status", {}, maintenanceText), "Unknown");
assert.equal(updateApplyButtonText({ status: "rebuilding", current_step: "rebuilding" }, maintenanceText), "Rebuilding");
assert.equal(updateApplyButtonText({ status: "health_check", current_step: "health_check" }, maintenanceText), "Health check");
assert.deepEqual(updateApplyStepRows({ steps: [{ name: "rebuilding", status: "running" }, { name: "health_check", status: "pending" }] }, maintenanceText), [
  { name: "rebuilding", label: "Rebuilding", status: "running", statusLabel: "Running" },
  { name: "health_check", label: "Health check", status: "pending", statusLabel: "Pending" },
]);
assert.match(buildUpdateApplyConfirmation(maintenanceText, updateStatus), /Target commit: bbbbbbbbbbbb\.\.\./);
assert.equal(formatUpdateNotice({ code: "source_metadata_invalid", message: "Installed source metadata is unavailable or invalid." }, maintenanceText, "ru"), "Source metadata localized");
assert.equal(formatUpdateNotice({ message: "Installed source metadata is unavailable or invalid." }, maintenanceText, "ru"), "Source metadata localized");
assert.equal(formatUpdateNotice({ message: "Last update metadata is unavailable or invalid." }, maintenanceText, "ru"), "Update metadata localized");
assert.equal(formatUpdateNotice({ code: "release_requires_migration", message: "Release requires migration support outside Stage 6.0.8." }, maintenanceText, "ru"), "Migration localized");
assert.equal(formatUpdateNotice({ code: "manifest_not_configured" }, maintenanceText, "ru"), "Manifest localized");
assert.equal(formatUpdateNotice({ code: "commit_mismatch" }, maintenanceText, "en"), "Commit mismatch localized");
assert.equal(formatUpdateNotice({ message: "Unknown backend warning in English." }, maintenanceText, "ru"), "Generic safe warning");
assert.equal(formatUpdateNotice({ message: "Unknown backend warning in English." }, maintenanceText, "zh-CN"), "Generic safe warning");
assert.equal(formatMaintenanceMessage("Schema metadata is already valid.", maintenanceText, "ru"), "Schema localized");
assert.equal(formatMaintenanceMessage("Schema is current; no pending migrations.", maintenanceText, "ru"), "Migration localized");
assert.equal(formatMaintenanceMessage("No valid restore artifacts are available in configured backup root.", maintenanceText, "ru"), "Restore localized");
assert.equal(formatMaintenanceMessage("update_apply_not_available_for_release", maintenanceText, "ru"), "Update apply localized");
assert.equal(formatMaintenanceMessage("No durable maintenance action history is available beyond current status and generated upgrade report summary.", maintenanceText, "ru"), "History localized");
assert.equal(formatMaintenanceMessage("drift_known_safe", maintenanceText, "ru"), "No critical issues localized");
assert.equal(formatMaintenanceMessage("draft_known_safe", maintenanceText, "ru"), "Draft localized");
assert.equal(formatMaintenanceMessage("unexpected_raw_backend_message", maintenanceText, "ru"), "Safe fallback");
assert.equal(formatMaintenanceMessage("unexpected_raw_backend_message", maintenanceText, "zh-CN", "action"), "Action fallback");

assert.equal(auditMessage({ message_ru: "Привет", message_en: "Hello" }, "en"), "Hello");
assert.equal(auditLabel("severity", "warning", "en"), "Warning");
assert.equal(auditTarget({ target_type: "camera", target_name: "Front" }, { journalTargetEmpty: "none" }), "camera: Front");
assert.deepEqual(safeMetadataRows({ a: "x", b: { c: 1 } }), [{ key: "a", value: "x" }, { key: "b", value: "{\"c\":1}" }]);
assert.equal(humanErrorText(JSON.stringify({ detail: [{ msg: "Bad input" }] }), "Fallback"), "Bad input");

assert.equal(backendLabel("qsv", "en"), "Intel Quick Sync / QSV");
assert.equal(hardwareOptionState("auto", {}, { failedValidation: "failed", notDetected: "missing" }).selectable, true);
assert.deepEqual(hardwareOptionState("qsv", { available_backends: ["qsv"] }, { failedValidation: "failed", notDetected: "missing" }), { selectable: true, reason: "" });
assert.deepEqual(hardwareOptionState("nvenc", { backend_status: { nvenc: { candidate: true, reason: "no device" } } }, { failedValidation: "failed", notDetected: "missing" }), { selectable: false, reason: "no device" });

const users = [
  { id: 2, username: "viewer", role: "viewer", is_active: true },
  { id: 1, username: "owner", role: "owner", is_active: true },
  { id: 3, username: "admin", role: "admin", is_active: true },
];
assert.deepEqual(sortedUsersForTable(users).map((user) => user.role), ["owner", "admin", "viewer"]);
assert.equal(roleLabel("operator", { roleOwner: "Owner", roleAdmin: "Admin", roleOperator: "Operator", roleViewer: "Viewer" }), "Operator");
assert.deepEqual(roleOptionsFor({ role: "owner" }), ["admin", "operator", "viewer"]);
assert.equal(userCanBeManaged({ role: "admin" }, { role: "viewer" }), true);
assert.equal(userCanBeManaged({ role: "admin" }, { role: "owner" }), false);
assert.equal(userCanBeDeleted({ id: 3, role: "admin" }, { id: 2, role: "viewer" }, users), true);
assert.equal(userCanBeDeleted({ id: 3, role: "admin" }, { id: 3, role: "admin" }, users), false);
