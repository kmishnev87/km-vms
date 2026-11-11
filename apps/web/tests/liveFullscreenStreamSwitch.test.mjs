import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const live = read("app/live/page.js");
const css = read("app/styles/90-live-workspace.css");
const menu = live.slice(live.indexOf("{camera && fullscreenTileId === tile.id ? ("),
  live.indexOf("{camera && ptzCapabilities[camera.id]?.supported", live.indexOf("data-live-fullscreen-stream-switch=\"true\"")));

test("the stream menu belongs to the native fullscreen video, not its hidden tile bar", () => {
  assert.ok(menu.length > 0);
  assert.ok(live.indexOf('className="liveWorkspaceTileVideo"') < live.indexOf('data-live-fullscreen-stream-switch="true"'));
  assert.match(live, /const fullscreenEl = tileVideoRefs.current.get\(tileId\) \|\| tileRefs.current.get\(tileId\)/);
  assert.match(live, /await fullscreenEl.requestFullscreen\?\.\(\)/);
  assert.doesNotMatch(menu, /liveWorkspaceTileBar|liveWorkspaceTileTitle|removeTile|requestFullscreen|exitFullscreen/);
});

test("only available streams are shown; switching updates the existing tile without leaving fullscreen", () => {
  assert.match(menu, /streams.length > 1 \? streams.map/);
  assert.match(menu, /streams\.map\(\(item\) =>/);
  assert.match(menu, /aria-pressed=\{stream === item.key\}/);
  assert.match(menu, /if \(stream !== item.key\) updateTile\(tile.id, \{ stream: item.key \}\)/);
  assert.match(menu, /role="group"/);
  assert.match(menu, /aria-label=\{t\("cameras.liveStream"\)\}/);
  for (const lang of ["en", "ru", "zhCN"]) {
    assert.match(read(`lib/i18n/${lang}.js`), /liveStream:\s*"[^"\n]+"/);
  }
});

test("fullscreen audio uses the tile's real capability and shared mute state, including one-stream cameras", () => {
  assert.match(menu, /data-live-fullscreen-audio-button="true"/);
  assert.match(menu, /title=\{audioState.title\}/);
  assert.match(menu, /aria-label=\{audioState.title\}/);
  assert.match(menu, /disabled=\{audioState.disabled\}/);
  assert.match(menu, /aria-pressed=\{audioState.active \? "true" : "false"\}/);
  assert.match(menu, /onClick=\{\(\) => toggleTileAudio\(tile.id\)\}/);
  assert.ok(menu.indexOf('data-live-fullscreen-audio-button="true"') < menu.indexOf('streams.length > 1 ? streams.map'));
  assert.equal((live.match(/<LiveAudioIcon \/>/g) || []).length, 2);
  assert.match(live, /if \(fact.audioDisabledByConfig \|\| !fact.audioAvailable\) return/);
  assert.match(css, /\.liveFullscreenStreamSwitch \.liveWorkspaceAudioButton \{[^}]*width: 30px;[^}]*background: transparent;[^}]*box-shadow: none/);
});

test("menu gestures do not move a tile or toggle fullscreen, including the gaps between buttons", () => {
  assert.match(menu, /onPointerDown=\{\(event\) => event.stopPropagation\(\)\}/);
  assert.match(menu, /onDoubleClick=\{\(event\) => event.stopPropagation\(\)\}/);
  assert.match(live, /function handleTileSurfacePointerUp[\s\S]*?closest\?\.\("[^"\n]*\[data-live-fullscreen-stream-switch\]/);
  assert.match(live, /onDoubleClickCapture=\{\(event\) => \{\s*if \(event.target\?\.closest\?\.\("[^"\n]*\[data-live-fullscreen-stream-switch\]/);
});

test("fullscreen stays image-only and the shared menu no longer reserves a PTZ slot on its right", () => {
  assert.match(css, /\.liveFullscreenStreamSwitch \{[^}]*position: absolute;[^}]*background: rgba\(2, 6, 23, 0.6\)/);
  assert.doesNotMatch(css, /\.liveFullscreenStreamSwitch.hasPtz/);
  assert.match(menu, /className="liveFullscreenStreamSwitch"/);
  assert.match(css, /\.liveFullscreenStreamSwitch button:focus-visible/);
  assert.ok(css.includes('.liveFullscreenStreamSwitch button:hover:not(:disabled):not([aria-pressed="true"])'),
    "hover cannot hide the selected stream or active audio highlight");
  assert.match(live, /onTouchDoubleTap=\{\(\) => toggleTileFullscreen\(tile.id\)\}/);
  assert.match(live, /lowLatency=\{ptzCameraId === camera.id\}/);
});
