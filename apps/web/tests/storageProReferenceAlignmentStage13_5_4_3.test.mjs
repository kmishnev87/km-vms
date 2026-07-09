import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");
const storagePage = read("app/storage/page.js");
const storageSource = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const i18n = read("lib/i18n.js");
const settingsPage = read("app/settings/page.js");

const context = {};
vm.runInNewContext(
  `${storageSource}
this.storageTopHealthModel = storageTopHealthModel;
this.accessRightsModel = accessRightsModel;`,
  context
);

for (const required of [
  "copy.primaryAction",
  "copy.refresh",
  "copy.total",
  "copy.used",
  "copy.free",
  "copy.recording",
  "copy.archiveProblems",
  "copy.archiveSpace",
  "copy.archiveOperations",
  "copy.archiveRoots",
  "copy.supportDetails",
]) {
  assert.match(storagePage, new RegExp(required.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), `${required} is rendered`);
}

const operationsStart = storagePage.indexOf("<Section title={copy.archiveOperations}");
const operationsEnd = storagePage.indexOf("<Section title={copy.archiveRoots}", operationsStart);
assert.ok(operationsStart > 0, "unified archive operations section exists");
assert.ok(operationsEnd > operationsStart, "archive roots follow the operations section");
const operationsBlock = storagePage.slice(operationsStart, operationsEnd);
assert.match(operationsBlock, /copy\.retentionRules/);
assert.match(operationsBlock, /copy\.integrityCheck/);
assert.match(operationsBlock, /copy\.archiveMigration/);
assert.match(operationsBlock, /copy\.autoFreeSpace/);
assert.match(operationsBlock, /setAutoFreeSpace/, "auto-free-space lives inside unified archive operations");

for (const scattered of [
  "<Section title={copy.safeActions}",
  "<Section title={copy.autoFreeSpace}",
  "<Section title={copy.retention}",
  "<Section title={copy.integrity}",
  "<Section title={copy.migrationPreview}",
]) {
  assert.equal(storagePage.includes(scattered), false, `${scattered} must not return as a primary equal card`);
}

assert.match(i18n, /retentionDryRun: "Показать, что будет удалено по правилам хранения"/);
assert.match(i18n, /retentionApply: "Удалить найденные старые записи"/);
assert.match(i18n, /reconciliationApply: "Исправить только безопасные проблемы метаданных"/);
assert.match(i18n, /refresh: "Обновить"/);
assert.doesNotMatch(i18n, /Предпросмотр регламента|Удалить по плану|Применить безопасно|Самое безопасное действие|Доступность: Нет/);

const primaryRender = storagePage.slice(storagePage.indexOf("<section className={`storageOpsOverview"), storagePage.indexOf("<details className=\"storageOpsSupportDetails\""));
assert.doesNotMatch(primaryRender, /namespace|host_bind_env|Default archive|active_recording_jobs|raw JSON/i);
assert.match(primaryRender, /archiveRootPath\(root, archivePathText\)/, "archive roots show human archive paths");

assert.match(storagePage, /availabilityState\(pathHealth, copy\)/);
assert.match(storagePage, /recordingState\(operations, pathHealth, policy, copy\)/);
assert.doesNotMatch(storagePage, /pathHealth\.available === false \? copy\.availabilityNeedsCheck : copy\.availabilityConfirmed/);
assert.equal(context.accessRightsModel({}).label, "Права на чтение и запись: не проверены");
const unknownHealth = context.storageTopHealthModel({ operations: { status: "available" }, capacity: { total_bytes: 100 }, pathHealth: {} });
assert.equal(unknownHealth.status, "unknown");

assert.match(storagePage, /isStorageAccessDeniedError\(err\)/);
assert.match(storagePage, /if \(silent && statusRef\.current\)/);
assert.match(storagePage, /setRefreshWarning\(/);
assert.match(storagePage, /archiveRootScenarioModel/);
assert.match(storagePage, /retentionScenarioModel/);
assert.match(storagePage, /reconciliationScenarioModel/);
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
