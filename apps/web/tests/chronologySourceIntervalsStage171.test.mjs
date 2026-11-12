import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import { createCoalescedPlaybackResolver } from "../lib/coalescedPlaybackResolver.js";
import { reconcilePlaybackMembership } from "../lib/workspaceContinuity.js";
import * as archiveGroupPlayback from "../lib/archiveGroupPlayback.js";

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const plain = source => source.replace(/^import\b[\s\S]*?;\r?\n/gm, "").replaceAll("export default ", "").replaceAll("export ", "");
const baseline = read("tests/stage14FinalArchiveOwnershipRecovery.test.mjs"), noop = () => {};
const utility = { assert, noop, vm };
vm.runInNewContext(baseline.slice(baseline.indexOf("function hooks("), baseline.indexOf("\nconst exportSource=")) + "\nthis.hooks=hooks;", utility);
const hooks = utility.hooks;
const turn = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
const startMs = Date.UTC(2026, 10, 12, 17);
const response = (offset = 10, id = 1) => ({ has_video: true, rel_path: `synthetic-${id}.mkv`, segment_id: id,
  archive_root_id: "root", offset_sec: offset, duration_sec: 60, file_start: "2026-11-12T12:00:00", file_end: "2026-11-12T12:01:00" });

function sources() {
  const requests = [];
  const ctx = { ...archiveGroupPlayback, AbortController, setTimeout, clearTimeout, createCoalescedPlaybackResolver, reconcilePlaybackMembership,
    apiFetch(path, options) { return new Promise((resolve, reject) => {
      requests.push({ path, signal: options.signal, resolve, reject });
      options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }); } };
  let props = { tilesRef: { current: [{ id: "tile", cameraId: "1" }] }, tileSourceKey: "tile:1", currentTs: new Date(startMs + 10000),
    isPlaying: false, isScrubbingRef: { current: false }, formatProductTimestampParam: date => date.toISOString() };
  const h = hooks(plain(read("lib/useChronologySources.js")), ctx, "useChronologySources", props);
  return { h, requests, update(patch) { props = { ...props, ...patch }; h.flush(props); } };
}

test("same-segment playback and seeks preserve source identity without per-second lookups", async () => {
  const a = sources(); a.requests[0].resolve(response()); await turn(); a.h.flush();
  const key = a.h.result.playbackMap.tile.playbackKey;
  a.update({ isPlaying: true }); await turn(); a.h.flush();
  for (let i = 11; i < 20; i++) { a.update({ currentTs: new Date(startMs + i * 1000) }); await turn(); a.h.flush(); }
  assert.equal(a.requests.length, 1);
  const seek = a.h.result.resolvePlaybackForTimestamp("synthetic", false, false, startMs + 40250);
  await seek; a.h.flush();
  assert.equal(a.requests.length, 1);
  assert.equal(a.h.result.playbackMap.tile.offsetSec, 40.25);
  assert.equal(a.h.result.playbackMap.tile.playbackKey, key);
  a.h.unmount();
});

test("crossing a segment boundary resolves the next file and preserves its exact time anchor", async () => {
  const a = sources(); a.requests[0].resolve(response()); await turn(); a.h.flush();
  const seek = a.h.result.resolvePlaybackForTimestamp("synthetic-next", false, false, startMs + 60000);
  assert.equal(a.requests.length, 2);
  a.requests[1].resolve(response(0, 2)); await seek; a.h.flush();
  assert.equal(a.h.result.playbackMap.tile.sourceStartMs, startMs + 60000);
  assert.equal(a.h.result.playbackMap.tile.segmentId, 2);
  assert.equal(a.h.result.playbackMap.tile.offsetSec, 0);
  a.h.unmount();
});

test("network failure is an explicit lookup error, never a confirmed gap", async () => {
  const a = sources(); a.requests[0].reject(new Error("synthetic network loss")); await turn(); a.h.flush();
  assert.equal(a.h.result.playbackMap.tile.lookupState, "error");
  assert.equal(archiveGroupPlayback.confirmedArchiveEmpty(a.h.result.playbackMap.tile), false);
  a.h.unmount();
});

test("a failed forced metadata refresh cannot erase a still-valid buffered source", async () => {
  const a = sources(); a.requests[0].resolve(response()); await turn(); a.h.flush();
  const before = a.h.result.playbackMap.tile;
  const refresh = a.h.result.resolvePlaybackForTimestamp("synthetic", true, false, startMs + 10000);
  a.requests[1].reject(new Error("synthetic timeout")); await refresh; a.h.flush();
  assert.equal(a.h.result.playbackMap.tile, before);
  a.h.unmount();
});

test("a new timestamp cancels the old lookup and late old metadata cannot win", async () => {
  const a = sources();
  const seek = a.h.result.resolvePlaybackForTimestamp("synthetic-new", false, false, startMs + 30250);
  assert.equal(a.requests[0].signal.aborted, true);
  a.requests[1].resolve(response(30.25)); await seek; a.h.flush();
  a.requests[0].resolve(response(10, 99)); await turn(); a.h.flush();
  assert.equal(a.h.result.playbackMap.tile.segmentId, 1);
  assert.equal(a.h.result.playbackMap.tile.sourceStartMs, startMs);
  a.h.unmount();
});

const archive = read("components/ArchiveTilePlayer.js");
const archiveComponent = archive.slice(archive.indexOf("export default function ArchiveTilePlayer("), archive.lastIndexOf("\n  return (")).replace("export default ", "") + "\nreturn {status};\n}";
const harness = { vm, read, plain, archiveComponent, hooks, noop, archiveGroupPlayback, URLSearchParams, AbortController };
vm.runInNewContext(baseline.slice(baseline.indexOf("function archiveHarness(){"), baseline.indexOf("\nawait check('F08")) + "\nthis.make=archiveHarness;", harness);

test("a late playable frame completes preparation even after its timeout; no fabricated ready", async () => {
  const a = harness.make();
  a.video.buffered = { length: 1, start: () => 0, end: () => 600 };
  a.operation("late");
  a.update({ coordination: { operationId: "late", releaseState: "holding", shouldResume: true } });
  a.requests[0].resolve("SYNTHETIC_ONLY"); await turn(); a.h.flush();
  a.video.emit("loadedmetadata", 1); a.h.flush();
  const timeout = [...a.timers.entries()].find(([, value]) => value.delay === 6000);
  assert.ok(timeout); a.timers.delete(timeout[0]); timeout[1].fn(); a.h.flush();
  assert.equal(a.h.result.status, "loading");
  assert.equal(a.states.at(-1).state, "timeout");
  a.video.emit("canplay", 3); a.h.flush();
  assert.equal(a.h.result.status, "ready");
  assert.equal(a.states.at(-1).state, "ready");
  assert.equal(a.requests.length, 1);
  a.h.unmount();
});

test("the real media element is retained across source changes and the last-frame layer is present", () => {
  const video = archive.slice(archive.indexOf("        <video"), archive.indexOf("        <CompactVideoCanvas"));
  assert.doesNotMatch(video, /key=\{playback/);
  assert.match(video, /archiveHeldFrame/);
});

test("small same-file seeks cannot stick between seek and readiness tolerances", () => {
  const code = archive.slice(archive.indexOf("  function safeTargetTimeForVideo"),
    archive.indexOf("  useEffect(() => {", archive.indexOf("  function safeTargetTimeForVideo")));
  for (const delta of [0.21, 0.25, 0.34, -0.21, -0.25, -0.34]) {
    const states = [], timers = new Map();
    const video = { readyState: 4, currentTime: 10, duration: 60, pause: noop,
      addEventListener: noop, removeEventListener: noop };
    const ctx = { archiveTargetReady: archiveGroupPlayback.archiveTargetReady, setHeldFrame: noop,
      videoRef: { current: video }, prepareSequenceRef: { current: 0 }, prepareCancelRef: { current: null },
      coordinationRef: { current: { operationId: "small-seek", releaseState: "holding", shouldResume: false } },
      playIntentRef: { current: false }, playbackRateRef: { current: 1 },
      sampleVideoDimensions: noop, setStatus: state => states.push(state), reportTileState: noop,
      setTimeout: fn => { timers.set(1, fn); return 1; }, clearTimeout: id => timers.delete(id) };
    vm.runInNewContext(code + "\nthis.prepare=prepareVideoAtTarget;", ctx);
    ctx.prepare({ targetTime: 10 + delta });
    assert.equal(video.currentTime, 10 + delta);
    assert.equal(states.at(-1), "ready");
    assert.equal(timers.size, 0);
  }
});
