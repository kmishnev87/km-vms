import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { enDictionary } from "../lib/i18n/en.js";
import { ruDictionary } from "../lib/i18n/ru.js";
import { zhCNDictionary } from "../lib/i18n/zhCN.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const helperSource = fs
  .readFileSync(resolve(__dirname, "../lib/cameraOnvifConfig.js"), "utf8")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${helperSource}
this.onvifConfigFromResult = onvifConfigFromResult;
this.onvifConfigValuesEqual = onvifConfigValuesEqual;
this.buildOnvifConfigDiff = buildOnvifConfigDiff;
this.onvifConfigTruthState = onvifConfigTruthState;
this.normalizeOnvifConfigError = normalizeOnvifConfigError;`,
  context,
);

const {
  onvifConfigFromResult,
  onvifConfigValuesEqual,
  buildOnvifConfigDiff,
  onvifConfigTruthState,
  normalizeOnvifConfigError,
} = context;

const supported = {
  codec: { writable: true },
  resolution: { writable: true },
  fps: { writable: true },
  bitrate: { writable: true },
  iframe_interval: { writable: false },
  quality: { writable: false },
};
const baseline = {
  codec: "H264",
  resolution: "3840x2160",
  fps: 25,
  bitrate: 4096,
  iframe_interval: 50,
  quality: 5,
  supported,
};

assert.deepEqual(
  JSON.parse(JSON.stringify(buildOnvifConfigDiff(baseline, baseline, supported))),
  {},
);
assert.deepEqual(
  JSON.parse(JSON.stringify(buildOnvifConfigDiff(
    { ...baseline, bitrate: "2048", iframe_interval: 75 },
    baseline,
    supported,
  ))),
  { bitrate: 2048 },
);
assert.equal(onvifConfigValuesEqual("bitrate", "4096", 4096), true);
assert.equal(onvifConfigValuesEqual("codec", "h264", "H264"), true);
assert.equal(onvifConfigValuesEqual("resolution", "1920x1080", "3840x2160"), false);

const snapshot = onvifConfigFromResult({
  config: { codec: "H265", width: 1920, height: 1080, fps: 20, bitrate: 2048 },
  supported,
});
assert.equal(snapshot.codec, "H265");
assert.equal(snapshot.resolution, "1920x1080");
assert.equal(snapshot.bitrate, 2048);
assert.equal(snapshot.supported, supported);
assert.equal(onvifConfigTruthState({ status: "ok" }), "current");
assert.equal(onvifConfigTruthState({ status: "unsupported" }), "current");
assert.equal(onvifConfigTruthState({ status: "error" }), "unavailable");

const rawBackendEnglish = "The camera refused the ONVIF encoder configuration update.";
for (const [locale, copy] of [
  ["ru", ruDictionary.cameras],
  ["en", enDictionary.cameras],
  ["zhCN", zhCNDictionary.cameras],
]) {
  assert.equal(
    normalizeOnvifConfigError(
      { code: "video_encoder_configuration_mismatch", message: rawBackendEnglish },
      copy,
    ),
    copy.profileConfigMismatch,
    `${locale} mismatch localization`,
  );
  assert.equal(
    normalizeOnvifConfigError(
      { code: "video_encoder_configuration_set_failed", message: rawBackendEnglish },
      copy,
    ),
    copy.profileConfigSetFailed,
    `${locale} set failure localization`,
  );
  assert.equal(
    normalizeOnvifConfigError(
      { detail: { code: "video_encoder_configuration_verify_failed" } },
      copy,
    ),
    copy.profileConfigVerifyFailed,
    `${locale} verify failure localization`,
  );
  assert.equal(
    normalizeOnvifConfigError(
      { data: { detail: { code: "video_encoder_configuration_read_failed" } } },
      copy,
    ),
    copy.profileConfigUnavailable,
    `${locale} read failure localization`,
  );
  assert.equal(
    normalizeOnvifConfigError(
      { code: "video_encoder_vendor_extension_failed", message: rawBackendEnglish },
      copy,
    ),
    copy.actionFailed,
    `${locale} unknown configuration fallback`,
  );
  const existingAuthFallback = normalizeOnvifConfigError(
    { code: "wrong_credentials", message: rawBackendEnglish },
    copy,
  ) ?? copy.authError;
  assert.equal(existingAuthFallback, copy.authError, `${locale} existing auth fallback`);
  assert.notEqual(
    normalizeOnvifConfigError(
      { code: "video_encoder_configuration_set_failed", message: rawBackendEnglish },
      copy,
    ),
    locale === "en" ? "" : rawBackendEnglish,
    `${locale} must not expose raw backend English as primary text`,
  );
}

const page = fs.readFileSync(resolve(__dirname, "../app/cameras/page.js"), "utf8");
assert.equal(page.includes("setOnvifConfigBaseline(snapshot)"), true);
assert.equal(page.includes("buildOnvifConfigDiff(\n        onvifConfig,\n        onvifConfigBaseline"), true);
assert.equal(page.includes("result?.verification?.matched ? \"verified\""), true);
assert.equal(page.includes("video_encoder_configuration_mismatch"), true);
assert.equal(page.includes("setOnvifConfigTruth(\"unavailable\")"), true);
assert.equal(page.includes("normalizeOnvifConfigError(err, copy)"), true);

const loadStart = page.indexOf("async function loadOnvifProfileConfig");
const applyStart = page.indexOf("async function applyOnvifProfileConfig");
const loadBlock = page.slice(loadStart, applyStart);
assert.equal(loadBlock.includes("setForm((prev)"), false);
assert.equal(loadBlock.includes("onvif_profile_token:"), false);
assert.equal(loadBlock.includes("onvif_sub_profile_token:"), false);

for (const locale of ["ru", "en", "zhCN"]) {
  const dictionary = fs.readFileSync(resolve(__dirname, `../lib/i18n/${locale}.js`), "utf8");
  for (const key of [
    "profileConfigNotChecked",
    "profileConfigCurrent",
    "profileConfigChanged",
    "profileConfigVerified",
    "profileConfigMismatch",
    "profileConfigUnavailable",
    "profileConfigSetFailed",
    "profileConfigVerifyFailed",
  ]) {
    assert.equal(dictionary.includes(`${key}:`), true, `${locale} is missing ${key}`);
  }
}

console.log("Stage 13.7.2.2 ONVIF frontend truth tests PASS");
