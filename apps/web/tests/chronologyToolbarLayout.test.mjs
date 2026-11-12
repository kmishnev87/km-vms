import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
const read = p => fs.readFileSync(new URL(p, import.meta.url), "utf8");
const live = read("../app/live/page.js"), page = read("../app/chronology/page.js");
const css = read("../app/styles/100-chronology-workspace.css");
const body = name => {
  const result = page.match(new RegExp(`  function ${name}\\([^)]*\\) \\{([\\s\\S]*?)\\n  \\}`));
  assert.ok(result, name);
  return result[1];
};
const buttons = (source, prefix) => {
  const start = source.indexOf(`<div className="${prefix}PanelActions">`);
  return [...source.slice(start, source.indexOf(`<div className="${prefix}CameraList">`, start)).matchAll(/<button\b([\s\S]*?)>([\s\S]*?)<\/button>/g)];
};
test("Chronology has the accepted five icons, order and localized tooltips", () => {
  const a = buttons(live, "liveWorkspace"), b = buttons(page, "chronology");
  assert.equal(b.length, 5);
  b.forEach(([, attrs, contents], i) => {
    assert.equal(contents.trim(), a[i][2].trim());
    for (const name of ["title", "aria-label", "onClick"])
      assert.equal(attrs.match(new RegExp(`${name}=\\{[^}]+\\}`))?.[0], a[i][1].match(new RegExp(`${name}=\\{[^}]+\\}`))?.[0]);
  });
  assert.match(css, /grid-template-columns: repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.chronologyPanelHeader \{[^}]*justify-content: center;[^}]*min-height: 30px/s);
});
test("frame mode can turn off without moving views and never seeks or changes playback keys", () => {
  const calls = [];
  new Function("frameLayoutActive", "setFrameLayoutActive", "applyFrameLayout", body("alignFrames"))(
    true, v => calls.push(v), () => { throw new Error("must not move on toggle off"); });
  assert.deepEqual(calls, [false]);
  for (const name of ["applyFrameLayout", "autoLayoutTiles", "alignFrames"])
    assert.doesNotMatch(body(name), /resolvePlaybackForTimestamp|cancelPlaybackCoordinator|setCurrentTs|playbackKey\s*[:=]/);
  assert.match(page, /setFrameLayoutActive\(false\);\s*updateTile\(dragState/);
  assert.match(page, /setFrameLayoutActive\(false\);\s*updateTile\(resizeState/);
});
test("sizing uses this archive source, not stale metadata; no video falls back to saved Main", () => {
  for (const valid of [true, false]) {
    const tile = { id: "view", cameraId: "1" }, camera = { id: 1, stream_dimensions: { main: { width: 1920, height: 1080 } } };
    let chosen;
    const run = new Function("workspaceRef", "setTiles", "activeLayoutTiles", "cameraMap", "tileRefs", "playbackMapRef", "fitCameraFrames", "sameFrameLayout", body("applyFrameLayout"));
    const player = { getAttribute: name => name === "data-natural-resolution" ? "1024x1024" : valid ? "current" : "old" };
    run({ current: { clientWidth: 1000, clientHeight: 700, querySelector: () => null } }, fn => fn([tile]), tiles => tiles,
      new Map([["1", camera]]), { current: new Map([["view", { querySelector: () => player }]]) },
      { current: { view: { playbackKey: "current" } } }, (tiles, cameras) => { chosen = cameras[0]; return tiles; }, () => true);
    assert.deepEqual(chosen.stream_dimensions.main, valid ? { width: 1024, height: 1024 } : { width: 1920, height: 1080 });
  }
  assert.match(page, /attributeFilter: \["data-natural-resolution"\]/);
  assert.match(page, /observer\.disconnect\(\); metadata\.disconnect\(\)/);
});
test("closing a view no longer references the absent page-local setter", () => {
  const calls = [];
  const tiles = [{ id: "a" }, { id: "b" }];
  new Function("tileId", "tilesRef", "invalidateSeekActions", "playbackResolverRef", "setIsPlaying", "setTiles", "setFullscreenTileId", body("removeTile"))(
    "a", { current: tiles }, () => calls.push("seek"), { current: { invalidate: () => calls.push("lookup") } },
    value => calls.push(value), fn => assert.deepEqual(fn(tiles), [{ id: "b" }]),
    fn => { assert.equal(fn("a"), null); assert.equal(fn("b"), "b"); });
  assert.deepEqual(calls, [], "closing one tile must not stop the remaining playback group");
  assert.doesNotMatch(page, /setPlaybackMap\(/);
});
test("closing all invalidates pending work, stops playback and only clears views", () => {
  const names = ["invalidateSeekActions", "cancelPlaybackCoordinator", "playbackResolverRef", "tilesRef", "setIsPlaying", "setFullscreenTileId", "setDragState", "setResizeState", "setDownloadChooserOpen", "setTiles"];
  const calls = [], tilesRef = { current: [{ id: "a" }] };
  const values = names.map(name => name === "tilesRef" ? tilesRef : name === "playbackResolverRef" ? { current: { invalidate: () => calls.push(["invalidate"]) } } : value => calls.push([name, value]));
  new Function(...names, body("removeAllTiles"))(...values);
  assert.deepEqual(tilesRef.current, []);
  assert.ok(calls.some(([name, value]) => name === "setIsPlaying" && value === false));
  assert.ok(calls.some(([name, value]) => name === "setTiles" && Array.isArray(value) && value.length === 0));
  assert.doesNotMatch(body("removeAllTiles"), /apiFetch|DELETE|setCurrentTs|setDate\(|setTime\(/);
});
