import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const livePage = read("app/live/page.js");
const tilePlayer = read("components/TilePlayer.js");
const liveCss = read("app/styles/90-live-workspace.css");

assert.equal(livePage.includes("activeAudioTileId"), true, "LivePage must own a single active audio tile state");
assert.equal(livePage.includes('useState("")'), true, "audio starts globally off by default");
assert.equal(livePage.includes("setActiveAudioTileId((current) => (current === tileId ? \"\" : tileId))"), true);
assert.equal(livePage.includes("setActiveAudioTileId((current) => (current === tileId ? \"\" : current))"), true);
assert.equal(livePage.includes("audioRequestId"), true, "explicit speaker clicks must drive audio requests");
assert.equal(livePage.includes("handleAudioStatusChange"), true, "backend/HLS audio facts must feed the tile state");
assert.equal(livePage.includes("audioDisabledByConfig"), true);
assert.equal(livePage.includes("audioAvailable"), true);

for (const text of [
  "\\u0412\\u043a\\u043b\\u044e\\u0447\\u0438\\u0442\\u044c \\u0437\\u0432\\u0443\\u043a",
  "\\u0412\\u044b\\u043a\\u043b\\u044e\\u0447\\u0438\\u0442\\u044c \\u0437\\u0432\\u0443\\u043a",
  "\\u0410\\u0443\\u0434\\u0438\\u043e \\u043d\\u0435\\u0434\\u043e\\u0441\\u0442\\u0443\\u043f\\u043d\\u043e",
  "\\u0410\\u0443\\u0434\\u0438\\u043e \\u043e\\u0442\\u043a\\u043b\\u044e\\u0447\\u0435\\u043d\\u043e",
]) {
  assert.equal(livePage.includes(text), true, `${text} tooltip text must be present`);
}

assert.equal(livePage.includes('data-live-audio-button="true"'), true);
assert.equal(livePage.includes("disabled={audioState.disabled}"), true, "no-audio/config-disabled tiles must not enable false sound");
assert.equal(livePage.includes("onAudioPlaybackBlocked"), true, "browser autoplay rejection must be handled");
assert.equal(livePage.includes("live-audio-blocked-"), true);
assert.equal(livePage.includes("setError(TEXT.audioBlocked)"), false);
assert.equal(livePage.includes("addAllCameras"), true);
assert.equal(livePage.includes("layoutTiles([...active, ...additions]"), true, "Add All behavior remains present");
assert.equal(livePage.includes("setActiveAudioTileId(\"\")"), true, "active audio can be turned off");
assert.equal(livePage.includes("onChange={(event) => updateTile(tile.id, { stream: event.target.value })}"), true);

assert.equal(tilePlayer.includes("audioEnabled = false"), true, "TilePlayer defaults to muted audio");
assert.equal(tilePlayer.includes("muted={!audioEnabled}"), true, "video must not be permanently hardcoded muted");
assert.equal(tilePlayer.includes("controls={false}"), true, "native browser controls are not the product audio UX");
assert.equal(tilePlayer.includes("video.muted = !audioEnabled"), true);
assert.equal(tilePlayer.includes("audioRequestId"), true);
assert.equal(tilePlayer.includes("audioPlaybackBlockedRef"), true);
assert.equal(tilePlayer.includes("reportAudioStatus"), true);
assert.equal(tilePlayer.includes("input_audio_codec"), true);
assert.equal(tilePlayer.includes("audio_available"), true);
assert.equal(tilePlayer.includes("audio_enabled"), true);
assert.equal(tilePlayer.includes("Hls.Events.MANIFEST_PARSED"), true);

assert.equal(tilePlayer.includes("CompactVideoCanvas"), true, "Stage 17 compact renderer must remain wired");
assert.equal(tilePlayer.includes("nativeVideoSuppressed"), true, "native video suppression gate must remain present");
assert.equal(livePage.includes("SIDEBAR_CAMERA_REORDER_MIME"), true, "Stage 18 sidebar reorder must remain present");
assert.equal(livePage.includes("enterSystemFullscreen"), true, "Stage 18 fullscreen control must remain present");

for (const forbidden of [
  "rtsp://",
  "Authorization",
  "playlist_path",
  "stderr_tail",
  "pid_cmdline",
  "ffmpeg_cmd",
  "command",
  "<pre",
  "dangerouslySetInnerHTML",
]) {
  assert.equal(`${livePage}\n${tilePlayer}`.includes(forbidden), false, `${forbidden} must not be rendered by live audio UX`);
}

assert.equal(liveCss.includes(".liveWorkspaceAudioButton"), true);
assert.equal(liveCss.includes(".liveWorkspaceTile.audioActive"), true);
