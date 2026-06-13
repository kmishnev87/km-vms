import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");

const setupPage = read("app/setup/page.js");
const i18n = read("lib/i18n.js");

assert.equal((setupPage.match(/<LanguageSelect/g) || []).length, 1, "setup wizard renders exactly one language selector");
assert.match(setupPage, /manualPathSupported/, "setup wizard supports manual root fallback");
assert.match(setupPage, /UTC_TIMEZONES[\s\S]*timezoneValueForSettings/, "setup wizard reuses settings timezone helpers");
assert.match(setupPage, /setupTimezoneValue/, "setup wizard normalizes legacy IANA timezone names to GMT offset choices");
assert.match(setupPage, /timezone:\s*"Etc\/GMT-5"/, "setup wizard default timezone is a GMT offset value");
assert.match(setupPage, /className="select setupTimezoneSelect"/, "setup wizard renders timezone as a select, not a free text input");
assert.match(setupPage, /UTC_TIMEZONES\.map/, "setup wizard offers sorted GMT/UTC timezone choices");
assert.doesNotMatch(setupPage, /<option value=\{form\.timezone\}>/, "setup wizard does not expose raw IANA timezone text as a selectable option");
assert.match(setupPage, /candidate_id: usingManualRoot \? "manual"/, "manual root mode uses explicit backend contract");
assert.match(setupPage, /storageReady = Boolean\(storageState\.confirmation\?\.ready/, "Next gate depends on active storage readiness");
assert.match(setupPage, /storageStatusText\(storageState\.confirmation\?\.status/, "operator UI maps backend statuses before rendering");
assert.match(setupPage, /SETUP_DRAFT_KEY = "kmvms\.setupWizardDraft\.v1"/, "setup wizard uses versioned non-secret draft storage");
assert.match(setupPage, /window\.sessionStorage\.setItem\(SETUP_DRAFT_KEY/, "setup wizard persists non-secret draft in sessionStorage");
assert.match(setupPage, /password:\s*""/, "setup wizard clears password fields while restoring draft");
assert.doesNotMatch(setupPage, /sessionStorage\.setItem[\s\S]{0,500}password_confirm/, "setup wizard does not persist password confirmation");
assert.match(setupPage, /AbortController/, "final setup submit uses abortable request");
assert.match(setupPage, /SETUP_SUBMIT_TIMEOUT_MS = 30000/, "final setup submit has bounded timeout");
assert.match(setupPage, /storageActionDisabledReason\(\)[\s\S]*storageInputReason\(\)/, "storage action has a dedicated disabled gate");
assert.doesNotMatch(setupPage, /storageActionDisabledReason\(\)[\s\S]{0,250}!storageState\.preview/, "storage action is not disabled solely by missing preview");
assert.match(setupPage, /data\.ready[\s\S]{0,200}setStep/, "setup wizard resumes from backend active storage state");
assert.doesNotMatch(setupPage, />Status</, "setup page does not hardcode raw English status labels");
assert.doesNotMatch(setupPage, /pending_host_helper_restart_required|run_storage_apply_helper_and_restart/, "setup page does not leak old helper-only status codes");
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
