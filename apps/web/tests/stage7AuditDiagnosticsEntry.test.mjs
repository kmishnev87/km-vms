import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const api = read("lib/api.js");
const routePermissions = read("lib/routePermissions.js");
const component = read("components/AuditDiagnosticsEntries.js");
const securityPage = read("app/security-journal/page.js");
const diagnosticsPage = read("app/diagnostics/page.js");
const i18n = read("lib/i18n.js");
const systemStatusPage = read("app/system-status/page.js");
const problemBanners = read("components/OperatorProblemBanners.js");
const dashboard = read("app/page.js");
const contract = await import(pathToFileURL(resolve(root, "lib/auditEntryContract.js")));

assert.equal(api.includes("canUserAccessRoute(user, href)"), true);
assert.equal(routePermissions.includes('"/security-journal"'), true);
assert.equal(routePermissions.includes('permission: "manage_settings"'), true);
assert.equal(routePermissions.includes('"/diagnostics"'), true);
assert.equal(routePermissions.includes('permission: "run_diagnostics"'), true);

assert.equal(securityPage.includes("<SecurityJournalEntry />"), true);
assert.equal(diagnosticsPage.includes("<DiagnosticsEntry />"), true);
assert.equal(read("lib/auditEntryContract.js").includes('/audit/events?'), true);
assert.equal(component.includes('/settings/logs/archive?mode='), true);
assert.equal(component.includes("setArchiveChoiceOpen(true)"), true);
assert.equal(component.includes("<OperationDialog"), true);
assert.equal(component.includes("diagnosticsEntry.normalArchiveDescription"), true);
assert.equal(component.includes("diagnosticsEntry.extendedArchiveDescription"), true);
const diagnosticsDialogStart = component.indexOf('id: "diagnostics-entry-archive-choice"');
const diagnosticsDialogEnd = component.indexOf("onClose={() => setArchiveChoiceOpen(false)}", diagnosticsDialogStart);
const diagnosticsDialog = component.slice(diagnosticsDialogStart, diagnosticsDialogEnd);
assert.equal(diagnosticsDialog.includes("descriptions: ["), true);
assert.equal(diagnosticsDialog.includes("summary:"), false);
assert.equal(diagnosticsDialog.includes("showFooterClose: false"), true);
assert.equal(i18n.includes('archiveChoiceTitle: "Выберите диагностический архив"'), true);
assert.equal(i18n.includes('normalArchive: "Обычный"'), true);
assert.equal(i18n.includes('extendedArchive: "Расширенный"'), true);
assert.equal(component.includes('apiFetchBlob("/settings/bug-report"'), true);
assert.equal(component.includes("include_logs: false"), true);
assert.equal(component.includes("dangerouslySetInnerHTML"), false);
assert.equal(component.includes("<pre"), false);
assert.equal(component.includes("JSON.stringify("), true, "only request body serialization should use JSON.stringify");

for (const key of [
  "securityJournal",
  "diagnosticsEntry",
  "unsupportedFilters",
  "normalArchive",
  "extendedArchive",
  "bugReportReady",
]) {
  assert.equal(i18n.includes(key), true, `${key} i18n key missing`);
}

function securityJournalSection(anchor) {
  const start = i18n.indexOf(anchor);
  assert.notEqual(start, -1, `${anchor} Security Journal block missing`);
  const end = i18n.indexOf("diagnosticsEntry:", start);
  assert.notEqual(end, -1, `${anchor} diagnosticsEntry marker missing`);
  return i18n.slice(start, end);
}

function quotedLabel(section, key) {
  const match = section.match(new RegExp(`${key}: "([^"]+)"`));
  assert.notEqual(match, null, `${key} label missing`);
  return match[1];
}

const ruSecurityJournal = securityJournalSection('title: "Журнал безопасности"');
const enSecurityJournal = securityJournalSection('title: "Security Journal"');
const zhSecurityJournal = securityJournalSection('title: "安全日志"');

assert.equal(quotedLabel(ruSecurityJournal, "actor"), "Инициатор");
assert.equal(quotedLabel(ruSecurityJournal, "target"), "Объект");
assert.equal(quotedLabel(ruSecurityJournal, "systemActor"), "Система");
assert.equal(quotedLabel(ruSecurityJournal, "noTarget"), "без объекта");

assert.equal(quotedLabel(enSecurityJournal, "actor"), "Actor");
assert.equal(quotedLabel(enSecurityJournal, "target"), "Target");
assert.equal(quotedLabel(enSecurityJournal, "systemActor"), "System");
assert.equal(quotedLabel(enSecurityJournal, "noTarget"), "No target");

assert.equal(quotedLabel(zhSecurityJournal, "actor"), "操作者");
assert.equal(quotedLabel(zhSecurityJournal, "target"), "对象");
assert.equal(quotedLabel(zhSecurityJournal, "systemActor"), "系统");
assert.equal(quotedLabel(zhSecurityJournal, "noTarget"), "无对象");

for (const [locale, section] of [
  ["ru", ruSecurityJournal],
  ["zh-CN", zhSecurityJournal],
]) {
  assert.notEqual(quotedLabel(section, "actor"), "Actor", `${locale} actor label must be localized`);
  assert.notEqual(quotedLabel(section, "target"), "Target", `${locale} target label must be localized`);
  assert.notEqual(quotedLabel(section, "systemActor"), "system", `${locale} system actor label must be localized`);
  assert.equal(/target/i.test(quotedLabel(section, "noTarget")), false, `${locale} no-target label must be localized`);
}

const parsed = contract.sanitizeAuditFiltersFromEntries([
  ["category", "security"],
  ["severity", "warning"],
  ["period", "6h"],
  ["actor", "owner"],
  ["target_type", "camera"],
  ["event_type", "security.permission_denied"],
  ["q", "denied"],
  ["camera_id", "12"],
  ["q", "credential sample"],
]);

assert.equal(parsed.filters.category, "security");
assert.equal(parsed.filters.severity, "warning");
assert.equal(parsed.filters.since_minutes, "360");
assert.equal(parsed.filters.actor, "owner");
assert.equal(parsed.filters.target_type, "camera");
assert.equal(parsed.filters.event_type, "security.permission_denied");
assert.equal(parsed.filters.q, "denied");
assert.deepEqual(parsed.unsupported, ["camera_id"]);
assert.deepEqual(parsed.invalid, ["q"]);

const path = contract.buildAuditEventsPath(parsed.filters, 0);
assert.equal(path.includes("category=security"), true);
assert.equal(path.includes("severity=warning"), true);
assert.equal(path.includes("since_minutes=360"), true);
assert.equal(path.includes("credential"), false);
assert.equal(path.includes("sample"), false);

for (const source of [systemStatusPage, problemBanners, dashboard]) {
  assert.equal(source.includes('href="/security-journal"'), false);
  assert.equal(source.includes('href: "/security-journal"'), false);
}

assert.equal(dashboard.includes('href="/diagnostics"'), false);
assert.equal(dashboard.includes('href: "/diagnostics"'), false);
assert.equal(systemStatusPage.includes('href="/diagnostics"'), false);
assert.equal(problemBanners.includes('href: "/diagnostics"'), true);
