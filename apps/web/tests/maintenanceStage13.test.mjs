import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const settingsPage = fs.readFileSync(resolve(__dirname, "../app/settings/page.js"), "utf8");
const settingsHelpers = fs.readFileSync(resolve(__dirname, "../lib/settingsPageHelpers.js"), "utf8");

function read(relative) {
  return fs.readFileSync(resolve(__dirname, "..", relative), "utf8");
}

function readEffectiveCss(relative) {
  const content = read(relative);
  const imports = [...content.matchAll(/@import\s+"\.\/([^"]+)";/g)];
  if (!imports.length) return content;
  return imports.map((match) => read(`app/${match[1]}`)).join("\n");
}

const css = readEffectiveCss("app/globals.css");

function extractBlockAfter(marker) {
  const start = settingsPage.indexOf(marker);
  assert.notEqual(start, -1, `${marker} not found`);
  const bodyStart = settingsPage.indexOf("{", start);
  let depth = 0;
  for (let index = bodyStart; index < settingsPage.length; index += 1) {
    const char = settingsPage[index];
    if (char === "{") depth += 1;
    if (char === "}") depth -= 1;
    if (depth === 0) return settingsPage.slice(bodyStart, index + 1);
  }
  throw new Error(`${marker} block end not found`);
}

assert.equal(settingsPage.includes('apiFetch("/system/maintenance/overview")'), true);
assert.equal(settingsPage.includes('apiFetch("/system/upgrade/report")'), true);
assert.equal(settingsPage.includes('apiFetchBlob("/system/upgrade/report")'), true);
assert.equal(settingsPage.includes("MAINTENANCE_DRY_RUN_ENDPOINTS"), true);
assert.equal(settingsHelpers.includes("MAINTENANCE_DRY_RUN_ENDPOINTS"), true);
assert.equal(settingsHelpers.includes("/system/db-adoption/dry-run"), true);
assert.equal(settingsHelpers.includes("/system/migrations/dry-run"), true);
assert.equal(settingsHelpers.includes("/system/restore/dry-run"), true);
assert.equal(settingsHelpers.includes("/system/update/check"), true);
assert.equal(settingsPage.includes("/system/update/apply"), true);
assert.equal(settingsPage.includes("/system/update/apply/status"), true);
assert.equal(settingsPage.includes("window.confirm(t.updateApplyConfirm)"), true);
assert.equal(settingsPage.includes("expected_manifest_version"), true);
assert.equal(settingsPage.includes("expected_manifest_commit"), true);
assert.equal(settingsPage.includes('type="password"'), true);
assert.equal(settingsPage.includes("github-token"), false);
assert.equal(settingsPage.includes("localStorage"), false);
assert.equal(settingsPage.includes("sessionStorage"), false);
assert.equal(settingsPage.includes("/system/migrations/apply"), false);
assert.equal(settingsPage.includes("/system/restore/apply"), false);
assert.equal(settingsPage.includes("/system/db-adoption/apply"), false);
assert.equal(settingsPage.includes("maintenanceNoApply"), true);
assert.equal(settingsPage.includes("ZH_TEXT_OVERRIDES"), true);
assert.equal(settingsPage.includes("Обзор обслуживания"), true);
assert.equal(settingsPage.includes("Maintenance overview"), true);
assert.equal(settingsPage.includes("维护概览"), true);
assert.equal(settingsPage.includes("maintenanceLabels"), true);

const requiredMaintenanceKeys = [
  "pending",
  "artifacts",
  "current",
  "target",
  "available",
  "backup",
  "confirm",
  "apply",
  "reportId",
];

for (const key of requiredMaintenanceKeys) {
  assert.equal(settingsPage.includes(`${key}:`), true, `${key} label key missing`);
}

for (const expected of [
  'pending: "Ожидают"',
  'pending: "Pending"',
  'pending: "待处理"',
  'reportId: "ID отчёта"',
  'reportId: "Report ID"',
  'reportId: "报告 ID"',
  'db_adoption: "Принятие БД"',
  'db_adoption: "DB adoption"',
  'db_adoption: "数据库接管"',
]) {
  assert.equal(settingsPage.includes(expected), true, `${expected} missing`);
}

const maintenanceDetailRows = settingsHelpers.slice(settingsHelpers.indexOf("function maintenanceDetailRows"));
for (const label of ["Pending", "Artifacts", "Current", "Target", "Available", "Backup", "Confirm", "Apply"]) {
  assert.equal(maintenanceDetailRows.includes(`"${label}"`), false);
  assert.equal(maintenanceDetailRows.includes(`'${label}'`), false);
  assert.equal(maintenanceDetailRows.includes(`[${label}`), false);
}
assert.equal(maintenanceDetailRows.includes("t.maintenanceLabels"), true);

const reportStart = settingsPage.indexOf('className="settingsMaintenanceReportPreview"');
assert.notEqual(reportStart, -1, "maintenance report preview not found");
const maintenanceReportPreview = settingsPage.slice(reportStart, settingsPage.indexOf("</section>", reportStart));
assert.equal(maintenanceReportPreview.includes("<dt>ID</dt>"), false);
assert.equal(maintenanceReportPreview.includes("<dt>DB adoption</dt>"), false);
assert.equal(maintenanceReportPreview.includes("t.maintenanceLabels?.reportId"), true);
assert.equal(maintenanceReportPreview.includes("t.maintenanceFlows?.db_adoption"), true);

assert.equal(css.includes(".settingsMaintenanceModal"), true);
assert.equal(css.includes(".settingsMaintenanceGrid"), true);
assert.equal(css.includes(".settingsUpdateApplyPanel"), true);
