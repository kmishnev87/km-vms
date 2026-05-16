import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));

function read(path) {
  return fs.readFileSync(resolve(__dirname, "..", path), "utf8");
}

const helperSource = read("lib/operatorWarnings.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${helperSource}\nthis.buildOperatorWarnings = buildOperatorWarnings;\nthis.buildDashboardStatusSummary = buildDashboardStatusSummary;`,
  context
);
const { buildOperatorWarnings, buildDashboardStatusSummary } = context;

const component = read("components/OperatorProblemBanners.js");
const css = read("app/styles/10-dashboard-auth-setup.css");
const responsiveCss = read("app/styles/60-responsive-shared.css");

assert.match(component, /operatorWarningStrip/);
assert.match(component, /usePathname/);
assert.match(component, /actionForContext/);
assert.match(component, /routeSection/);
assert.match(component, /detailsOpen, setDetailsOpen\] = useState\(false\)/);
assert.match(component, /aria-expanded=\{detailsOpen\}/);
assert.match(component, /onClick=\{\(\) => setDetailsOpen/);
assert.match(component, /operatorWarningDetails/);
assert.match(component, /groupedWarnings\.map/);
assert.doesNotMatch(component, /className=\{`operatorWarning operatorWarning-/);
assert.doesNotMatch(component, /operatorStatusQuiet/);
assert.doesNotMatch(component, /Нет активных предупреждений/);
assert.match(component, /canAccessPath\(user, action\.href\)/);
assert.match(component, /ActionLink action=\{item\.action\}/);
assert.match(component, /href: "\/diagnostics", label: "Диагностика"/);
assert.match(component, /Поток запускается дольше 30 секунд/);
assert.match(component, /Запуск онлайн-потока превысил ожидаемое время/);
assert.match(component, /Откройте диагностику, если проблема сохраняется/);
assert.doesNotMatch(component, /currentSection !== actionSection[\s\S]{0,240}return null/);

assert.match(css, /\.operatorWarningStrip/);
assert.match(css, /min-height:\s*38px/);
assert.match(css, /\.operatorWarningChips/);
assert.match(css, /flex-wrap:\s*wrap/);
assert.match(css, /\.operatorWarningDetails/);
assert.match(css, /max-height:\s*min\(340px, 45vh\)/);
assert.match(css, /\.operatorWarningRow/);
assert.match(css, /grid-template-columns:\s*10px minmax\(0, 1fr\) auto/);
assert.doesNotMatch(css, /grid-template-columns:\s*96px minmax\(0, 1fr\) auto/);
assert.match(css, /\.operatorWarningRowSeverity[\s\S]*border-radius:\s*999px/);
assert.match(responsiveCss, /\.operatorWarningStrip/);
assert.match(responsiveCss, /\.operatorWarningRow/);

const runtimeStatus = {
  domains: {
    storage: { severity: "error", available: false, reason_codes: ["storage_unavailable"] },
    cameras: {
      items: [
        { severity: "warning", reason_codes: ["recording_stale"] },
        { severity: "ok", reason_codes: [] },
      ],
    },
    live: {
      items: [
        { severity: "unknown", state: "unknown", running: false, ready: false, reason_codes: ["no_evidence"] },
      ],
    },
    recorder: { severity: "ok", safe_reason_codes: [] },
    retention: { severity: "ok", reason_codes: [] },
    reconciliation: { severity: "ok", reason_codes: [] },
  },
};

const warnings = buildOperatorWarnings(runtimeStatus);
assert.equal(JSON.stringify(warnings.map((item) => item.id)), JSON.stringify(["storage-unavailable", "camera-recording-warnings"]));
assert.equal(warnings[0].action.href, "/storage");
assert.equal(warnings[1].action.href, "/cameras");

const summary = buildDashboardStatusSummary(runtimeStatus);
assert.equal(summary.problem_count, 2);
assert.equal(summary.rows.find((row) => row.domain === "live").severity, "ok");
assert.equal(summary.problems.every((item) => item.domain_label && item.severity_label), true);

const rendered = JSON.stringify(warnings);
for (const forbidden of [
  ["rt", "sp://"].join(""),
  "Authorization",
  ["pass", "word"].join(""),
  ["tok", "en"].join(""),
  "debug",
  "playlist_path",
  "/Volume",
]) {
  assert.equal(rendered.includes(forbidden), false);
}

const emptySummary = buildDashboardStatusSummary({ domains: { storage: { severity: "ok" } } });
assert.equal(emptySummary.problem_count, 0);
assert.equal(component.includes("operatorStatusQuiet"), false);

const selfLinkRules = [
  'currentSection === "/live"',
  'currentSection === "/storage"',
  'return null;',
];
for (const marker of selfLinkRules) {
  assert.equal(component.includes(marker), true, `${marker} expected in context-aware action rules`);
}
