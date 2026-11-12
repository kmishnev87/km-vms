import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { buildDashboardStatusSummary, systemHealthIndicatorModel } from "../lib/operatorWarnings.js";
import { ruDictionary } from "../lib/i18n/ru.js";
import { enDictionary } from "../lib/i18n/en.js";
import { zhCNDictionary } from "../lib/i18n/zhCN.js";

const read = (path) => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const source = read("lib/useArchiveIntegrityReviewController.js");
const healthy = () => Object.fromEntries(["storage", "recorder", "cameras", "live", "retention", "reconciliation"].map((name) => [name, { severity: "ok", items: [] }]));
const domains = healthy();
domains.reconciliation = { severity: "warning", reason_codes: ["reconciliation_problems_found"], problem_file_count: 100,
  category_counts: { partial_file: 42, stale_writing_segment: 52, zero_size_file: 6 } };
const summary = buildDashboardStatusSummary({ domains });
assert.equal(summary.severity, "warning");
assert.equal(summary.problems.length, 1);
assert.equal(summary.problems[0].affected_count, 100);
assert.equal(summary.problems[0].domain, "reconciliation");
assert.equal(summary.rows.find((row) => row.domain === "storage").severity, "ok");
assert.equal(systemHealthIndicatorModel({ user: { permissions: ["run_diagnostics"] }, summary, runtimeStatusKnown: true }).hasProblems, true);
for (const name of Object.keys(healthy())) {
  const current = healthy(); current[name] = { severity: "warning", reason_codes: ["new_status"] };
  const result = buildDashboardStatusSummary({ domains: current });
  assert.ok(result.problems.length > 0, `warning ${name} must always have a visible explanation`);
}

function keys(value, prefix = "") {
  return Object.entries(value).flatMap(([key, item]) => typeof item === "string" ? [`${prefix}${key}`] : keys(item, `${prefix}${key}.`)).sort();
}
assert.deepEqual(keys(ruDictionary.archiveReview), keys(enDictionary.archiveReview));
assert.deepEqual(keys(ruDictionary.archiveReview), keys(zhCNDictionary.archiveReview));
for (const dictionary of [ruDictionary, enDictionary, zhCNDictionary]) {
  for (const key of ["recheck", "repair", "delete", "acknowledge", "reopen"]) assert.ok(dictionary.archiveReview.actions[key] && dictionary.archiveReview.effects[key]);
}

const settle = async () => { for (let tick = 0; tick < 30; tick += 1) await Promise.resolve(); };
function mount() {
  const state = [], hooks = [], effects = [], requests = [], notices = [];
  let cursor = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => value === b[index]);
  const context = {
    useState(initial) { const i = cursor++; if (!(i in state)) state[i] = typeof initial === "function" ? initial() : initial;
      return [state[i], (value) => { state[i] = typeof value === "function" ? value(state[i]) : value; }]; },
    useRef(initial) { const i = cursor++; if (!hooks[i]) hooks[i] = { current: initial }; return hooks[i]; },
    useCallback(fn, deps) { const i = cursor++; if (!hooks[i] || !same(hooks[i].deps, deps)) hooks[i] = { fn, deps }; return hooks[i].fn; },
    useEffect(fn, deps) { const i = cursor++; if (!hooks[i] || !same(hooks[i].deps, deps)) { effects.push(() => { hooks[i]?.cleanup?.(); hooks[i] = { deps, cleanup: fn() }; }); } },
    useI18n: () => ({ t: (key) => key }),
    apiFetch: (path, options = {}) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })),
    newStorageOperationId: () => "review-ui-unit-request",
    window: { setInterval() { return 1; }, clearInterval() {}, dispatchEvent() {} },
    Event: class {}, URLSearchParams, Set,
  };
  const executable = source.replace(/^import .*;\s*$/gm, "").replaceAll("export default function ", "function ")
    .replaceAll("export function ", "function ").replaceAll("export const ", "const ");
  vm.runInNewContext(executable + "\nthis.controller = useStorageIntegrityController; this.failure = reviewFailureText;", context);
  const props = { diagnosticsPermission: { allowed: true }, currentUser: { permissions: ["run_diagnostics", "manage_settings", "delete_recordings"] },
    loadStatus: async () => {}, requireFreshStorageStatus: () => true, setOperationToast: (value) => notices.push(value) };
  return { requests, notices,
    render() { cursor = 0; return context.controller(props); },
    effects() { while (effects.length) effects.shift()(); },
    failure: context.failure,
  };
}
const app = mount();
let c = app.render(); app.effects();
const opening = c.openIntegrityDialog();
app.requests.find((r) => r.path === "/storage/integrity/scans/latest").resolve({ scan_id: "scan-1", status: "completed", progress: { found_count: 70 } });
app.requests.find((r) => r.path === "/storage/integrity/actions/latest").resolve(null);
await settle(); c = app.render(); app.effects();
const initial = app.requests.filter((r) => r.path.includes("/findings?"));
for (const request of initial) request.resolve({ items: [{ finding_id: "initial", actions: ["recheck"] }], total_count: 70, cameras: [], roots: [], categories: [], review_counts: {} });
await opening; await settle(); c = app.render(); app.effects();
assert.equal(c.page.total_count, 70);
c.setFilters({ state: "active", camera_id: "1" }); c = app.render(); app.effects();
const old = app.requests.at(-1);
c.setFilters({ state: "active", camera_id: "2" }); c = app.render(); app.effects();
const fresh = app.requests.at(-1);
fresh.resolve({ items: [{ finding_id: "camera-two", actions: ["recheck"] }], total_count: 64, cameras: [], roots: [], categories: [], review_counts: {} });
await settle(); old.resolve({ items: [{ finding_id: "camera-one" }], total_count: 2 }); await settle();
c = app.render(); app.effects();
assert.equal(c.page.items[0].finding_id, "camera-two", "late old-filter response cannot overwrite the selected camera");
c.setAllMatching(true); c = app.render(); app.effects();
assert.equal(c.selectedCount, 64, "all matching means all server-side matches, not the loaded page");
c.toggleSelected("camera-two"); c = app.render(); app.effects();
assert.equal(c.selectedCount, 63);
assert.equal(c.isSelected("camera-two"), false, "unchecking a row excludes that row, not the entire bulk selection");
const preparing = c.prepareAction("acknowledge");
const previewRequest = app.requests.at(-1);
const body = JSON.parse(previewRequest.options.body);
assert.equal(body.selection.filters.camera_id, "2");
assert.equal(body.selection.ids, undefined);
assert.deepEqual(body.selection.exclude_ids, ["camera-two"]);
c.setFilters({ state: "active", camera_id: "3" }); c = app.render(); app.effects();
previewRequest.resolve({ scan_id: "scan-1", eligible_count: 64, action: "acknowledge" });
await preparing; await settle(); c = app.render();
assert.equal(c.confirmation, null, "an old preview cannot confirm a different current filter");
const translated = app.failure({ detail: { reason_code: "archive_integrity_file_changed" } }, (key) => key === "archiveReview.reasons.archive_integrity_file_changed" ? "File changed" : key);
assert.equal(translated, "File changed");
const dialog = read("components/storage/ArchiveIntegrityDialog.js");
assert.match(dialog, /role="dialog"/);
assert.match(dialog, /archiveReviewFilters/);
assert.match(dialog, /allMatching/);
assert.match(dialog, /archiveReviewConfirmation/);
assert.doesNotMatch(dialog, /storageIntegrityConfirmationOverlay|window\.confirm/);
assert.match(dialog, /className="archiveReviewDetails"/);
assert.match(dialog, /className="archiveReviewDetailResult"/);
assert.match(dialog, /archiveReview\.checkResult/);
const styles = read("app/styles/40-09-storage-migration-integrity.css");
assert.match(styles, /\.archiveReviewTable \.archiveReviewDetailsRow > td\s*\{[^}]*text-align:\s*left/);
assert.match(styles, /\.archiveReviewDetails dl\s*\{[^}]*display:\s*grid/);
assert.match(styles, /\.archiveReviewDetails dl > div\s*\{[^}]*display:\s*grid/);
assert.match(styles, /@media \(max-width: 480px\)[\s\S]*?\.archiveReviewDetails dl\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/);
console.log("Archive review: indicator explanations, locale parity, filter races, exact bulk scope and stale preview PASS");
