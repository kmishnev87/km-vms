import { readI18nSource } from "./helpers/readI18nSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import * as storageOperations from "../lib/storageOperations.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");
const storagePage = read("app/storage/page.js");
const storageSource = read("lib/storageOperations.js");
const i18n = readI18nSource();
const settingsPage = read("app/settings/page.js");

const context = storageOperations;

for (const required of [
  "copy.archiveAccess",
  "copy.refresh",
  "copy.total",
  "copy.used",
  "copy.free",
  "copy.recording",
  "copy.archiveProblems",
  "copy.archiveSpace",
  "copy.archiveManagementTitle",
  "copy.operationHistory",
  "copy.archiveRoots",
]) {
  assert.match(storagePage, new RegExp(required.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), `${required} is rendered`);
}

const rootsStart = storagePage.indexOf("<Section title={copy.archiveRoots}");
const managementStart = storagePage.indexOf("<ArchiveManagementCenter", rootsStart);
assert.ok(rootsStart > 0, "archive roots section exists");
assert.ok(managementStart > rootsStart, "full-width archive management follows archive roots");
assert.match(storagePage, /title: copy\.archiveManagementRetentionTitle/);
assert.match(storagePage, /title: copy\.archiveManagementIntegrityTitle/);
assert.match(storagePage, /title: copy\.archiveManagementMigrationTitle/);
assert.match(storagePage, /title: copy\.archiveManagementAutoFreeTitle/);
assert.match(storagePage, /<ArchivePolicySwitch[\s\S]*onChange=\{requestAutoFreeSpace\}/, "auto-free-space lives inside unified archive management");

for (const scattered of [
  "<Section title={copy.safeActions}",
  "<Section title={copy.autoFreeSpace}",
  "<Section title={copy.retention}",
  "<Section title={copy.integrity}",
  "<Section title={copy.migrationPreview}",
]) {
  assert.equal(storagePage.includes(scattered), false, `${scattered} must not return as a primary equal card`);
}

assert.match(i18n, /archiveManagementTitle: "Управление архивом"/);
assert.match(i18n, /archiveManagementProtectionGroup: "Хранение и защита"/);
assert.match(i18n, /operationHistoryTitle: "История операций с архивом"/);
assert.match(i18n, /refresh: "Обновить"/);
assert.doesNotMatch(i18n, /Предпросмотр регламента|Удалить по плану|Самое безопасное действие|Доступность: Нет/);

const primaryRenderStart = storagePage.indexOf("<section className={`storageOpsOverview");
const primaryRender = storagePage.slice(primaryRenderStart, storagePage.indexOf("<ArchiveIntegrityDialog", primaryRenderStart));
assert.doesNotMatch(primaryRender, /namespace|host_bind_env|Default archive|active_recording_jobs|raw JSON/i);
assert.match(primaryRender, /archiveRootPath\(root, archivePathText\)/, "archive roots show human archive paths");

assert.match(storagePage, /recordingState\(operations, pathHealth, policy, copy\)/);
assert.match(storagePage, /label=\{copy\.archiveAccess\}\s+value=\{recording\.label\}/);
assert.doesNotMatch(storagePage, /pathHealth\.available === false \? copy\.availabilityNeedsCheck : copy\.availabilityConfirmed/);
const unknownHealth = context.storageTopHealthModel({ operations: { status: "available" }, capacity: { total_bytes: 100 }, pathHealth: {} });
assert.equal(unknownHealth.status, "unknown");

assert.match(storagePage, /isStorageAccessDeniedError\(err\)/);
assert.match(storagePage, /if \(silent && statusRef\.current\)/);
assert.match(storagePage, /setRefreshWarning\(/);
assert.match(storagePage, /archiveRootScenarioModel/);
assert.match(storagePage, /retentionOperationPresentation/);
assert.match(storagePage, /integrityOperationPresentation/);
assert.match(storagePage, /migrationScenarioModel/);

assert.equal(settingsPage.includes("settings-storage"), false);
assert.equal(settingsPage.includes('href="/storage"'), false);

for (const [file, source] of [
  ["app/storage/page.js", storagePage],
  ["lib/storageOperations.js", read("lib/storageOperations.js")],
  ["lib/i18n.js", i18n],
]) {
  assert.doesNotMatch(source, /\uFFFD|Рџ|Рґ|Ð|�|\?\?\?/, `${file} must not contain mojibake markers`);
}
