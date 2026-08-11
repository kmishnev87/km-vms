import assert from "node:assert/strict";
import fs from "node:fs";
import path, { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readSettingsPageHelperSource } from "./helpers/readSettingsPageHelperSources.mjs";
import { readSettingsMaintenanceSources } from "./helpers/readSettingsMaintenanceSources.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const repoRoot = resolve(webRoot, "../..");
const settingsPage = readSettingsMaintenanceSources();
const settingsHelpers = readSettingsPageHelperSource();
const css = fs.readFileSync(resolve(webRoot, "app/styles/20-settings-maintenance.css"), "utf8");

function walkFiles(root) {
  const result = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) result.push(...walkFiles(full));
    else result.push(full);
  }
  return result;
}

const appRoutes = walkFiles(resolve(webRoot, "app"))
  .map((file) => path.relative(resolve(webRoot, "app"), file).replaceAll(path.sep, "/"));

assert.equal(appRoutes.some((file) => file === "update/page.js" || file.startsWith("update/")), false, "Stage 6.1.0 must not create a standalone update route");
assert.equal(settingsPage.includes('className="settingsUpdateApplyPanel"'), true, "Update apply panel must remain inside Settings Maintenance");
assert.equal(settingsPage.includes('apiFetch("/system/update/status")'), true);
assert.equal(settingsPage.includes('apiFetch("/system/update/apply/status")'), true);
assert.equal(settingsPage.includes('apiFetch("/system/update/apply"'), true);
assert.equal(settingsPage.includes('apiFetch("/system/update/check"'), true, "Update check must stay inside the dedicated update apply panel");
assert.equal(settingsPage.includes('variant: checkFailed ? "warning" : "success"'), true, "A failed check must not show a success toast");
assert.equal(settingsHelpers.includes('update: { path: "/system/update/check", body: {} }'), false, "Legacy maintenance overview update flow must be removed");

assert.equal(settingsPage.includes("buildUpdateApplyConfirmation(t, updateStatus)"), false);
assert.equal(settingsPage.includes("updateApplyCandidateSnapshot(updateStatus)"), true);
assert.equal(settingsPage.includes("expected_manifest_version"), true);
assert.equal(settingsPage.includes("expected_manifest_commit"), true);
assert.equal(settingsPage.includes("updateApplyOperatorModel"), true);
assert.equal(settingsHelpers.includes("updateApplyEffectiveStatus"), true);
assert.equal(settingsHelpers.includes("updateApplyRecoveryText"), true);
assert.equal(settingsHelpers.includes("updateApplyFactRows"), true);
assert.equal(settingsPage.includes("settingsUpdateApplySupport"), false);
assert.equal(settingsPage.includes("formatUpdateNotice(item, t, lang)"), false);
assert.equal(settingsPage.includes("{item.message || item.code}"), false);
assert.equal(settingsHelpers.includes("commit_verified === false"), true);
assert.equal(settingsHelpers.includes("expected_commit"), true);
assert.equal(settingsHelpers.includes("installed_commit"), true);
assert.equal(settingsHelpers.includes("installed source metadata is unavailable or invalid"), true);
assert.equal(settingsHelpers.includes("last update metadata is unavailable or invalid"), true);

assert.equal(settingsPage.includes("Применение не запускается с этого экрана"), false);
assert.equal(settingsPage.includes("Apply is not executed from this screen"), false);
assert.equal(settingsPage.includes("raw JSON"), false);
assert.equal(settingsPage.includes("helper logs"), false);
assert.equal(settingsPage.includes("localStorage"), false);
assert.equal(settingsPage.includes("UPDATE_APPLY_PENDING_STORAGE_KEY"), true);
assert.equal(settingsPage.includes("sessionStorage.setItem(TOKEN_KEY"), false);
assert.equal(settingsPage.includes('name="token"'), false);
assert.equal(settingsPage.includes('name="url"'), false);
assert.equal(settingsPage.includes('name="repo"'), false);
assert.equal(settingsPage.includes('name="ref"'), false);
assert.equal(settingsPage.includes('name="path"'), false);

assert.equal(css.includes(".settingsUpdateApplyNotice"), true);
assert.equal(css.includes(".settingsUpdateApplyWarnings"), true);
assert.equal(css.includes("overflow-wrap: anywhere"), true);

const productFiles = walkFiles(repoRoot)
  .filter((file) => !file.includes(`${path.sep}.git${path.sep}`))
  .filter((file) => !file.includes(`${path.sep}node_modules${path.sep}`))
  .filter((file) => !file.includes(`${path.sep}Working folder${path.sep}`))
  .map((file) => path.relative(repoRoot, file).replaceAll(path.sep, "/"));

assert.equal(productFiles.some((file) => file === "apps/web/app/update/page.js"), false);
