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
  "safeActions",
  "autoFreeSpace",
  "retentionDryRun",
  "reconciliationDryRun",
  "archiveRoots",
  "migrationPreview",
  "byCameras",
  "recentOperations",
  "technicalDetails",
]) {
  assert.match(storagePage, new RegExp(`copy\\.${required}\\b`), `/storage renders ${required}`);
}

assert.match(storagePage, /apiFetch\("\/settings"[\s\S]*auto_free_space_cleanup_enabled/, "/storage owns auto-free-space toggle");
assert.match(storagePage, /apiFetch\("\/recordings\/retention\/dry-run"/, "/storage owns retention preview");
assert.match(storagePage, /apiFetch\("\/recordings\/retention\/run"/, "/storage owns retention apply");
assert.match(storagePage, /apiFetch\("\/storage\/reconciliation\/summary"/, "/storage owns archive integrity preview");
assert.match(storagePage, /apiFetch\("\/storage\/reconcile"/, "/storage owns archive integrity apply");
assert.match(storagePage, /humanBlockerReason\(item, language\)/, "blocked actions use human wording");
assert.match(storagePage, /storageSourceLabel\(storageContract\.archive_primary_path_source, copy\)/, "backend source codes are mapped before display");
assert.match(storagePage, /<details className="storageOpsDetails">/, "technical details are collapsed");
assert.match(storagePage, /primaryStorageActionText\(\{ operations, pathHealth, capacity, policy, reconciliation, migrationPreview \}, language\)/, "primary action is reason-prioritized");
assert.match(storagePage, /factLabel\(pathHealth\.readable, language\)/, "unknown read state is not rendered as confirmed no");
assert.match(storagePage, /archiveRootLabel\(root, copy\)/, "archive root labels are mapped before display");
assert.match(storagePage, /<summary>\{copy\.addArchiveRoot\}<\/summary>/, "raw archive-root path input is collapsed");
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
  "ownershipBoundaryText",
  "retentionSafetyNote",
  "reconciliationConfirm",
  "technicalDetails",
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
