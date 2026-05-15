import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import {
  compactRenderTierForRatio,
  isCompactSmoothingSourceVideo,
  planCompactVideoDownscale,
  selectCompactVideoRenderMode,
} from "../lib/playbackResolution.js";

const root = path.resolve(import.meta.dirname, "..");

function read(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("compact renderer uses progressive ratio tiers instead of old 0.50 cliff", () => {
  assert.equal(compactRenderTierForRatio(0.76).name, "normal");
  assert.equal(compactRenderTierForRatio(0.70).name, "ultra-soft");
  assert.equal(compactRenderTierForRatio(0.60).name, "soft");
  assert.equal(compactRenderTierForRatio(0.50).name, "medium-soft");
  assert.equal(compactRenderTierForRatio(0.40).name, "medium");
  assert.equal(compactRenderTierForRatio(0.30).name, "strong");
  assert.equal(compactRenderTierForRatio(0.20).name, "stronger");
  assert.equal(compactRenderTierForRatio(0.10).name, "strongest");
});

test("large and fullscreen views stay native while compact high-res uses canvas", () => {
  assert.equal(
    selectCompactVideoRenderMode({
      dimensions: { width: 3840, height: 2160 },
      rect: { width: 3200, height: 1800 },
    }).renderer,
    "native"
  );
  assert.equal(
    selectCompactVideoRenderMode({
      dimensions: { width: 3840, height: 2160 },
      rect: { width: 900, height: 500 },
    }).renderer,
    "canvas"
  );
  assert.equal(
    selectCompactVideoRenderMode({
      dimensions: { width: 3840, height: 2160 },
      rect: { width: 900, height: 500 },
      isFullscreen: true,
    }).renderer,
    "native"
  );
});

test("low-resolution substream is not over-treated by dimensions alone", () => {
  assert.equal(isCompactSmoothingSourceVideo({ width: 1280, height: 720 }), false);
  assert.equal(
    selectCompactVideoRenderMode({
      dimensions: { width: 1280, height: 720 },
      rect: { width: 320, height: 180 },
      sourceHighResolution: false,
    }).renderer,
    "native"
  );
});

test("high-resolution substreams are eligible for compact smoothing by dimensions", () => {
  assert.equal(isCompactSmoothingSourceVideo({ width: 2048, height: 928 }), true);
  assert.equal(isCompactSmoothingSourceVideo({ width: 1920, height: 1080 }), true);

  assert.equal(
    selectCompactVideoRenderMode({
      dimensions: { width: 2048, height: 928 },
      rect: { width: 512, height: 232 },
      sourceHighResolution: false,
    }).renderer,
    "canvas"
  );
  assert.equal(
    selectCompactVideoRenderMode({
      dimensions: { width: 1920, height: 1080 },
      rect: { width: 480, height: 270 },
      sourceHighResolution: false,
    }).renderer,
    "canvas"
  );
});

test("compact downscale plan keeps aspect ratio and uses multipass for strong reduction", () => {
  const plan = planCompactVideoDownscale({
    sourceWidth: 3840,
    sourceHeight: 2160,
    cssWidth: 480,
    cssHeight: 270,
    mode: "compact-strong",
    ratio: 0.125,
    backingScale: 1,
    devicePixelRatio: 2,
  });
  assert.equal(plan.renderer, "canvas");
  assert.equal(plan.target.width, 480);
  assert.equal(plan.target.height, 270);
  assert.equal(plan.qualityPath, "multipass-downscale");
  assert.ok(plan.passCount > 1);
});

test("V4 renderer lifecycle forbids V3 premature native suppression", () => {
  const tile = read("components/TilePlayer.js");
  const archive = read("components/ArchiveTilePlayer.js");
  const canvas = read("components/CompactVideoCanvas.js");

  assert.equal(tile.includes("isCompactSmoothingSourceVideo(naturalResolution)"), true);
  assert.equal(tile.includes("sourceHighResolution: stream === \"main\""), false);

  for (const source of [tile, archive]) {
    assert.equal(source.includes("compactCanvasActive = renderState.renderer === \"canvas\" && readyState >= 2"), false);
    assert.equal(source.includes("nativeVideoSuppressed ="), true);
    assert.equal(source.includes("canvasFrame.ready"), true);
    assert.equal(source.includes("canvasFrame.generation === canvasGeneration"), true);
    assert.equal(source.includes("data-canvas-ready"), true);
    assert.equal(source.includes("data-first-frame-drawn"), true);
  }

  assert.equal(canvas.includes("requestVideoFrameCallback"), true);
  assert.equal(canvas.includes("getContext(\"2d\", { alpha: false })"), true);
  assert.equal(canvas.includes("onFrameState"), true);
  assert.equal(canvas.includes("draw-exception"), true);
});

test("forbidden backend preview/transcode markers are absent from touched web files", () => {
  const files = [
    "components/TilePlayer.js",
    "components/ArchiveTilePlayer.js",
    "components/CompactVideoCanvas.js",
    "lib/playbackResolution.js",
    "app/recordings/page.js",
  ];
  const combined = files.map(read).join("\n");
  assert.equal(combined.includes("ARCHIVE_PREVIEW_WINDOW_SECONDS"), false);
  assert.equal(combined.includes("previewWindowDurationSec"), false);
  assert.equal(combined.includes("recordings/preview"), false);
  assert.equal(combined.includes("chronology/preview"), false);
  assert.equal(combined.includes("ffmpeg"), false);
});

test("records page remains native and does not introduce canvas playback", () => {
  const records = read("app/recordings/page.js");
  assert.equal(records.includes("data-render-context=\"records\""), true);
  assert.equal(records.includes("data-renderer=\"native\""), true);
  assert.equal(records.includes("CompactVideoCanvas"), false);
  assert.equal(records.includes("issueRecordingMediaToken(item.path, \"stream\")"), true);
});
