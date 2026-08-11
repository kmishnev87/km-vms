import { readI18nSource } from "./helpers/readI18nSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import * as storageOperations from "../lib/storageOperations.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");
const storagePage = read("app/storage/page.js");
const i18n = readI18nSource();

const context = storageOperations;

assert.equal(context.accessRightsModel({ readable: true, writable: true }).label, "Права на чтение и запись: есть");
assert.equal(context.accessRightsModel({ readable: true, writable: false }).label, "Чтение есть, запись недоступна");
assert.equal(context.accessRightsModel({ readable: false, writable: true }).label, "Чтение недоступно, запись есть");
assert.equal(context.accessRightsModel({ readable: false, writable: false }).label, "Права на чтение и запись: нет");
assert.equal(storagePage.includes("`${copy.write}: ${factLabel(pathHealth.writable"), false);

const ambiguous = context.storageTopHealthModel({
  operations: { status: "available" },
  capacity: { total_bytes: 100, free_percent: 77 },
  pathHealth: { readable: true, writable: true, available: false },
});
assert.equal(ambiguous.status, "availability_unconfirmed");
assert.doesNotMatch(ambiguous.nextStep, /недоступен/);
assert.doesNotMatch(ambiguous.nextStep, /namespace/);
assert.match(ambiguous.nextStep, /служебную папку архива/);

const hardUnavailable = context.storageTopHealthModel({
  operations: { status: "unavailable" },
  capacity: { total_bytes: 0 },
  pathHealth: { readable: false, writable: false, available: false },
});
assert.match(hardUnavailable.nextStep, /недоступен/);

assert.equal(context.freeSpaceTone({ free_percent: 77 }, { warning_threshold_percent: 10 }), "neutral");
assert.equal(context.freeSpaceTone({ free_percent: 5 }, { warning_threshold_percent: 10 }), "warning");

const normalized = context.normalizeReconciliationSummary({
  classification_counts: {
    missing_file: 2,
    orphan_file: 4,
    corrupted_file: 1,
    ok_owned_finalized: 20,
  },
  cleanup_candidates: { count: 4, classification_counts: { orphan_file: 4 } },
  apply_safe_summary: { updated_metadata_count: 0, reason_counts: {} },
  total_metadata_rows_checked: 26,
});
assert.equal(normalized.problemCount, 7);
assert.equal(normalized.reviewOnlyCount, 4);
assert.equal(normalized.safeFixCount, 3);
assert.equal(normalized.totalRows, 26);
assert.equal(normalized.categories.some((item) => item.label === "Файлы без метаданных"), true);
assert.equal(normalized.categories.some((item) => item.label === "Поврежденные файлы"), true);

const reviewOnly = context.reconciliationScenarioModel({
  preview: {
    classification_counts: { orphan_file: 6 },
    cleanup_candidates: { count: 6, classification_counts: { orphan_file: 6 } },
    total_metadata_rows_checked: 1483,
  },
  canCheck: { allowed: true },
  canApply: { allowed: true },
});
assert.equal(reviewOnly.canApply, false);
assert.equal(reviewOnly.noAutoFixReason, "review_only");

assert.match(storagePage, /<MiniFact label=\{copy\.archiveSize\}/, "archive-specific facts remain");
const archiveSectionStart = storagePage.indexOf("<Section title={copy.archiveSpace}");
const archiveSectionEnd = storagePage.indexOf("<Section title={copy.cameras}", archiveSectionStart);
const archiveSection = storagePage.slice(archiveSectionStart, archiveSectionEnd);
assert.equal(archiveSection.includes("copy.total"), true, "Archive and space includes total capacity context");
assert.equal(archiveSection.includes("copy.used"), true, "Archive and space includes used capacity context");
assert.equal(archiveSection.includes("copy.free"), true, "Archive and space includes free capacity context");
assert.doesNotMatch(storagePage, /<Stat label=\{copy\.foreignSkipped\}/, "ownership internals are not primary stats");
assert.doesNotMatch(storagePage, /<SummaryRow label=\{copy\.ownershipBoundary\}/, "ownership boundary is not primary");
assert.doesNotMatch(storagePage, /<dt>\{copy\.ownershipBoundary\}<\/dt>/, "ownership boundary is removed from visible support UI");

assert.match(storagePage, /copy\.migrationPrepare/, "migration planning has endpoint-specific wording");
assert.equal(i18n.includes("migrationPrepare: \"Подготовить план\""), true);
assert.doesNotMatch(storagePage, /storageOpsSection-recent/, "recent operations are not a permanent page block");
assert.match(storagePage, /<OperationDialog dialog=\{historyDialog\}/, "bounded operation history is available in a modal");
assert.match(storagePage, /setRefreshWarning/, "silent refresh failure preserves last valid status with warning");
assert.doesNotMatch(storagePage, /Запись: Да|Доступность: Нет/, "known contradictory primary phrases are absent from source");
