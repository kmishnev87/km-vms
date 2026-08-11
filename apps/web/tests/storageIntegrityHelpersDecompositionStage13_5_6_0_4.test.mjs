import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import * as facade from "../lib/storageOperations.js";
import * as integrityOwner from "../lib/storageArchiveIntegrityHelpers.js";
import * as shared from "../lib/storageOperationsSharedHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (relative) => fs.readFileSync(resolve(__dirname, "..", relative), "utf8");
const facadeSource = read("lib/storageOperations.js");
const integritySource = read("lib/storageArchiveIntegrityHelpers.js");
const sharedSource = read("lib/storageOperationsSharedHelpers.js");
const storagePage = read("app/storage/page.js");

const integrityExports = [
  "integrityOperationPresentation",
  "reconciliationClassLabel",
  "normalizeReconciliationSummary",
  "archiveIntegrityScanModel",
  "archiveIntegrityFindingPresentation",
  "archiveIntegrityCategoryPresentations",
  "archiveIntegrityActionContract",
];
for (const name of integrityExports) {
  assert.equal(typeof integrityOwner[name], "function", `${name} is owned by the integrity module`);
  assert.equal(facade[name], integrityOwner[name], `${name} keeps the stable facade export`);
  assert.doesNotMatch(
    facadeSource,
    new RegExp(`export\\s+function\\s+${name}\\b`),
    `${name} has no duplicate facade implementation`,
  );
}

assert.equal(facade.asNumber, shared.asNumber);
assert.equal(facade.statusLabel, shared.statusLabel);
assert.equal(typeof shared.finiteCount, "function");
for (const name of ["asNumber", "statusLabel", "finiteCount"]) {
  assert.match(sharedSource, new RegExp(`export\\s+function\\s+${name}\\b`));
}

for (const name of [
  "REVIEW_ONLY_RECONCILIATION_CLASSES",
  "SAFE_METADATA_RECONCILIATION_CLASSES",
  "ARCHIVE_INTEGRITY_CATEGORY_KEYS",
  "ARCHIVE_INTEGRITY_IMPACT_KEYS",
  "ARCHIVE_INTEGRITY_ACTION_KEYS",
  "ARCHIVE_INTEGRITY_NO_ACTION_KEYS",
  "ARCHIVE_INTEGRITY_SCAN_STATUS_KEYS",
]) {
  assert.match(integritySource, new RegExp(`const\\s+${name}\\s*=`));
  assert.doesNotMatch(facadeSource, new RegExp(`const\\s+${name}\\s*=`));
}

assert.match(integritySource, /from "\.\/storageOperationsSharedHelpers\.js"/);
assert.doesNotMatch(integritySource, /from "\.\/storageOperations\.js"/);
assert.doesNotMatch(sharedSource, /^import\s/m);
assert.doesNotMatch(storagePage, /storageArchiveIntegrityHelpers|storageOperationsSharedHelpers/);
