import { readI18nSource } from "./helpers/readI18nSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const feedback = read("components/OperationFeedback.js");
const icons = read("components/CompactActionIcons.js");
const entries = read("components/AuditDiagnosticsEntries.js");
const cameras = read("app/cameras/page.js");
const settings = read("app/settings/page.js");
const storage = read("app/storage/page.js");
const systemStatus = read("app/system-status/page.js");
const i18n = readI18nSource();
const settingsCss = read("app/styles/20-settings-maintenance.css");
const sharedCss = read("app/styles/40-storage-records-shared.css");
const camerasCss = read("app/styles/50-cameras-shared-modals.css");
const responsiveCss = read("app/styles/60-responsive-shared.css");

const openCreate = cameras.slice(
  cameras.indexOf("function openCreate()"),
  cameras.indexOf("function openEdit(")
);
assert.doesNotMatch(openCreate, /setDeleteNotice|setDeleteWarning/);
assert.match(openCreate, /setShowEditor\(true\)/);
assert.match(cameras, /className="pageWarnings cameraPageWarnings"/);
assert.doesNotMatch(cameras, /\\u270e|\\u23fb|\\u2713|\\ud83d\\uddd1/);
for (const icon of ["EditIcon", "PowerIcon", "CheckIcon", "TrashIcon"]) {
  assert.match(icons, new RegExp(`export function ${icon}`));
}
assert.match(camerasCss, /\.cameraPageWarnings\s*\{[\s\S]*?margin-bottom:\s*16px;/);
assert.match(camerasCss, /\.cameraTileIconButton\s*\{[\s\S]*?width:\s*34px;[\s\S]*?height:\s*34px;/);

assert.match(entries, /diagnosticsEntry\.contentTitle/);
assert.match(entries, /securityJournal\.contentTitle/);
assert.match(entries, /className="diagnosticsEntryGrid"/);
assert.equal((entries.match(/presentation:\s*"neutral-choice"/g) || []).length, 1);
assert.equal((entries.match(/tone:\s*"neutral"/g) || []).length, 1);
assert.match(settings, /id:\s*"diagnostic-archive-choice"[\s\S]*?presentation:\s*"neutral-choice"[\s\S]*?tone:\s*"neutral"/);
assert.match(storage, /id:\s*"archive-operation-history"[\s\S]*?tone:\s*"neutral"/);
assert.match(feedback, /operationFeedbackDialog-neutralChoice/);
assert.match(sharedCss, /\.operationFeedbackDialog-neutral\s*\{/);
assert.match(sharedCss, /\.operationFeedbackDescriptions-neutralChoice\s*\{[\s\S]*?grid-template-columns:\s*repeat\(2,/);
assert.match(sharedCss, /\.operationFeedbackFooter-neutralChoice\s*\{[\s\S]*?grid-template-columns:\s*repeat\(2,/);
assert.match(responsiveCss, /\.operationFeedbackDescriptions-neutralChoice,[\s\S]*?\.operationFeedbackFooter-neutralChoice\s*\{[\s\S]*?grid-template-columns:\s*1fr;/);

for (const label of ["t.edit", "t.activate", "t.deactivate", "t.delete"]) {
  assert.equal(settings.includes(label), true, `${label} must drive a localized user action`);
}
assert.doesNotMatch(settings, /title="(?:Изменить|Удалить)"|aria-label="(?:Изменить|Удалить)"/);
assert.match(settingsCss, /\.settingsUserStatus\s*\{[\s\S]*?width:\s*fit-content;/);
assert.match(settingsCss, /\.settingsUserActions\s*\{[\s\S]*?gap:\s*6px;/);

assert.match(systemStatus, /formatProductDateTime/);
assert.match(systemStatus, /runtimeStatus\?\.system_timezone/);
assert.match(systemStatus, /dateTime=\{systemHealth\.runtimeStatus\?\.generated_at_utc/);
assert.equal((i18n.match(/contentTitle:/g) || []).length, 6);
assert.equal((i18n.match(/integrityCheckAgain:\s*"Проверить снова"/g) || []).length, 1);

const integrityFooter = storage.slice(
  storage.indexOf('<footer className="storageIntegrityDialogFooter">'),
  storage.indexOf("</footer>", storage.indexOf('<footer className="storageIntegrityDialogFooter">'))
);
assert.ok(integrityFooter.indexOf("copy.close") < integrityFooter.indexOf("primaryStartLabel"));
assert.match(sharedCss, /\.storageIntegrityDialogFooter\s*\{[\s\S]*?justify-content:\s*space-between;/);
const integrityMobileCss = sharedCss.slice(sharedCss.indexOf("@media (max-width: 760px)", sharedCss.indexOf(".storageIntegrityDialogFooter")));
assert.match(integrityMobileCss, /\.storageIntegrityDialogFooter \.button\s*\{[\s\S]*?flex:\s*0 0 auto;/);
assert.match(integrityMobileCss, /\.storageIntegrityDialogPrimaryActions\s*\{[\s\S]*?flex:\s*0 0 auto;[\s\S]*?margin-left:\s*auto;/);
assert.doesNotMatch(integrityMobileCss, /\.storageIntegrityDialogFooter \.button\s*\{[\s\S]*?flex:\s*1 1 auto;/);
assert.match(responsiveCss, /\.storageIntegrityDialogFooter \.button\s*\{[\s\S]*?width:\s*auto;[\s\S]*?flex:\s*0 0 auto;/);
assert.match(responsiveCss, /\.storageIntegrityDialogPrimaryActions\s*\{[\s\S]*?width:\s*auto;[\s\S]*?flex:\s*0 0 auto;[\s\S]*?margin-left:\s*auto;/);

console.log("Stage 13.5.5 operator UI consistency contract passed");
