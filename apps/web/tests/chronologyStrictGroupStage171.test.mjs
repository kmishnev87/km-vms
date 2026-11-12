import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { createPlaybackCoordinator } from "../lib/chronologyPlaybackCoordinator.js";
import { createCoalescedPlaybackResolver } from "../lib/coalescedPlaybackResolver.js";
import { archiveSourceCovers, archiveTargetOffset, archivePositionMs, archiveTargetReady, confirmedArchiveEmpty } from "../lib/archiveGroupPlayback.js";

function group(ids = ["a", "b", "c", "missing"]) {
  let time = 0, serial = 0;
  const timers = new Map(), resumes = [], holds = [], clocks = [], positions = {}, pauses = [];
  const store = createPlaybackCoordinator({ now: () => ++time, onResume: facts => resumes.push(facts),
    onHold: (ms, reason) => holds.push({ ms, reason }), onProgress: ms => clocks.push(ms),
    schedule: (fn, delay) => { timers.set(++serial, { fn, delay }); return serial; }, cancelTimer: id => timers.delete(id) });
  for (const id of ids) {
    positions[id] = 10000;
    store.registerTile(id, { pause: () => pauses.push(id), sample: () => ({ positionMs: positions[id] }) });
  }
  const op = store.start({ targetMs: 10000, targetTs: "synthetic", shouldResume: true, expectedTileIds: ids });
  return { store, op, timers, resumes, holds, clocks, positions, pauses,
    ready(id, state = "ready") { store.updateTile(id, store.getSnapshot().id, state); },
    release() { for (const id of ids) store.updateTile(id, store.getSnapshot().id, id === "missing" ? "empty" : "ready"); },
    expire() { for (const [id, timer] of [...timers]) { timers.delete(id); timer.fn(); } } };
}

test("three recordings and a proven archive gap start together, never before the last recording", () => {
  const g = group();
  g.ready("missing", "empty"); g.ready("a"); g.ready("b");
  g.expire();
  assert.equal(g.store.getSnapshot().releaseState, "holding");
  assert.equal(g.resumes.length, 0);
  g.ready("c");
  assert.equal(g.store.getSnapshot().releaseState, "released");
  assert.equal(g.resumes.length, 1);
  assert.equal(g.resumes[0].empty, false);
  g.store.dispose();
});

for (const blocked of ["pending", "loading", "buffering", "timeout", "error", "unsupported"]) {
  test(`${blocked} is not an archive gap and cannot release the group`, () => {
    const g = group(["a", "b"]); g.ready("a"); g.ready("b", blocked); g.expire();
    assert.equal(g.store.getSnapshot().releaseState, "holding");
    assert.equal(g.store.release(g.store.getSnapshot().id), false);
    assert.equal(g.resumes.length, 0);
    g.ready("b"); assert.equal(g.resumes.length, 1); g.store.dispose();
  });
}

test("one buffering tile synchronously pauses all, preserves one target, and resumes only together", () => {
  const g = group(); g.release(); const before = g.pauses.length;
  g.positions.a = 20000; g.positions.b = 20020; g.positions.c = 20010;
  g.positions.missing = -99999;
  g.store.updateTile("b", g.store.getSnapshot().id, "buffering");
  assert.equal(g.pauses.length - before, 4);
  assert.equal(g.store.getSnapshot().targetMs, 20000);
  assert.equal(g.store.getSnapshot().releaseState, "holding");
  g.ready("a"); g.ready("c"); g.ready("missing", "empty"); g.expire();
  assert.equal(g.resumes.length, 1);
  g.ready("b"); assert.equal(g.resumes.length, 2);
  g.store.dispose();
});

test("manual pause during buffering rejects late readiness and never auto-resumes", () => {
  const g = group(); g.release(); g.store.updateTile("a", g.store.getSnapshot().id, "buffering");
  const operation = g.store.getSnapshot().id;
  g.store.cancel("paused");
  for (const id of ["a", "b", "c", "missing"]) g.store.updateTile(id, operation, id === "missing" ? "empty" : "ready");
  g.expire(); assert.equal(g.resumes.length, 1);
  assert.equal(g.store.getSnapshot().releaseState, "cancelled"); g.store.dispose();
});

test("segment end holds the group at its exact archive boundary, not at the previous seek", () => {
  const g = group(); g.release();
  g.store.updateTile("a", g.store.getSnapshot().id, "boundary", { targetMs: 60500 });
  assert.equal(g.store.getSnapshot().targetMs, 60500);
  assert.equal(g.holds.at(-1).reason, "boundary");
  g.release(); assert.equal(g.resumes.length, 2); g.store.dispose();
});

test("clock samples actual media positions, excluding archive gaps, and holds on divergence", () => {
  const g = group(); g.release();
  g.positions.a = 15000; g.positions.b = 15030; g.positions.c = 15010; g.positions.missing = 1;
  g.store.updateTile("a", g.store.getSnapshot().id, "progress");
  assert.equal(g.clocks.at(-1), 15000);
  g.positions.b = 18000;
  g.store.updateTile("a", g.store.getSnapshot().id, "progress");
  assert.equal(g.store.getSnapshot().releaseState, "holding");
  assert.equal(g.store.getSnapshot().targetMs, 15000); g.store.dispose();
});

test("removing the blocking tile releases remaining ready recordings, adding one holds again", () => {
  const g = group(["a", "b"]); g.ready("a");
  g.store.setMembership(["a"]); assert.equal(g.resumes.length, 1);
  g.store.setMembership(["a", "c"]); assert.equal(g.store.getSnapshot().releaseState, "holding");
  g.ready("a"); assert.equal(g.resumes.length, 1);
  g.ready("c", "empty"); assert.equal(g.resumes.length, 2); g.store.dispose();
});

test("an unprepared added player cannot rewind the group to its file start", () => {
  const g = group(["a"]); g.release(); g.positions.a = 15400;
  g.store.registerTile("new", { pause() {}, sample: () => ({ positionMs: 10000 }) });
  g.store.setMembership(["a", "new"]);
  assert.equal(g.store.getSnapshot().targetMs, 15400);
  assert.equal(g.store.getSnapshot().releaseState, "holding");
  g.ready("a"); g.ready("new");
  assert.equal(g.resumes.length, 2);
  g.store.dispose();
});

test("every reactive hold can refresh file membership, while explicit seeks keep one resolver", () => {
  const page = fs.readFileSync(new URL("../app/chronology/page.js", import.meta.url), "utf8");
  const code = page.slice(page.indexOf("      onHold: (ms, reason) => {"), page.indexOf("    });", page.indexOf("      onHold: (ms, reason) => {")));
  const seen = [], ctx = { setCurrentTs() {}, playbackBridgeRef: { current: ms => seen.push(ms) } };
  vm.runInNewContext("this.hold=({" + code + "}).onHold;", ctx);
  for (const reason of ["boundary", "membership", "resynchronizing", "buffering", "loading"]) ctx.hold(15400, reason);
  assert.deepEqual(seen, [15400, 15400, 15400, 15400, 15400]);
  ctx.hold(15400, "seek"); assert.equal(seen.length, 5);
});

for (const mode of ["playing", "waiting", "paused", "last-tile"]) {
  test(`the real page close handler preserves the remaining group's intent: ${mode}`, () => {
    const g = group(mode === "last-tile" ? ["a"] : ["a", "b"]);
    g.ready("a");
    if (mode === "playing" || mode === "paused") g.ready("b", "empty");
    if (mode === "paused") g.store.cancel("paused");
    let tiles = g.store.getSnapshot().expectedTileIds.map(id => ({ id }));
    let stopped = false;
    const page = fs.readFileSync(new URL("../app/chronology/page.js", import.meta.url), "utf8");
    const code = page.slice(page.indexOf("  function removeTile("), page.indexOf("  function removeAllTiles("));
    const ctx = { tilesRef: { current: tiles }, invalidateSeekActions() {},
      playbackResolverRef: { current: { invalidate() {} } }, setIsPlaying: value => { stopped = !value; },
      setTiles: update => { tiles = update(tiles); g.store.setMembership(tiles.map(tile => tile.id)); },
      setFullscreenTileId() {}, cancelPlaybackCoordinator: () => g.store.cancel("incorrect-cancellation") };
    vm.runInNewContext(code + "\nthis.close=removeTile;", ctx);
    ctx.close(mode === "last-tile" ? "a" : "b");
    assert.equal(g.store.getSnapshot().releaseState,
      ["paused", "last-tile"].includes(mode) ? "cancelled" : "released");
    assert.equal(stopped, mode === "last-tile");
    assert.equal(g.resumes.length, 1);
    g.store.dispose();
  });
}

test("metadata alone, an in-progress seek or one unbuffered frame are never playable readiness", () => {
  const video = { currentTime: 10, duration: 20, readyState: 1, seeking: false, buffered: { length: 1, start: () => 0, end: () => 20 } };
  assert.equal(archiveTargetReady(video, 10, { resume: true }), false);
  video.readyState = 3; video.seeking = true;
  assert.equal(archiveTargetReady(video, 10, { resume: true }), false);
  video.seeking = false; video.buffered.end = () => 10.1;
  assert.equal(archiveTargetReady(video, 10, { resume: true }), false);
  video.buffered.end = () => 12;
  assert.equal(archiveTargetReady(video, 10, { resume: true }), true);
  assert.equal(archiveTargetReady(video, 8, { resume: true }), false);
});

test("source intervals preserve subsecond offsets and exclude the next segment boundary", () => {
  const source = { hasVideo: true, sourceStartMs: 10000, sourceEndMs: 20000, offsetSec: 1 };
  assert.equal(archiveSourceCovers(source, 19999), true);
  assert.equal(archiveSourceCovers(source, 20000), false);
  assert.equal(archiveTargetOffset(source, 12345), 2.345);
  assert.equal(archivePositionMs(source, 2.345), 12345);
  assert.equal(confirmedArchiveEmpty({ hasVideo: false, lookupState: "error" }), false);
  assert.equal(confirmedArchiveEmpty({ hasVideo: false, lookupState: "empty" }), true);
});

test("a new seek starts immediately and aborts the obsolete lookup without accepting its late result", async () => {
  const resolver = createCoalescedPlaybackResolver(), applied = []; let oldReply, oldSignal;
  const old = resolver.resolve({ run: signal => { oldSignal = signal; return new Promise(resolve => { oldReply = resolve; }); }, apply: value => applied.push(value) });
  const fresh = resolver.resolve({ run: () => Promise.resolve("fresh"), apply: value => applied.push(value) });
  assert.equal(oldSignal.aborted, true);
  assert.deepEqual(await old, { applied: false });
  assert.deepEqual(await fresh, { applied: true });
  oldReply("obsolete"); await Promise.resolve();
  assert.deepEqual(applied, ["fresh"]);
});
