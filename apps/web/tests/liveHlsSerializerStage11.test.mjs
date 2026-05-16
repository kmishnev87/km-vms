import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const tilePlayer = read("components/TilePlayer.js");
const livePage = read("app/live/page.js");
const api = read("lib/api.js");
const routePermissions = read("lib/routePermissions.js");

assert.equal(tilePlayer.includes("detail?.debug"), false);
assert.equal(tilePlayer.includes("item?.last_error"), false);
assert.equal(tilePlayer.includes("item?.exit_code"), false);
assert.equal(tilePlayer.includes("safe_failure_reason"), true);
assert.equal(tilePlayer.includes("issueLiveMediaTokenInfo"), true);
assert.equal(tilePlayer.includes("mediaToken"), true);
assert.equal(tilePlayer.includes("viewer_id"), true);
assert.equal(tilePlayer.includes("/live/status?camera_id="), true);
assert.equal(tilePlayer.includes("/live/viewers"), true);

for (const forbidden of [
  "dangerouslySetInnerHTML",
  "<pre",
  "rtsp://",
  "Authorization",
  "playlist_path",
  "segment_path",
  "stderr_tail",
  "pid_cmdline",
  "ffmpeg_cmd",
  "command",
]) {
  assert.equal(`${tilePlayer}\n${livePage}`.includes(forbidden), false, `${forbidden} must not be rendered or consumed by /live`);
}

assert.equal(api.includes('apiFetch("/live/media-token"'), true);
assert.equal(routePermissions.includes('"/live"'), true);
assert.equal(routePermissions.includes('permission: "view_live"'), true);

assert.equal(livePage.includes("backendPayload(tiles, sidebarCameraOrder)"), true);
assert.equal(tilePlayer.includes("body: JSON.stringify({"), true);
assert.equal(tilePlayer.includes("JSON.stringify(response"), false);
assert.equal(tilePlayer.includes("JSON.stringify(viewer"), false);
