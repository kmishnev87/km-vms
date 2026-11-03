import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(resolve(here, "../lib/cameraOnvifConfig.js"), "utf8")
  .replaceAll("export const ", "const ").replaceAll("export function ", "function ");
const ctx = {};
vm.runInNewContext(`${source}\nthis.model={onvifConfigFromResult,changeOnvifConfigField,onvifConfigFieldsFor,buildOnvifConfigDiff,invalidOnvifConfigField,normalizeOnvifConfigError,onvifConfigDisplayTruth};`, ctx);
const m = ctx.model;
assert.equal(m.onvifConfigDisplayTruth("changed", false), "current", "returning every field to the read baseline must clear the unsaved-change message");
assert.equal(m.onvifConfigDisplayTruth("current", true), "changed");
assert.equal(m.onvifConfigDisplayTruth("verified", false), "verified");
assert.equal(m.onvifConfigDisplayTruth("mismatch", false), "mismatch");
assert.equal(m.onvifConfigDisplayTruth("unavailable", true), "unavailable");
const plain = (value) => JSON.parse(JSON.stringify(value));
const field = (value, options = []) => ({ readable: true, writable: true, value, options });
const h264 = { codec: field("H265", ["H264", "H265"]), resolution: field("1920x1080", ["1920x1080", "3840x2160"]),
  fps: field(29.97, [29.97, 25, 15]), bitrate: { ...field(4096), range: { min: 64, max: 8192 } },
  iframe_interval: { ...field(25), range: { min: 1, max: 150 } }, quality: { ...field(6), range: { min: 1, max: 6 } },
  bitrate_mode: field("VBR", ["VBR", "CBR"]), codec_profile: field("High", ["Main", "High"]) };
const h265 = { ...h264, codec_profile: field("Main", ["Main"]), fps: field(25, [25, 15]),
  bitrate: { ...field(4096), range: { min: 128, max: 4096 } } };
const lowFps = { ...h265, fps: field(15, [15, 5]) };
const snapshot = m.onvifConfigFromResult({
  config: { codec: "H264", resolution: "1920x1080", fps: 29.97, bitrate: 6144, iframe_interval: 25,
    quality: 6, bitrate_mode: "VBR", codec_profile: "High" },
  supported: h264, options_by_codec: { H264: { fields: h264 }, H265: { fields: h265,
    by_resolution: { "1920x1080": h265, "3840x2160": lowFps } } }, shared_profile_tokens: ["one", "two"],
});
assert.equal(m.invalidOnvifConfigField(snapshot), null);
assert.deepEqual(plain(m.buildOnvifConfigDiff(snapshot, snapshot, h264)), {});
const changed = m.changeOnvifConfigField(snapshot, "codec", "H265");
assert.equal(changed.codec_profile, "Main", "old codec profile must not be carried into an incompatible codec");
assert.equal(changed.fps, 25);
assert.equal(changed.bitrate, 4096);
assert.equal(changed.bitrate_mode, "VBR");
assert.equal(changed.iframe_interval, 25);
assert.equal(m.invalidOnvifConfigField(changed), null);
assert.deepEqual(plain(m.buildOnvifConfigDiff(changed, snapshot, changed.supported)), {
  codec: "H265", fps: 25, bitrate: 4096, codec_profile: "Main",
});
const changedResolution = m.changeOnvifConfigField(changed, "resolution", "3840x2160");
assert.equal(changedResolution.fps, 15, "resolution-specific choices must be respected");
const changedMode = m.changeOnvifConfigField(snapshot, "bitrate_mode", "CBR");
assert.deepEqual(plain(m.buildOnvifConfigDiff(changedMode, snapshot, changedMode.supported)), { bitrate_mode: "CBR" });
assert.equal(m.invalidOnvifConfigField({ ...snapshot, bitrate: "9000" }), "bitrate");
assert.equal(m.invalidOnvifConfigField({ ...snapshot, fps: "24" }), "fps");
assert.equal(m.invalidOnvifConfigField({ ...snapshot, bitrate: "NaN" }), "bitrate");
assert.equal(m.invalidOnvifConfigField({ ...snapshot, iframe_interval: "1.5" }), "iframe_interval");
assert.equal(m.invalidOnvifConfigField({ ...snapshot, bitrate: "" }), "bitrate");
assert.equal(m.invalidOnvifConfigField({ ...snapshot, codec_profile: "vendor-current", bitrate: 2048 }, { ...snapshot, codec_profile: "vendor-current" }), null,
  "a camera-reported pre-existing value outside options must not block unrelated changes");
assert.equal(m.normalizeOnvifConfigError({ code: "video_encoder_configuration_result_unknown" }, { profileConfigResultUnknown: "unknown" }), "unknown");
assert.equal(m.normalizeOnvifConfigError({ code: "onvif_authentication_failed" }, { profileConfigAuthFailed: "denied" }), "denied");
assert.deepEqual(plain(snapshot.shared_profile_tokens), ["one", "two"]);
assert.equal(snapshot.fps, 29.97, "editing a draft must not mutate the baseline");
const editor = fs.readFileSync(resolve(here, "../components/cameras/CameraDeviceSettings.js"), "utf8");
const layoutSlots = editor.match(/const slots = \[([\s\S]*?)\n  \];/)?.[1];
assert.ok(layoutSlots, "settings keep an explicit, stable field layout");
assert.deepEqual([...layoutSlots.matchAll(/\["([a-z_]+)",/g)].map(match => match[1]), [
  "codec", "codec_profile", "resolution", "fps", "bitrate_mode", "bitrate", "quality", "iframe_interval",
], "codec/profile, resolution/FPS and mode/bitrate must be neighboring pairs in the requested order");
assert.match(editor, /<summary>\{copy\.editorDeviceSettings\}<\/summary>/, "the device settings heading must not include the Optional label");
assert.equal(editor.includes("slots.filter("), false, "field rows must not appear/disappear during reads");
assert.equal(editor.includes("editorSavedProfile"), false);
assert.equal(editor.includes("automaticRead.current !== selected"), true);
assert.equal(editor.includes("requestId !== currentRequest.current"), true);
assert.equal(editor.includes("setInterval"), false, "no permanent device polling");
const apply = editor.slice(editor.indexOf("async function applyOnvifProfileConfig"), editor.indexOf("const displayTruth"));
assert.ok(apply.indexOf("onVideoChanged()") > apply.indexOf("await request("), "video may only be invalidated after a camera write attempt, not before sending");
const hook = fs.readFileSync(resolve(here, "../components/cameras/useCameraEditor.js"), "utf8");
assert.ok(hook.includes("result.stream_path"));
assert.ok(hook.includes("result.shared_profile_tokens?.includes(profile.token)"));
const surface = fs.readFileSync(resolve(here, "../components/cameras/CameraEditor.js"), "utf8");
assert.ok(surface.includes('profile?.video_config_state === "ok"'));
assert.ok(surface.includes('profileConfigFrameRateLimit.replace("{fps}", fps)'));
console.log("Stage 13.7.13 ONVIF dependent settings, roundtrip feedback and stable UI model PASS");
