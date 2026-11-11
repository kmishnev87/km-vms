import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = path => fs.readFileSync(new URL(path, import.meta.url), "utf8");
const live = read("../app/live/page.js");
const css = read("../app/styles/90-live-workspace.css");
const layout = read("../components/Layout.js");
const body = name => {
  const result = live.match(new RegExp(`  function ${name}\\(\\) \\{([\\s\\S]*?)\\n  \\}`));
  assert.ok(result, `Missing handler ${name}`);
  return result[1];
};

test("Fit frames toggles off without moving any tile; the next press aligns again", () => {
  for (const active of [false, true]) {
    const calls = [];
    const run = new Function("frameLayoutActive", "setFrameLayoutActive", "applyFrameLayout", body("alignFrames"));
    run(active, next => calls.push(["mode", next]), () => calls.push(["align"]));
    assert.deepEqual(calls, active ? [["mode", false]] : [["mode", true], ["align"]]);
  }
});

test("ordinary Align is a one-shot grid action and exits automatic frame mode first", () => {
  const calls = [], tiles = [{ id: "a" }], bounds = { width: 1000, height: 600 };
  const run = new Function("setFrameLayoutActive", "setTiles", "layoutTiles", "activeLayoutTiles", "workspaceBounds", body("autoLayoutTiles"));
  run(value => calls.push(["mode", value]), fn => calls.push(["tiles", fn(tiles)]),
    (current, box) => { assert.equal(current, tiles); assert.equal(box, bounds); return "grid"; }, current => current, () => bounds);
  assert.deepEqual(calls, [["mode", false], ["tiles", "grid"]]);
});

test("Remove all closes views, PTZ and audio without deleting cameras or recordings", () => {
  const names = ["closePtz", "setPtzPositions", "setFullscreenTileId", "setActiveAudioTileId", "setAudioFactsByTileId", "setDragState", "setResizeState", "setTiles"];
  const calls = [];
  new Function(...names, body("removeAllTiles"))(...names.map(name => value => calls.push([name, value])));
  assert.deepEqual(calls, [["closePtz", undefined], ["setPtzPositions", {}], ["setFullscreenTileId", null],
    ["setActiveAudioTileId", ""], ["setAudioFactsByTileId", {}], ["setDragState", null], ["setResizeState", null], ["setTiles", []]]);
  assert.doesNotMatch(body("removeAllTiles"), /apiFetch|fetch\(|DELETE/);
});

test("camera heading is alone; five accessible icon buttons share one ordered row", () => {
  const start = live.indexOf('<div className="liveWorkspacePanelHeader">');
  const rowStart = live.indexOf('<div className="liveWorkspacePanelActions">', start);
  const rowEnd = live.indexOf('<div className="liveWorkspaceCameraList">', rowStart);
  const heading = live.slice(start, rowStart), row = live.slice(rowStart, rowEnd);
  assert.match(heading, /\{TEXT\.cameras\}/);
  assert.doesNotMatch(heading, /<button/);
  const buttons = [...row.matchAll(/<button\b([\s\S]*?)>([\s\S]*?)<\/button>/g)];
  assert.equal(buttons.length, 5);
  const expected = ['data-workspace-action="add-all"', 'data-workspace-action="remove-all"',
    'data-workspace-align-button="grid-2x2"', 'data-workspace-align-button="frames"', 'data-workspace-action="fullscreen"'];
  buttons.forEach(([all, attrs, content], i) => {
    assert.ok(attrs.includes(expected[i]));
    assert.match(attrs, /title=\{/);
    assert.match(attrs, /aria-label=\{/);
    assert.match(content, /<svg|<AlignGridIcon/);
    assert.doesNotMatch(content, /\{TEXT\.|\{t\(/);
    assert.equal(attrs.includes("aria-pressed="), i === 3);
  });
  assert.match(css, /grid-template-columns: repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.liveWorkspacePanelActions > button:focus-visible/);
});

test("Online fills remaining viewport with flex instead of old overflowing fixed heights", () => {
  assert.match(live, /<Layout className="liveWorkspaceLayout">/);
  assert.match(layout, /className \? `layoutShell \$\{className\}` : "layoutShell"/);
  assert.match(css, /\.liveWorkspaceLayout \{[^}]*height: 100dvh;[^}]*min-height: 0/s);
  assert.match(css, /\.liveWorkspaceLayout > \.topNav \{[^}]*flex-shrink: 0/s);
  assert.match(css, /\.liveWorkspaceLayout > \.mainContent \{[^}]*display: flex;[^}]*min-height: 0/s);
  const mobile = css.slice(css.indexOf("@media (max-width: 900px)"), css.indexOf("@media (max-width: 640px)"));
  assert.doesNotMatch(mobile, /height: 720px|height: auto;[\s\S]*flex-direction/);
  assert.match(mobile, /max-height: min\(260px, 40%\)/);
  assert.doesNotMatch(css.slice(0, css.indexOf(".liveSystemFullscreenBody")), /overflow:\s*hidden|100vh - 78px|min-height: 620px/);
});

test("frame layout and close-all tooltips are translated consistently", async () => {
  for (const [file, key, expected] of [["ru", "ruDictionary", "Выровнять по кадру"], ["en", "enDictionary", "Fit frames"], ["zhCN", "zhCNDictionary", "按画面排列"]]) {
    const source = read(`../lib/i18n/${file}.js`);
    const dictionary = (await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`))[key];
    assert.equal(dictionary.liveWorkspace.fitFrames, expected);
    assert.ok(dictionary.liveWorkspace.removeAll.length > 0);
    assert.ok(dictionary.liveWorkspace.addAll.length > 0);
    assert.ok(dictionary.liveWorkspace.alignTiles.length > 0);
  }
});
