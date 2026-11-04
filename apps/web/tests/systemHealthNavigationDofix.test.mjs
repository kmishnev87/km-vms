import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { createRuntimeStatusCache } from "../lib/runtimeStatusCache.js";

let now = 1000;
const cache = createRuntimeStatusCache(() => now);
const user = { id: 7, role: "owner", permissions: ["run_diagnostics"] };
const key = JSON.stringify(["7", "owner", ["run_diagnostics"]]);
const healthy = { domains: Object.fromEntries(["storage", "cameras", "live", "recorder", "retention", "reconciliation"].map(name => [name, { severity: "ok" }])) };
cache.write(key, "session-a", healthy);
assert.equal(cache.read(key, "session-a").runtimeStatus, healthy);
assert.equal(cache.read("other-user", "session-a"), null);
assert.equal(cache.read(key, "session-b"), null);
now += 90001;
assert.equal(cache.read(key, "session-a"), null, "old snapshots are not kept indefinitely");
cache.write(key, "session-a", healthy, { denied: true });
assert.equal(cache.read(key, "session-a").runtimeStatus, null);
assert.equal(cache.read(key, "session-b"), null);

const read = (path) => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const helpers = {};
vm.runInNewContext(read("lib/operatorWarnings.js").replaceAll("export const ", "const ").replaceAll("export function ", "function ")
  + "\nthis.h = { buildDashboardStatusSummary, runtimeStatusUserIdentity, shouldStopRuntimeStatusPolling, systemHealthIndicatorModel, userCanReadRuntimeStatus };", helpers);
const source = read("components/SystemHealthIndicator.js");
const hook = source.slice(source.indexOf("export function useSystemHealthStatus("), source.indexOf("\nexport default function")).replace("export function ", "function ");
const shared = createRuntimeStatusCache(() => now);
const settle = async () => { for (let tick = 0; tick < 25; tick += 1) await Promise.resolve(); };

function mount() {
  const states = [], requests = [], intervals = new Map(), timers = new Map(), listeners = new Map();
  let cursor = 0, effect = null, cleanup = null, lastDeps = null, nextTimer = 0, session = "session-a";
  const context = {
    ...helpers.h, REFRESH_MS: 30000, runtimeStatusCache: shared,
    getAuthToken: () => session,
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index], (value) => { states[index] = typeof value === "function" ? value(states[index]) : value; }];
    },
    useEffect(fn, deps) {
      if (!lastDeps || deps.some((dep, i) => dep !== lastDeps[i])) {
        effect = fn; lastDeps = deps;
      }
    },
    useMemo: (fn) => fn(),
    getSystemRuntimeStatus: () => new Promise((resolve, reject) => requests.push({ path: '/system/runtime/status', resolve, reject })),
    setInterval(fn, ms) { const id = ++nextTimer; intervals.set(id, { fn, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    window: {
      addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn); },
      removeEventListener(name, fn) { listeners.get(name)?.delete(fn); },
    },
  };
  vm.runInNewContext(`${hook}\nthis.useStatus = useSystemHealthStatus;`, context);
  return {
    requests, intervals, timers,
    render(currentUser = user) { cursor = 0; return context.useStatus(currentUser); },
    effects() { if (effect) { cleanup?.(); cleanup = effect(); effect = null; } },
    unmount() { cleanup?.(); },
    session(value) { session = value; },
  };
}

const first = mount();
assert.equal(first.render().state, "unknown", "a cold load must not invent health before the first response");
first.effects();
first.requests[0].resolve(healthy); await settle();
assert.equal(first.render().state, "healthy");
assert.equal([...first.intervals.values()][0].ms, 30000);
first.unmount();
const second = mount();
assert.equal(second.render().state, "healthy", "the first frame after route remount reuses the known status");
second.effects();
assert.equal(second.render().state, "healthy", "starting the refresh must not make the icon grey");
assert.equal(second.render({ ...user }).state, "healthy", "an equivalent new user object must not reset health");
second.effects();
assert.equal(second.requests.length, 1);
second.requests[0].reject({ status: 503 }); await settle();
assert.equal(second.render().state, "unknown", "a real transport failure is not falsely shown as healthy");
second.unmount();

shared.write(key, "session-a", healthy);
const denial = mount();
assert.equal(denial.render().state, "healthy"); denial.effects();
denial.requests[0].reject({ status: 403 }); await settle();
assert.equal(denial.render().visible, false);
assert.equal(denial.intervals.size, 0);
denial.unmount();
const deniedRemount = mount();
assert.equal(deniedRemount.render().visible, false, "a navigation cannot expose the denied cached snapshot");
deniedRemount.effects();
assert.equal(deniedRemount.requests.length, 0);
deniedRemount.unmount();

shared.write(key, "session-a", healthy);
const different = mount();
assert.equal(different.render({ ...user, id: 8 }).runtimeStatus, null);
assert.equal(different.render({ ...user, permissions: [] }).visible, false);
assert.equal(different.render().runtimeStatus, null, "changing back does not expose a different state owner before its effect");
different.effects(); different.unmount();
const sessionChange = mount();
assert.equal(sessionChange.render().state, "healthy"); sessionChange.effects();
sessionChange.session("session-b");
assert.equal(sessionChange.render().runtimeStatus, null, "a new sign-in is isolated even for the same account");
sessionChange.effects();
sessionChange.requests[0].resolve({ privateOldSession: true }); await settle();
assert.equal(sessionChange.render().runtimeStatus, null, "a late old-session response is ignored");
sessionChange.requests[1].resolve(healthy); await settle();
assert.equal(sessionChange.render().state, "healthy");
sessionChange.unmount();
assert.doesNotMatch(read("lib/runtimeStatusCache.js"), /localStorage|sessionStorage|indexedDB/);
assert.match(read("app/system-status/page.js"), /useState\(\(\) => Boolean\(currentUser\) && !loading\)/, "a known authorized route must not blank the shared header for one commit");
console.log("System health first-frame navigation, polling, expiry, denial and session separation PASS");
