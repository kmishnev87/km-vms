import test from "node:test";
import assert from "node:assert/strict";
import { cameraIsBeingWatched, silenceWatchedCameraEvent } from "../lib/cameraEventAttention.js";

function scene() {
  const video = { paused: false, ended: false, error: null, readyState: 3 };
  const player = { playing: true, getAttribute() { return String(this.playing); }, querySelector() { return video; },
    getBoundingClientRect: () => ({ left: 20, top: 20, right: 400, bottom: 250 }), contains: item => item === video };
  const doc = { visibilityState: "visible", defaultView: { innerWidth: 1000, innerHeight: 700 },
    querySelectorAll: selector => selector.includes('="3"') ? [player] : [], elementFromPoint: () => video };
  return { video, player, doc };
}

test("only the exact camera that is visibly playing is quiet", () => {
  const { doc } = scene();
  assert.equal(cameraIsBeingWatched(3, doc), true);
  assert.equal(cameraIsBeingWatched(4, doc), false);
  assert.equal(cameraIsBeingWatched(null, doc), false);
  assert.equal(cameraIsBeingWatched('3"]', doc), false);
});

test("background, buffering, errors, pausing and another fullscreen never mute alerts", () => {
  const { doc, video, player } = scene();
  doc.visibilityState = "hidden"; assert.equal(cameraIsBeingWatched(3, doc), false);
  doc.visibilityState = "visible"; player.playing = false; assert.equal(cameraIsBeingWatched(3, doc), false);
  player.playing = true; video.paused = true; assert.equal(cameraIsBeingWatched(3, doc), false);
  video.paused = false; video.error = {}; assert.equal(cameraIsBeingWatched(3, doc), false);
  video.error = null; video.readyState = 1; assert.equal(cameraIsBeingWatched(3, doc), false);
  video.readyState = 3; doc.fullscreenElement = { contains: () => false }; assert.equal(cameraIsBeingWatched(3, doc), false);
});

test("offscreen or covered players are not treated as watched", () => {
  const { doc, player } = scene();
  doc.elementFromPoint = () => ({}); assert.equal(cameraIsBeingWatched(3, doc), false);
  doc.elementFromPoint = () => null; assert.equal(cameraIsBeingWatched(3, doc), false);
  player.getBoundingClientRect = () => ({ left: -400, top: 0, right: -1, bottom: 100 });
  assert.equal(cameraIsBeingWatched(3, doc), false);
});

test("critical alarms still alert while watching and no history is modified", () => {
  const { doc } = scene();
  const event = { camera_id: 3, kind: "motion", read: false };
  assert.equal(silenceWatchedCameraEvent(event, doc), true);
  assert.deepEqual(event, { camera_id: 3, kind: "motion", read: false });
  for (const kind of ["fire", "tamper"]) assert.equal(silenceWatchedCameraEvent({ ...event, kind }, doc), false);
});
