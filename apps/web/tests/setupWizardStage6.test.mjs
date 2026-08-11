import { readI18nSource } from "./helpers/readI18nSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");

const setupPage = read("app/setup/page.js");
const i18n = readI18nSource();

assert.equal((setupPage.match(/<LanguageSelect/g) || []).length, 1, "setup wizard renders exactly one language selector");
assert.match(setupPage, /manualPathSupported/, "setup wizard supports manual root fallback");
assert.match(setupPage, /UTC_TIMEZONES[\s\S]*timezoneValueForSettings/, "setup wizard reuses settings timezone helpers");
assert.match(setupPage, /setupTimezoneValue/, "setup wizard normalizes legacy IANA timezone names to GMT offset choices");
assert.match(setupPage, /timezone:\s*"Etc\/GMT-5"/, "setup wizard default timezone is a GMT offset value");
assert.match(setupPage, /className="select setupTimezoneSelect"/, "setup wizard renders timezone as a select, not a free text input");
assert.match(setupPage, /UTC_TIMEZONES\.map/, "setup wizard offers sorted GMT/UTC timezone choices");
assert.doesNotMatch(setupPage, /<option value=\{form\.timezone\}>/, "setup wizard does not expose raw IANA timezone text as a selectable option");
assert.match(setupPage, /setupFormGrid setupFormGrid-two/, "setup wizard aligns two-column fields through a dedicated setup grid");
assert.match(setupPage, /setupOwnerGrid[\s\S]*setupOwnerPassword[\s\S]*setupOwnerConfirm/, "owner password confirmation is grouped under the password field");
assert.match(setupPage, /setupStorageFolder[\s\S]*setupActionField/, "storage folder input and action button share the setup field rhythm");
assert.match(setupPage, /canAdvance = \[true, storageReady, ownerValid, recordingValid/, "setup wizard applies storage before owner credentials");
assert.doesNotMatch(setupPage, /system_name|systemName|reviewNote/, "removed system name is absent from setup UI and payload");
assert.match(setupPage, /if \(step === 1 && !storageReady\)/, "storage validation runs before owner password validation");
assert.match(setupPage, /if \(step === 2\)[\s\S]{0,220}form\.password/, "owner password validation runs after storage is active");
assert.match(setupPage, /step !== 1 \|\| busy/, "storage preview runs only on the storage step");
assert.match(setupPage, /candidate_id: usingManualRoot \? "manual"/, "manual root mode uses explicit backend contract");
assert.match(setupPage, /storageReady = Boolean\(storageState\.confirmation\?\.ready/, "Next gate depends on active storage readiness");
assert.match(setupPage, /storageStatusText\(storageState\.confirmation\?\.status/, "operator UI maps backend statuses before rendering");
assert.match(setupPage, /SETUP_DRAFT_KEY = "kmvms\.setupWizardDraft\.v1"/, "setup wizard uses versioned non-secret draft storage");
assert.match(setupPage, /window\.sessionStorage\.setItem\(SETUP_DRAFT_KEY/, "setup wizard persists non-secret draft in sessionStorage");
assert.match(setupPage, /password:\s*""/, "setup wizard clears password fields while restoring draft");
assert.match(setupPage, /function restoreDraftStep/, "setup wizard caps draft resume because passwords are not persisted");
assert.match(setupPage, /return step > 2 \? 2 : step/, "draft restore returns to owner step after reload instead of showing an invalid final review");
assert.doesNotMatch(setupPage, /sessionStorage\.setItem[\s\S]{0,500}password_confirm/, "setup wizard does not persist password confirmation");
assert.match(setupPage, /AbortController/, "final setup submit uses abortable request");
assert.match(setupPage, /SETUP_SUBMIT_TIMEOUT_MS = 30000/, "final setup submit has bounded timeout");
assert.match(setupPage, /async function recoverCompletedSetup/, "final setup submit can recover when the server completed but the client timed out");
assert.match(setupPage, /AbortError[\s\S]{0,180}recoverCompletedSetup\(\)/, "AbortError path checks backend completion before resetting the operator");
assert.doesNotMatch(setupPage, /<form className="setupCard setupWizard"/, "setup wizard does not use implicit browser form submit");
assert.doesNotMatch(setupPage, /onSubmit=\{/, "setup wizard completion is not wired through form submit");
assert.doesNotMatch(setupPage, /type="submit"/, "setup wizard has no implicit submit button");
assert.match(setupPage, /key="setup-finish"[\s\S]{0,120}type="button"[\s\S]{0,120}onClick=\{submitSetup\}/, "finish action is an explicit button click");
assert.match(setupPage, /storageActionDisabledReason\(\)[\s\S]*storageInputReason\(\)/, "storage action has a dedicated disabled gate");
assert.doesNotMatch(setupPage, /storageActionDisabledReason\(\)[\s\S]{0,250}!storageState\.preview/, "storage action is not disabled solely by missing preview");
assert.match(setupPage, /const actionLabel = storageState\.preview\?\.action === "create_and_select"/, "storage action label is driven by the confirmed preview action");
assert.doesNotMatch(setupPage, /const actionLabel = storageState\.previewing/, "background preview state must not leave the storage button stuck on checking");
assert.match(setupPage, /\{storageState\.applying \? t\.storageChecking : actionLabel\}/, "storage button shows checking only during the operator action");
assert.match(setupPage, /data\.ready[\s\S]{0,200}setStep/, "setup wizard resumes from backend active storage state");
assert.doesNotMatch(setupPage, />Status</, "setup page does not hardcode raw English status labels");
assert.doesNotMatch(setupPage, /pending_host_helper_restart_required|run_storage_apply_helper_and_restart/, "setup page does not leak old helper-only status codes");
assert.match(i18n, /steps: \["Язык", "Хранилище", "Владелец", "Запись", "Проверка"\]/, "Russian setup steps put storage before owner credentials");
assert.match(i18n, /steps: \["Language", "Storage", "Owner", "Recording", "Review"\]/, "English setup steps put storage before owner credentials");
assert.match(i18n, /Русский/, "language selector uses readable Russian label");
assert.match(i18n, /Первый запуск KM VMS/, "setup wizard Russian copy is not mojibake");

for (const key of [
  "storageManualOption",
  "storageCreateSelect",
  "storageStatusActive",
  "storageNextWait",
  "storageFolderWillBeCreated",
  "storageActionReady",
  "storageRetryAvailable",
  "setupSubmitTimeout",
  "setupSubmitFailed",
]) {
  assert.match(i18n, new RegExp(`${key}:\\s*"`), `i18n includes ${key}`);
}
