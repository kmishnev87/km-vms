import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const i18nSource = fs.readFileSync(resolve(__dirname, "../lib/i18n.js"), "utf8");
const pageSource = fs.readFileSync(resolve(__dirname, "../app/storage/page.js"), "utf8");

function extractStoragePage(locale) {
  const localeMarkers = [`  ${locale}: {`, `  ${JSON.stringify(locale)}: {`];
  const localeStart = Math.max(...localeMarkers.map((marker) => i18nSource.indexOf(marker)));
  assert.notEqual(localeStart, -1, `locale ${locale} exists`);
  const storageStart = i18nSource.indexOf("    storagePage: {", localeStart);
  assert.notEqual(storageStart, -1, `storagePage for ${locale} exists`);
  const nextSetup = i18nSource.indexOf("    setup: {", storageStart);
  assert.notEqual(nextSetup, -1, `storagePage for ${locale} closes before setup`);
  const block = i18nSource.slice(storageStart, nextSetup);
  const values = {};
  for (const match of block.matchAll(/^\s+([A-Za-z0-9_]+):\s*"((?:\\"|[^"])*)",?$/gm)) {
    values[match[1]] = match[2].replace(/\\"/g, '"');
  }
  return values;
}

const stage9RenderedKeys = [
  "previewOnlyMigration",
  "migrationPreview",
  "migrationNote",
  "applyState",
  "applyMigration",
  "applying",
  "applyConfirm",
  "applyCompleted",
  "applyBlocked",
  "applyReport",
  "executed",
  "sourcePreserved",
  "cleanupPending",
  "refreshPreview",
  "previewUpdated",
  "noCameraOwned",
  "lowDiskNote",
  "autoFreeNote",
  "foreignSkipped",
  "deletedExcluded",
  "missingNoMetadata",
  "rootsNote",
  "reasonOrphanFile",
];

for (const key of stage9RenderedKeys) {
  assert.match(pageSource, new RegExp(`copy\\.${key}\\b`), `${key} is used by /storage render path`);
}

const ru = extractStoragePage("ru");
const zh = extractStoragePage("zh-CN");
const en = extractStoragePage("en");

const staleStorageMigrationPatterns = [
  new RegExp(["Stage", "4\\.0"].join(" "), "i"),
  /Apply\/перенос/i,
  new RegExp(["Apply\\/file move", "is not performed"].join(" "), "i"),
  new RegExp([[ "перенос", "файлов" ].join(" "), [ "не", "выполняется" ].join(" ")].join('[^"\\n]*'), "i"),
  new RegExp([[ "不", "执行", "应用" ].join(""), [ "文件", "移动" ].join("")].join(String.fromCharCode(25110))),
  new RegExp([[ "不", "执行" ].join(""), [ "文件", "移动" ].join("")].join(".*")),
];

for (const pattern of staleStorageMigrationPatterns) {
  assert.equal(pattern.test(i18nSource), false, `Storage migration i18n must not contain stale wording ${pattern}`);
}

const ruForbidden = [
  /\bPreview(?:-only)?\b/i,
  /\bApply\b/,
  /\bcopy-only\b/i,
  /\bSource\b/,
  /\bcleanup\b/i,
  /\bmetadata\b/i,
  /\bowned\b/i,
  /Отч[её]т apply/i,
];
const zhForbidden = [
  /\bPreview(?:-only)?\b/i,
  /\bApply\b/,
  /\bSource\b/,
  /\bCleanup\b/i,
  /\bcopy-only\b/i,
  /\bowned\b/i,
  /\bmetadata\b/i,
];

for (const key of stage9RenderedKeys) {
  assert.ok(ru[key], `ru.${key} exists`);
  assert.ok(zh[key], `zh-CN.${key} exists`);
  for (const pattern of ruForbidden) {
    assert.equal(pattern.test(ru[key]), false, `ru.${key} must not contain ${pattern}`);
  }
  for (const pattern of zhForbidden) {
    assert.equal(pattern.test(zh[key]), false, `zh-CN.${key} must not contain ${pattern}`);
  }
}

assert.equal(ru.applyState, "Применение");
assert.equal(ru.applyReport, "Отчёт о применении");
assert.equal(ru.noCameraOwned, "Нет записей камер, принадлежащих KM VMS.");
assert.equal(zh.applyState, "应用状态");
assert.equal(zh.applyReport, "应用报告");
assert.equal(zh.noCameraOwned, "没有属于 KM VMS 的摄像机录像。");

assert.equal(en.applyState, "Apply");
assert.equal(en.applyReport, "Apply report");
assert.match(en.applyConfirm, /copy-only/i);

for (const [locale, value] of Object.entries({ ru: ru.previewUpdated, en: en.previewUpdated, "zh-CN": zh.previewUpdated })) {
  assert.doesNotMatch(value, new RegExp(["Stage", "4\\.0"].join(" "), "i"), `${locale}.previewUpdated must not mention obsolete stage text`);
  assert.doesNotMatch(value, /not (?:apply|move)|не выполня|不执行/i, `${locale}.previewUpdated must not claim apply is unavailable in this stage`);
}

assert.match(ru.previewUpdated, /не меняет файлы/);
assert.match(ru.previewUpdated, /отдельным подтвержд[её]нным действием/);
assert.match(en.previewUpdated, /read-only/);
assert.match(en.previewUpdated, /separate confirmed copy-only action/);
assert.match(zh.previewUpdated, /不会更改文件/);
assert.match(zh.previewUpdated, /单独确认/);

const legacyPreviewUpdatedSource = "Предпросмотр миграции обновлён. Предпросмотр только проверяет план и не меняет файлы; применение запускается отдельным подтверждённым действием и сохраняет исходные файлы.";
assert.match(i18nSource, new RegExp(legacyPreviewUpdatedSource.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
assert.match(i18nSource, /Migration preview refreshed\. Preview is read-only and does not change files; apply runs only as a separate confirmed copy-only action and preserves source files\./);
assert.match(i18nSource, /迁移预览已刷新。预览仅检查计划且不会更改文件；应用迁移只会作为单独确认的仅复制操作执行，并保留源文件。/);
