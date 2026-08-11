import { readI18nSource } from "./helpers/readI18nSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const operatorWarnings = read("lib/operatorWarnings.js");
const systemStatusPage = read("app/system-status/page.js");
const problemBanners = read("components/OperatorProblemBanners.js");
const dashboard = read("app/page.js");
const api = read("lib/api.js");
const routePermissions = read("lib/routePermissions.js");
const i18n = readI18nSource();

for (const route of [
  "app/security-journal/page.js",
  "app/diagnostics/page.js",
  "app/storage/page.js",
  "app/cameras/page.js",
  "app/live/page.js",
  "app/recordings/page.js",
  "app/chronology/page.js",
  "app/settings/page.js",
]) {
  assert.equal(fs.existsSync(resolve(root, route)), true, `${route} target route missing`);
}

assert.equal(api.includes("canUserAccessRoute(user, href)"), true);
for (const [route, permission] of [
  ["/cameras", "manage_cameras"],
  ["/live", "view_live"],
  ["/storage", "manage_settings"],
  ["/diagnostics", "run_diagnostics"],
  ["/security-journal", "manage_settings"],
]) {
  assert.equal(routePermissions.includes(`"${route}"`), true, `${route} missing from route access registry`);
  assert.equal(routePermissions.includes(`permission: "${permission}"`), true, `${permission} missing from route access registry`);
}

assert.equal(problemBanners.includes("canAccessPath("), true);
assert.equal(problemBanners.includes("action.href"), true);
assert.equal(systemStatusPage.includes("canAccessPath(user, action.href)"), true);
assert.equal(systemStatusPage.includes("<SystemStatusProblemAction"), true);
assert.equal(systemStatusPage.includes("disabled title={t(\"systemStatus.activeProblems\")"), false);
assert.equal(/(^|[^A-Za-z0-9_])t\(action\.label/.test(systemStatusPage), false);
assert.equal(/(^|[^A-Za-z0-9_])t\(action\.hint/.test(systemStatusPage), false);
assert.equal(/(^|[^A-Za-z0-9_])t\(action\.label/.test(problemBanners), false);
assert.equal(/(^|[^A-Za-z0-9_])t\(action\.hint/.test(problemBanners), false);
assert.equal(systemStatusPage.includes("text(action.label)"), true);
assert.equal(problemBanners.includes("text(action.label)"), true);
assert.equal(operatorWarnings.includes("labelKey"), false);
assert.equal(operatorWarnings.includes("hintKey"), false);
assert.equal(operatorWarnings.includes('label: "Открыть камеры"'), true);
assert.equal(operatorWarnings.includes('label: "Открыть онлайн"'), true);
assert.equal(operatorWarnings.includes('label: "Открыть диагностику"'), true);
assert.equal(operatorWarnings.includes('label: "Открыть хранилище"'), true);
assert.equal(operatorWarnings.includes("hint:"), true);
assert.equal(systemStatusPage.includes("action.hint"), false);
assert.equal(problemBanners.includes("action.hint"), false);
assert.equal(problemBanners.includes("text(item.action_hint)"), true);

const context = {};
vm.runInNewContext(
  `${operatorWarnings.replaceAll("export const ", "const ").replaceAll("export function ", "function ")}
this.buildOperatorWarnings = buildOperatorWarnings;
this.buildDashboardStatusSummary = buildDashboardStatusSummary;`,
  context
);

function runtime(domains) {
  return { domains };
}

const warnings = context.buildOperatorWarnings(runtime({
  cameras: { items: [{ severity: "error", reason_codes: ["recording_failed"] }] },
  live: { items: [{ severity: "error", state: "failed", running: false, ready: false, reason_codes: ["live_failed"] }] },
  recorder: { severity: "error", safe_reason_codes: ["recorder_heartbeat_stale"] },
  storage: { severity: "warning", reason_codes: ["storage_low_space"] },
  retention: { severity: "warning", reason_codes: ["retention_completed_with_warnings"] },
  reconciliation: { severity: "warning", reason_codes: ["reconciliation_problems_found"], problem_file_count: 1 },
}), { limit: 12 });

const actionsById = Object.fromEntries(warnings.map((item) => [item.id, item.action?.href]));
assert.equal(actionsById["camera-recording-errors"], "/cameras");
assert.equal(actionsById["live-stream-errors"], "/live");
assert.equal(actionsById["recorder-stale"], "/diagnostics");
assert.equal(actionsById["storage-low-space"], "/storage");
assert.equal(actionsById["retention-warning"], "/storage");
assert.equal(actionsById["reconciliation-problems"], "/storage");

const renderedActions = JSON.stringify(warnings.map((item) => item.action));
for (const allowed of ["/cameras", "/live", "/diagnostics", "/storage"]) {
  assert.equal(renderedActions.includes(allowed), true, `${allowed} expected in actions`);
}
const forbiddenValues = [
  ["rt", "sp://"].join(""),
  ["Author", "ization"].join(""),
  ["Bear", "er "].join(""),
  ["pass", "word"].join(""),
  ["tok", "en="].join(""),
  ["access", "_token"].join(""),
  ["media", "_token"].join(""),
  ["sec", "ret"].join(""),
  [".", "env"].join(""),
  ["/security", "-journal?"].join(""),
  "q=",
  "target=",
  "message=",
  "detail=",
  ["/place", "holder"].join(""),
  ["/fu", "ture"].join(""),
];

for (const forbidden of forbiddenValues) {
  assert.equal(`${renderedActions}\n${systemStatusPage}\n${problemBanners}\n${dashboard}`.includes(forbidden), false, `${forbidden} must not appear in Stage 8 links`);
}

assert.equal(systemStatusPage.includes('href="/security-journal"'), false);
assert.equal(systemStatusPage.includes('href="/diagnostics"'), false);
assert.equal(dashboard.includes('href="/security-journal"'), false);
assert.equal(dashboard.includes('href="/diagnostics"'), false);

for (const label of ["Открыть камеры", "Открыть онлайн", "Открыть диагностику", "Открыть хранилище"]) {
  assert.equal(i18n.includes(`"${label}"`), true, `${label} RU label missing from text translation index`);
}
for (const text of ["Open cameras", "Open live", "Open diagnostics", "Open storage", "打开摄像机", "打开实时", "打开诊断", "打开存储"]) {
  assert.equal(i18n.includes(text), true, `${text} translation missing`);
}

for (const hint of [
  "Проверьте настройки камер, сеть и параметры записи.",
  "Проверьте активный онлайн-просмотр и доступность камеры.",
  "Откройте диагностику и соберите архив только явным действием.",
  "Проверьте настройки хранилища и доступность архива.",
]) {
  assert.equal(i18n.includes(`"${hint}"`), true, `${hint} RU hint missing from text translation index`);
}
for (const hintTranslation of [
  "Check camera settings, network, and recording parameters.",
  "Check active live viewing and camera availability.",
  "Open diagnostics and collect an archive only by explicit action.",
  "Check storage settings and archive availability.",
  "Check archive integrity in the existing storage section.",
  "检查摄像机设置、网络和录像参数。",
  "检查实时查看和摄像机可用性。",
  "打开诊断，并且只通过明确操作收集归档。",
  "检查存储设置和归档可用性。",
  "在现有存储部分检查归档完整性。",
]) {
  assert.equal(i18n.includes(hintTranslation), true, `${hintTranslation} hint translation missing`);
}
