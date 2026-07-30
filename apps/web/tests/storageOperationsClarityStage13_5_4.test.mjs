import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");

const storagePage = read("app/storage/page.js");
const settingsPage = read("app/settings/page.js");
const i18n = read("lib/i18n.js");
const storageHelpers = read("lib/storageOperations.js");

for (const required of [
  "healthOkTitle",
  "archiveSpace",
  "archiveManagementTitle",
  "archiveManagementProtectionGroup",
  "archiveManagementMaintenanceGroup",
  "archiveManagementRetentionTitle",
  "archiveManagementAutoFreeTitle",
  "archiveRoots",
  "archiveManagementIntegrityTitle",
  "archiveManagementMigrationTitle",
  "cameras",
  "operationHistory",
]) {
  assert.match(storagePage, new RegExp(`copy\\.${required}\\b`), `/storage renders ${required}`);
}

assert.match(storagePage, /apiFetch\("\/settings"[\s\S]*auto_free_space_cleanup_enabled/, "/storage owns auto-free-space toggle");
assert.doesNotMatch(storagePage, /apiFetch\("\/recordings\/retention\/(dry-run|run)"/, "automatic retention has no manual preview/apply UI");
assert.match(storagePage, /apiFetch\("\/storage\/integrity\/scans\/latest"/, "/storage loads durable integrity truth");
assert.match(storagePage, /apiFetch\("\/storage\/integrity\/scans"/, "/storage can start an explicit integrity scan");
assert.match(storagePage, /\/storage\/integrity\/scans\/\$\{encodeURIComponent\(scanId\)\}\/findings/, "/storage exposes bounded integrity findings");
assert.match(storagePage, /\/storage\/integrity\/remediation-plans\/\$\{encodeURIComponent\(integrityPlan\.plan_id\)\}\/apply-\$\{contract\.planKind\}/, "/storage applies only typed integrity remediation");
assert.match(storagePage, /humanBlockerReason\(item, language\)/, "blocked actions use human wording");
assert.match(storagePage, /Array\.from\(new Set\(labels\)\)\.join\(" "\)/, "duplicate blocked action labels are collapsed before rendering");
assert.doesNotMatch(storagePage, /storageSourceLabel\(storageContract\.archive_primary_path_source, copy\)/, "backend source codes are not primary UI");
assert.doesNotMatch(storagePage, /storageOpsSupportDetails/, "the obsolete page-level support block stays removed");
assert.match(storagePage, /<ArchiveManagementCenter/, "archive scenarios are composed into one management center");
assert.match(storagePage, /<OperationDialog dialog=\{historyDialog\}/, "bounded operation history is opened in a modal");
assert.match(storagePage, /healthActionText\(topHealth, copy\)/, "primary action is reason-prioritized");
assert.match(storagePage, /accessRightsModel\(pathHealth, language\)/, "read/write access is combined before primary rendering");
assert.match(storagePage, /archiveRootPath\(root, archivePathText\)/, "archive root paths are mapped before display");
assert.match(storagePage, /\/storage\/archive-roots\/discovery/, "archive root flow discovers NAS roots before selection");
assert.match(storagePage, /storageOpsRootForm-product/, "archive root primary flow is a product selection form");
assert.doesNotMatch(storagePage, /copy\.addArchiveRootAdvanced|archiveRootManualPath|\brootPath\b/, "manual archive-root path input is not exposed in /storage");
assert.doesNotMatch(storagePage, /<pre|raw JSON|JSON block/i, "production storage UI does not render raw JSON blocks");
assert.doesNotMatch(storagePage, /href="\/settings"/, "/storage does not send storage workflows back to Settings");

for (const forbiddenSettingsSource of [
  "retentionPreview",
  "retentionResult",
  "retentionBusy",
  "retentionConfirmed",
  "reconciliationPreview",
  "reconciliationResult",
  "reconciliationBusy",
  "reconciliationConfirmed",
  "runRetentionPreview",
  "applyRetentionPlan",
  "runReconciliationPreview",
  "applyReconciliationSafe",
  'href="/storage"',
  'id="settings-auto-free-space"',
  "archive_primary_path",
  "storage_host_path",
  "storage_root",
  "storage_recordings_path",
  "auto_free_space_cleanup_enabled",
  "storageOperationsMoved",
  "storageOperationsOpen",
]) {
  assert.equal(settingsPage.includes(forbiddenSettingsSource), false, `Settings must not contain ${forbiddenSettingsSource}`);
}
assert.doesNotMatch(settingsPage, /<OperatorProblemBanners[\s\S]*storage/, "Settings must not render storage warning content");

for (const key of [
  "healthOkTitle",
  "archiveManagementTitle",
  "archiveManagementRetentionStatusHealthy",
  "archiveManagementIntegrityStatusNotRun",
  "operationHistoryTitle",
  "defaultArchive",
  "addArchiveRoot",
]) {
  assert.match(i18n, new RegExp(`${key}:\\s*"`), `i18n includes ${key}`);
}

for (const bad of ["namespace_bounded", "max_dirs", "active_recording_jobs", "Default archive"]) {
  assert.equal(storagePage.includes(bad), false, `raw internal label ${bad} is not primary /storage UI`);
}
for (const visibleBad of ["bounded", "Default archive", "host_bind_env", "namespace_bounded", "active_recording_jobs"]) {
  assert.equal(i18n.includes(visibleBad), false, `visible i18n must not contain ${visibleBad}`);
}
assert.doesNotMatch(storagePage, /value=\{storageContract\.archive_primary_path_source/, "raw archive source is not rendered directly");
assert.match(storageHelpers, /active_recording_jobs[\s\S]*Перенос заблокирован/, "raw migration blocker has a human RU label");
assert.match(storageHelpers, /function factLabel/, "helper distinguishes unknown facts from confirmed false");
assert.match(storageHelpers, /function accessRightsModel/, "helper combines read/write access for primary UI");
assert.match(storageHelpers, /function primaryStorageActionText/, "helper centralizes primary action priority");

for (const stage3Guard of [
  "components/VideoZoomPanSurface.js",
  "lib/videoZoomPanCore.js",
  "components/TilePlayer.js",
  "components/ArchiveTilePlayer.js",
]) {
  const source = read(stage3Guard);
  assert.match(source, /VideoZoomPanSurface|requestFullscreen|zoom|CompactVideoCanvas/, `${stage3Guard} remains present for Stage 3 guard`);
}
