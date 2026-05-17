import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const livePage = read("app/live/page.js");
const chronologyPage = read("app/chronology/page.js");
const timeline = read("components/ChronologyTimeline.js");
const liveCss = read("app/styles/90-live-workspace.css");
const chronologyCss = read("app/styles/100-chronology-workspace.css");
const timelineCss = read("app/styles/80-chronology-timeline.css");

for (const source of [livePage, chronologyPage, liveCss, chronologyCss]) {
  assert.doesNotMatch(source, /SidebarAddButton/);
  assert.doesNotMatch(source, /tablet-add-camera/);
  assert.doesNotMatch(source, /tablet-add-stream/);
  assert.doesNotMatch(source, /data-live-tile-fullscreen-button/);
  assert.doesNotMatch(source, /data-chronology-tile-fullscreen-button/);
}

assert.match(livePage, /function addTileFromSidebar\(cameraId, stream\)/);
assert.match(livePage, /data-touch-add-path="double-tap-card"/);
assert.match(livePage, /onDoubleClick=\{\(event\) => \{\s*if \(event\.target\?\.closest\?\.\("button, select, input, textarea, a"\)\) return;\s*event\.preventDefault\(\);\s*addTileFromSidebar\(camera\.id, initialStream\);/s);
assert.match(livePage, /function startSidebarPointerDrag\(event, camera, stream\)/);
assert.match(livePage, /function updateSidebarPointerDrag\(event\)/);
assert.match(livePage, /function finishSidebarPointerDrag\(event, cameraId, stream\)/);
assert.match(livePage, /if \(event\.pointerType !== "mouse"\) event\.preventDefault\(\)/);
assert.match(livePage, /setSidebarDragPreview\(\{ cameraId: state\.cameraId, label: state\.label, meta: state\.meta, x: event\.clientX, y: event\.clientY, mode: "workspace" \}\)/);
assert.match(livePage, /className=\{`liveWorkspaceSidebarDragGhost \$\{sidebarDragPreview\.mode\}`\}/);
assert.match(livePage, /isPointInsideWorkspace\(event\.clientX, event\.clientY\)[\s\S]*addTile\(state\.cameraId, state\.stream, event\.clientX, event\.clientY\)/);
assert.match(livePage, /reorderSidebarCamera\(rowCameraId, sidebarDropPositionFromPoint\(row, event\.clientY\), state\.cameraId\)/);
assert.match(livePage, /reorderSidebarCamera\(camera\.id, sidebarDropPosition\(event\), reorderId\)/);
assert.match(livePage, /event\.dataTransfer\.setData\(LIVE_CAMERA_DROP_MIME, String\(camera\.id\)\)/);
assert.match(livePage, /event\.dataTransfer\.setData\(LIVE_CAMERA_STREAM_DROP_MIME, stream\.key\)/);
assert.match(livePage, /event\.dataTransfer\.setData\(SIDEBAR_CAMERA_REORDER_MIME, String\(camera\.id\)\)/);

assert.match(chronologyPage, /function addTileFromSidebar\(cameraId\)/);
assert.match(chronologyPage, /data-touch-add-path="double-tap-card"/);
assert.match(chronologyPage, /onDoubleClick=\{\(event\) => \{\s*if \(event\.target\?\.closest\?\.\("button, select, input, textarea, a"\)\) return;\s*event\.preventDefault\(\);\s*addTileFromSidebar\(camera\.id\);/s);
assert.match(chronologyPage, /function startSidebarPointerDrag\(event, camera\)/);
assert.match(chronologyPage, /function updateSidebarPointerDrag\(event\)/);
assert.match(chronologyPage, /function finishSidebarPointerDrag\(event, cameraId\)/);
assert.match(chronologyPage, /if \(event\.pointerType !== "mouse"\) event\.preventDefault\(\)/);
assert.match(chronologyPage, /setSidebarDragPreview\(\{ cameraId: state\.cameraId, label: state\.label, meta: state\.meta, x: event\.clientX, y: event\.clientY, mode: "workspace" \}\)/);
assert.match(chronologyPage, /className=\{`chronologySidebarDragGhost \$\{sidebarDragPreview\.mode\}`\}/);
assert.match(chronologyPage, /isPointInsideWorkspace\(event\.clientX, event\.clientY\)[\s\S]*addTile\(state\.cameraId, event\.clientX, event\.clientY\)/);
assert.match(chronologyPage, /reorderSidebarCamera\(rowCameraId, sidebarDropPositionFromPoint\(row, event\.clientY\), state\.cameraId\)/);
assert.match(chronologyPage, /reorderSidebarCamera\(camera\.id, sidebarDropPosition\(event\), reorderId\)/);
assert.match(chronologyPage, /event\.dataTransfer\.setData\(CHRONOLOGY_CAMERA_DROP_MIME, String\(camera\.id\)\)/);
assert.match(chronologyPage, /event\.dataTransfer\.setData\(SIDEBAR_CAMERA_REORDER_MIME, String\(camera\.id\)\)/);

for (const source of [livePage, chronologyPage]) {
  assert.match(source, /setPointerCapture\?\.\(event\.pointerId\)/);
  assert.match(source, /releasePointerCapture\?\.\(event\.pointerId\)/);
  assert.match(source, /window\.addEventListener\("pointermove"/);
  assert.match(source, /window\.addEventListener\("pointerup", onUp, \{ once: true \}\)/);
  assert.match(source, /window\.addEventListener\("pointercancel", onUp, \{ once: true \}\)/);
  assert.match(source, /data-sidebar-pointer-drag-mode/);
  assert.match(source, /data-sidebar-reorder-drop-position/);
  assert.match(source, /onPointerDown=\{\(event\) => event\.stopPropagation\(\)\}/);
}

assert.match(livePage, /data-live-audio-button="true"/);
assert.match(livePage, /function toggleTileFullscreen\(tileId\)/);
assert.match(livePage, /data-live-tile-video-id=\{tile\.id\}/);
assert.match(livePage, /const tileFullscreenReturnStateRef = useRef\(null\)/);
assert.match(livePage, /const returnState = \{ isSystemFullscreen, isSidebarCollapsed \};\s*tileFullscreenReturnStateRef\.current = returnState;\s*if \(!returnState\.isSystemFullscreen\) setIsSystemFullscreen\(false\);\s*setFullscreenTileId\(tileId\);/s);
assert.match(livePage, /if \(!tileFullscreenReturnStateRef\.current\?\.isSystemFullscreen\) setIsSystemFullscreen\(false\);\s*setFullscreenTileId\(tileId\);/s);
assert.match(livePage, /event\.key === "Escape" && isSystemFullscreen && !fullscreenTileId/);
assert.match(livePage, /const returnState = tileFullscreenReturnStateRef\.current;\s*tileFullscreenReturnStateRef\.current = null;\s*setIsSystemFullscreen\(Boolean\(returnState\?\.isSystemFullscreen\)\);\s*setIsSidebarCollapsed\(returnState\?\.isSystemFullscreen \? returnState\.isSidebarCollapsed : false\);\s*setFullscreenTileId\(null\);/s);
assert.match(livePage, /if \(event\.target\?\.closest\?\.\("button, select, input, textarea, a"\)\) return;\s*if \(dragState \|\| resizeState\) return;\s*if \(event\.pointerType === "mouse" \|\| event\.pointerType === "touch"\) return;\s*event\.stopPropagation\(\);/s);
assert.match(livePage, /if \(event\.pointerType === "mouse" && event\.buttons === 0\) \{\s*setDragState\(null\);\s*return;\s*\}/s);
assert.match(livePage, /onPointerUpCapture=\{\(event\) => handleTileSurfacePointerUp\(event, tile\.id\)\}/);
assert.match(livePage, /onTouchEndCapture=\{\(event\) => handleTileSurfaceTouchEnd\(event, tile\.id\)\}/);
assert.match(livePage, /function handleTileSurfaceTouchEnd\(event, tileId\)/);
assert.match(livePage, /onDoubleClickCapture=\{\(event\) => \{/);
assert.match(livePage, /onPointerUp=\{\(event\) => handleTileSurfacePointerUp\(event, tile\.id\)\}/);
assert.match(livePage, /onDoubleClick=\{\(event\) => \{\s*event\.stopPropagation\(\);\s*toggleTileFullscreen\(tile\.id\);/s);
assert.match(livePage, /requestFullscreen\?\.\(\)/);
assert.match(livePage, /event\.target\?\.closest\?\.\("\[data-live-tile-video-id\], button, select, input, textarea, a"\)/);

assert.match(chronologyPage, /function toggleTileFullscreen\(tileId\)/);
assert.match(chronologyPage, /data-chronology-tile-video-id=\{tile\.id\}/);
assert.match(chronologyPage, /const tileFullscreenReturnStateRef = useRef\(null\)/);
assert.match(chronologyPage, /const returnState = \{ isSystemFullscreen, isSidebarCollapsed \};\s*tileFullscreenReturnStateRef\.current = returnState;\s*if \(!returnState\.isSystemFullscreen\) setIsSystemFullscreen\(false\);\s*setFullscreenTileId\(tileId\);/s);
assert.match(chronologyPage, /if \(!tileFullscreenReturnStateRef\.current\?\.isSystemFullscreen\) setIsSystemFullscreen\(false\);\s*setFullscreenTileId\(tileId\);/s);
assert.match(chronologyPage, /if \(!fullscreenTileId\) \{\s*setIsSystemFullscreen\(false\);\s*setIsTimelineCollapsed\(false\);\s*\}/s);
assert.match(chronologyPage, /if \(returnState\?\.isSystemFullscreen\) \{\s*setIsSystemFullscreen\(true\);\s*setIsSidebarCollapsed\(returnState\.isSidebarCollapsed\);\s*\} else \{\s*setIsSystemFullscreen\(false\);\s*setIsTimelineCollapsed\(false\);\s*\}/s);
assert.match(chronologyPage, /if \(event\.target\?\.closest\?\.\("button, select, input, textarea, a"\)\) return;\s*if \(dragState \|\| resizeState\) return;\s*if \(event\.pointerType === "mouse" \|\| event\.pointerType === "touch"\) return;\s*event\.stopPropagation\(\);/s);
assert.match(chronologyPage, /if \(event\.pointerType === "mouse" && event\.buttons === 0\) \{\s*setDragState\(null\);\s*return;\s*\}/s);
assert.match(chronologyPage, /onPointerUpCapture=\{\(event\) => handleTileSurfacePointerUp\(event, tile\.id\)\}/);
assert.match(chronologyPage, /onTouchEndCapture=\{\(event\) => handleTileSurfaceTouchEnd\(event, tile\.id\)\}/);
assert.match(chronologyPage, /function handleTileSurfaceTouchEnd\(event, tileId\)/);
assert.match(chronologyPage, /onDoubleClickCapture=\{async \(event\) => \{/);
assert.match(chronologyPage, /onPointerUp=\{\(event\) => handleTileSurfacePointerUp\(event, tile\.id\)\}/);
assert.match(chronologyPage, /onDoubleClick=\{async \(event\) => \{\s*event\.stopPropagation\(\);\s*await toggleTileFullscreen\(tile\.id\);/s);
assert.match(chronologyPage, /requestFullscreen\?\.\(\)/);
assert.match(chronologyPage, /event\.target\?\.closest\?\.\("\[data-chronology-tile-video-id\], button, select, input, textarea, a"\)/);

assert.match(timeline, /data-chronology-timeline-pointer="true"/);
assert.match(timeline, /onPointerDown=\{handlePointerDown\}/);
assert.match(timeline, /onPointerMove=\{handlePointerMove\}/);
assert.match(timeline, /onPointerUp=\{\(event\) => finishPointerInteraction\(event\)\}/);
assert.match(timeline, /onPointerCancel=\{\(event\) => finishPointerInteraction\(event, true\)\}/);
assert.doesNotMatch(timeline, /addEventListener\("mousemove"/);
assert.doesNotMatch(timeline, /addEventListener\("mouseup"/);
assert.doesNotMatch(timeline, /onMouseDown=/);

assert.match(liveCss, /\.liveWorkspaceCameraItem\[data-sidebar-pointer-drag-mode="workspace"\]/);
assert.match(liveCss, /\.liveWorkspaceCameraItem[\s\S]*touch-action: none/);
assert.match(liveCss, /\.liveWorkspaceSidebarDragGhost[\s\S]*position: fixed/);
assert.match(liveCss, /\.liveWorkspaceTileVideo:fullscreen/);
assert.match(liveCss, /touch-action: none/);
assert.match(liveCss, /touch-action: manipulation/);
assert.match(chronologyCss, /\.chronologyCameraItem\[data-sidebar-pointer-drag-mode="workspace"\]/);
assert.match(chronologyCss, /\.chronologyCameraItem[\s\S]*touch-action: none/);
assert.match(chronologyCss, /\.chronologySidebarDragGhost[\s\S]*position: fixed/);
assert.match(chronologyCss, /\.chronologyShell\.systemFullscreen \.chronologyTimelineWrap \.chronologyTimelineBody[\s\S]*background: linear-gradient/);
assert.match(chronologyCss, /touch-action: none/);
assert.match(timelineCss, /\.chronologyTimelineBody[\s\S]*touch-action: pan-y/);
