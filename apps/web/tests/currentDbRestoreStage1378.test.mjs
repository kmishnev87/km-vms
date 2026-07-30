import assert from "node:assert/strict";
import fs from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";


const webRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const page = fs.readFileSync(
  resolve(webRoot, "app/settings/page.js"),
  "utf8",
);
const css = fs.readFileSync(
  resolve(webRoot, "app/styles/20-settings-maintenance.css"),
  "utf8",
);
const restoreStart = page.indexOf(
  "async function requestCurrentDatabaseRestore",
);
const restoreEnd = page.indexOf(
  "function backupOperationFallback",
  restoreStart,
);
const restoreFlow = page.slice(restoreStart, restoreEnd);
const dialogStart = page.indexOf(
  "id: `current-db-restore-",
);
const dialogEnd = page.indexOf(
  "onClose={closeCurrentRestoreDialog}",
  dialogStart,
);
const dialog = page.slice(dialogStart, dialogEnd);


assert.ok(restoreStart > 0);
assert.ok(restoreEnd > restoreStart);
assert.match(
  restoreFlow,
  /apiFetch\("\/system\/restore\/current\/preflight"/,
);
assert.match(
  restoreFlow,
  /apiFetch\("\/system\/restore\/current\/apply"/,
);
assert.match(
  page,
  /apiFetch\("\/system\/restore\/current\/status"/,
);
assert.match(page, /CURRENT_RESTORE_CONFIRMATION_PHRASE = "RESTORE KM VMS"/);
assert.doesNotMatch(restoreFlow, /\bconfirm\s*\(/);

const pendingWrite = restoreFlow.indexOf(
  "commitCurrentRestorePending({",
);
const applyPost = restoreFlow.indexOf(
  'apiFetch("/system/restore/current/apply"',
);
assert.ok(pendingWrite > 0);
assert.ok(applyPost > pendingWrite);
assert.match(page, /CURRENT_RESTORE_PENDING_STORAGE_KEY/);
assert.match(page, /Date\.now\(\) - value\.createdAt < 24 \* 60 \* 60 \* 1000/);
assert.match(page, /Math\.min\(10000, Math\.max\(2500, delay \* 2\)\)/);

for (const phase of [
  "preflight",
  "pre_restore_backup",
  "writers_paused",
  "restore_running",
  "services_starting",
  "post_restore_check",
]) {
  assert.match(page, new RegExp(`"${phase}"`));
}
assert.match(
  page,
  /\["completed", "blocked", "failed_rolled_back", "failed_recovery_required"\]/,
);
assert.match(restoreFlow, /currentRestoreTerminal\(status\)/);
assert.match(page, /current_database_restored|maintenanceCurrentRestoreRolledBack/);
assert.match(page, /maintenanceCurrentRestoreRecoveryRequired/);
assert.match(restoreFlow, /currentRestoreFailedPhase\(status\)/);
assert.match(page, /status\?\.failed_phase/);
assert.match(page, /automatic_rollback_api_recovery_failed:\s*"services_starting"/);
assert.match(page, /automatic_rollback_recorder_recovery_failed:\s*"post_restore_check"/);
assert.match(restoreFlow, /state === "failed"[\s\S]*?"!"/);
assert.match(restoreFlow, /resultState === "rolled-back"[\s\S]*?"↩"/);

assert.match(dialog, /settingsCurrentRestoreDialogOverlay/);
assert.match(dialog, /maintenanceCurrentRestoreChanges/);
assert.match(dialog, /maintenanceCurrentRestoreVideoSafe/);
assert.match(dialog, /maintenanceCurrentRestoreBackupFirst/);
assert.match(dialog, /maintenanceCurrentRestoreInterruption/);
assert.match(dialog, /maintenanceCurrentRestoreActor/);
assert.match(dialog, /confirmTone: "danger"/);
assert.doesNotMatch(dialog, /presentation: "compact-confirmation"/);
assert.doesNotMatch(dialog, /showFooterClose:\s*true/);
assert.doesNotMatch(page, /settingsCurrentRestoreStatus/);
assert.doesNotMatch(page, /maintenanceCurrentRestoreStatusLabel/);
assert.doesNotMatch(page, /settingsMaintenanceBackupRestoreNote/);

assert.equal(
  (page.match(/maintenanceCurrentRestoreTitle:/g) || []).length,
  3,
);
assert.equal(
  (page.match(/maintenanceCurrentRestoreReasons:/g) || []).length,
  3,
);
assert.equal(
  (page.match(/automatic_rollback_api_recovery_failed:/g) || []).length,
  4,
);
assert.equal(
  (page.match(/maintenanceCurrentRestoreBackupFirst:/g) || []).length,
  3,
);
assert.match(
  page,
  /Страховочная копия базы возвращена, но API не запустился/,
);
assert.match(
  css,
  /\.operationFeedbackOverlay\.settingsCurrentRestoreDialogOverlay\s*\{[\s\S]*z-index:\s*9700;/,
);
assert.match(css, /\.settingsCurrentRestoreTimeline/);
assert.match(css, /\.settingsCurrentRestoreTimeline li\.is-failed/);
assert.match(css, /\.settingsCurrentRestoreTimeline li\.is-rolled-back/);
assert.match(css, /@media \(max-width: 640px\)[\s\S]*\.settingsCurrentRestoreTimeline/);

const actionStart = page.indexOf(
  'title={artifact.canRestore',
);
const actionEnd = page.indexOf(
  "<MaintenanceRestoreIcon />",
  actionStart,
);
const action = page.slice(actionStart, actionEnd);
assert.match(action, /currentRestoreReasonText\(artifact\.restoreIneligibleReason\)/);
assert.match(action, /disabled=\{/);
assert.match(action, /aria-label=/);

console.log("Stage 13.7.8 current DB restore frontend contract passed.");
