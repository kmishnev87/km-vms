import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import {
  DEFAULT_VIDEO_ZOOM_STATE,
  VIDEO_ZOOM_MAX,
  clampPan,
  containedMediaRect,
  consumeTouchDoubleTapSuppressionToken,
  createTouchDoubleTapSuppressionToken,
  createVideoTouchGestureState,
  distanceBetweenPoints,
  isTouchDoubleTap,
  matchesTouchDoubleTapSuppressionToken,
  midpointBetweenPoints,
  panBy,
  touchDoubleTapZone,
  transitionVideoTouchGesture,
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
    midpointBetweenPoints(startA, startB),
    midpointBetweenPoints(endA, endB),
    VIDEO_ZOOM_MAX
  );
  assert.ok(zoomed.scale > 1);

  const zoomedOut = zoomFromPinch(
    zoomed,
    rect,
    300,
    1,
    { x: 400, y: 200 },
    { x: 400, y: 200 },
    VIDEO_ZOOM_MAX
  );
  assert.deepEqual(zoomedOut, DEFAULT_VIDEO_ZOOM_STATE);

  const translated = zoomFromPinch(
    { scale: 2, panX: 0, panY: 0 },
    { width: 800, height: 450 },
    200,
    200,
    { x: 400, y: 225 },
    { x: 450, y: 250 },
    VIDEO_ZOOM_MAX
  );
  assert.deepEqual(translated, { scale: 2, panX: 50, panY: 25 });
});

test("touch gesture lifecycle never inherits one-finger pan after pinch", () => {
  let state = createVideoTouchGestureState();
  let result = transitionVideoTouchGesture(state, {
    type: "down",
    pointerId: 1,
    point: { x: 100, y: 100 },
    time: 0,
  });
  state = result.state;
  assert.equal(state.mode, "pending");
  assert.equal(result.consumeEvent, false);

  result = transitionVideoTouchGesture(state, {
    type: "move",
    pointerId: 1,
    point: { x: 100, y: 140 },
    time: 80,
  });
  state = result.state;
  assert.equal(state.moved, true);
  assert.equal(result.consumeEvent, false);
  assert.equal(result.invalidatedTap, true);

  result = transitionVideoTouchGesture(state, {
    type: "up",
    pointerId: 1,
    point: { x: 100, y: 140 },
    time: 120,
  });
  assert.equal(result.completedTap, false);
  assert.equal(result.state.mode, "idle");

  state = transitionVideoTouchGesture(createVideoTouchGestureState(), {
    type: "down",
    pointerId: 11,
    point: { x: 200, y: 180 },
    time: 200,
  }).state;
  result = transitionVideoTouchGesture(state, {
    type: "down",
    pointerId: 12,
    point: { x: 400, y: 180 },
    time: 220,
  });
  state = result.state;
  assert.equal(result.startedPinch, true);
  assert.equal(state.mode, "pinch");

  result = transitionVideoTouchGesture(state, {
    type: "up",
    pointerId: 12,
    point: { x: 420, y: 180 },
    time: 260,
  });
  state = result.state;
  assert.equal(result.endedPinch, true);
  assert.equal(result.completedTap, false);
  assert.equal(state.mode, "consumed");

  result = transitionVideoTouchGesture(state, {
    type: "move",
    pointerId: 11,
    point: { x: 240, y: 180 },
    time: 280,
  });
  assert.equal(result.consumeEvent, true);
  assert.equal(result.completedTap, false);

  result = transitionVideoTouchGesture(result.state, {
    type: "up",
    pointerId: 11,
    point: { x: 240, y: 180 },
    time: 300,
  });
  assert.equal(result.state.mode, "idle");
  assert.equal(result.completedTap, false);
});

test("fresh one-finger pan is zoom-gated and a second pointer promotes touchPan to pinch", () => {
  let result = transitionVideoTouchGesture(createVideoTouchGestureState(), {
    type: "down",
    pointerId: 21,
    point: { x: 120, y: 100 },
    time: 0,
    panEnabled: true,
  });
  assert.equal(result.state.mode, "pending");
  assert.equal(result.consumeEvent, false);

  result = transitionVideoTouchGesture(result.state, {
    type: "move",
    pointerId: 21,
    point: { x: 150, y: 118 },
    time: 40,
  });
  assert.equal(result.state.mode, "touchPan");
  assert.equal(result.startedPan, true);
  assert.equal(result.invalidatedTap, true);
  assert.equal(result.consumeEvent, true);

  result = transitionVideoTouchGesture(result.state, {
    type: "down",
    pointerId: 22,
    point: { x: 330, y: 118 },
    time: 60,
    panEnabled: true,
  });
  assert.equal(result.state.mode, "pinch");
  assert.equal(result.startedPinch, true);
  assert.deepEqual(result.state.activePointerIds, [21, 22]);
  assert.equal(result.invalidatedTap, true);

  const beforePinch = { scale: 2, panX: 35, panY: -20 };
  assert.deepEqual(
    zoomFromPinch(
      beforePinch,
      { width: 800, height: 450 },
      180,
      180,
      { x: 240, y: 118 },
      { x: 240, y: 118 },
      VIDEO_ZOOM_MAX
    ),
    beforePinch
  );
});

test("moved or cancelled touch sequences invalidate the previous tap candidate", () => {
  const previousTap = { time: 100, point: { x: 100, y: 100 } };
  let result = transitionVideoTouchGesture(createVideoTouchGestureState(), {
    type: "down",
    pointerId: 31,
    point: { x: 100, y: 100 },
    time: 150,
  });
  result = transitionVideoTouchGesture(result.state, {
    type: "move",
    pointerId: 31,
    point: { x: 100, y: 145 },
    time: 190,
  });
  assert.equal(result.invalidatedTap, true);
  const afterMoveTapCandidate = result.invalidatedTap ? null : previousTap;
  assert.equal(
    isTouchDoubleTap(afterMoveTapCandidate, { time: 300, point: { x: 102, y: 101 } }),
    false
  );

  result = transitionVideoTouchGesture(createVideoTouchGestureState(), {
    type: "down",
    pointerId: 32,
    point: { x: 100, y: 100 },
    time: 400,
  });
  result = transitionVideoTouchGesture(result.state, {
    type: "cancel",
    pointerId: 32,
    point: { x: 100, y: 100 },
    time: 420,
  });
  assert.equal(result.invalidatedTap, true);
  assert.equal(result.state.mode, "idle");
});

test("touch compatibility double-click suppression requires one matching token and consumes it once", () => {
  const token = createTouchDoubleTapSuppressionToken({
    time: 1000,
    point: { x: 240, y: 160 },
    ownerKey: "records:camera-1",
  });

  const noToken = consumeTouchDoubleTapSuppressionToken(null, {
    time: 1500,
    point: { x: 250, y: 168 },
    ownerKey: "records:camera-1",
    touchGenerated: true,
  });
  assert.equal(noToken.consumed, false);
  assert.equal(noToken.nextToken, null);

  const mismatched = consumeTouchDoubleTapSuppressionToken(token, {
    time: 1500,
    point: { x: 250, y: 168 },
    ownerKey: "records:camera-2",
    touchGenerated: true,
  });
  assert.equal(mismatched.consumed, false);
  assert.equal(mismatched.nextToken, token);

  const matching = consumeTouchDoubleTapSuppressionToken(token, {
    time: 1500,
    point: { x: 250, y: 168 },
    ownerKey: "records:camera-1",
    touchGenerated: true,
  });
  assert.equal(matching.consumed, true);
  assert.equal(matching.nextToken, null);

  const repeatedCompatibilityEvent = consumeTouchDoubleTapSuppressionToken(matching.nextToken, {
    time: 1510,
    point: { x: 250, y: 168 },
    ownerKey: "records:camera-1",
    touchGenerated: true,
  });
  assert.equal(repeatedCompatibilityEvent.consumed, false);

  const nextMouseDoubleClick = consumeTouchDoubleTapSuppressionToken(repeatedCompatibilityEvent.nextToken, {
    time: 1600,
    point: { x: 250, y: 168 },
    ownerKey: "records:camera-1",
    pointerType: "mouse",
  });
  assert.equal(nextMouseDoubleClick.consumed, false);

  assert.equal(
    matchesTouchDoubleTapSuppressionToken(token, {
      time: 1500,
      point: { x: 250, y: 168 },
      ownerKey: "records:camera-1",
    }),
    true
  );
  assert.equal(
    matchesTouchDoubleTapSuppressionToken(token, {
      time: 1500,
      point: { x: 250, y: 168 },
      ownerKey: "records:camera-2",
    }),
    false
  );
  assert.equal(
    matchesTouchDoubleTapSuppressionToken(token, {
      time: 2000,
      point: { x: 250, y: 168 },
      ownerKey: "records:camera-1",
    }),
    false
  );
});

test("touch double-tap zones and matching boundaries are deterministic", () => {
  const rect = { width: 1000, height: 600 };
  assert.equal(touchDoubleTapZone({ x: 349, y: 10 }, rect), "left");
  assert.equal(touchDoubleTapZone({ x: 350, y: 10 }, rect), "center");
  assert.equal(touchDoubleTapZone({ x: 649, y: 10 }, rect), "center");
  assert.equal(touchDoubleTapZone({ x: 650, y: 10 }, rect), "right");
  assert.equal(
    isTouchDoubleTap(
      { time: 1000, point: { x: 100, y: 100 } },
      { time: 1300, point: { x: 110, y: 105 } }
    ),
    true
  );
  assert.equal(
    isTouchDoubleTap(
      { time: 1000, point: { x: 100, y: 100 } },
      { time: 1500, point: { x: 110, y: 105 } }
    ),
    false
  );
});

test("touch double-tap zones use the visible contained media, not letterbox space", () => {
  const visible = containedMediaRect({ width: 1000, height: 500 }, 800, 600);
  assert.equal(Math.round(visible.left), 167);
  assert.equal(Math.round(visible.width), 667);
  assert.equal(touchDoubleTapZone({ x: 399, y: 250 }, visible), "left");
  assert.equal(touchDoubleTapZone({ x: 400, y: 250 }, visible), "center");
  assert.equal(touchDoubleTapZone({ x: 599, y: 250 }, visible), "center");
  assert.equal(touchDoubleTapZone({ x: 600, y: 250 }, visible), "right");
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
  assert.equal(recordsPage.includes("onDoubleClickCapture={toggleViewerFrameFullscreen}"), false);
  assert.equal(recordsPage.includes("fullscreenElement === video"), false);
  assert.equal(recordsPage.includes("onDesktopDoubleClick={toggleViewerFrameFullscreen}"), true);
  assert.equal(recordsPage.includes("onTouchDoubleTap={handleViewerTouchDoubleTap}"), true);
  assert.equal(recordsPage.includes("seekViewerBySeconds(-10)"), true);
  assert.equal(recordsPage.includes("seekViewerBySeconds(10)"), true);
  assert.equal(recordsPage.includes("video.currentTime = target"), true);
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
  assert.equal(surface.includes("transitionVideoTouchGesture"), true);
  assert.equal(surface.includes("data-video-touch-owner"), true);
  assert.equal(surface.includes("touchDoubleTapSuppressionRef"), true);
  assert.equal(surface.includes("ownedSecondTouchTapRef"), true);
  assert.match(surface, /isTouchDoubleTap\(lastTouchTapRef\.current,[\s\S]*?stopHandledGesture\(event\)/);
  assert.equal(surface.includes("visibleMediaRect(element)"), true);
  assert.equal(surface.includes("consumeTouchDoubleTapSuppressionToken"), true);
  assert.match(surface, /if \(!suppression\.consumed\) return false;/);
  assert.doesNotMatch(surface, /if \(!touchGenerated && !tokenMatches\) return false;/);
  assert.equal(surface.includes("stopImmediatePropagation"), true);
  assert.equal(surface.includes("mousePan"), true);
  assert.equal(surface.includes("fullscreenMedia"), true);
  assert.equal(surface.includes("fullscreenMedia.style.transform = transform"), true);
  assert.equal(surface.includes("data-video-zoom-surface"), true);
  assert.equal(css.includes(".videoZoomPanIndicator"), true);
  assert.match(css, /\.videoZoomPanSurface\.isZoomed \{[\s\S]*?touch-action: none;/);
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
  assert.equal(livePage.includes("onTouchDoubleTap={() => toggleTileFullscreen(tile.id)}"), true);
  assert.equal(chronologyPage.includes("handleTileVideoTouchDoubleTap(tile.id, zone)"), true);
  assert.equal(recordsPage.includes("PAGE_SIZE_OPTIONS = [15, 30, 50, 100]"), true);
  assert.equal(recordsPage.includes("recordsPageScopedVerifiedLoad"), false);
  assert.equal(recordsPage.includes("recordingsViewerBackdrop"), true);
  assert.equal(recordsPage.includes('body.style.position = "fixed"'), true);
  assert.equal(recordsPage.includes("window.scrollTo(scrollX, scrollY)"), true);

  for (const source of [livePlayer, archivePlayer]) {
    assert.equal(source.includes("CompactVideoCanvas"), true);
    assert.equal(source.includes("nativeVideoSuppressed"), true);
    assert.equal(source.includes("data-canvas-ready"), true);
    assert.equal(source.includes("data-first-frame-drawn"), true);
    assert.equal(source.includes("onZoomActiveChange={setZoomActive}"), true);
    assert.equal(source.includes("!zoomActive && renderState.renderer === \"canvas\""), true);
  }
  assert.equal(canvas.includes("requestVideoFrameCallback"), true);
});

test("mobile Live and Chronology sidebars share a 32px scroll gutter and header edge", () => {
  const liveCss = read("app/styles/90-live-workspace.css");
  const chronologyCss = read("app/styles/100-chronology-workspace.css");
  assert.match(liveCss, /--live-camera-scroll-gutter:\s*32px/);
  assert.match(liveCss, /\.liveWorkspacePanelHeader,\s*\n\s*\.liveWorkspaceCameraList/);
  assert.match(chronologyCss, /--chronology-camera-scroll-gutter:\s*32px/);
  assert.match(chronologyCss, /\.chronologyPanelHeader,\s*\n\s*\.chronologyCameraList/);
});
