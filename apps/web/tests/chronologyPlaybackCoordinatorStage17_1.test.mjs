import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const root = path.resolve(import.meta.dirname, "..");

function read(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("Chronology owns a bounded playback coordinator for seek and startup", () => {
  const page = read("app/chronology/page.js");

  assert.equal(page.includes("PLAYBACK_COORDINATOR_SOFT_TIMEOUT_MS = 3500"), true);
  assert.equal(page.includes("PLAYBACK_COORDINATOR_HARD_TIMEOUT_MS = 7000"), true);
  assert.equal(page.includes("startPlaybackCoordinator"), true);
  assert.equal(page.includes("handleTilePlaybackState"), true);
  assert.equal(page.includes("coordinatorBarrierComplete"), true);
  assert.equal(page.includes("releasePlaybackCoordinator"), true);
  assert.equal(page.includes("markCoordinatorTimeout"), true);
});

test("Chronology seek starts coordinator only after playback metadata is applied", () => {
  const page = read("app/chronology/page.js");

  assert.equal(page.includes("const result = await resolvePlaybackForTimestamp(targetTs, false);"), true);
  assert.equal(page.includes("startPlaybackCoordinator({ targetTs, shouldResume: action.shouldResume, expectedTileIds })"), true);
  assert.equal(page.includes("resolvePlaybackForTimestamp(targetTs, true)"), false);
  assert.equal(page.includes("setIsPlaying(true);\n    }\n  }\n\n  async function handleTimelineSelect"), false);
});

test("ArchiveTilePlayer prepares same source at target instead of rebuilding source", () => {
  const archive = read("components/ArchiveTilePlayer.js");

  assert.equal(archive.includes("coordination = null"), true);
  assert.equal(archive.includes("onTilePlaybackState"), true);
  assert.equal(archive.includes("same-source-seek"), true);
  assert.equal(archive.includes("operation-waiting-for-source"), true);
  assert.equal(archive.includes("prepareVideoAtTarget"), true);
  assert.equal(archive.includes("reportTileState(\"ready\""), true);
  assert.equal(archive.includes("coordination?.releaseState === \"holding\""), true);
  assert.equal(archive.includes("data-playback-release-state"), true);
  assert.equal(archive.includes("data-playback-operation"), true);
});

test("Stage 17 compact smoothing preservation markers remain present", () => {
  const archive = read("components/ArchiveTilePlayer.js");
  const live = read("components/TilePlayer.js");
  const helper = read("lib/playbackResolution.js");

  assert.equal(archive.includes("CompactVideoCanvas"), true);
  assert.equal(archive.includes("nativeVideoSuppressed"), true);
  assert.equal(live.includes("isCompactSmoothingSourceVideo(naturalResolution)"), true);
  assert.equal(helper.includes("isCompactSmoothingSourceVideo"), true);
  assert.equal(helper.includes("COMPACT_SMOOTHING_SOURCE_THRESHOLD"), true);
});
