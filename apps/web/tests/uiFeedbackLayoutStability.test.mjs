import { readProductSourceFile } from "./helpers/readProductSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import { acquireModalScrollLock } from "../lib/modalScrollLock.js";
import { updateApplyPhaseText, updateApplyProgressText } from "../lib/settingsUpdateApplyHelpers.js";

const read = (path) => readProductSourceFile(new URL(`../${path}`, import.meta.url), "utf8");
const style = () => ({ overflow: "", position: "", top: "", left: "", right: "", width: "", overscrollBehavior: "", paddingRight: "5px" });
const movements = [];
globalThis.document = { body: { style: style() }, documentElement: { style: style() } };
globalThis.window = { scrollX: 0, scrollY: 421, scrollTo: (...args) => movements.push(args) };
for (const releaseParentFirst of [true, false]) {
  const first = acquireModalScrollLock();
  const second = acquireModalScrollLock();
  assert.equal(document.body.style.overflow, "hidden");
  (releaseParentFirst ? first : second)();
  assert.equal(document.body.style.overflow, "hidden");
  (releaseParentFirst ? second : first)();
  assert.equal(document.body.style.overflow, "");
  first(); second();
  assert.equal(document.body.style.overflow, "");
}
const viewer = acquireModalScrollLock({ freezePosition: true });
const nested = acquireModalScrollLock();
assert.equal(document.body.style.top, "-421px");
assert.equal(document.body.style.paddingRight, "5px");
viewer();
assert.equal(document.body.style.position, "");
assert.equal(document.body.style.overflow, "hidden");
assert.deepEqual(movements.at(-1), [0, 421]);
nested();
assert.equal(document.body.style.overflow, "");
assert.equal(document.documentElement.style.overflow, "");

const phases = ["staging_source", "validating_compose", "building_images", "pulling_images", "verifying_images", "schema_preflight"];
const copy = { maintenanceStatuses: Object.fromEntries(["preflight", ...phases].map((key) => [key, `label:${key}`])), updateApplyProgressIndeterminate: "Working" };
for (const phase of phases) {
  const status = { phase, current_step: "preflight", status: "staging" };
  assert.equal(updateApplyPhaseText(status, copy), `label:${phase}`);
  assert.equal(updateApplyProgressText(status, copy), `label:${phase} — Working`);
}
assert.equal(updateApplyPhaseText({ phase: "private_raw_value", current_step: "preflight" }, copy), "label:preflight");

// Execute the real request owner, not a replacement implementation of it.
const recordings = read("app/recordings/page.js");
const loadSource = recordings.match(/^  async function loadRecordings\([\s\S]*?^  }/m)?.[0];
assert.ok(loadSource);
const state = { items: [{ filename: "old.mkv" }], phase: "loaded", query: "old", errors: [] };
const pending = [];
const requestIdRef = { current: 0 };
const load = new Function("apiFetch", "requestIdRef", "setRecordingsLoadState", "setItems", "setSelectedPaths", "setRecordingsSummary", "setRecordingsPagination", "setProductTimezone", "setError", "setLoadedQueryKey", "showRecordingActionError", "normalizeRecordingError", "productDateFilterParam", `
 const selectedDate = "", currentPage = 1, pageSize = 20, sortBy = "created_at", sortDir = "desc", loadedQueryKey = "old";
 ${loadSource}; return loadRecordings;
`)(
  () => new Promise((resolve, reject) => pending.push({ resolve, reject })), requestIdRef,
  (value) => { state.phase = typeof value === "function" ? value(state.phase) : value; },
  (value) => { state.items = value; }, () => {}, () => {}, () => {}, () => {}, () => {},
  (value) => { state.query = value; }, (error) => state.errors.push(error), (value) => value, (value) => value,
);
const firstRequest = load();
assert.equal(state.phase, "refreshing");
assert.equal(state.items[0].filename, "old.mkv");
const newerRequest = load("second-camera");
pending[1].resolve({ items: [{ filename: "new.mkv" }] });
await newerRequest;
pending[0].resolve({ items: [{ filename: "obsolete.mkv" }] });
await firstRequest;
assert.equal(state.items[0].filename, "new.mkv");
const failingRequest = load("second-camera");
pending[2].reject(new Error("temporary connection failure"));
await failingRequest;
assert.equal(state.items[0].filename, "new.mkv");
assert.equal(state.phase, "error");
assert.equal(state.errors.length, 1);
const retry = load("second-camera");
assert.equal(state.phase, "refreshing", "retry after an error retains the loaded snapshot instead of reverting to first-load state");
assert.equal(state.items[0].filename, "new.mkv");
pending[3].resolve({ items: [{ filename: "retry.mkv" }] });
await retry;
assert.equal(state.phase, "loaded");
assert.equal(state.items[0].filename, "retry.mkv");
assert.match(recordings, /recordingsActionReady = recordingsLoadState === "loaded" && loadedQueryKey === currentQueryKey/);
assert.match(recordings, /inert=\{recordingsActionReady \? undefined : true\}/);
assert.doesNotMatch(recordings, /viewerPlaybackError \? \([\s\S]*?\) : \([\s\S]*?ref=\{viewerFrameRef\}/);

const cameras = read("app/cameras/page.js");
assert.doesNotMatch(cameras, /setDeleteWarning|deleteWarning &&/);
assert.match(cameras, /if \(cleanupPartial\)[\s\S]*?setDeleteResult/);
assert.match(cameras, /counts\.deleted_count[\s\S]*?counts\.skipped_count[\s\S]*?counts\.failed_count/);
assert.match(cameras, /OperationDialog dialog=\{deleteResult\}/);
const surface = read("components/SettingsMaintenanceSurface.js");
assert.doesNotMatch(surface, /settingsMaintenanceBackupPending|settingsMaintenanceResult|settingsJournalEmpty error/);
assert.match(surface, /maintenanceModalOpen && maintenanceDetail/);
// Settings CSS is imported before the shared operation-dialog defaults. Child
// overlays and widths need compound specificity, otherwise they hide behind the
// inert Maintenance parent despite a seemingly higher z-index in the source.
const settingsCss = read("app/styles/20-settings-maintenance.css");
assert.match(settingsCss, /\.operationFeedbackOverlay\.settingsMaintenanceDetailOverlay\s*\{\s*z-index: 9500/);
assert.match(settingsCss, /\.operationFeedbackDialog\.settingsMaintenanceDetailDialog\s*\{\s*width: min\(820px, 100%\)/);
assert.match(settingsCss, /\.settingsMaintenanceDetailDialog \.settingsUpdateApplyTimeline\s*\{ overflow: visible/);
assert.match(settingsCss, /\.settingsMaintenanceDetailDialog \.settingsUpdateApplyTimeline ol\s*\{ min-width: 0/);
assert.match(read("app/styles/40-storage-records-shared.css"), /\.operationFeedbackBody\s*\{[^}]*grid-auto-rows: max-content/);
assert.match(read("components/AuditDiagnosticsEntries.js"), /error && !items\.length/);
const auditEntries = read("components/AuditDiagnosticsEntries.js");
const [journalEntry, diagnosticsEntry] = auditEntries.split("export function DiagnosticsEntry()");
assert.equal((journalEntry.match(/<OperationToast /g) || []).length, 1, "journal owns exactly one toast renderer");
assert.equal((diagnosticsEntry.match(/<OperationToast /g) || []).length, 1, "diagnostics owns exactly one toast renderer");
const settings = read("app/settings/page.js");
const patchUserSource = settings.match(/^  function patchUserModal\([\s\S]*?^  }/m)?.[0];
assert.ok(patchUserSource);
let userDraft = { username: "", error: "", fieldErrors: {} };
const patchUser = new Function("setUserModal", `${patchUserSource}; return patchUserModal;`)((update) => { userDraft = update(userDraft); });
patchUser("error", "Username required");
assert.equal(userDraft.error, "Username required", "general form validation must not clear its own message");
patchUser("username", "fixture");
assert.equal(userDraft.error, "", "editing a normal field clears obsolete general validation");
assert.doesNotMatch(read("components/OperatorProblemBanners.js"), /className=\{`operatorStatus /);
assert.match(read("app/storage/page.js"), /if \(statusRef\.current\)/);
assert.doesNotMatch(read("app/storage/page.js"), /refreshWarning \? <div className="storageOpsState/);
assert.match(read("components/cameras/CameraEditor.js"), /id: "camera-discovery"/);
assert.match(read("components/cameras/CameraEditor.js"), /inert=\{discoveryOpen \|\| confirm \? true : undefined\}/);
assert.match(read("components/cameras/CameraEditor.js"), /childWasOpenRef\.current && !childOpen/);
assert.match(read("components/cameras/CameraEditor.js"), /target\?\.focus\(\{ preventScroll: true \}\)/);
assert.doesNotMatch(read("lib/settingsMaintenanceController.js"), /settingsBodyScrollLockCount/);
assert.match(read("app/settings/page.js"), /useModalBodyScrollLock\(Boolean\(usersModalOpen \|\| userModal \|\| securityModalOpen\)\)/);
console.log("UI feedback layout stability: shared leases, truthful phases, real list request races, and presentation contracts passed");
