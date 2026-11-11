import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const load = path => import(`data:text/javascript;base64,${Buffer.from(read(path)).toString("base64")}`);
const { ptzImageBounds, ptzImageRectangle } = await load("lib/ptzUiGeometry.js");
const { createPtzController } = await load("lib/cameraPtzController.js");
const bounds = { left: 100, top: 200, width: 400, height: 400 }, dimensions = { width: 1920, height: 1080 };
const rectangle = { left: .2, top: .2, right: .8, bottom: .8 };

test("area geometry measures actual video rather than letterbox bars", () => {
  assert.deepEqual(ptzImageBounds(bounds, dimensions), { left: 100, top: 287.5, width: 400, height: 225 });
  const result = ptzImageRectangle({ x: 200, y: 343.75 }, { x: 400, y: 456.25 }, bounds, dimensions);
  assert.deepEqual(result, { left: .25, top: .25, right: .75, bottom: .75 });
  assert.equal(ptzImageRectangle({ x: 200, y: 220 }, { x: 400, y: 450 }, bounds, dimensions), null);
});

test("any drag direction zooms in to the same rectangle and clips endpoints to the image", () => {
  const start = { x: 200, y: 343.75 }, end = { x: 400, y: 456.25 };
  assert.deepEqual(ptzImageRectangle(start, end, bounds, dimensions), ptzImageRectangle(end, start, bounds, dimensions));
  const clipped = ptzImageRectangle(start, { x: 900, y: 900 }, bounds, dimensions);
  assert.deepEqual(clipped, { left: .25, top: .25, right: 1, bottom: 1 });
});

test("clicks, nearly flat selections and unknown or invalid dimensions are inert", () => {
  const start = { x: 200, y: 350 };
  assert.equal(ptzImageRectangle(start, start, bounds, dimensions), null);
  assert.equal(ptzImageRectangle(start, { x: 400, y: 355 }, bounds, dimensions), null);
  assert.equal(ptzImageRectangle(start, { x: 400, y: 450 }, bounds, { width: 0, height: 0 }), null);
  assert.equal(ptzImageRectangle(start, { x: Infinity, y: 450 }, bounds, dimensions), null);
});

function harness(respond) {
  const calls = [], errors = [];
  const controller = createPtzController({ makeId: () => "area-control-001", send: async payload => {
    calls.push(payload);
    return respond ? respond(payload) : payload.action === "stop" ? { camera_stopped: true } : { executed: true };
  }, onError: error => errors.push(error), schedule: () => assert.fail("finite area movement must not renew") });
  return { calls, controller, errors };
}

test("area action uses the bounded existing command endpoint with a cancellable identity", async () => {
  const h = harness();
  assert.equal(await h.controller.areaZoom(rectangle), true);
  assert.deepEqual(h.calls, [{ action: "area_zoom", ...rectangle, dry_run: false, control_id: "area-control-001", sequence: 1 }]);
  await h.controller.stop();
  assert.equal(h.calls.at(-1).action, "stop");
  assert.equal(h.calls.at(-1).control_id, "area-control-001");
});

test("closing while area response is delayed sends Stop and never reports late success", async () => {
  let resolve;
  const response = new Promise(done => { resolve = done; });
  const h = harness(payload => payload.action === "stop" ? { camera_stopped: true } : response);
  const running = h.controller.areaZoom(rectangle);
  await h.controller.dispose(); resolve({ executed: true });
  assert.equal(await running, false);
  assert.deepEqual(h.calls.map(p => p.action), ["area_zoom", "stop"]);
});

test("finite area errors stop without retrying a possibly accepted camera command", async () => {
  const h = harness(payload => { if (payload.action === "stop") return { camera_stopped: true }; throw new Error("lost reply"); });
  assert.equal(await h.controller.areaZoom(rectangle), false);
  assert.deepEqual(h.calls.map(p => p.action), ["area_zoom", "stop"]);
  assert.equal(h.errors.length, 1);
});

test("invalid area coordinates never send requests", async () => {
  const h = harness();
  for (const value of [{ ...rectangle, left: NaN }, { ...rectangle, bottom: 2 },
    { ...rectangle, left: true }, { ...rectangle, right: .2 }, null]) {
    assert.equal(await h.controller.areaZoom(value), false);
  }
  assert.equal(h.calls.length, 0);
});

test("camera dialog, sidebar and fullscreen share mutually exclusive point/area modes", () => {
  const live = read("app/live/page.js"), controls = read("components/cameras/CameraPtz.js");
  assert.match(live, /function pointMode[\s\S]*?setPtzAreaCameraId\(null\)/);
  assert.match(live, /function areaMode[\s\S]*?setPtzPointCameraId\(null\)/);
  assert.equal((live.match(/onAreaZoomModeChange=/g) || []).length, 2);
  assert.match(live, /onAreaZoom=\{rectangle => pointController.areaZoom\(rectangle\)\}/);
  assert.match(controls, /capability\?\.can_area_zoom && onAreaZoomModeChange/);
  assert.match(controls, /onAreaZoom=\{rectangle => controller.areaZoom\(rectangle\)\}/);
});

test("selection is overlay-only and can be cancelled without a hidden camera command", () => {
  const surface = read("components/cameras/PtzPointSurface.js"), css = read("app/styles/95-camera-ptz.css");
  assert.match(surface, /onPointerMove=/); assert.match(surface, /onPointerCancel=\{cancelPointer\}/);
  assert.match(surface, /onLostPointerCapture=\{cancelPointer\}/);
  assert.match(surface, /if \(pointer.current\?\.id !== event.pointerId\) return/);
  assert.match(surface, /sameBounds\(down.bounds, bounds\)/);
  assert.match(surface, /event.key !== "Escape"/);
  assert.match(surface, /useLayoutEffect\(\(\) => \{\s*pointer\.current = null; setSelection\(null\); setMarker\(null\)/);
  assert.match(css, /\.ptzAreaMarker \{ position:absolute;/);
  for (const language of ["en", "ru", "zhCN"]) {
    assert.match(read(`lib/i18n/${language}.js`), /areaMode: /);
    assert.match(read(`lib/i18n/${language}.js`), /areaHint: /);
  }
});

test("saved positions actuate on selection with no arrow, success toast or instruction paragraph", () => {
  const controls = read("components/cameras/CameraPtz.js"), css = read("app/styles/95-camera-ptz.css");
  assert.match(controls, /onChange=\{event => \{ void go\(event.target.value\); \}\}/);
  assert.match(controls, /await controller.gotoPreset\(token\)/);
  assert.doesNotMatch(controls, /ptzGo|ptzHoldHint|ptz\.presetSent|ptz\.holdHint/);
  assert.match(css, /background:var\(--ptz-stop,#e8eef7\)/);
  assert.match(css, /--ptz-stop:rgba\(100,116,139,\.4\)/);
  assert.match(css, /\.liveWorkspaceCameraIdentity \.cameraPtzBadge\[aria-expanded="true"\] \{ color:#fff; background:rgba/);
});
