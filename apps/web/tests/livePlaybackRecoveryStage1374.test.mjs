import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => fs.readFileSync(resolve(root, path), "utf8");
const { liveAudioAllowed } = await import(`data:text/javascript;base64,${Buffer.from(read("lib/liveAudioPlayback.js")).toString("base64")}`);
const helper = read("lib/livePlaybackRecovery.js").replaceAll("export const ", "const ").replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(`${helper}\nthis.delay=liveRecoveryDelay; this.message=liveFailureMessage;`, context);
assert.equal(context.delay(0), 2000);
assert.equal(context.delay(3), 20000);
assert.equal(context.delay(100), 60000);
assert.match(context.message("camera_unreachable"), /Камера недоступна/);
assert.match(context.message("auth_failed"), /логин и пароль/);
assert.match(context.message(null, true), /Просмотр прервался/);

const tile = read("components/TilePlayer.js");
assert.doesNotMatch(tile, /loadSource\(next\.url\)/);
assert.doesNotMatch(tile, /video\.src = next\.url/);
assert.doesNotMatch(tile, /index\.m3u8\?media_token/);
assert.match(tile, /hls\.once\(Hls\.Events\.MEDIA_ATTACHED/);
assert.match(tile, /void closeViewer\(\)/);
assert.match(tile, /currentViewerId = viewer\?\.viewer_id/);
assert.doesNotMatch(tile, /viewerIdRef/); // old, cancelled effects cannot overwrite a new viewer
assert.match(tile, /liveRecoveryDelay\(recoveryAttempt\+\+\)/);
assert.match(tile, /video\.currentTime > lastPlaybackTime/);

const start = tile.indexOf("async function refreshPlaylistSource()");
const end = tile.indexOf("function scheduleMediaTokenRefresh", start);
let issued = 0, sourceChanges = 0, scheduled = 0;
const refreshContext = {
  cancelled: false,
  mediaTokenRefreshInFlightRef: { current: null },
  buildPlaylistUrl: async () => { issued += 1; return { url: "/api/live/1/sub/index.m3u8", expiresIn: 299 }; },
  scheduleMediaTokenRefresh: () => { scheduled += 1; },
  hlsRef: { current: { loadSource: () => { sourceChanges += 1; } } },
  videoRef: { current: { load: () => { sourceChanges += 1; } } },
  lastTokenError: null,
};
vm.runInNewContext(`${tile.slice(start, end)}\nthis.refresh=refreshPlaylistSource;`, refreshContext);
await Promise.all([refreshContext.refresh(), refreshContext.refresh(), refreshContext.refresh()]);
assert.equal(issued, 1, "parallel renewal is deduplicated");
assert.equal(sourceChanges, 0, "renewal never resets an intact media source");
assert.equal(scheduled, 1);
assert.equal(refreshContext.mediaTokenRefreshInFlightRef.current, null);
refreshContext.cancelled = true;
await refreshContext.refresh();
assert.equal(issued, 1, "a closed viewer does not renew");

const warningSource = read("lib/operatorWarnings.js").replaceAll("export const ", "const ").replaceAll("export function ", "function ");
const warningsContext = {};
vm.runInNewContext(`${warningSource}\nthis.warnings=buildOperatorWarnings;`, warningsContext);
const warnings = warningsContext.warnings({ domains: { live: { items: [
  { camera_id: 1, camera_name: "Disconnected camera", state: "restarting", severity: "error", safe_failure_reason: "camera_unreachable", reason_codes: ["live_failed", "live_recovering"], next_retry_at: 1000 },
] } } });
assert.equal(warnings[0].affected_cameras[0].camera_name, "Disconnected camera");
assert.equal(warnings[0].affected_cameras[0].reason, "camera_unreachable");
assert.equal(warnings[0].affected_cameras[0].next_retry_at, 1000);

const translations = read("lib/i18n/legacyTextTranslations.js");
const translationsContext = {};
vm.runInNewContext(translations.replace("export const ", "const ").replace("export default TEXT_TRANSLATIONS;", "this.translations=TEXT_TRANSLATIONS;"), translationsContext);
for (const locale of ["en", "zh-CN"]) {
  for (const reason of ["camera_unreachable", "auth_failed", "resource_limit", "no_rtsp_url", null]) {
    assert.ok(translationsContext.translations[locale][context.message(reason, true)]);
  }
  for (const key of ["предупреждение", "предупреждения", "предупреждений", "Показать детали", "Скрыть детали", "Онлайн-поток не запустился"]) {
    assert.ok(translationsContext.translations[locale][key], `${locale}: ${key}`);
  }
}

const banners = read("components/OperatorProblemBanners.js");
const countStart = banners.indexOf("function warningCountLabel");
const countEnd = banners.indexOf("function displayTitle", countStart);
const countContext = {};
vm.runInNewContext(`${banners.slice(countStart, countEnd)}\nthis.label=warningCountLabel;`, countContext);
const en = (value) => translationsContext.translations.en[value];
assert.equal(countContext.label(1, en), "1 warning");
assert.equal(countContext.label(2, en), "2 warnings");
assert.equal(countContext.label(11, en), "11 warnings");
assert.equal(countContext.label(21, (v) => v), "21 предупреждение");

// A real readiness poll reports state transitions, not every failed sample.
// Operator warnings update promptly without introducing a busy global poll.
const readinessStart = tile.indexOf("async function waitForReady");
const readinessEnd = tile.indexOf("function failWithMessage", readinessStart);
let readinessRequests = 0, runtimeEvents = 0;
const states = [
  { status: "restarting", safe_failure_reason: "camera_unreachable", retry_count: 1 },
  { status: "restarting", safe_failure_reason: "camera_unreachable", retry_count: 1 },
  { status: "restarting", safe_failure_reason: "camera_unreachable", retry_count: 2 },
  { status: "ready", running: true, ready: true, retry_count: 2 },
];
const readinessContext = {
  cancelled: false, generation: 1, sourceKey: { cameraId: 1, stream: "sub" },
  apiFetch: async () => {
    const state = states[readinessRequests++];
    if (!state) {
      readinessContext.cancelled = true;
      throw new Error("Unexpected readiness retry in test");
    }
    return { items: [state] };
  },
  liveAudioAllowed, audioAllowedByConfig: true,
  lastRuntimeState: "", notifyLiveRuntimeChanged: () => { runtimeEvents += 1; },
  reportAudioStatus: () => {}, applyNaturalResolution: () => {},
  setError: () => {}, setStatus: () => {}, sleep: async () => {},
  liveFailureMessage: () => "", everPlayed: false, READY_POLL_INTERVAL_MS: 1500,
};
vm.runInNewContext(`${tile.slice(readinessStart, readinessEnd)}\nthis.wait=waitForReady;`, readinessContext);
assert.equal((await readinessContext.wait(1)).ready, true);
assert.equal(readinessRequests, 4);
assert.equal(runtimeEvents, 3);

const livePage = read("app/live/page.js");
const escapeStart = livePage.indexOf("function handleKeyDown(event)");
const escapeEnd = livePage.indexOf('window.addEventListener("keydown"', escapeStart);
const escapeSource = livePage.slice(escapeStart, escapeEnd);
for (const [tileId, systemFullscreen, key, expectedTile, expectedSystem] of [
  ["tile-1", false, "Escape", 1, 0],
  ["tile-1", true, "Escape", 1, 0],
  [null, true, "Escape", 0, 1],
  [null, false, "Escape", 0, 0],
  ["tile-1", true, "Enter", 0, 0],
]) {
  let tileExits = 0, systemExits = 0, prevented = 0;
  const keyContext = {
    fullscreenTileId: tileId, isSystemFullscreen: systemFullscreen,
    exitTileFullscreen: () => { tileExits += 1; },
    exitSystemFullscreen: () => { systemExits += 1; },
  };
  vm.runInNewContext(`${escapeSource}\nthis.keydown = handleKeyDown;`, keyContext);
  keyContext.keydown({ key, preventDefault: () => { prevented += 1; } });
  assert.equal(tileExits, expectedTile);
  assert.equal(systemExits, expectedSystem);
  assert.equal(prevented, expectedTile);
}
