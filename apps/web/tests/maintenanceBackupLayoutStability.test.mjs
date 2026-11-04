import { readProductSourceFile } from "./helpers/readProductSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";

const read = (path) => readProductSourceFile(new URL(`../${path}`, import.meta.url), "utf8");
const controller = read("lib/settingsMaintenanceController.js");
const surface = read("components/SettingsMaintenanceSurface.js");
const css = read("app/styles/20-settings-maintenance.css");
const extract = (name) => {
  const body = controller.match(new RegExp(`^  (?:async )?function ${name}\\([^]*?^  }`, "m"))?.[0];
  assert.ok(body, `missing controller action: ${name}`);
  return body;
};

function operationHarness({ busy = "", pending = null, restore = null, reject = false, ambiguous = false } = {}) {
  const state = { dialogs: [], requests: [], order: [], busy, result: null, reconciled: 0 };
  const pendingRef = { current: pending };
  const scope = {
    maintenanceBusy: busy, currentRestorePending: restore,
    maintenanceBackupPendingRef: pendingRef,
    maintenanceBackupRecoveryRef: { current: false },
    maintenanceBackupAdmissionRef: { current: null },
    createUpdateApplySubmissionId: () => "synthetic-submission",
    createBackupOperationPending: (kind, artifactId, submissionId) => ({ kind, artifactId, submissionId }),
    commitBackupOperationPending: (value) => { state.order.push("pending"); pendingRef.current = value; return true; },
    setMaintenanceBusy: (value) => { state.busy = typeof value === "function" ? value(state.busy) : value; },
    setMaintenanceConfirm: (value) => { if (value) state.dialogs.push(value); },
    setMaintenanceDetail: (value) => { if (value) state.dialogs.push(value); },
    setMaintenanceActionResult: () => {},
    setMaintenanceBackupResult: (value) => { state.result = value; },
    showToast: () => {}, backupOperationFallback: () => "Operation failed",
    updateApplyRequestIsAmbiguous: () => ambiguous,
    t: {
      maintenanceBackupOperationLabels: { create: "Create", check: "Check", delete: "Delete" },
      maintenanceBackupsTitle: "Backups", maintenanceBackupDelete: "Delete",
      maintenanceBackupDeleteConfirm: "Delete {date}?",
      maintenanceBackupCreate: "Create", maintenanceBackupCreateConfirm: "Create?",
    },
    apiFetch: async (path, options) => {
      state.order.push("request");
      state.requests.push({ path, body: JSON.parse(options.body) });
      if (reject) throw { status: ambiguous ? 0 : 400 };
      return { state: "completed" };
    },
    acceptBackupOperationReceipt: async () => { pendingRef.current = null; state.result = { status: "completed" }; },
    reconcilePendingBackupOperation: async () => { state.reconciled += 1; },
  };
  const actions = new Function(...Object.keys(scope), `
    ${extract("performMaintenanceBackupOperation")}
    ${extract("performMaintenanceBackupCreate")}
    ${extract("requestDeleteMaintenanceBackup")}
    ${extract("createMaintenanceBackup")}
    return {performMaintenanceBackupOperation,requestDeleteMaintenanceBackup,createMaintenanceBackup};
  `)(...Object.values(scope));
  return { state, ...actions };
}

const endpoints = {
  create: "/system/backup/create",
  check: "/system/restore/apply",
  delete: "/system/restore/artifacts/synthetic-artifact/delete",
};
for (const kind of Object.keys(endpoints)) {
  for (const reject of [false, true]) {
    const test = operationHarness({ reject });
    await test.performMaintenanceBackupOperation(kind, kind === "create" ? null : { id: "synthetic-artifact" });
    assert.deepEqual(test.state.dialogs, [], `${kind} must not replace the list with a progress/result dialog`);
    assert.equal(test.state.requests[0].path, endpoints[kind]);
    assert.equal(test.state.requests[0].body.confirm, true, "backend confirmation contract is retained");
    assert.deepEqual(test.state.order.slice(0, 2), ["pending", "request"]);
    assert.equal(test.state.busy, "");
    assert.equal(test.state.result.status, reject ? "failed" : "completed");
  }
  const uncertain = operationHarness({ reject: true, ambiguous: true });
  await uncertain.performMaintenanceBackupOperation(kind, kind === "create" ? null : { id: "synthetic-artifact" });
  assert.equal(uncertain.state.requests.length, 1, "ambiguous submissions must not be blindly retried");
  assert.equal(uncertain.state.reconciled, 1, "pending operation recovery remains active");
  assert.equal(uncertain.state.result.recovering, true);
  assert.deepEqual(uncertain.state.dialogs, []);
}
for (const options of [{ busy: "backup-create" }, { pending: { submissionId: "existing" } }, { restore: { status: "running" } }]) {
  const test = operationHarness(options);
  await test.performMaintenanceBackupOperation("delete", { id: "synthetic-artifact" });
  assert.equal(test.state.requests.length, 0, "conflicting operations remain blocked");
}
for (const kind of ["delete", "create"]) {
  const test = operationHarness();
  if (kind === "delete") test.requestDeleteMaintenanceBackup({ id: "synthetic-artifact", createdAt: "synthetic date" });
  else await test.createMaintenanceBackup();
  assert.equal(test.state.dialogs.length, 1, `${kind} must retain its deliberate confirmation`);
  assert.equal(test.state.requests.length, 0, "opening confirmation must not submit an operation");
}

const focusEffect = controller.match(/useEffect\(\(\) => \{\n    if \(!maintenanceModalOpen \|\| maintenanceChildDialogOpen \|\| maintenanceBusy\) return undefined;[\s\S]*?\n  \}, \[maintenanceModalOpen, maintenanceChildDialogOpen, maintenanceBusy\]\);/)?.[0];
assert.ok(focusEffect, "the parent must regain its focus owner after a child closes");
for (const [childOpen, inside, disabled, expectedFocus, busy = ""] of [[true, false, false, 0], [false, false, false, 1], [false, true, false, 0], [false, true, true, 1], [false, false, false, 0, "backup-check"]]) {
  const calls = [];
  const container = { contains: () => inside };
  const childRef = { current: childOpen };
  let cleanup;
  new Function("useEffect", "maintenanceModalOpen", "maintenanceChildDialogOpen", "maintenanceDialogRef", "document", "focusableElements", "maintenanceChildDialogOpenRef", "maintenanceBusyRef", "closeMaintenanceModal", "maintenanceTriggerRef", "maintenanceBusy", focusEffect)(
    (fn) => { cleanup = fn(); }, true, childOpen, { current: container },
    { activeElement: { matches: () => disabled }, addEventListener: () => {}, removeEventListener: () => {} },
    () => [{ focus: (options) => calls.push(options) }], childRef, { current: "" }, () => {},
    { current: { focus: () => assert.fail("must not focus the page behind an open child") } },
    busy,
  );
  assert.equal(calls.length, expectedFocus);
  if (calls.length) assert.deepEqual(calls[0], { preventScroll: true });
  childRef.current = true;
  cleanup?.();
}

assert.match(surface, /settingsMaintenanceContent\$\{maintenanceBackupDetailOpen \? " is-backup-list"/);
assert.match(surface, /settingsMaintenanceBackupList[\s\S]*settingsMaintenanceBackupListHead[\s\S]*settingsMaintenanceBackupRows[\s\S]*settingsMaintenanceBackupPagination/);
assert.doesNotMatch(surface, /artifact\.checkedAt \|\| artifact\.validatedAt \? \(/, "evidence keeps its reserved rows before the first check");
assert.match(css, /\.settingsMaintenanceBackupEvidenceTime\s*\{[^}]*grid-template-rows:\s*repeat\(2, minmax\(1\.3em, auto\)\)/);
assert.match(css, /\.settingsMaintenanceBackupEvidenceTime > span\s*\{\s*font:\s*inherit/, "general backup labels must not change the height of timestamp rows");
assert.match(css, /\.settingsMaintenanceBackupStatusPill\s*\{[^}]*min-height:\s*26px/, "unperformed and checked states must keep the same badge height");
assert.match(css, /\.settingsMaintenanceBackupDetail \.settingsMaintenanceBackupList\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*hidden/);
assert.match(css, /\.settingsMaintenanceBackupRows\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*auto;[^}]*scrollbar-gutter:\s*stable/);
assert.match(css, /\.settingsMaintenanceBackupListHead,\s*\.settingsMaintenanceBackupPagination\s*\{\s*flex-shrink:\s*0/);
console.log("Maintenance backup layout: no automatic result windows, guarded operations, focus recovery, persistent scroll region and pagination passed");
