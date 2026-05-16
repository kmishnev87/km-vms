import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const livePage = read("app/live/page.js");
const chronologyPage = read("app/chronology/page.js");
const liveCss = read("app/styles/90-live-workspace.css");
const chronologyCss = read("app/styles/100-chronology-workspace.css");

for (const source of [livePage, chronologyPage]) {
  assert.match(source, /SIDEBAR_CAMERA_REORDER_MIME/);
  assert.doesNotMatch(source, /data-sidebar-reorder-handle/);
  assert.doesNotMatch(source, /ReorderHandle/);
  assert.doesNotMatch(source, /reorderCamera/);
  assert.match(source, /data-sidebar-reorder-dragging/);
  assert.match(source, /data-sidebar-reorder-drop-target/);
  assert.match(source, /data-sidebar-reorder-drop-position/);
  assert.match(source, /isReorderDragging/);
  assert.match(source, /isReorderDropTarget/);
  assert.match(source, /isReorderDropBefore/);
  assert.match(source, /isReorderDropAfter/);
  assert.match(source, /data-workspace-align-button="grid-2x2"/);
  assert.match(source, /data-workspace-align-icon="grid-2x2"/);
}

assert.match(livePage, /setData\(SIDEBAR_CAMERA_REORDER_MIME, String\(camera\.id\)\)/);
assert.match(livePage, /setData\(LIVE_CAMERA_DROP_MIME, String\(camera\.id\)\)/);
assert.match(chronologyPage, /setData\(SIDEBAR_CAMERA_REORDER_MIME, String\(camera\.id\)\)/);
assert.match(chronologyPage, /setData\(CHRONOLOGY_CAMERA_DROP_MIME, String\(camera\.id\)\)/);

assert.match(liveCss, /\.liveWorkspaceCameraItem\.isReorderDragging/);
assert.match(liveCss, /\.liveWorkspaceCameraItem\.isReorderDropTarget/);
assert.match(liveCss, /\.liveWorkspaceCameraItem\.isReorderDropBefore/);
assert.match(liveCss, /\.liveWorkspaceCameraItem\.isReorderDropAfter/);
assert.match(chronologyCss, /\.chronologyCameraItem\.isReorderDragging/);
assert.match(chronologyCss, /\.chronologyCameraItem\.isReorderDropTarget/);
assert.match(chronologyCss, /\.chronologyCameraItem\.isReorderDropBefore/);
assert.match(chronologyCss, /\.chronologyCameraItem\.isReorderDropAfter/);
assert.match(liveCss, /\.workspaceAlignGridIconLine\.vertical\.left/);
assert.match(liveCss, /\.workspaceAlignGridIconLine\.horizontal\.bottom/);

assert.match(liveCss, /\.liveWorkspaceShell\.systemFullscreen[\s\S]*overflow: hidden/);
assert.match(liveCss, /\.liveWorkspaceShell\.systemFullscreen \.liveWorkspaceCanvas[\s\S]*min-height: 0/);
assert.match(livePage, /fullscreenchange/);
assert.match(livePage, /data-live-fullscreen-active/);
assert.match(livePage, /data-live-sidebar-collapsed/);

assert.match(chronologyPage, /isTimelineCollapsed/);
assert.match(chronologyPage, /data-chronology-timeline-collapsed/);
assert.doesNotMatch(chronologyPage, /data-chronology-fullscreen-exit="true"/);
assert.doesNotMatch(chronologyPage, /chronologyFullscreenExitButton/);
assert.match(chronologyPage, /chronologyAlignButton chronologyFullscreenButton/);
assert.match(chronologyPage, /chronologyTimelineTab/);
assert.match(chronologyPage, /setIsTimelineCollapsed\(false\)/);
assert.match(chronologyCss, /\.chronologyTimelinePanel/);
assert.match(chronologyCss, /\.chronologyShell\.systemFullscreen\.timelineCollapsed \.chronologyToolbar/);
assert.match(chronologyCss, /\.chronologyShell\.systemFullscreen\.timelineCollapsed \.chronologyTimelineWrap/);
assert.match(chronologyCss, /\.chronologyTimelineTab/);
assert.doesNotMatch(chronologyCss, /\.chronologyFullscreenExitButton/);
assert.match(liveCss, /\.liveWorkspaceShell\.systemFullscreen\.sidebarOpen \.liveWorkspaceCameraPanel[\s\S]*background: rgba\(15, 23, 42/);
assert.match(chronologyCss, /\.chronologyShell\.systemFullscreen\.sidebarOpen \.chronologyCameraPanel[\s\S]*background: rgba\(15, 23, 42/);
assert.match(chronologyCss, /\.chronologyShell\.systemFullscreen \.chronologyTimelineWrap \.chronologyTimelineCard[\s\S]*background: rgba\(15, 23, 42/);
