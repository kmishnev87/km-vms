import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import {
  DEFAULT_VIDEO_ZOOM_STATE,
  VIDEO_ZOOM_MAX,
  clampPan,
  distanceBetweenPoints,
  midpointBetweenPoints,
  panBy,
  zoomAtPoint,
  zoomFromPinch,
} from "../lib/videoZoomPanCore.js";

const root = path.resolve(import.meta.dirname, "..");

function read(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("wheel zoom math is bounded and keeps cursor focal point stable", () => {
  const rect = { width: 800, height: 450 };
  const focal = { x: 600, y: 260 };
  const zoomed = zoomAtPoint(DEFAULT_VIDEO_ZOOM_STATE, rect, focal, 2, VIDEO_ZOOM_MAX);

  assert.equal(zoomed.scale, 2);
  const beforeX = (focal.x - rect.width / 2 - DEFAULT_VIDEO_ZOOM_STATE.panX) / DEFAULT_VIDEO_ZOOM_STATE.scale;
  const afterX = (focal.x - rect.width / 2 - zoomed.panX) / zoomed.scale;
  assert.equal(Math.round(afterX), Math.round(beforeX));

  const maxed = zoomAtPoint(zoomed, rect, focal, 99, VIDEO_ZOOM_MAX);
  assert.equal(maxed.scale, VIDEO_ZOOM_MAX);

  const reset = zoomAtPoint(zoomed, rect, focal, 0.01, VIDEO_ZOOM_MAX);
  assert.deepEqual(reset, DEFAULT_VIDEO_ZOOM_STATE);
});

test("pan clamps and invalid numeric input cannot create unsafe state", () => {
  const rect = { width: 640, height: 360 };
  const zoomed = { scale: 3, panX: 0, panY: 0 };
  assert.deepEqual(panBy(zoomed, rect, 9999, -9999), {
    scale: 3,
    panX: 640,
    panY: -360,
  });
  assert.deepEqual(clampPan({ scale: Number.NaN, panX: Infinity, panY: -Infinity }, rect), DEFAULT_VIDEO_ZOOM_STATE);
});

test("pinch math distinguishes distance, midpoint and clamped pan", () => {
  const rect = { width: 900, height: 500 };
  const startA = { x: 300, y: 200 };
  const startB = { x: 500, y: 200 };
  const endA = { x: 250, y: 180 };
  const endB = { x: 550, y: 220 };

  assert.equal(distanceBetweenPoints(startA, startB), 200);
  assert.deepEqual(midpointBetweenPoints(endA, endB), { x: 400, y: 200 });

  const zoomed = zoomFromPinch(
    DEFAULT_VIDEO_ZOOM_STATE,
    rect,
    distanceBetweenPoints(startA, startB),
    distanceBetweenPoints(endA, endB),
    midpointBetweenPoints(endA, endB),
    VIDEO_ZOOM_MAX
  );
  assert.ok(zoomed.scale > 1);

  const zoomedOut = zoomFromPinch(zoomed, rect, 300, 1, { x: 400, y: 200 }, VIDEO_ZOOM_MAX);
  assert.deepEqual(zoomedOut, DEFAULT_VIDEO_ZOOM_STATE);
});

test("Live, Chronology and Records use the shared zoom surface without permanent zoom controls", () => {
  const livePlayer = read("components/TilePlayer.js");
  const archivePlayer = read("components/ArchiveTilePlayer.js");
  const recordsPage = read("app/recordings/page.js");
  const surface = read("components/VideoZoomPanSurface.js");
  const css = read("app/styles/50-cameras-shared-modals.css");

  for (const source of [livePlayer, archivePlayer, recordsPage]) {
    assert.equal(source.includes("VideoZoomPanSurface"), true);
    assert.doesNotMatch(source, /zoom(In|Out|Reset)|resetZoom|ZoomButton|zoomToolbar|zoomControls/);
  }

  assert.equal(livePlayer.includes("context=\"live\""), true);
  assert.equal(livePlayer.includes("sourceKey={`${cameraId || \"\"}:${stream || \"\"}`}"), true);
  assert.equal(livePlayer.includes("sourceKey={`${cameraId || \"\"}:${stream || \"\"}:${canvasGeneration}`}"), false);
  assert.equal(archivePlayer.includes("context=\"chronology\""), true);
  assert.equal(archivePlayer.includes("sourceKey={playback?.playbackKey || \"empty\"}"), true);
  assert.equal(archivePlayer.includes("sourceKey={`${playback?.playbackKey || \"empty\"}:${canvasGeneration}`}"), false);
  assert.equal(recordsPage.includes("context=\"records\""), true);
  assert.equal(recordsPage.includes("frame.requestFullscreen"), true);
  assert.equal(recordsPage.includes("onDoubleClickCapture={toggleViewerFrameFullscreen}"), true);
  assert.equal(recordsPage.includes("fullscreenElement === video"), true);
  assert.equal(recordsPage.includes("viewerFullscreenPromoteRef"), false);
  assert.equal(recordsPage.includes("controlsList=\"nofullscreen\""), true);
  assert.equal(recordsPage.includes("playsInline"), true);
  assert.equal(recordsPage.includes("data-recording-frame-fullscreen-button=\"true\""), false);
  assert.equal(recordsPage.includes("t.enterFullscreen"), false);
  assert.equal(css.includes(".recordingViewerFullscreenButton"), false);
  assert.equal(css.includes(".recordingVideoFrame:fullscreen .recordingVideoZoomSurface"), true);
  assert.equal(css.includes(".recordingVideoFrame:fullscreen .videoZoomPanContent"), true);
  assert.equal(css.includes(".recordingVideo::-webkit-media-controls-fullscreen-button"), true);
  assert.equal(surface.includes("addEventListener?.(\"wheel\""), true);
  assert.equal(surface.includes("passive: false"), true);
  assert.equal(surface.includes("capture: true"), true);
  assert.equal(surface.includes("touchPinch"), true);
  assert.equal(surface.includes("touchPan"), true);
  assert.equal(surface.includes("mousePan"), true);
  assert.equal(surface.includes("fullscreenMedia"), true);
  assert.equal(surface.includes("fullscreenMedia.style.transform = transform"), true);
  assert.equal(surface.includes("data-video-zoom-surface"), true);
  assert.equal(css.includes(".videoZoomPanIndicator"), true);
  assert.doesNotMatch(css, /\.videoZoomPan.*button/i);
});

test("closed-stage interaction markers remain protected", () => {
  const livePage = read("app/live/page.js");
  const chronologyPage = read("app/chronology/page.js");
  const recordsPage = read("app/recordings/page.js");
  const livePlayer = read("components/TilePlayer.js");
  const archivePlayer = read("components/ArchiveTilePlayer.js");
  const canvas = read("components/CompactVideoCanvas.js");

  assert.equal(livePage.includes("data-live-audio-button=\"true\""), true);
  assert.match(livePage, /onDoubleClick=\{\(event\) => \{[\s\S]*?event\.stopPropagation\(\);[\s\S]*?toggleTileFullscreen\(tile\.id\);/);
  assert.match(chronologyPage, /onDoubleClick=\{async \(event\) => \{[\s\S]*?event\.stopPropagation\(\);[\s\S]*?await toggleTileFullscreen\(tile\.id\);/);
  assert.equal(recordsPage.includes("PAGE_SIZE_OPTIONS = [15, 30, 50, 100]"), true);
  assert.equal(recordsPage.includes("recordsPageScopedVerifiedLoad"), false);

  for (const source of [livePlayer, archivePlayer]) {
    assert.equal(source.includes("CompactVideoCanvas"), true);
    assert.equal(source.includes("nativeVideoSuppressed"), true);
    assert.equal(source.includes("data-canvas-ready"), true);
    assert.equal(source.includes("data-first-frame-drawn"), true);
  }
  assert.equal(canvas.includes("requestVideoFrameCallback"), true);
});
