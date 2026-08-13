import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const helperSource = fs
  .readFileSync(resolve(__dirname, "../lib/cameraStage16.js"), "utf8")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${helperSource}
this.applyCameraFormPatch = applyCameraFormPatch;
this.cameraViewModeStorageKey = cameraViewModeStorageKey;
this.loadCamerasViewMode = loadCamerasViewMode;
this.normalizeCameraPort = normalizeCameraPort;
this.saveCamerasViewMode = saveCamerasViewMode;`,
  context
);

const {
  applyCameraFormPatch,
  cameraViewModeStorageKey,
  loadCamerasViewMode,
  normalizeCameraPort,
  saveCamerasViewMode,
} = context;
const camerasPage = fs.readFileSync(resolve(__dirname, "../app/cameras/page.js"), "utf8");
const camerasCss = fs.readFileSync(resolve(__dirname, "../app/styles/50-cameras-shared-modals.css"), "utf8");
const responsiveCss = fs.readFileSync(resolve(__dirname, "../app/styles/60-responsive-shared.css"), "utf8");

const sensitive = new Set([
  "protocol", "host", "port", "username", "password", "rtsp_host", "rtsp_port",
  "rtsp_transport", "rtsp_main_url", "rtsp_sub_url", "onvif_path",
  "onvif_profile_token", "onvif_sub_profile_token", "onvif_channel_id",
]);

assert.equal(normalizeCameraPort(null), null);
assert.equal(normalizeCameraPort(""), null);
assert.equal(normalizeCameraPort("0"), null);
assert.equal(normalizeCameraPort(" 20003 "), 20003);

let form = {
  protocol: "onvif",
  host: "camera.example.test",
  port: 80,
  rtsp_host: "camera.example.test",
  rtsp_port: 554,
  rtsp_main_url: "/main",
  rtsp_sub_url: "/sub",
  onvif_profile_token: "main-token",
  onvif_sub_profile_token: "sub-token",
  preview_token: "preview",
  validation_token: "validation",
  main_validation_token: "main-proof",
  sub_validation_token: "sub-proof",
  onvif_probe_token: "probe",
  manual_confirm_unverified: true,
};

form = applyCameraFormPatch(form, "port", 2020, sensitive);
assert.equal(form.rtsp_port, 554);
assert.equal(form.preview_token, null);
assert.equal(form.onvif_probe_token, null);
assert.equal(form.main_validation_token, "main-proof");
assert.equal(form.sub_validation_token, "sub-proof");

form.onvif_probe_token = "probe-2";
form = applyCameraFormPatch(form, "rtsp_port", 554, sensitive);
assert.equal(form.rtsp_port, 554);
assert.equal(form.port, 2020);
assert.equal(form.onvif_probe_token, null);
assert.equal(form.main_validation_token, null);
assert.equal(form.sub_validation_token, null);

form = { ...form, main_validation_token: "main-2", sub_validation_token: "sub-2", onvif_probe_token: "probe-3" };
form = applyCameraFormPatch(form, "onvif_profile_token", "main-new", sensitive);
assert.equal(form.main_validation_token, null);
assert.equal(form.sub_validation_token, "sub-2");
assert.equal(form.onvif_probe_token, "probe-3");

form = { ...form, main_validation_token: "main-3", sub_validation_token: "sub-3" };
form = applyCameraFormPatch(form, "onvif_sub_profile_token", "sub-new", sensitive);
assert.equal(form.main_validation_token, "main-3");
assert.equal(form.sub_validation_token, null);

const switched = applyCameraFormPatch(
  { protocol: "rtsp", host: "camera.example.test", port: 8554, rtsp_host: "", rtsp_port: "" },
  "protocol",
  "onvif",
  sensitive
);
assert.equal(switched.rtsp_host, "camera.example.test");
assert.equal(switched.rtsp_port, 554);

const storage = new Map();
const localStorageLike = {
  getItem: (key) => storage.get(key) || null,
  setItem: (key, value) => storage.set(key, value),
};
const userA = { id: 10, username: "operator" };
const userB = { id: 11, username: "viewer" };
assert.equal(cameraViewModeStorageKey(userA), "km_vms_cameras_view_mode:10");
assert.equal(loadCamerasViewMode(localStorageLike, userA), "list");
assert.equal(saveCamerasViewMode(localStorageLike, userA, "cards"), true);
assert.equal(loadCamerasViewMode(localStorageLike, userA), "cards");
assert.equal(loadCamerasViewMode(localStorageLike, userB), "list");
storage.set(cameraViewModeStorageKey(userA), "bad-value");
assert.equal(loadCamerasViewMode(localStorageLike, userA), "list");

assert.equal(camerasPage.includes("useCurrentUser()"), true);
assert.equal(camerasPage.includes("loadCamerasViewMode(window.localStorage, currentUser"), true);
assert.equal(camerasPage.includes("saveCamerasViewMode(window.localStorage, currentUser, viewMode"), true);
assert.equal(camerasPage.includes("rtsp_port_manually_set"), false);
assert.equal(camerasPage.includes('runTest("main")'), true);
assert.equal(camerasPage.includes('runTest("sub")'), true);
assert.equal(camerasPage.includes('testResult.tested_role === "sub" ? copy.testSubOk : copy.testMainOk'), true);
assert.equal(camerasPage.includes("onvif_sub_profile_token"), true);
assert.equal(camerasPage.includes("AuthenticatedPreviewImage"), true);
assert.equal(camerasPage.includes("<img src={camera.preview_url}"), false);
assert.equal(camerasPage.includes("testResult.preview_url}?v="), false);
assert.equal(camerasPage.includes("cameraTileStatus"), false);
assert.equal(camerasPage.includes("__deleted_"), false);

assert.equal(camerasCss.includes("grid-template-columns: repeat(4, minmax(0, 1fr));"), true);
assert.equal(camerasCss.includes("gap: 21px;"), true);
assert.equal(camerasCss.includes("aspect-ratio: 260 / 148;"), true);
assert.equal(camerasCss.includes(".cameraTileStatus"), false);
assert.equal(camerasCss.includes("grid-template-columns: auto minmax(0, 1fr);"), true);
assert.equal(camerasCss.includes(".cameraProfileNameLine"), true);
assert.equal(camerasCss.includes(".cameraProfileName"), true);
assert.equal(camerasCss.includes("flex: 0 0 auto;"), true);

assert.equal(responsiveCss.includes("grid-template-columns: repeat(3, minmax(0, 1fr));"), true);
assert.equal(responsiveCss.includes("grid-template-columns: repeat(2, minmax(0, 1fr));"), true);
assert.equal(responsiveCss.includes("grid-template-columns: minmax(0, 1fr);"), true);
