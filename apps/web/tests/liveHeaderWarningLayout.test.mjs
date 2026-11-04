import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import test from "node:test";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => fs.readFileSync(resolve(webRoot, path), "utf8");
const page = read("app/live/page.js");
const css = read("app/styles/90-live-workspace.css");

function rule(selector) {
  const marker = css.indexOf(`\n${selector} {`);
  assert.notEqual(marker, -1, `Missing rule: ${selector}`);
  const start = marker + 1;
  return css.slice(start, css.indexOf("}", start) + 1);
}

test("Live warnings no longer reserve a slot in the camera header", () => {
  const headerStart = page.indexOf('<div className="liveWorkspacePanelHeader">');
  const headerEnd = page.indexOf('<div className="liveWorkspaceCameraList">', headerStart);
  assert.ok(headerStart >= 0 && headerEnd > headerStart);
  const header = page.slice(headerStart, headerEnd);
  assert.doesNotMatch(header, /OperatorProblemBanners/);
  assert.match(header, /liveWorkspacePanelTitle/);
  assert.match(header, /onClick=\{addAllCameras\}/);
  assert.match(header, /onClick=\{autoLayoutTiles\}/);
  assert.match(header, /onClick=\{isSystemFullscreen \? exitSystemFullscreen : enterSystemFullscreen\}/);
});

test("Live keeps one contextual warning control inside the fullscreen workspace", () => {
  assert.equal((page.match(/<OperatorProblemBanners\b/g) || []).length, 1);
  const canvas = page.slice(page.indexOf('className={`liveWorkspaceCanvas'));
  assert.match(canvas, /<OperatorProblemBanners domains=\{\["live"\]\} limit=\{3\} className="liveWorkspaceProblemAnchor"/);
  const anchor = rule(".liveWorkspaceCanvas > .liveWorkspaceProblemAnchor");
  assert.match(anchor, /position:\s*absolute/);
  assert.match(anchor, /right:\s*12px/);
  assert.match(anchor, /bottom:\s*12px/);
  assert.match(anchor, /z-index:\s*5200/);
});

test("Live fixes the reserved slot without shrinking the existing controls", () => {
  assert.match(rule(".liveWorkspaceCameraPanel"), /width:\s*204px/);
  assert.match(rule(".liveWorkspaceCameraPanel"), /flex:\s*0 0 204px/);
  assert.match(rule(".liveWorkspacePanelHeader"), /min-height:\s*30px/);
  assert.match(rule(".liveWorkspacePanelTitle"), /font-size:\s*15px/);
  assert.match(rule(".liveWorkspaceAlignButton"), /height:\s*26px/);
  assert.match(rule(".liveWorkspaceAlignButton"), /padding:\s*0 8px/);
  assert.match(rule(".liveWorkspaceAlignButton"), /font-size:\s*11px/);
  assert.match(rule(".liveWorkspaceFullscreenButton"), /width:\s*28px/);
});
