import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pageSource = fs.readFileSync(resolve(__dirname, "../app/settings/page.js"), "utf8");
const cssSource = fs.readFileSync(resolve(__dirname, "../app/styles/20-settings-maintenance.css"), "utf8");
const operationFeedbackSource = fs.readFileSync(resolve(__dirname, "../components/OperationFeedback.js"), "utf8");

const updateDialogMarker = pageSource.indexOf('id: "update-apply-confirm"');
const dialogStart = pageSource.lastIndexOf("<OperationDialog", updateDialogMarker);
const dialogEnd = pageSource.indexOf("/>", dialogStart) + 2;
assert.ok(updateDialogMarker >= 0 && dialogStart >= 0 && dialogEnd > dialogStart);
const dialogSource = pageSource.slice(dialogStart, dialogEnd);

assert.equal(dialogSource.includes('presentation: "compact-confirmation"'), true);
assert.equal(dialogSource.includes('overlayClassName: "settingsUpdateApplyDialogOverlay"'), true);
assert.equal(dialogSource.includes("updateApplyModalTitle"), true);
assert.equal(dialogSource.includes("updateApplyConfirm"), true);
assert.equal(dialogSource.includes("updateApplyDialog.candidate"), false);
assert.equal(dialogSource.includes("updateApplyOperator.currentVersion"), false);
assert.equal(dialogSource.includes("shortCommit"), false);
assert.equal(dialogSource.includes("settingsModalActions"), false);
assert.equal(dialogSource.includes("updateApplyLaunchChecking"), false);
assert.equal(dialogSource.includes("onConfirm: confirmUpdateApply"), true);
assert.equal(pageSource.includes("previewUpdateApplyDialog"), false);
assert.equal(pageSource.includes("updateApplyTest"), false);

const confirmStart = pageSource.indexOf("async function confirmUpdateApply");
const confirmEnd = pageSource.indexOf("async function downloadMaintenanceReport", confirmStart);
assert.ok(confirmStart >= 0 && confirmEnd > confirmStart);
const confirmSource = pageSource.slice(confirmStart, confirmEnd);

assert.ok(
  confirmSource.indexOf("setUpdateApplyDialog(null)") <
    confirmSource.indexOf('apiFetch("/system/update/apply"'),
);
assert.equal(confirmSource.includes("submission-ticket"), false);
assert.equal(confirmSource.includes("submission_proof"), false);
assert.equal(confirmSource.includes("humanErrorText"), false);
assert.equal(confirmSource.includes("safeUpdateLaunchError(err)"), true);
assert.equal(confirmSource.includes("setMaintenanceActionResult"), true);
assert.equal(confirmSource.includes("displayReason: message"), true);
assert.equal(confirmSource.includes("showToast"), true);

const safeErrorStart = pageSource.indexOf("function safeUpdateLaunchError");
const safeErrorEnd = pageSource.indexOf("function commitUpdateApplyPending", safeErrorStart);
assert.ok(safeErrorStart >= 0 && safeErrorEnd > safeErrorStart);
const safeErrorSource = pageSource.slice(safeErrorStart, safeErrorEnd);
assert.equal(safeErrorSource.includes('"update_already_running"'), true);
assert.equal(safeErrorSource.includes('"update_admission_unknown"'), false);
assert.equal(safeErrorSource.includes("error?.message"), false);
assert.equal(
  pageSource.includes("maintenanceActionResult.displayReason || formatMaintenanceMessage"),
  true,
);

assert.equal(pageSource.includes("window.confirm"), false);
assert.equal(pageSource.includes("updateApplyModalTarget"), false);
assert.equal(pageSource.includes("updateApplyModalRelease"), false);
assert.equal(pageSource.includes("updateApplyModalCommit"), false);
assert.equal(pageSource.includes("updateApplyModalRestartTitle"), false);
assert.equal(pageSource.includes("updateApplyModalRestartText"), false);
assert.match(cssSource, /\.operationFeedbackOverlay\.settingsUpdateApplyDialogOverlay\s*\{[^}]*z-index:\s*9600/s);
assert.equal(cssSource.includes(".settingsUpdateApplyDialog {"), false);
assert.equal(operationFeedbackSource.includes("dialog.overlayClassName"), true);
assert.equal(operationFeedbackSource.includes('className={`operationFeedbackOverlay'), true);

console.log("Compact update confirmation dofix tests passed");
