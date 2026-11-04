import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => fs.readFileSync(resolve(root, path), "utf8");
const scope = {};
vm.runInNewContext(read("lib/operatorWarnings.js").replaceAll("export const ", "const ").replaceAll("export function ", "function ")
  + "\nthis.helpers = { cameraRuntimeWarningKey, recordingFailureMessage, buildOperatorWarnings, userCanReadRuntimeStatus, shouldStopRuntimeStatusPolling };", scope);
const h = scope.helpers;
const plain = (value) => JSON.parse(JSON.stringify(value));
const liveItem = { camera_id: 7, camera_name: "PTZ", severity: "error", reason_codes: ["live_failed", "camera_unreachable"], safe_failure_reason: "camera_unreachable" };
const warnings = h.buildOperatorWarnings({ domains: {
  cameras: { items: [{ ...liveItem, name: "PTZ", confirmed_recording: true }] }, live: { items: [liveItem] },
} });
assert.deepEqual(plain(warnings.map((item) => item.id)), ["live-stream-errors"], "a live fault is not a duplicate recording fault");
const recording = h.buildOperatorWarnings({ domains: { cameras: { items: [{ camera_id: 7, name: "PTZ", severity: "warning", reason_codes: ["no_evidence", "recording_start_timeout"], recording_state: "starting" }] } } });
assert.equal(recording[0].affected_cameras[0].camera_name, "PTZ");
assert.equal(h.recordingFailureMessage(null, recording[0].affected_cameras[0].reason_codes), "Запись не началась за 30 секунд.");
assert.equal(h.recordingFailureMessage("auth_failed", ["recording_failed"]), "Камера отклонила логин или пароль.");
assert.equal(h.recordingFailureMessage("unsafe arbitrary backend text", []), "Нет актуального подтверждения записи.");

const camera = { camera_id: 7, enabled: true, job_state: "starting", current_failure: false, confirmed_recording: false, recording_health: "starting", heartbeat_current: true, recording_health_reason_codes: [], recording_startup_elapsed_seconds: 5, recording_startup_warning_after_seconds: 30 };
const summary = { health: "healthy", service_status: "healthy", camera_recording_states: [camera] };
const key = h.cameraRuntimeWarningKey(summary);
assert.equal(h.cameraRuntimeWarningKey(null), null);
assert.equal(h.cameraRuntimeWarningKey({ ...summary, camera_recording_states: [{ ...camera, recording_startup_elapsed_seconds: 15, current_segment_size_bytes: 500000, media_progress_age_seconds: 4 }] }), key, "age and bytes do not cause constant invalidations");
assert.notEqual(h.cameraRuntimeWarningKey({ ...summary, camera_recording_states: [{ ...camera, recording_startup_elapsed_seconds: 30 }] }), key, "crossing the grace boundary invalidates once");
assert.notEqual(h.cameraRuntimeWarningKey({ ...summary, camera_recording_states: [{ ...camera, job_state: "recording", confirmed_recording: true, recording_health: "recording" }] }), key);
assert.notEqual(h.cameraRuntimeWarningKey({ ...summary, camera_recording_states: [{ ...camera, enabled: false }] }), key);
assert.notEqual(h.cameraRuntimeWarningKey({ ...summary, health: "degraded" }), key);

const settle = async () => { for (let tick = 0; tick < 25; tick += 1) await Promise.resolve(); };
function effectHarness(path, canRead = true) {
  const source = read(path);
  const from = source.indexOf("  useEffect(() => {");
  const end = source.indexOf(path.includes("OperatorProblem") ? "\n  const warnings" : "\n  const ownsStatus", from);
  assert(from >= 0 && end > from);
  const listeners = new Map(), timers = new Map(), intervals = new Map(), requests = [];
  let next = 0, cleanup, runtime = null, accessDenied = false, loading = false, stale = false;
  const user = { id: 1, permissions: canRead ? ["run_diagnostics"] : [] };
  const context = {
    ...h, DEFAULT_REFRESH_MS: 30000, REFRESH_MS: 30000,
    currentUser: user, effectiveCurrentUser: user, currentUserReady: true, userIdentity: "1", canRead, enabled: true,
    cacheIdentity: "user-1", authSession: "test-session", runtimeStatusCache: { read: () => null, write() {} },
    setStatusOwner() {},
    useEffect(effect) { cleanup = effect(); },
    setRuntimeStatus(value) { runtime = value; }, setAccessDenied(value) { accessDenied = value; },
    setStale(value) { stale = value; },
    setDeniedUserIdentity(value) { if (typeof value !== "function") accessDenied = Boolean(value); }, setLoading(value) { loading = value; },
    getSystemRuntimeStatus() { return new Promise((resolve, reject) => requests.push({ path: '/system/runtime/status', resolve, reject })); },
    setTimeout(fn, ms) { const id = ++next; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, ms) { const id = ++next; intervals.set(id, { fn, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
    window: {
      addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn); },
      removeEventListener(name, fn) { listeners.get(name)?.delete(fn); },
    },
  };
  vm.runInNewContext(source.slice(from, end), context);
  return { requests, timers, intervals, dispatch(name) { for (const fn of listeners.get(name) || []) fn(); },
    flush() { const work = [...timers.values()]; timers.clear(); for (const item of work) item.fn(); },
    get runtime() { return runtime; }, get denied() { return accessDenied; }, unmount() { cleanup(); },
    get stale() { return stale; },
    get listenerCount() { return [...listeners.values()].reduce((sum, items) => sum + items.size, 0); },
  };
}

for (const component of ["components/OperatorProblemBanners.js", "components/SystemHealthIndicator.js"]) {
  const effect = effectHarness(component);
  assert.equal(effect.requests.length, 1);
  effect.dispatch("kmvms:camera-runtime-changed"); effect.dispatch("kmvms:live-runtime-changed"); effect.flush();
  assert.equal(effect.requests.length, 1, "an active fetch is not duplicated");
  effect.requests[0].resolve({ generation: "before-change" }); await settle();
  assert.equal(effect.timers.size, 1, "an event arriving during a fetch is retained");
  effect.flush(); assert.equal(effect.requests.length, 2);
  effect.requests[1].resolve({ generation: "after-change" }); await settle();
  assert.equal(effect.runtime.generation, "after-change");
  assert.equal([...effect.intervals.values()][0].ms, 30000, "background polling is not accelerated");
  effect.dispatch("kmvms:camera-runtime-changed"); effect.dispatch("kmvms:camera-runtime-changed"); effect.flush();
  assert.equal(effect.requests.length, 3, "bursts coalesce into one refresh");
  effect.requests[2].reject({ status: 503 }); await settle();
  assert.equal(effect.runtime.generation, "after-change", "a transport failure keeps the last known snapshot");
  assert.equal(effect.stale, true);
  assert.equal(effect.intervals.size, 1, "a temporary transport failure does not stop retries");
  effect.dispatch("kmvms:camera-runtime-changed"); effect.flush();
  effect.requests[3].reject({ status: 403 }); await settle();
  assert.equal(effect.denied, true); assert.equal(effect.intervals.size, 0);
  effect.dispatch("kmvms:camera-runtime-changed"); effect.flush();
  assert.equal(effect.requests.length, 4, "access denial stops event-triggered retries too");
  effect.unmount(); assert.equal(effect.listenerCount, 0); assert.equal(effect.timers.size, 0);

  const restricted = effectHarness(component, false);
  restricted.dispatch("kmvms:camera-runtime-changed"); restricted.flush();
  assert.equal(restricted.requests.length, 0); restricted.unmount();

  const closed = effectHarness(component);
  closed.unmount(); closed.requests[0].resolve({ generation: "late" }); await settle();
  assert.equal(closed.runtime, null); assert.equal(closed.timers.size, 0);
}
const page = read("app/cameras/page.js");
assert(page.includes('domains={["cameras", "recorder", "live"]}'));
assert(page.includes("request !== secondaryRequestRef.current"));
assert(page.includes("request !== camerasRequestRef.current"));
for (const message of read("lib/operatorWarnings.js").matchAll(/return "([^"\n]*[А-Яа-яЁё][^"\n]*)";/g)) {
  if (message[1].includes(".")) assert(read("lib/i18n/legacyTextTranslations.js").includes(`"${message[1]}":`), `translation missing for ${message[1]}`);
}
console.log("Operator warning startup, named reasons, convergence and refresh lifecycle PASS");
