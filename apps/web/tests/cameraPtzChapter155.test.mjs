import assert from "node:assert/strict";
import fs from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = name => fs.readFileSync(resolve(root, name), "utf8");
const source = read("lib/cameraPtzController.js");
const { createPtzController } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const settle = async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); };
const deferred = () => { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function harness(respond = () => ({ executed: true, movement_mode: "continuous", lease_seconds: 1.5 })) {
  const calls = [], changes = [], errors = [], timers = new Map();
  let serial = 0, timerId = 0, current = true;
  const controller = createPtzController({
    send: async (payload, options) => { calls.push({ payload, options }); return respond(payload, options); },
    makeId: () => `gesture-${++serial}`, isCurrent: () => current,
    onChange: value => changes.push(value), onError: error => errors.push(error),
    schedule: (run, delay) => { const id = ++timerId; timers.set(id, { run, delay }); return id; },
    unschedule: id => timers.delete(id),
  });
  return { controller, calls, changes, errors, timers, changeSession: () => { current = false; },
    renew: async () => { const [id, timer] = timers.entries().next().value; timers.delete(id); timer.run(); await settle(); } };
}

test("idle PTZ has no background timer or network request", async () => {
  const h = harness(); await settle();
  assert.equal(h.calls.length, 0); assert.equal(h.timers.size, 0);
  await h.controller.dispose(); assert.equal(h.calls.length, 0);
});

test("a held direction renews only its own lease, release sends independent keepalive Stop", async () => {
  const h = harness(); h.controller.start("move", "left", .08); await settle();
  assert.deepEqual(h.calls[0].payload, { action: "move", direction: "left", speed: .08,
    duration_seconds: 1.5, dry_run: false, hold: true, control_id: "gesture-1", sequence: 1 });
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].delay, 600);
  await h.renew(); assert.equal(h.calls[1].payload.sequence, 2);
  await h.controller.stop();
  assert.equal(h.timers.size, 0);
  assert.equal(h.calls.at(-1).payload.action, "stop");
  assert.equal(h.calls.at(-1).payload.control_id, "gesture-1");
  assert.deepEqual(h.calls.at(-1).options, { keepalive: true });
});

test("release before start reply cannot schedule another movement", async () => {
  const pending = deferred();
  const h = harness(payload => payload.action === "stop" ? { camera_stopped: true } : pending.promise);
  h.controller.start("zoom", "in", .04); await h.controller.stop();
  pending.resolve({ executed: true, movement_mode: "continuous", lease_seconds: 1.5 }); await settle();
  assert.equal(h.timers.size, 0);
  assert.deepEqual(h.calls.map(call => call.payload.action), ["zoom", "stop"]);
});

test("lost Stop reply is retried after late start acknowledgment with the same gesture", async () => {
  const pending = deferred(); let stops = 0;
  const h = harness(payload => {
    if (payload.action !== "stop") return pending.promise;
    if (++stops === 1) throw new Error("lost reply");
    return { camera_stopped: true };
  });
  h.controller.start("move", "right", .08); await h.controller.stop();
  pending.resolve({ executed: true }); await settle();
  assert.equal(stops, 2); assert.equal(h.timers.size, 0);
  assert.equal(h.calls.at(-1).payload.control_id, "gesture-1");
});

test("late response from a previous gesture cannot cancel the newer gesture", async () => {
  const first = deferred();
  const h = harness(payload => payload.action === "stop" ? { camera_stopped: true }
    : payload.control_id === "gesture-1" ? first.promise : { executed: true, movement_mode: "continuous", lease_seconds: 1.5 });
  h.controller.start("move", "left", .08); h.controller.start("move", "right", .08); await settle();
  first.resolve({ executed: true }); await settle();
  assert.equal(h.timers.size, 1);
  assert.equal(h.changes.at(-1).direction, "right");
  assert.equal(h.calls.filter(call => call.payload.action === "stop").length, 1);
  await h.controller.dispose(); assert.equal(h.calls.at(-1).payload.control_id, "gesture-2");
});

test("backend preemption clears pressed state without renewing", async () => {
  const h = harness(() => ({ ignored_stale_control: true, executed: false }));
  h.controller.start("move", "up", .08); await settle();
  assert.equal(h.changes.at(-1), null); assert.equal(h.timers.size, 0);
});

test("failed command attempts Stop and reports failure without retry loop", async () => {
  const h = harness(payload => { if (payload.action !== "stop") throw new Error("offline"); return { camera_stopped: true }; });
  h.controller.start("move", "down", .08); await settle();
  assert.deepEqual(h.calls.map(call => call.payload.action), ["move", "stop"]);
  assert.equal(h.errors.length, 1); assert.equal(h.timers.size, 0);
});

test("dispose stops a held gesture; StrictMode activation remains usable", async () => {
  const h = harness(); h.controller.start("zoom", "out", .04); await settle();
  await h.controller.dispose(); assert.equal(h.timers.size, 0);
  const before = h.calls.length;
  h.controller.start("move", "up", .08); assert.equal(h.calls.length, before);
  h.controller.activate(); h.controller.start("move", "up", .08); await settle();
  assert.equal(h.calls.at(-1).payload.direction, "up");
  await h.controller.dispose();
});

test("an expired or replaced auth session cannot send commands or late renewals", async () => {
  const pending = deferred(); const h = harness(() => pending.promise);
  h.controller.start("move", "up", .08); h.changeSession();
  pending.resolve({ executed: true }); await settle();
  h.controller.start("move", "down", .08); await h.controller.stop();
  assert.equal(h.calls.length, 1); assert.equal(h.timers.size, 0);
});

test("preset goto waits for held control Stop and suppresses duplicate clicks", async () => {
  const stop = deferred();
  const h = harness(payload => payload.action === "stop" ? stop.promise : { executed: true });
  h.controller.start("move", "left", .08); await settle();
  const go = h.controller.gotoPreset("gate");
  assert.equal(await h.controller.gotoPreset("home"), false);
  assert.equal(h.calls.at(-1).payload.action, "stop");
  stop.resolve({ camera_stopped: true }); assert.equal(await go, true);
  assert.deepEqual(h.calls.at(-1).payload, { action: "goto_preset", preset_token: "gate", dry_run: false });
  assert.equal(h.timers.size, 0);
});

test("emergency Stop works even without an active hold", async () => {
  const h = harness(() => ({ camera_stopped: true }));
  assert.equal(await h.controller.stop(true), true);
  assert.deepEqual(h.calls[0].payload, { action: "stop", dry_run: false });
});

test("the real hook sends JSON and binds commands to its selected profile", async () => {
  const hookSource = read("lib/useCameraPtz.js").replace(/^import .*;\r?\n/gm, "").replace(/export function /g, "function ");
  let options, request;
  const hook = new Function("useEffect", "useMemo", "useRef", "useState", "apiFetch", "getAuthToken",
    "useCurrentUser", "useAuthSession", "createPtzController", `${hookSource}\nreturn usePtzController;`)(
      () => {}, callback => callback(), value => ({ current: value }), value => [value, () => {}],
      async (path, init) => { request = { path, ...init }; return { camera_stopped: true }; },
      () => "test-session", () => ({}), () => "test-session", value => { options = value; return {}; });
  hook(89, "moving-profile", () => {});
  await options.send({ action: "stop", dry_run: false }, { keepalive: true });
  assert.equal(request.path, "/cameras/89/onvif/ptz/command");
  assert.equal(request.headers["Content-Type"], "application/json");
  assert.equal(request.keepalive, true);
  assert.deepEqual(JSON.parse(request.body), { action: "stop", dry_run: false, profile_token: "moving-profile" });
});

test("real view wiring keeps PTZ separate from camera actions and before audio/stream/close", () => {
  const cameras = read("app/cameras/page.js"), live = read("app/live/page.js"), css = read("app/styles/95-camera-ptz.css");
  assert.equal((cameras.match(/className="[^"]*\bcameraNameWithPtz\b[^"]*"/g) || []).length, 2, "both list and cards have an identity badge");
  const header = live.slice(live.indexOf('<div className={`liveWorkspaceTileBar'), live.indexOf('className="liveWorkspaceTileVideo"'));
  const badge = header.indexOf("<CameraPtzBadge"), stream = header.indexOf('className="liveWorkspaceTileSelect"');
  const audio = header.indexOf('data-live-audio-button="true"'), close = header.indexOf('removeTile(tile.id)');
  assert(badge >= 0 && badge < audio && audio < stream && stream < close, "PTZ → audio → stream → close");
  assert.match(css, /\.liveWorkspaceTileBar\.hasPtz\s*\{\s*grid-template-columns:minmax\(0,1fr\) 30px 30px 76px 30px/);
  assert.match(live, /className="liveSidebarPtz"/);
  assert.match(live, /fullscreenTileId === tile\.id \|\| \(isSystemFullscreen && isSidebarCollapsed\)/);
  assert.match(css, /--ptz-bg:rgba\(2,6,23,\.72\)/);
  assert.match(css, /\.cameraField\.cameraNameField > \.cameraPrimary\.cameraNameWithPtz\s*\{ display:flex; align-items:center; gap:8px;/,
    "list name/icon alignment must override the legacy display:block selector without changing cards");
  assert.match(css, /\.liveWorkspaceCameraIdentity \.cameraPtzBadge svg\s*\{ transform:translateY\(-1px\);/,
    "sidebar glyph optical center needs its measured 1px correction, not a shift of the whole row");
});

test("UI keeps existing permissions, profile binding, lifecycle Stops and no idle polling", () => {
  const hook = read("lib/useCameraPtz.js"), component = read("components/cameras/CameraPtz.js");
  assert.match(hook, /permissions\?\.includes\("manage_cameras"\)/);
  assert.match(hook, /profile_token: profileToken/);
  assert.match(hook, /headers: \{ "Content-Type": "application\/json" \}/, "JSON commands must not be parsed as text/plain");
  assert.match(component, /\[camera.id, capability\?\.profile_token, capability\?\.can_presets\]/, "changing lens must reload its presets");
  for (const event of ["blur", "pagehide", "visibilitychange", "km-vms-before-logout"]) assert(hook.includes(event));
  assert(!`${hook}\n${source}`.includes("setInterval"));
  assert(!/SetPreset|RemovePreset|SetVideoEncoderConfiguration/.test(component));
  assert.match(component, /onPointerCancel=\{cancel\}/);
  assert.match(component, /onLostPointerCapture=\{cancel\}/);
  assert.match(component, /document\.fullscreenElement \|\| document\.body/, "toast host must follow native fullscreen");
  assert.match(component, /createPortal\(<OperationToast toast=\{toast\} onClose=\{onClose\} \/>, host\)/,
    "PTZ toast must not inherit the panel's clipped positioning context");
});
