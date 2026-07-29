import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const feedback = read("components/OperationFeedback.js");
const sharedCss = read("app/styles/40-storage-records-shared.css");
const responsiveCss = read("app/styles/60-responsive-shared.css");
const settingsCss = read("app/styles/20-settings-maintenance.css");
const camerasCss = read("app/styles/50-cameras-shared-modals.css");
const settings = read("app/settings/page.js");
const cameras = read("app/cameras/page.js");
const live = read("app/live/page.js");
const chronology = read("app/chronology/page.js");
const recordings = read("app/recordings/page.js");
const storage = read("app/storage/page.js");
const login = read("app/login/page.js");
const diagnostics = read("components/AuditDiagnosticsEntries.js");
const i18n = read("lib/i18n.js");

const toastSource = feedback.slice(feedback.indexOf("export function OperationToast"));
assert.match(toastSource, /window\.setTimeout\(\(\) => onCloseRef\.current\?\.\(\), 2500\)/);
assert.match(toastSource, /\}, \[toast\]\);/);
assert.doesNotMatch(toastSource, /autoDismissMs/);
assert.doesNotMatch(toastSource, /<button/);
assert.match(toastSource, /\["success", "error", "warning", "info"\]/);

assert.match(sharedCss, /\.operationFeedbackToastRegion\s*\{[\s\S]*?top:\s*50%;[\s\S]*?left:\s*50%;[\s\S]*?transform:\s*translate\(-50%, -50%\);/);
for (const tone of ["success", "error", "warning", "info"]) {
  assert.match(sharedCss, new RegExp(`\\.operationFeedbackToast-${tone}\\s*\\{`));
}
assert.match(sharedCss, /\.operationFeedbackDialog-compactConfirmation\s*\{/);
assert.match(responsiveCss, /\.operationFeedbackOverlay-compactConfirmation\s*\{[\s\S]*?align-items:\s*center;/);

assert.doesNotMatch(settings, /settingsToast|toastTimerRef|settingsConfirmModal|settingsConfirmOverlay/);
assert.doesNotMatch(settingsCss, /\.settingsToast|\.settingsConfirmModal|\.settingsConfirmOverlay/);
assert.doesNotMatch(cameras, /cameraProfileToast|profileToastTimerRef/);
assert.doesNotMatch(camerasCss, /\.cameraProfileToast/);
assert.match(settings, /<OperationToast toast=\{toast\}/);
assert.match(cameras, /<OperationToast toast=\{operationToast\}/);

for (const marker of [
  'id: `user-delete-${userDeleteTarget.id}`',
  'id: "maintenance-confirm"',
  'id: "update-apply-confirm"',
]) {
  const markerIndex = settings.indexOf(marker);
  assert.ok(markerIndex >= 0, `${marker} missing`);
  assert.ok(settings.slice(markerIndex, markerIndex + 260).includes('presentation: "compact-confirmation"'));
}
assert.match(cameras, /id:\s*`camera-delete-\$\{cameraToDelete\?\.id \|\| "current"\}`[\s\S]*?presentation:\s*"compact-confirmation"/);
assert.equal((recordings.match(/presentation:\s*"compact-confirmation"/g) || []).length, 3);
assert.ok((storage.match(/compact-confirmation/g) || []).length >= 5);

assert.match(live, /live-duplicate-[\s\S]*?tone:\s*"info"/);
assert.match(live, /live-audio-blocked-[\s\S]*?tone:\s*"warning"/);
assert.doesNotMatch(live, /setError\(TEXT\.duplicate\)|setError\(TEXT\.audioBlocked\)/);
assert.match(chronology, /chronology-duplicate-[\s\S]*?tone:\s*"info"/);
assert.match(chronology, /chronology-download-[\s\S]*?tone:\s*"success"/);
assert.match(chronology, /startQuickDownloadForCamera\(cameraId, \{ notify: false \}\)/);
assert.match(chronology, /if \(notify\) \{[\s\S]*?setOperationToast/);
assert.doesNotMatch(chronology, /setExportStatus\(`\$\{TEXT\.quickDownloadReady\}/);

assert.match(recordings, /recordings-clip-[\s\S]*?tone:\s*"success"/);
assert.doesNotMatch(recordings, /const \[notice, setNotice\]|setNotice\(t\.exportReady\)/);
assert.match(storage, /if \(completed\) \{[\s\S]*?setArchiveRootDialog[\s\S]*?setOperationToast/);
assert.doesNotMatch(storage, /const operationId = archiveRootDialog\?\.activationOperationId/);

assert.match(cameras, /const cleanupPartial = deleteFiles/);
assert.match(cameras, /if \(cleanupPartial\) \{[\s\S]*?setDeleteWarning\(cleanupMessage\)/);
assert.match(cameras, /setOperationToast\(\{[\s\S]*?camera-delete-/);
assert.match(cameras, /if \(!result\?\.camera_removed\)/);

assert.match(settings, /sessionStorage\.setItem\(CREDENTIALS_CHANGED_NOTICE_KEY, "credentials_changed"\)/);
assert.match(login, /sessionStorage\.getItem\(CREDENTIALS_CHANGED_NOTICE_KEY\)/);
assert.match(login, /sessionStorage\.removeItem\(CREDENTIALS_CHANGED_NOTICE_KEY\)/);
assert.match(login, /<OperationToast toast=\{operationToast\}/);
assert.equal((i18n.match(/credentialsChanged:/g) || []).length, 3);

assert.match(diagnostics, /const \[error, setError\]/);
assert.match(diagnostics, /diagnostics-archive-[\s\S]*?tone:\s*"success"/);
assert.match(diagnostics, /diagnostics-bug-report-[\s\S]*?tone:\s*"success"/);
assert.match(diagnostics, /settingsJournalEmpty error/);
assert.doesNotMatch(diagnostics, /const \[message, setMessage\]/);

assert.doesNotMatch(settings, /function submitBugReport/);
assert.doesNotMatch(settings, /onClick=\{submitBugReport\}/);
assert.match(settings, /title=\{t\.reportSendingPending\}/);

console.log("UI feedback and compact confirmation unification tests passed");
