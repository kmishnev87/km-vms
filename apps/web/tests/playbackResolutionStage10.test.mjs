import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import {
  HIGH_RESOLUTION_THRESHOLD,
  isCompactPlaybackViewer,
  isHighResolutionVideo,
  shouldUseAdaptiveHighResolutionPlayback,
} from "../lib/playbackResolution.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const recordsPage = fs.readFileSync(resolve(__dirname, "../app/recordings/page.js"), "utf8");
const archivePlayer = fs.readFileSync(resolve(__dirname, "../components/ArchiveTilePlayer.js"), "utf8");
const camerasPage = fs.readFileSync(resolve(__dirname, "../app/cameras/page.js"), "utf8");

assert.deepEqual(HIGH_RESOLUTION_THRESHOLD, { width: 2560, height: 1440 });
assert.equal(isHighResolutionVideo({ width: 2560, height: 1440 }), true);
assert.equal(isHighResolutionVideo({ width: 3840, height: 2160 }), true);
assert.equal(isHighResolutionVideo({ width: 1920, height: 1080 }), false);
assert.equal(isHighResolutionVideo({ width: 0, height: 0 }), false);
assert.equal(isHighResolutionVideo(null), false);

assert.equal(isCompactPlaybackViewer({ width: 3840, height: 2160 }, { width: 800, height: 450 }), true);
assert.equal(isCompactPlaybackViewer({ width: 3840, height: 2160 }, { width: 1600, height: 900 }), false);
assert.equal(shouldUseAdaptiveHighResolutionPlayback({ width: 3840, height: 2160 }, { width: 800, height: 450 }, false), true);
assert.equal(shouldUseAdaptiveHighResolutionPlayback({ width: 3840, height: 2160 }, { width: 800, height: 450 }, true), false);
assert.equal(shouldUseAdaptiveHighResolutionPlayback({ width: 1920, height: 1080 }, { width: 800, height: 450 }, false), false);

assert.equal(recordsPage.includes("video.videoWidth"), true);
assert.equal(recordsPage.includes("video.videoHeight"), true);
assert.equal(recordsPage.includes("RECORDS_VIEW_MODE_KEY"), true);
assert.equal(recordsPage.includes("km_vms_records_view_mode"), true);
assert.equal(recordsPage.includes("recordingActiveEmpty"), true);
assert.equal(recordsPage.includes("data-highres-adaptive"), true);
assert.equal(recordsPage.includes("isDirectPlaybackUnsupported"), false);
assert.equal(recordsPage.includes("mkv\") || value.includes(\"matroska"), false);

assert.equal(archivePlayer.includes("video.videoWidth"), true);
assert.equal(archivePlayer.includes("video.videoHeight"), true);
assert.equal(archivePlayer.includes("data-highres-adaptive"), true);
assert.equal(archivePlayer.includes("isUnsupportedDirectPlayback"), false);
assert.equal(archivePlayer.includes("Двойной клик для полноэкранного режима"), true);

assert.equal(camerasPage.includes("validation_token: null"), true);
assert.equal(camerasPage.includes("onvif_probe_token: null"), true);
assert.equal(camerasPage.includes("manual_confirm_unverified: false"), true);

for (const source of [recordsPage, archivePlayer, camerasPage]) {
  assert.equal(source.includes("????????"), false);
  assert.equal(source.includes("РЎРє") || source.includes("Р—Р°") || source.includes("РќРµ"), false);
}
