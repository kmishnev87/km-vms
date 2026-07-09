import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");
const storagePage = read("app/storage/page.js");
const storageCss = read("app/styles/40-storage-records-shared.css");
const responsiveCss = read("app/styles/60-responsive-shared.css");
const i18n = read("lib/i18n.js");
const settingsPage = read("app/settings/page.js");
const storageSource = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");

const context = {};
vm.runInNewContext(
  `${storageSource}
this.storageTopHealthModel = storageTopHealthModel;
this.primaryStorageActionText = primaryStorageActionText;`,
  context
);

for (const pathHealth of [
  { readable: true, writable: true, available: undefined },
  { readable: true, writable: true, available: null },
  { readable: true, writable: true },
]) {
  const args = { operations: { status: "available" }, capacity: { total_bytes: 100 }, pathHealth };
  const health = context.storageTopHealthModel(args, "ru");
  assert.equal(health.status, "unknown", "missing availability must stay unknown");
  assert.notEqual(context.primaryStorageActionText(args, "ru"), "Немедленных действий не требуется.");
}

assert.doesNotMatch(storagePage, /<Section title=\{copy\.diagnostics\}/, "diagnostics is not a main card");
assert.match(storagePage, /storageOpsSupportDetails/, "support details are collapsed and low priority");
const supportStart = storagePage.indexOf("storageOpsSupportDetails");
const primary = storagePage.slice(storagePage.indexOf("<section className={`storageOpsOverview"), supportStart);
assert.doesNotMatch(primary, /copy\.ownershipBoundary|copy\.ownershipBoundaryText|copy\.deletedExcluded|copy\.dockerPath|raw JSON|active_recording_jobs/i);

assert.match(storagePage, /copy\.currentArchive/);
assert.match(storagePage, /const currentArchivePath = archiveRootPath\(currentArchiveRoot, archivePathText\)/);
assert.match(storagePage, /storageOpsRootList/);
assert.match(storagePage, /archiveRootPath\(root, archivePathText\)/);
assert.match(storagePage, /archiveRoots\.length > 1 \?/, "engineering root table is only behind multi-root advanced details");
assert.doesNotMatch(storagePage, /copy\.addArchiveRootAdvanced|archiveRootManualPath|\brootPath\b/);
assert.doesNotMatch(primary, /archiveRootSummary/);

const operationsStart = storagePage.indexOf("<Section title={copy.archiveOperations}");
const operationsEnd = storagePage.indexOf("<Section title={copy.archiveRoots}", operationsStart);
const operations = storagePage.slice(operationsStart, operationsEnd);
assert.match(operations, /copy\.retentionRules/);
assert.match(operations, /copy\.autoFreeSpace/);
assert.match(operations, /copy\.integrityCheck/);
assert.match(operations, /copy\.archiveMigration/);
assert.match(operations, /setAutoFreeSpace/);
assert.doesNotMatch(operations, /<summary>\{copy\.technicalDetails\}<\/summary>/);
assert.match(operations, /<summary>\{copy\.retentionDiagnostics\}<\/summary>/);
assert.match(operations, /copy\.retentionPlanShort/);
assert.match(operations, /copy\.integrityCheckShort/);
assert.match(operations, /copy\.migrationPreviewShort/);

assert.match(storagePage, /storageOpsCameraCards/);
assert.match(responsiveCss, /storageOpsCameraTable[\s\S]*display:\s*none/);
assert.match(storageCss, /storageOpsDashboard/);
assert.match(storageCss, /storageOpsRootList/);

assert.match(i18n, /supportDetails: "Служебные сведения для поддержки"/);
assert.match(i18n, /currentArchive: "Текущий архив"/);
assert.doesNotMatch(i18n, /Предпросмотр регламента|Удалить по плану|Применить безопасно|Самое безопасное действие|Доступность: Нет/);

assert.equal(settingsPage.includes("settings-storage"), false);
assert.equal(settingsPage.includes('href="/storage"'), false);

for (const [file, source] of [
  ["app/storage/page.js", storagePage],
  ["app/styles/40-storage-records-shared.css", storageCss],
  ["app/styles/60-responsive-shared.css", responsiveCss],
  ["lib/i18n.js", i18n],
]) {
  assert.doesNotMatch(source, /\uFFFD|Рџ|Рґ|Ð|�|\?\?\?/, `${file} must not contain mojibake markers`);
}
