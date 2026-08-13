import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  readSettingsMaintenanceSourceFiles,
  settingsMaintenanceSourcePaths,
} from "./helpers/readSettingsMaintenanceSources.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const repoRoot = resolve(webRoot, "../..");
const read = (relative) => fs.readFileSync(resolve(webRoot, relative), "utf8");
const sha256 = (value) => crypto.createHash("sha256").update(value).digest("hex");
const occurrences = (source, literal) => source.split(literal).length - 1;
const counts = (values) => Object.fromEntries(
  [...new Set(values)].sort().map((value) => [value, values.filter((item) => item === value).length]),
);
const sliceBetween = (source, startMarker, endMarker = "") => {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `missing source marker: ${startMarker}`);
  const end = endMarker ? source.indexOf(endMarker, start + startMarker.length) : source.length;
  assert.ok(end > start, `invalid source range: ${startMarker} -> ${endMarker}`);
  return source.slice(start, end);
};

const { pageSource, controllerSource, surfaceSource } = readSettingsMaintenanceSourceFiles();
const physicalSources = {
  "app/settings/page.js": pageSource,
  "lib/settingsMaintenanceController.js": controllerSource,
  "components/SettingsMaintenanceSurface.js": surfaceSource,
};

assert.deepEqual(settingsMaintenanceSourcePaths, [
  "app/settings/page.js",
  "lib/settingsMaintenanceController.js",
  "components/SettingsMaintenanceSurface.js",
]);

assert.equal((pageSource.match(/^export default function SettingsPage\(\)/gm) || []).length, 1);
assert.equal((controllerSource.match(/^export default/gm) || []).length, 0);
assert.equal((surfaceSource.match(/^export default/gm) || []).length, 0);

const hookCall = pageSource.indexOf("const maintenanceController = useSettingsMaintenanceController({");
const pageReturn = pageSource.indexOf("  return (", pageSource.indexOf("export default function SettingsPage"));
assert.ok(hookCall > 0 && hookCall < pageReturn, "Maintenance controller hook must mount unconditionally before render");
assert.equal(occurrences(pageSource, "useSettingsMaintenanceController({"), 1);
assert.match(pageSource, /clearToast:\s*\(\) => setToast\(null\)/);

for (const source of [controllerSource, surfaceSource]) {
  assert.equal(source.includes('app/settings/page.js'), false);
  assert.equal(source.includes('app\\settings\\page.js'), false);
}
assert.equal(controllerSource.includes("SettingsMaintenanceSurface"), false);
assert.equal(surfaceSource.includes("settingsMaintenanceController"), false);

const expectedStates = [
  {
    "state": "maintenanceModalOpen",
    "setter": "setMaintenanceModalOpen",
    "initial": "false"
  },
  {
    "state": "maintenanceOverview",
    "setter": "setMaintenanceOverview",
    "initial": "null"
  },
  {
    "state": "maintenanceBackupDetail",
    "setter": "setMaintenanceBackupDetail",
    "initial": "null"
  },
  {
    "state": "maintenanceBackupDetailOpen",
    "setter": "setMaintenanceBackupDetailOpen",
    "initial": "false"
  },
  {
    "state": "maintenanceLoading",
    "setter": "setMaintenanceLoading",
    "initial": "false"
  },
  {
    "state": "maintenanceError",
    "setter": "setMaintenanceError",
    "initial": "\"\""
  },
  {
    "state": "maintenanceBusy",
    "setter": "setMaintenanceBusy",
    "initial": "\"\""
  },
  {
    "state": "maintenanceActionResult",
    "setter": "setMaintenanceActionResult",
    "initial": "null"
  },
  {
    "state": "maintenanceBackupResult",
    "setter": "setMaintenanceBackupResult",
    "initial": "null"
  },
  {
    "state": "maintenanceConfirm",
    "setter": "setMaintenanceConfirm",
    "initial": "null"
  },
  {
    "state": "maintenanceBackupPending",
    "setter": "setMaintenanceBackupPending",
    "initial": "null"
  },
  {
    "state": "currentRestoreDialog",
    "setter": "setCurrentRestoreDialog",
    "initial": "null"
  },
  {
    "state": "currentRestorePending",
    "setter": "setCurrentRestorePending",
    "initial": "null"
  },
  {
    "state": "currentRestoreStatus",
    "setter": "setCurrentRestoreStatus",
    "initial": "null"
  },
  {
    "state": "updateStatus",
    "setter": "setUpdateStatus",
    "initial": "null"
  },
  {
    "state": "updateApplyStatus",
    "setter": "setUpdateApplyStatus",
    "initial": "null"
  },
  {
    "state": "updateTransportErrors",
    "setter": "setUpdateTransportErrors",
    "initial": "{ update: null, apply: null }"
  },
  {
    "state": "updateApplyReconnectSnapshot",
    "setter": "setUpdateApplyReconnectSnapshot",
    "initial": "null"
  },
  {
    "state": "updateApplyClockMs",
    "setter": "setUpdateApplyClockMs",
    "initial": "() => Date.now()"
  },
  {
    "state": "updateApplyDialog",
    "setter": "setUpdateApplyDialog",
    "initial": "null"
  },
  {
    "state": "updateApplyPending",
    "setter": "setUpdateApplyPending",
    "initial": "null"
  }
];
for (const state of expectedStates) {
  const definition = `const [${state.state}, ${state.setter}] = useState(${state.initial});`;
  assert.equal(occurrences(controllerSource, definition), 1, `${state.state} must have one controller owner`);
  assert.equal(occurrences(pageSource, definition), 0, `${state.state} leaked into page`);
  assert.equal(occurrences(surfaceSource, definition), 0, `${state.state} leaked into view`);
}

const expectedRefs = [
  {
    "name": "updatePollInFlightRef",
    "initial": "false"
  },
  {
    "name": "updateApplyPendingRef",
    "initial": "null"
  },
  {
    "name": "maintenanceBackupPendingRef",
    "initial": "null"
  },
  {
    "name": "maintenanceBackupRecoveryRef",
    "initial": "false"
  },
  {
    "name": "maintenanceBackupAdmissionRef",
    "initial": "null"
  },
  {
    "name": "maintenanceBackupPollInFlightRef",
    "initial": "false"
  },
  {
    "name": "maintenanceBackupDetailRef",
    "initial": "null"
  },
  {
    "name": "currentRestorePendingRef",
    "initial": "null"
  },
  {
    "name": "currentRestorePollInFlightRef",
    "initial": "false"
  },
  {
    "name": "currentRestoreDialogRef",
    "initial": "null"
  },
  {
    "name": "updateApplyDialogRef",
    "initial": "null"
  },
  {
    "name": "maintenanceChildDialogOpenRef",
    "initial": "false"
  },
  {
    "name": "maintenanceDialogRef",
    "initial": "null"
  },
  {
    "name": "maintenanceTriggerRef",
    "initial": "null"
  },
  {
    "name": "maintenanceBusyRef",
    "initial": "\"\""
  }
];
for (const ref of expectedRefs) {
  const definition = `const ${ref.name} = useRef(${ref.initial});`;
  assert.equal(occurrences(controllerSource, definition), 1, `${ref.name} must have one controller owner`);
  assert.equal(occurrences(pageSource, definition), 0, `${ref.name} leaked into page`);
  assert.equal(occurrences(surfaceSource, definition), 0, `${ref.name} leaked into view`);
}

const expectedActions = [
  {
    "name": "openMaintenanceModal",
    "async": false,
    "parameters": ""
  },
  {
    "name": "closeMaintenanceModal",
    "async": false,
    "parameters": ""
  },
  {
    "name": "safeUpdateTransportError",
    "async": false,
    "parameters": "error, fallback"
  },
  {
    "name": "safeUpdateLaunchError",
    "async": false,
    "parameters": "error"
  },
  {
    "name": "commitUpdateApplyPending",
    "async": false,
    "parameters": "nextRecord"
  },
  {
    "name": "commitBackupOperationPending",
    "async": false,
    "parameters": "nextRecord"
  },
  {
    "name": "commitCurrentRestorePending",
    "async": false,
    "parameters": "nextRecord"
  },
  {
    "name": "currentRestoreReasonCode",
    "async": false,
    "parameters": "value"
  },
  {
    "name": "currentRestoreReasonText",
    "async": false,
    "parameters": "code, fallback = t.maintenanceCurrentRestoreRequestRejected,"
  },
  {
    "name": "currentRestoreFailedPhase",
    "async": false,
    "parameters": "status"
  },
  {
    "name": "currentRestoreTerminal",
    "async": false,
    "parameters": "status = currentRestoreStatus"
  },
  {
    "name": "currentRestoreTerminalText",
    "async": false,
    "parameters": "status = currentRestoreStatus"
  },
  {
    "name": "closeCurrentRestoreDialog",
    "async": false,
    "parameters": ""
  },
  {
    "name": "pollCurrentRestoreStatus",
    "async": true,
    "parameters": "pendingOverride = null"
  },
  {
    "name": "requestCurrentDatabaseRestore",
    "async": true,
    "parameters": "artifact"
  },
  {
    "name": "confirmCurrentDatabaseRestore",
    "async": true,
    "parameters": ""
  },
  {
    "name": "backupOperationFallback",
    "async": false,
    "parameters": "kind"
  },
  {
    "name": "backupOperationSuccess",
    "async": false,
    "parameters": "kind"
  },
  {
    "name": "acceptBackupOperationReceipt",
    "async": true,
    "parameters": "receipt, pendingRecord, { recovered = false } = {}"
  },
  {
    "name": "reconcilePendingBackupOperation",
    "async": true,
    "parameters": ""
  },
  {
    "name": "closeUpdateApplyDialog",
    "async": false,
    "parameters": ""
  },
  {
    "name": "reconcilePendingUpdateApply",
    "async": false,
    "parameters": "applyData, observedAtMs"
  },
  {
    "name": "refreshMaintenanceSurface",
    "async": true,
    "parameters": ""
  },
  {
    "name": "loadMaintenanceOverview",
    "async": true,
    "parameters": ""
  },
  {
    "name": "loadMaintenanceBackupPage",
    "async": true,
    "parameters": "offset, { allowClamp = false, silent = false } = {},"
  },
  {
    "name": "refreshMaintenanceBackupProjections",
    "async": true,
    "parameters": "{ clampInvalid = false } = {}"
  },
  {
    "name": "openMaintenanceBackupDetail",
    "async": true,
    "parameters": ""
  },
  {
    "name": "closeMaintenanceBackupDetail",
    "async": false,
    "parameters": ""
  },
  {
    "name": "loadUpdateApplySurface",
    "async": true,
    "parameters": "{ silent = false } = {}"
  },
  {
    "name": "runMaintenanceDryRun",
    "async": true,
    "parameters": "flowKey, bodyOverride = null"
  },
  {
    "name": "requestDbAdoptionApply",
    "async": false,
    "parameters": ""
  },
  {
    "name": "performDbAdoptionApply",
    "async": true,
    "parameters": ""
  },
  {
    "name": "createMaintenanceBackup",
    "async": true,
    "parameters": ""
  },
  {
    "name": "performMaintenanceBackupCreate",
    "async": true,
    "parameters": ""
  },
  {
    "name": "requestCheckMaintenanceBackup",
    "async": false,
    "parameters": "artifact"
  },
  {
    "name": "requestDeleteMaintenanceBackup",
    "async": false,
    "parameters": "artifact"
  },
  {
    "name": "performMaintenanceBackupOperation",
    "async": true,
    "parameters": "kind, artifact"
  },
  {
    "name": "runUpdateCheck",
    "async": true,
    "parameters": ""
  },
  {
    "name": "startUpdateApply",
    "async": false,
    "parameters": ""
  },
  {
    "name": "confirmUpdateApply",
    "async": true,
    "parameters": ""
  }
];
const expectedActionNames = new Set(expectedActions.map((item) => item.name));
const actualActions = [...controllerSource.matchAll(/^  (async )?function ([A-Za-z0-9_]+)\(([^)]*)\)/gm)]
  .map((match) => ({
    name: match[2],
    async: Boolean(match[1]),
    parameters: match[3].replace(/\s+/g, " ").trim(),
  }))
  .filter((item) => expectedActionNames.has(item.name));
assert.deepEqual(actualActions, expectedActions);
for (const action of expectedActions) {
  assert.equal(new RegExp(`function ${action.name}\\(`).test(pageSource), false, `${action.name} leaked into page`);
  assert.equal(new RegExp(`function ${action.name}\\(`).test(surfaceSource), false, `${action.name} leaked into view`);
}

for (const helperName of [
  "MaintenanceCheckIcon",
  "MaintenanceBackupDimensionStatus",
  "MaintenanceTrashIcon",
  "MaintenanceRestoreIcon",
]) {
  assert.equal(occurrences(surfaceSource, `function ${helperName}`), 1);
  assert.equal(occurrences(pageSource, `function ${helperName}`), 0);
  assert.equal(occurrences(controllerSource, `function ${helperName}`), 0);
}

assert.deepEqual(
  [...surfaceSource.matchAll(/^export function (Settings[A-Za-z0-9_]+)/gm)].map((match) => match[1]),
  [
    "SettingsMaintenanceConfirmationDialogs",
    "SettingsMaintenanceModal",
    "SettingsUpdateApplyDialog",
  ],
);

const endpointValues = [...controllerSource.matchAll(/["'`]((?:\/system\/)[^"'`]+)["'`]/g)]
  .map((match) => match[1]);
assert.deepEqual(counts(endpointValues), {
  "/system/backup/create": 1,
  "/system/backup/operations/${encodeURIComponent(pending.submissionId)}": 1,
  "/system/db-adoption/apply": 1,
  "/system/maintenance/overview": 1,
  "/system/restore/apply": 1,
  "/system/restore/artifacts/${encodeURIComponent(artifact.id)}/delete": 1,
  "/system/restore/current/apply": 1,
  "/system/restore/current/preflight": 1,
  "/system/restore/current/status": 1,
  "/system/restore/status?offset=${safeOffset}&limit=${MAINTENANCE_BACKUP_PAGE_SIZE}": 1,
  "/system/restore/status?offset=${validOffset}&limit=${MAINTENANCE_BACKUP_PAGE_SIZE}": 1,
  "/system/update/apply": 1,
  "/system/update/apply/status": 1,
  "/system/update/check": 1,
  "/system/update/status": 1
});
assert.equal((pageSource.match(/["'`](?:\/system\/)/g) || []).length, 0);
assert.equal((surfaceSource.match(/["'`](?:\/system\/)/g) || []).length, 0);

const persistenceValues = [...controllerSource.matchAll(/window\.sessionStorage\.(?:getItem|setItem|removeItem)\(\s*([A-Z0-9_]+)/g)]
  .map((match) => match[1]);
assert.deepEqual(counts(persistenceValues), {
  "BACKUP_OPERATION_PENDING_STORAGE_KEY": 4,
  "CURRENT_RESTORE_PENDING_STORAGE_KEY": 5,
  "UPDATE_APPLY_PENDING_STORAGE_KEY": 3
});
for (const key of Object.keys({
  "BACKUP_OPERATION_PENDING_STORAGE_KEY": 4,
  "CURRENT_RESTORE_PENDING_STORAGE_KEY": 5,
  "UPDATE_APPLY_PENDING_STORAGE_KEY": 3
})) {
  assert.equal(pageSource.includes(`window.sessionStorage.getItem(${key}`), false);
  assert.equal(surfaceSource.includes(key), false);
}

const confirmationSource = sliceBetween(
  surfaceSource,
  "export function SettingsMaintenanceConfirmationDialogs",
  "export function SettingsMaintenanceModal",
);
const modalSource = sliceBetween(
  surfaceSource,
  "export function SettingsMaintenanceModal",
  "export function SettingsUpdateApplyDialog",
);
const updateDialogSource = sliceBetween(
  surfaceSource,
  "export function SettingsUpdateApplyDialog",
);
const presentationSource = `${confirmationSource}\n${modalSource}\n${updateDialogSource}`;
const classMarkers = [...presentationSource.matchAll(/className=(?:"([^"]+)"|\{`([^`]+)`\})/g)]
  .map((match) => match[1] || match[2]);
const dialogIds = [...presentationSource.matchAll(/id:\s*(?:"([^"]+)"|`([^`]+)`)/g)]
  .map((match) => match[1] || match[2]);
assert.equal(sha256(JSON.stringify(classMarkers)), "43262881b8b3dcf4c2237b159df128c0ee202c4e4e7030aa730092c7685b62f4");
assert.equal(sha256(JSON.stringify(dialogIds)), "e47a36a2650bf6b9b6bff5fc43f79ada4baf1184f3cace4d47e224d178ff3f64");
assert.deepEqual(dialogIds, ["maintenance-confirm","current-db-restore-${currentRestoreDialog.artifact?.id || \"current\"}","update-apply-confirm"]);

assert.equal(pageSource.includes('className="settingsMaintenanceModal"'), false);
assert.equal(pageSource.includes('id: "maintenance-confirm"'), false);
assert.equal(pageSource.includes('id: "update-apply-confirm"'), false);
assert.equal(pageSource.includes('id: "diagnostic-archive-choice"'), true);
assert.equal(surfaceSource.includes('id: "diagnostic-archive-choice"'), false);
assert.match(surfaceSource, /onClick=\{onOpenDiagnosticChoice\}/);
assert.match(pageSource, /onOpenDiagnosticChoice=\{\(\) => setDiagnosticChoiceOpen\(true\)\}/);

const userDeleteDialog = pageSource.indexOf("dialog={userDeleteTarget ? {");
const maintenanceConfirmSurface = pageSource.indexOf("<SettingsMaintenanceConfirmationDialogs");
const userModal = pageSource.indexOf("{userModal ? (");
const maintenanceModalSurface = pageSource.indexOf("<SettingsMaintenanceModal");
const updateDialogSurface = pageSource.indexOf("<SettingsUpdateApplyDialog");
const diagnosticDialog = pageSource.indexOf('dialog={diagnosticChoiceOpen ? {');
assert.ok(userDeleteDialog < maintenanceConfirmSurface);
assert.ok(maintenanceConfirmSurface < userModal);
assert.ok(userModal < maintenanceModalSurface);
assert.ok(maintenanceModalSurface < updateDialogSurface);
assert.ok(updateDialogSurface < diagnosticDialog);

const startUpdateApply = sliceBetween(
  controllerSource,
  "function startUpdateApply()",
  "async function confirmUpdateApply",
);
assert.match(startUpdateApply, /clearToast\(\);\s*setUpdateApplyDialog\(\{ phase: "confirm"/);
assert.equal(startUpdateApply.includes("setToast(null)"), false);
assert.match(controllerSource, /if \(!canManageMaintenance \|\| !currentRestorePending\) return undefined;/);
assert.match(controllerSource, /if \(!canManageMaintenance \|\| !maintenanceBackupPending\) return undefined;/);
assert.match(
  controllerSource,
  /if \(!canManageMaintenance \|\| \(!maintenanceModalOpen && !active && !updateApplyPending\)\) return undefined;/,
);

const expectedProtected = {
  "apps/web/app/styles/20-settings-maintenance.css": "c4f901f27d716b152c439ed29a51654774ad3c305b56ce2801e10ad9d14531b5",
  "apps/web/components/OperationFeedback.js": "1e02f30c203481a788af6f79306f76991fa59dbcf86fb355605f355eaa87957b",
  "apps/web/lib/api.js": "c76e643fe17c6b65270df0631615902f03f4136e2f6823e9ab799d4ca417764c",
  "apps/web/lib/settingsPageHelpers.js": "adece850f2f049b7b535817c2fad8ec9cc7099d7abdfa0b52d129c909c1eee7c",
  "apps/web/lib/settingsPageSharedHelpers.js": "0b7f9df886faa2ff4c4893cae751c52de26c51e5b2665dc7903fc28eba82c755",
  "apps/web/lib/settingsUpdateApplyHelpers.js": "98e3282455f9c8c50b7813eaabaac5fce90485facadc7c587a454d1557290247"
};
for (const [relative, expectedHash] of Object.entries(expectedProtected)) {
  const protectedSource = fs.readFileSync(resolve(repoRoot, relative));
  assert.equal(sha256(protectedSource), expectedHash, `${relative} changed outside scope`);
  const helperSource = protectedSource.toString("utf8");
  assert.equal(helperSource.includes("settingsMaintenanceController"), false);
  assert.equal(helperSource.includes("SettingsMaintenanceSurface"), false);
}

console.log("Stage 13.5.6.0.6 Settings Maintenance decomposition contract passed.");
