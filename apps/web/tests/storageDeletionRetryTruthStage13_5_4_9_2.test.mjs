import { readI18nSource } from "./helpers/readI18nSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (file) => fs.readFileSync(resolve(root, file), "utf8");
const { archiveRootCleanupCapabilityModel } = await import(
  pathToFileURL(resolve(root, "lib/storageOperations.js"))
);

assert.deepEqual(
  archiveRootCleanupCapabilityModel({ retry_mode: "immediate", next_action: "retry_cleanup", retry_available: true }),
  {
    retryMode: "immediate",
    nextAction: "retry_cleanup",
    canRetryNow: true,
    shouldRefresh: false,
    needsExternalFix: false,
    retryAvailable: true,
  }
);
assert.equal(
  archiveRootCleanupCapabilityModel({ retry_mode: "after_refresh", next_action: "refresh_storage_state", retry_available: true }).shouldRefresh,
  true
);
assert.equal(
  archiveRootCleanupCapabilityModel({ retry_mode: "after_external_fix", next_action: "correct_storage_access" }).needsExternalFix,
  true
);
assert.equal(
  archiveRootCleanupCapabilityModel({ retry_mode: "none", next_action: "close", retry_available: true }).canRetryNow,
  false
);

for (const invalid of [
  {},
  { retry_mode: "immediate", next_action: "close", retry_available: true },
  { retry_mode: "unknown", next_action: "retry_cleanup", retry_available: true },
]) {
  assert.deepEqual(archiveRootCleanupCapabilityModel(invalid), {
    retryMode: "none",
    nextAction: "close",
    canRetryNow: false,
    shouldRefresh: false,
    needsExternalFix: false,
    retryAvailable: false,
  });
}

const storagePage = read("app/storage/page.js");
const i18n = readI18nSource();
assert.match(storagePage, /archiveRootCleanupCapabilityModel\(detail\)/);
assert.match(storagePage, /id: `root-delete-running-\$\{root\.id\}`/);
assert.match(storagePage, /busy: true,[\s\S]*?dismissible: false/);
assert.match(storagePage, /await loadStatus\(\{ silent: true \}\);[\s\S]*?setArchiveRootDialog\(null\);[\s\S]*?setOperationToast/);
assert.doesNotMatch(storagePage, /detail\?\.retry_available === true/);
assert.match(storagePage, /capability\.canRetryNow/);
assert.match(storagePage, /capability\.shouldRefresh/);
assert.match(storagePage, /capability\.needsExternalFix/);
assert.match(storagePage, /code:\s*"archive-root-cleanup"/);
assert.doesNotMatch(storagePage, /code:\s*String\(detail\.reason/);

for (const key of [
  "archiveRootCleanupRefreshAction",
  "archiveRootCleanupExternalFixAction",
  "archiveRootCleanupNoRetryAction",
  "archiveRootCleanupAccessProblem",
]) {
  assert.equal((i18n.match(new RegExp(`${key}:`, "g")) || []).length, 3, `${key} must exist in ru/en/zh-CN`);
}

console.log("Stage 13.5.4.9.2 cleanup retry truth contracts passed");
