import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const coreSource = fs
  .readFileSync(resolve(__dirname, "../lib/workspaceLayoutCore.js"), "utf8")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${coreSource}
this.mergeSidebarCameraOrder = mergeSidebarCameraOrder;
this.sanitizeSidebarCameraOrder = sanitizeSidebarCameraOrder;
this.SIDEBAR_CAMERA_REORDER_MIME = SIDEBAR_CAMERA_REORDER_MIME;
this.LIVE_CAMERA_DROP_MIME = LIVE_CAMERA_DROP_MIME;
this.CHRONOLOGY_CAMERA_DROP_MIME = CHRONOLOGY_CAMERA_DROP_MIME;`,
  context
);

const liveSource = fs.readFileSync(resolve(__dirname, "../app/live/page.js"), "utf8");
const chronologySource = fs.readFileSync(resolve(__dirname, "../app/chronology/page.js"), "utf8");

const cameras = [
  { id: "10", name: "Zulu" },
  { id: "5", name: "Alpha" },
  { id: "7", name: "Echo" },
];

assert.deepEqual(JSON.parse(JSON.stringify(context.mergeSidebarCameraOrder(cameras, ["7", "missing", "7", "10"]).map((camera) => camera.id))), ["7", "10", "5"]);
assert.deepEqual(JSON.parse(JSON.stringify(context.mergeSidebarCameraOrder(cameras, []).map((camera) => camera.id))), ["5", "7", "10"]);
assert.deepEqual(JSON.parse(JSON.stringify(context.sanitizeSidebarCameraOrder(["5", "5", 7, {}, ["bad"], "", null]))), ["5", "7"]);

assert.notEqual(context.SIDEBAR_CAMERA_REORDER_MIME, context.LIVE_CAMERA_DROP_MIME);
assert.notEqual(context.SIDEBAR_CAMERA_REORDER_MIME, context.CHRONOLOGY_CAMERA_DROP_MIME);

assert.match(liveSource, /WORKSPACE_KEY = "live"/);
assert.match(chronologySource, /WORKSPACE_KEY = "chronology"/);
assert.match(liveSource, /sidebarCameraOrder/);
assert.match(chronologySource, /sidebarCameraOrder/);
assert.doesNotMatch(liveSource, /localStorage\.setItem\([^)]*sidebarCameraOrder/);
assert.doesNotMatch(chronologySource, /localStorage\.setItem\([^)]*sidebarCameraOrder/);

assert.match(liveSource, /data-sidebar-camera-row/);
assert.doesNotMatch(liveSource, /data-sidebar-reorder-handle/);
assert.doesNotMatch(liveSource, /ReorderHandle/);
assert.doesNotMatch(liveSource, /reorderCamera/);
assert.match(liveSource, /data-sidebar-reorder-dragging/);
assert.match(liveSource, /data-sidebar-reorder-drop-target/);
assert.match(liveSource, /data-sidebar-reorder-drop-position/);
assert.match(chronologySource, /data-sidebar-camera-row/);
assert.doesNotMatch(chronologySource, /data-sidebar-reorder-handle/);
assert.doesNotMatch(chronologySource, /ReorderHandle/);
assert.doesNotMatch(chronologySource, /reorderCamera/);
assert.match(chronologySource, /data-sidebar-reorder-dragging/);
assert.match(chronologySource, /data-sidebar-reorder-drop-target/);
assert.match(chronologySource, /data-sidebar-reorder-drop-position/);
assert.match(liveSource, /SIDEBAR_CAMERA_REORDER_MIME/);
assert.match(chronologySource, /SIDEBAR_CAMERA_REORDER_MIME/);
assert.match(liveSource, /setData\(SIDEBAR_CAMERA_REORDER_MIME, String\(camera\.id\)\)/);
assert.match(liveSource, /setData\(LIVE_CAMERA_DROP_MIME, String\(camera\.id\)\)/);
assert.match(chronologySource, /setData\(SIDEBAR_CAMERA_REORDER_MIME, String\(camera\.id\)\)/);
assert.match(chronologySource, /setData\(CHRONOLOGY_CAMERA_DROP_MIME, String\(camera\.id\)\)/);

assert.match(liveSource, /\{TEXT\.addAll\}/);
assert.match(liveSource, /title=\{TEXT\.align\}/);
assert.match(liveSource, /aria-label=\{TEXT\.align\}/);
assert.match(liveSource, /data-workspace-align-button="grid-2x2"/);
assert.match(chronologySource, /data-workspace-align-button="grid-2x2"/);
assert.match(liveSource, /data-workspace-align-icon="grid-2x2"/);
assert.match(chronologySource, /data-workspace-align-icon="grid-2x2"/);
assert.match(liveSource, /title=\{isSystemFullscreen \? TEXT\.exitFullscreen : TEXT\.enterFullscreen\}/);
assert.match(liveSource, /data-live-fullscreen-active/);
assert.match(liveSource, /data-live-sidebar-collapsed/);
assert.match(chronologySource, /data-chronology-timeline-collapsed/);
assert.doesNotMatch(chronologySource, /data-chronology-fullscreen-exit="true"/);
assert.doesNotMatch(chronologySource, /chronologyFullscreenExitButton/);
assert.match(chronologySource, /chronologyAlignButton chronologyFullscreenButton/);
assert.doesNotMatch(liveSource, /topNav[^]*fullscreen/i);

assert.match(liveSource, /CompactVideoCanvas|TilePlayer/);
assert.match(chronologySource, /playbackCoordinator/);
assert.equal(fs.existsSync(resolve(__dirname, "../../api/app/services/archive_browser_playback.py")), false);
assert.equal(fs.existsSync(resolve(__dirname, "../../api/tests/test_archive_browser_playback_stage17_3.py")), false);
assert.equal(fs.existsSync(resolve(__dirname, "archiveBrowserPlaybackStage17_3.test.mjs")), false);
