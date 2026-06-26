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
  maintenanceDetailRows,
  maintenanceFlowRows,
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
    drift_known_safe: "Known-safe drift",
    update_available: "Update available",
    identity_incomplete: "Identity incomplete",
    installed_identity_drift: "Install drift",
    provider_unavailable: "Provider unavailable",
    installed_newer_than_available: "Installed newer",
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
    drift_known_safe: "Drift localized",
    draft_known_safe: "Draft localized",
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
assert.equal(maintenanceStatusText("drift_known_safe", maintenanceText), "Known-safe drift");
assert.equal(maintenanceStatusText("raw_unknown_code", maintenanceText), "Unknown");
assert.equal(maintenanceStatusClass("ok"), "ok");
assert.equal(maintenanceStatusClass("adoptable"), "warning");
assert.equal(maintenanceStatusClass("queued"), "warning");
assert.equal(maintenanceStatusClass("reconnecting"), "warning");
assert.equal(maintenanceStatusClass("completed"), "ok");
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
assert.equal(formatMaintenanceMessage("drift_known_safe", maintenanceText, "ru"), "Drift localized");
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
