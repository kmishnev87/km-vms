import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { buildDashboardStatusSummary, systemHealthIndicatorModel } from "../lib/operatorWarnings.js";
import { ruDictionary } from "../lib/i18n/ru.js";
import { enDictionary } from "../lib/i18n/en.js";
import { zhCNDictionary } from "../lib/i18n/zhCN.js";
import { archiveIntegrityFindingPresentation } from "../lib/storageArchiveIntegrityHelpers.js";

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
  return { requests, notices, props,
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

const fixtureRows = ["one", "two", "three"].map((id) => ({ finding_id: id, state: "active",
  actions: ["recheck", "delete", "acknowledge"] }));
async function loadedApp(scan = { scan_id: "scan-ready", status: "completed" }) {
  const instance = mount();
  let current = instance.render(); instance.effects();
  const opening = current.openIntegrityDialog();
  current = instance.render();
  assert.equal(current.opening, true, "initial fetch is loading, not 'never checked'");
  instance.requests.find((r) => r.path === "/storage/integrity/scans/latest").resolve(scan);
  instance.requests.find((r) => r.path === "/storage/integrity/actions/latest").resolve(null);
  await opening; await settle(); current = instance.render(); instance.effects();
  for (const request of instance.requests.filter((r) => r.path.includes("/findings?"))) {
    request.resolve({ items: fixtureRows, total_count: 3, next_cursor: "next-page", cameras: [], roots: [], categories: [], review_counts: {} });
  }
  await settle(); current = instance.render(); instance.effects();
  return { instance, current };
}

// A row dropdown stages its own action and selection; it never launches a
// single-row preview or silently rewrites a different selected row.
const staged = await loadedApp();
let s = staged.current;
const beforeStage = staged.instance.requests.length;
s.stageRowAction("one", "delete"); s = staged.instance.render(); staged.instance.effects();
assert.equal(s.rowAction("one"), "delete"); assert.equal(s.isSelected("one"), true);
s.stageRowAction("two", "acknowledge"); s = staged.instance.render(); staged.instance.effects();
assert.equal(s.selectedCount, 2); assert.equal(s.rowAction("one"), "delete");
assert.equal(s.rowAction("two"), "acknowledge"); assert.equal(s.confirmation, null);
assert.equal(staged.instance.requests.length, beforeStage, "choosing a row action must not call the server");
s.togglePage(); s = staged.instance.render(); staged.instance.effects();
assert.equal(s.selectedCount, 3); assert.equal(s.rowAction("one"), "delete", "select page preserves existing row assignments");
assert.equal(s.rowAction("two"), "acknowledge"); assert.equal(s.rowAction("three"), "recheck");
const mixedPreview = s.prepareAction();
const mixedRequest = staged.instance.requests.at(-1);
const mixedBody = JSON.parse(mixedRequest.options.body);
assert.deepEqual(mixedBody.selection.ids.sort(), ["one", "three", "two"]);
assert.deepEqual(mixedBody.action_overrides, { one: "delete", two: "acknowledge" });
mixedRequest.resolve({ scan_id: "scan-ready", action: "mixed", eligible_count: 3, selection_hash: "hash",
  action_counts: [{ action: "delete", eligible_count: 1 }, { action: "acknowledge", eligible_count: 1 }, { action: "recheck", eligible_count: 1 }] });
await mixedPreview; s = staged.instance.render(); staged.instance.effects();
assert.equal(s.confirmation.action_counts.length, 3);
s.toggleSelected("three"); s = staged.instance.render(); staged.instance.effects();
assert.equal(s.confirmation, null, "checkbox changes invalidate already-visible confirmation");
const countBeforeStaleApply = staged.instance.requests.length;
await s.applyAction();
assert.equal(staged.instance.requests.length, countBeforeStaleApply);
s.setBulkAction("acknowledge"); s = staged.instance.render(); staged.instance.effects();
assert.equal(s.rowAction("one"), "acknowledge", "an explicit bulk action replaces staged row choices");
assert.equal(s.rowAction("two"), "acknowledge");
s.stageRowAction("one", ""); s = staged.instance.render(); staged.instance.effects();
assert.equal(s.isSelected("one"), false); assert.equal(s.rowAction("one"), "");

// Draft invalidation is separate from list sequencing: changing a checkbox
// while another page arrives cannot strand the dialog in loading forever.
const nextPage = s.loadMoreIntegrityFindings();
const pendingPage = staged.instance.requests.at(-1);
s.toggleSelected("three"); s = staged.instance.render(); staged.instance.effects();
pendingPage.resolve({ items: [{ finding_id: "four", actions: ["recheck"] }], total_count: 4 });
await nextPage; s = staged.instance.render(); staged.instance.effects();
assert.equal(s.loading, false); assert.equal(s.page.items.length, 4);

// An in-flight destructive preview cannot reappear after starting a scan.
const scanRace = await loadedApp();
let r = scanRace.current;
r.stageRowAction("one", "delete"); r = scanRace.instance.render(); scanRace.instance.effects();
const delayedPreview = r.prepareAction();
const delayedRequest = scanRace.instance.requests.at(-1);
const start = r.startIntegrityScan();
const startRequest = scanRace.instance.requests.at(-1);
assert.equal(startRequest.path, "/storage/integrity/scans");
assert.equal(scanRace.instance.render().scanStarting, true, "the pre-response stage is explicitly busy too");
delayedRequest.resolve({ scan_id: "scan-ready", action: "delete", eligible_count: 1, selection_hash: "old" });
await delayedPreview;
startRequest.resolve({ scan_id: "scan-new", status: "queued" });
await start; await settle(); r = scanRace.instance.render(); scanRace.instance.effects();
assert.equal(r.confirmation, null); assert.equal(r.selectedCount, 0); assert.equal(r.reviewable, false);
const callsWhileScanning = scanRace.instance.requests.length;
await r.prepareAction("delete"); await r.applyAction();
assert.equal(scanRace.instance.requests.length, callsWhileScanning, "no preview/apply is sent against a running check");

// A visible confirmation is cleared immediately too, before POST completes.
const visible = await loadedApp();
let v = visible.current;
v.stageRowAction("one", "delete"); v = visible.instance.render(); visible.instance.effects();
const preparingVisible = v.prepareAction();
visible.instance.requests.at(-1).resolve({ scan_id: "scan-ready", action: "delete", eligible_count: 1, selection_hash: "visible" });
await preparingVisible; v = visible.instance.render(); visible.instance.effects();
assert.ok(v.confirmation);
const startingVisible = v.startIntegrityScan();
v = visible.instance.render();
assert.equal(v.confirmation, null, "a new check immediately drops the previous destructive confirmation");
visible.instance.requests.at(-1).resolve({ scan_id: "new-visible", status: "queued" }); await startingVisible;

const rejectedScan = await loadedApp();
const rejectedStart = rejectedScan.current.startIntegrityScan();
rejectedScan.instance.requests.at(-1).reject(new Error("request lost"));
await rejectedStart;
const rejectedState = rejectedScan.instance.render(); rejectedScan.instance.effects();
assert.equal(rejectedState.listFailed, true, "a failed scan request cannot turn a cleared list into a false healthy result");

// A failed initial fetch can be retried; it is never reported as a virgin or
// healthy archive, and it cannot leave destructive controls enabled.
const failedOpen = mount(); let f = failedOpen.render(); failedOpen.effects();
const openingFailure = f.openIntegrityDialog();
failedOpen.requests[0].reject(new Error("offline")); failedOpen.requests[1].resolve(null);
await openingFailure; f = failedOpen.render(); failedOpen.effects();
assert.equal(f.listFailed, true); assert.equal(f.opening, false); assert.equal(f.reviewable, false);
const refreshFailure = f.refreshReview();
assert.equal(failedOpen.requests.at(-1).path, "/storage/integrity/scans/latest");
failedOpen.requests.at(-1).resolve({ status: "not_run" }); await refreshFailure;
f = failedOpen.render(); failedOpen.effects(); assert.equal(f.listFailed, false);

// Applying a mixed confirmation sends its original base action plus bound
// per-row overrides. Retrying failures retains each failed row's own action.
const retry = await loadedApp(); let q = retry.current;
q.stageRowAction("one", "delete"); q.stageRowAction("two", "acknowledge");
q = retry.instance.render(); retry.instance.effects();
const retryPreview = q.prepareAction();
retry.instance.requests.at(-1).resolve({ scan_id: "scan-ready", action: "mixed", eligible_count: 2, selection_hash: "retry-hash" });
await retryPreview; q = retry.instance.render(); retry.instance.effects();
const applyMixed = q.applyAction();
const applyBody = JSON.parse(retry.instance.requests.at(-1).options.body);
assert.equal(applyBody.action, "recheck"); assert.deepEqual(applyBody.action_overrides, { one: "delete", two: "acknowledge" });
retry.instance.requests.at(-1).resolve({ batch_id: "mixed-batch", scan_id: "scan-ready", action: "mixed", requested_action: "recheck", state: "partial", failed_count: 1 });
await applyMixed; q = retry.instance.render(); retry.instance.effects();
const retrying = q.retryFailed();
retry.instance.requests.at(-1).resolve({ items: [{ finding_id: "one", action: "delete", state: "blocked" }], next_cursor: "more-results" });
await settle();
assert.match(retry.instance.requests.at(-1).path, /cursor=more-results/);
retry.instance.requests.at(-1).resolve({ items: [{ finding_id: "two", action: "acknowledge", state: "completed" }] });
await settle();
const retryRequest = retry.instance.requests.at(-1);
assert.match(retryRequest.path, /actions\/preview$/);
const retryBody = JSON.parse(retryRequest.options.body);
assert.deepEqual(retryBody.selection.ids, ["one"]); assert.deepEqual(retryBody.action_overrides, { one: "delete" });
retryRequest.resolve({ scan_id: "scan-ready", action: "delete", eligible_count: 1 }); await retrying;

assert.equal(archiveIntegrityFindingPresentation({ category: "stale_writing_segment", impact_key: "recording_unavailable" }).detailKey,
  "integrityImpactStateUnconfirmed", "old persisted impact cannot falsely label unconfirmed media as unplayable");
assert.match(dialog, /value=\{c\.rowAction\(finding.finding_id\)\}/);
assert.match(dialog, /c\.stageRowAction/);
assert.match(dialog, /model\.running \? t\("archiveReview\.checking"\)/);
assert.match(dialog, /c\.opening \|\| c\.scanStarting \? copy\.loading/);
assert.match(dialog, /c\.integrityBusy \|\| model\.running \? "true"/);
assert.match(dialog, /archiveReviewPlanCounts/);
assert.match(dialog, /confirmation\.action_counts/);
console.log("Archive review: locale parity, staged mixed actions, exact scope, time explanation, loading, scan/preview races, confirmation and retry PASS");
