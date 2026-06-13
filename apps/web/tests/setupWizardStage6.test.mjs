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
assert.match(setupPage, /candidate_id: usingManualRoot \? "manual"/, "manual root mode uses explicit backend contract");
assert.match(setupPage, /storageReady = Boolean\(storageState\.confirmation\?\.ready/, "Next gate depends on active storage readiness");
assert.match(setupPage, /storageStatusText\(storageState\.confirmation\?\.status/, "operator UI maps backend statuses before rendering");
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
]) {
  assert.match(i18n, new RegExp(`${key}:\\s*"`), `i18n includes ${key}`);
}
