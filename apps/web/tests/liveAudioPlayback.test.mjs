import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const { confirmedHlsAudio, liveAudioAllowed, playLiveAudio } = await import(`data:text/javascript;base64,${Buffer.from(read("lib/liveAudioPlayback.js")).toString("base64")}`);
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test("audio play starts immediately within the speaker gesture", () => {
  let calls = 0;
  const dispose = playLiveAudio({ play() { calls += 1; return Promise.resolve(); } });
  assert.equal(calls, 1);
  dispose();
});

test("a real current autoplay rejection still reaches the existing blocked-audio handler", async () => {
  const pending = deferred(), failures = [];
  playLiveAudio({ play: () => pending.promise }, error => failures.push(error.name));
  pending.reject({ name: "NotAllowedError" });
  await tick();
  assert.deepEqual(failures, ["NotAllowedError"]);
});

test("switching source may abort play without turning off the listening choice", async () => {
  const pending = deferred(), failures = [];
  playLiveAudio({ play: () => pending.promise }, error => failures.push(error.name));
  pending.reject({ name: "AbortError" });
  await tick();
  assert.deepEqual(failures, []);
});

test("a temporarily detached or unsupported source is not misreported as an autoplay permission denial", async () => {
  const failures = [];
  playLiveAudio({ play: () => Promise.reject({ name: "NotSupportedError" }) }, error => failures.push(error.name));
  playLiveAudio({ play() { throw { name: "InvalidStateError" }; } }, error => failures.push(error.name));
  await tick();
  assert.deepEqual(failures, []);
});

test("late failures from a disposed stream cannot mute the newly selected stream", async () => {
  const old = deferred(), current = deferred(), failures = [];
  const dispose = playLiveAudio({ play: () => old.promise }, () => failures.push("old"));
  dispose();
  playLiveAudio({ play: () => current.promise }, () => failures.push("current"));
  old.reject({ name: "NotAllowedError" });
  await tick();
  assert.deepEqual(failures, []);
  current.reject({ name: "NotAllowedError" });
  await tick();
  assert.deepEqual(failures, ["current"]);
});

test("successful play and cleanup do not pause media or create a second playback engine", async () => {
  let pauses = 0, failures = 0;
  const dispose = playLiveAudio({ play: () => Promise.resolve(), pause: () => pauses++ }, () => failures++);
  await tick();
  dispose();
  assert.equal(failures, 0);
  assert.equal(pauses, 0);
});

test("stream changes and capability probes preserve intent while effective audio still requires availability", () => {
  const live = read("app/live/page.js"), player = read("components/TilePlayer.js");
  const update = live.slice(live.indexOf("  function updateTile("), live.indexOf("  function removeTile("));
  const facts = live.slice(live.indexOf("  function handleAudioStatusChange("), live.indexOf("  function toggleTileAudio("));
  assert.doesNotMatch(update, /setActiveAudioTileId/);
  assert.match(update, /setAudioFactsByTileId/);
  assert.doesNotMatch(facts, /setActiveAudioTileId/);
  assert.match(live, /const active = activeAudioTileId === tileId && fact.audioAvailable && !fact.audioDisabledByConfig/);
  assert.match(live, /const disabled = fact.audioDisabledByConfig \|\| !fact.audioAvailable/);
  assert.match(player, /return playLiveAudio\(video, \(\) =>/);
  assert.match(player, /\[cameraId, stream, lowLatency, audioEnabled, audioRequestId\]/);
  assert.match(player, /hls.on\(Hls.Events.BUFFER_CODECS,[\s\S]*?if \(cancelled\) return;[\s\S]*?confirmedHlsAudio\(tracks, audioAllowedByConfig\)/);
});

test("actual HLS audio buffer confirms muxed audio despite an inconclusive earlier RTSP probe", () => {
  assert.deepEqual(confirmedHlsAudio({ audio: { codec: "mp4a.40.2" }, video: { codec: "avc1" } }),
    { audio_mode: "hls", audio_enabled: true, audio_available: true,
      audio_reason: "input_has_audio", input_audio_codec: "mp4a.40.2" });
});

test("a combined audio/video MP4 buffer also confirms real audio without guessing its camera codec", () => {
  assert.deepEqual(confirmedHlsAudio({ audiovideo: { codec: "mp4a.40.2,hvc1.1.6.L120.90" } }),
    { audio_mode: "hls", audio_enabled: true, audio_available: true,
      audio_reason: "input_has_audio", input_audio_codec: "" });
  assert.equal(confirmedHlsAudio({ audiovideo: { codec: "mp4a.40.2,hvc1.1.6.L120.90" } }, false), null);
});

test("a video-only buffer cannot invent audio, and explicit audio disable is respected", () => {
  assert.equal(confirmedHlsAudio({ video: { codec: "avc1" } }), null);
  assert.equal(confirmedHlsAudio({ audio: {} }), null);
  assert.equal(confirmedHlsAudio({ audio: { codec: "mp4a.40.2" } }, false), null);
});

test("an unsuccessful probe is not a configuration ban; all explicit disable modes are bans", () => {
  assert.equal(liveAudioAllowed({ audio_mode: "aac", audio_enabled: true, audio_reason: "probe_failed" }), true);
  assert.equal(liveAudioAllowed({ audio_enabled: false }), false);
  for (const mode of ["none", "off", "disable", "disabled", "OFF"]) {
    assert.equal(liveAudioAllowed({ audio_mode: mode, audio_enabled: true }), false);
  }
});
