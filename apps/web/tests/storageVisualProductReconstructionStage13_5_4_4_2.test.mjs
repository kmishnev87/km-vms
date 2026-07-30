import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");
const storagePage = read("app/storage/page.js");
const storageCss = read("app/styles/40-storage-records-shared.css");
const responsiveCss = read("app/styles/60-responsive-shared.css");
const i18n = read("lib/i18n.js");
const storageSource = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");

const context = {};
vm.runInNewContext(
  `${storageSource}
this.storageTopHealthModel = storageTopHealthModel;
this.archiveRootScenarioModel = archiveRootScenarioModel;`,
  context
);

for (const pathHealth of [{ readable: true, writable: true }, { readable: true, writable: true, available: null }]) {
  const health = context.storageTopHealthModel({
    operations: { status: "available" },
    capacity: { total_bytes: 100, free_percent: 80 },
    pathHealth,
  });
  assert.equal(health.status, "unknown", "unknown availability facts must not render confirmed OK");
}

assert.equal(
  context.archiveRootScenarioModel({ root: { is_available: false, configured_path: "/Volume2/KM-VMS" }, permission: { allowed: true } }).canActivate,
  true,
  "inactive configured roots can be activated through the runtime helper even before the API container can verify the final mount"
);
assert.equal(
  context.archiveRootScenarioModel({ root: { is_active: false, is_available: false, requires_activation: true, configured_path: "/Volume3/Surveillance", problem: "root_missing" }, permission: { allowed: true } }).canActivate,
  true,
  "inactive roots waiting for runtime activation must remain activatable even while their current runtime path is missing"
);
assert.equal(
  context.archiveRootScenarioModel({ root: { is_active: false, is_available: false, configured_path: "/Volume3/Broken", problem: "namespace_missing" }, permission: { allowed: true } }).canActivate,
  true,
  "visible root problems must not disable activation; backend preflight remains the safety authority"
);

const overviewStart = storagePage.indexOf("<section className={`storageOpsOverview");
const overviewEnd = storagePage.indexOf("{refreshWarning", overviewStart);
const overview = storagePage.slice(overviewStart, overviewEnd);
assert.equal((overview.match(/copy\.lastCheck/g) || []).length, 0, "top status block must not duplicate last check");
assert.match(storagePage.slice(0, overviewStart), /copy\.lastCheck/, "last check remains in header utility area");
assert.doesNotMatch(overview, /topHealth\.reason|primaryStorageActionText|archive_primary_path_source|sourceDeployConfig/);

const primaryStart = storagePage.indexOf("<section className={`storageOpsOverview");
const primaryEnd = storagePage.indexOf("<ArchiveIntegrityDialog", primaryStart);
const primary = storagePage.slice(primaryStart, primaryEnd);
assert.doesNotMatch(primary, /copy\.source\b|copy\.accessExplanation|copy\.dockerPath|copy\.ownershipBoundary|metadata|метаданн|кандидат|candidate/i);
assert.match(storagePage, /copy\.healthReasonAvailability/);
assert.match(storagePage, /copy\.actionCheckArchive/);

const operationsStart = storagePage.indexOf("const archiveManagementGroups");
const operationsEnd = storagePage.indexOf("const historyDialog", operationsStart);
const operations = storagePage.slice(operationsStart, operationsEnd);
for (const required of [
  "copy.archiveManagementRetentionTitle",
  "copy.archiveManagementAutoFreeTitle",
  "copy.archiveManagementIntegrityTitle",
  "copy.archiveManagementMigrationTitle",
]) {
  assert.match(operations, new RegExp(required.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), `${required} is a scenario row`);
}
assert.match(storagePage, /retentionOperationPresentation\(retention\)/, "retention primary text is model-driven, not candidate review");
assert.match(storagePage, /autoFreeOperationPresentation\(\{/, "auto-free primary text explains practical effect from backend facts");
assert.match(storagePage, /integrityOperationPresentation\(reconciliation\)/, "integrity row uses archive-check wording");
assert.match(storagePage, /migrationOperationPresentation\(migrationScenario, archiveRoots\.length\)/, "migration row is truthful about missing target selection");
assert.doesNotMatch(operations, /copy\.actionDetails/);

assert.match(storagePage, /storageOpsRootList/);
assert.match(storagePage, /archiveRootPath\(root, archivePathText\)/);
assert.match(i18n, /archiveRootPlaceholder: "\/Volume3\/Archive2"/);
assert.doesNotMatch(i18n, /archiveRootPlaceholder: "\/storage\/archive2"/);

const cameraStart = storagePage.indexOf("<Section title={copy.cameras}");
const cameraEnd = storagePage.indexOf("<Section title={copy.archiveRoots}", cameraStart);
const cameraBlock = storagePage.slice(cameraStart, cameraEnd);
assert.match(cameraBlock, /<th>\{copy\.camera\}<\/th>[\s\S]*<th>\{copy\.size\}<\/th>[\s\S]*<th>\{copy\.segments\}<\/th>[\s\S]*<th>\{copy\.problems\}<\/th>/);
assert.doesNotMatch(cameraBlock, /copy\.range|ID \{row\.camera_id|row\.oldest_recording_at|row\.newest_recording_at/);

assert.match(storageCss, /storageOpsRootListRow/);
assert.match(storageCss, /grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/, "top metrics use stable grid after capacity metrics moved out of the status block");
assert.match(storageCss, /grid-template-columns:\s*repeat\(5,\s*minmax\(0,\s*1fr\)\)/, "archive capacity facts fit in one desktop row");
assert.match(responsiveCss, /storageOpsRootListRow[\s\S]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);

for (const productText of [
  "Самые старые записи удаляются автоматически",
  "Защита тома включена",
  "Запустите полную проверку",
  "Для переноса нужно добавить второе расположение архива",
  "Камеры",
]) {
  assert.match(i18n, new RegExp(productText), `${productText} exists in RU copy`);
}
