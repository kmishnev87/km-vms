import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");

const settingsPage = read("app/settings/page.js");
const settingsHelpers = read("lib/settingsPageHelpers.js");

for (const forbidden of [
  "settings-storage",
  "settings-auto-free-space",
  "storageOperationsOpen",
  "docker-compose",
  "storageOperationsMoved",
  "storageContainerPath",
  "storageText",
  "autoFreeSpace",
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
  "archive_primary_path",
  "storage_host_path",
  "storage_root",
  "storage_recordings_path",
  "auto_free_space_cleanup_enabled",
  'href="/storage"',
]) {
  assert.equal(settingsPage.includes(forbidden), false, `Settings page must not contain ${forbidden}`);
}

for (const forbidden of [
  "storage_path",
  "archive_primary_path",
  "storage_host_path",
  "storage_root",
  "storage_recordings_path",
  "storage_namespace",
  "storage_change_requires",
  "auto_free_space_cleanup_enabled",
  "recording_suspended_by_low_disk",
]) {
  assert.equal(settingsHelpers.includes(forbidden), false, `Settings draft/payload must not carry removed storage field ${forbidden}`);
}

assert.match(settingsPage, /Системные параметры KM VMS: язык, время, запись, ускорение, безопасность и обслуживание\./);
