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
  maintenanceStatuses: { ok: "OK", blocked: "Blocked", unknown: "Unknown" },
  maintenanceLabels: {
    pending: "Pending",
    artifacts: "Artifacts",
    current: "Current",
    target: "Target",
    available: "Available",
    backup: "Backup",
    confirm: "Confirm",
    apply: "Apply",
  },
  maintenanceBackupRequired: "Backup required",
  maintenanceBackupNotRequired: "Backup not required",
  maintenanceConfirmationRequired: "Confirmation required",
  maintenanceUnsupported: "Unsupported",
};
const overview = {
  flows: {
    db_adoption: { status: "ok" },
    migration: { status: "blocked" },
  },
};
assert.deepEqual(maintenanceFlowRows(overview).map((row) => row.key), ["db_adoption", "migration", "restore", "update"]);
assert.equal(maintenanceStatusText("blocked", maintenanceText), "Blocked");
assert.equal(maintenanceStatusText("", maintenanceText), "Unknown");
assert.equal(maintenanceStatusClass("ok"), "ok");
assert.equal(maintenanceStatusClass("adoptable"), "warning");
assert.equal(maintenanceStatusClass("queued"), "warning");
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
assert.equal(MAINTENANCE_DRY_RUN_ENDPOINTS.update.path, "/system/update/check");

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
