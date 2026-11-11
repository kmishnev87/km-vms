import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const { fullscreenPtzPosition } = await import(`data:text/javascript;base64,${Buffer.from(read("lib/ptzUiGeometry.js")).toString("base64")}`);
const live = read("app/live/page.js"), floating = read("components/cameras/FloatingCameraPtz.js");

test("temporary inline dimensions cannot overwrite the fullscreen anchor", () => {
  const saved = { x: 900, y: 220 }, viewport = { width: 1700, height: 960 }, panel = { width: 186, height: 366 };
  const inline = fullscreenPtzPosition(saved, { width: 400, height: 220 }, panel, viewport);
  assert.deepEqual(inline, saved);
  assert.deepEqual(fullscreenPtzPosition(saved, viewport, panel, viewport, true), saved);
  assert.deepEqual(fullscreenPtzPosition(saved, viewport, { width: 186, height: 30 }, viewport, true), saved);
});

test("real viewport bounds still keep the panel reachable without mutating its chosen anchor", () => {
  const saved = { x: 900, y: 700 };
  assert.deepEqual(fullscreenPtzPosition(saved, { width: 800, height: 600 }, { width: 186, height: 366 },
    { width: 800, height: 600 }, true), { x: 602, y: 222 });
  assert.deepEqual(saved, { x: 900, y: 700 });
  assert.equal(fullscreenPtzPosition(null, {}, {}, {}), null);
});

test("closing a real tile drops only that view's anchor and closes its last PTZ control", () => {
  const begin = live.indexOf("  function removeTile(tileId) {");
  const end = live.indexOf("  function handleAudioStatusChange", begin);
  let positions = { first: { x: 900, y: 220 }, other: { x: 30, y: 50 } }, closed = 0;
  let tiles = [{ id: "first", cameraId: 89 }, { id: "other", cameraId: 95 }];
  const remove = new Function("tiles", "ptzCameraId", "closePtz", "setPtzPositions", "setFullscreenTileId",
    "setActiveAudioTileId", "setAudioFactsByTileId", "setTiles", `${live.slice(begin, end)};return removeTile;`)(
      tiles, 89, () => closed++, update => { positions = update(positions); }, () => {}, () => {}, () => {},
      update => { tiles = update(tiles); });
  remove("first");
  assert.deepEqual(positions, { other: { x: 30, y: 50 } }); assert.equal(closed, 1);
  assert.equal(tiles.length, 1);
  assert.equal(positions["readded-same-camera"], undefined, "a new view of the same camera has no old anchor");
});

test("fullscreen position belongs to the view and is neither stored with the camera nor persisted", () => {
  assert.match(live, /position=\{ptzPositions\[tile.id\]\}/);
  assert.match(live, /setPtzPositions\(\{\}\)/);
  assert.doesNotMatch(live, /position=\{ptzPreferences\[camera.id\]/);
  assert.match(floating, /anchor.current = gesture.last/);
  assert.match(floating, /fullscreenPtzPosition\(anchor.current/);
  assert.match(floating, /useLayoutEffect/);
  assert.match(floating, /bounds.right - menuBounds.left \+ 8/);
  assert.match(floating, /top: buttonBounds.top - bounds.top/);
  assert.doesNotMatch(floating, /localStorage|sessionStorage|apiFetch/);
});

test("both controls measure the same containing block before and during native fullscreen", () => {
  const css = read("app/styles/90-live-workspace.css");
  assert.match(css, /\.liveWorkspaceTileVideo \{\s*position: relative;/);
  assert.match(css, /\.liveWorkspaceTileVideo:fullscreen \{\s*position: fixed;\s*inset: 0;/);
});

test("normal release completes a click; cancellation and lost capture stop only an unfinished press", () => {
  const controls = read("components/cameras/CameraPtz.js");
  assert.match(controls, /controller.release\(\)/);
  assert.match(controls, /controller.press\(action, direction/);
  assert.match(controls, /function cancel\(event\) \{[\s\S]*?pressedPointer.current !== event.pointerId/);
  assert.match(controls, /onPointerCancel=\{cancel\} onLostPointerCapture=\{cancel\}/);
});
