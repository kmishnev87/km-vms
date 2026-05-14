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
this.isRtspPortManualOverrideForEdit = isRtspPortManualOverrideForEdit;
this.loadCamerasViewMode = loadCamerasViewMode;
this.normalizeCameraPort = normalizeCameraPort;
this.saveCamerasViewMode = saveCamerasViewMode;
this.smartRtspPort = smartRtspPort;`,
  context
);

const {
  applyCameraFormPatch,
  cameraViewModeStorageKey,
  isRtspPortManualOverrideForEdit,
  loadCamerasViewMode,
  normalizeCameraPort,
  saveCamerasViewMode,
  smartRtspPort,
} = context;
const camerasPage = fs.readFileSync(resolve(__dirname, "../app/cameras/page.js"), "utf8");
const camerasCss = fs.readFileSync(resolve(__dirname, "../app/styles/50-cameras-shared-modals.css"), "utf8");
const responsiveCss = fs.readFileSync(resolve(__dirname, "../app/styles/60-responsive-shared.css"), "utf8");

const sensitive = new Set(["port", "rtsp_port"]);

assert.equal(normalizeCameraPort(null), null);
assert.equal(normalizeCameraPort(""), null);
assert.equal(normalizeCameraPort("0"), null);
assert.equal(normalizeCameraPort(" 20003 "), 20003);

let form = {
  protocol: "onvif",
  host: "camera.example.test",
  port: 80,
  rtsp_host: "camera.example.test",
  rtsp_port: 80,
  rtsp_port_manually_set: false,
  preview_token: "preview",
  validation_token: "validation",
  onvif_probe_token: "probe",
  manual_confirm_unverified: true,
};

form = applyCameraFormPatch(form, "port", 2020, sensitive);
assert.equal(form.rtsp_port, 2020);
assert.equal(form.preview_token, null);
assert.equal(form.validation_token, null);

form = applyCameraFormPatch(form, "rtsp_port", 554, sensitive);
assert.equal(form.rtsp_port, 554);
assert.equal(form.rtsp_port_manually_set, true);

form = applyCameraFormPatch(form, "port", 2021, sensitive);
assert.equal(form.port, 2021);
assert.equal(form.rtsp_port, 554);
assert.equal(smartRtspPort(form, 2021), 554);

assert.equal(isRtspPortManualOverrideForEdit(20003, 20003), false);
assert.equal(isRtspPortManualOverrideForEdit("20003", "20003"), false);
assert.equal(isRtspPortManualOverrideForEdit(20003, null), false);
assert.equal(isRtspPortManualOverrideForEdit(20003, ""), false);
assert.equal(isRtspPortManualOverrideForEdit(20003, 0), false);
assert.equal(isRtspPortManualOverrideForEdit(2020, 554), true);

let editSamePort = {
  protocol: "onvif",
  port: 20003,
  rtsp_port: 20003,
  rtsp_port_manually_set: isRtspPortManualOverrideForEdit(20003, 20003),
};
assert.equal(editSamePort.rtsp_port_manually_set, false);
editSamePort = applyCameraFormPatch(editSamePort, "port", 20004, sensitive);
assert.equal(editSamePort.rtsp_port, 20004);

let editDifferentPort = {
  protocol: "onvif",
  port: 2020,
  rtsp_port: 554,
  rtsp_port_manually_set: isRtspPortManualOverrideForEdit(2020, 554),
};
assert.equal(editDifferentPort.rtsp_port_manually_set, true);
editDifferentPort = applyCameraFormPatch(editDifferentPort, "port", 2021, sensitive);
assert.equal(editDifferentPort.rtsp_port, 554);

let editMissingRtsp = {
  protocol: "onvif",
  port: 20003,
  rtsp_port: 554,
  rtsp_port_manually_set: isRtspPortManualOverrideForEdit(20003, null),
};
assert.equal(editMissingRtsp.rtsp_port_manually_set, false);
editMissingRtsp = applyCameraFormPatch(editMissingRtsp, "port", 20004, sensitive);
assert.equal(editMissingRtsp.rtsp_port, 20004);

let editMissingRtspWithUrlFallback = {
  protocol: "onvif",
  port: 20003,
  rtsp_port: 554,
  rtsp_port_manually_set: isRtspPortManualOverrideForEdit(20003, undefined),
};
assert.equal(editMissingRtspWithUrlFallback.rtsp_port_manually_set, false);
editMissingRtspWithUrlFallback = applyCameraFormPatch(editMissingRtspWithUrlFallback, "port", 20004, sensitive);
assert.equal(editMissingRtspWithUrlFallback.rtsp_port, 20004);

const dahua = applyCameraFormPatch(
  { protocol: "onvif", port: 37777, rtsp_port: 37777, rtsp_port_manually_set: false },
  "port",
  37777,
  new Set()
);
assert.equal(dahua.rtsp_port, 37777);

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
assert.equal(camerasPage.includes("delete payload.rtsp_port_manually_set"), true);
assert.equal(camerasPage.includes("const explicitEditRtspPort = camera.rtsp_port;"), true);
assert.equal(camerasPage.includes("isRtspPortManualOverrideForEdit(camera.port, explicitEditRtspPort)"), true);
assert.equal(camerasPage.includes("isRtspPortManualOverrideForEdit(camera.port, editRtspPort)"), false);
assert.equal(camerasPage.includes("cameraTileStatus"), false);
assert.equal(camerasPage.includes("__deleted_"), false);

assert.equal(camerasCss.includes("grid-template-columns: repeat(4, minmax(0, 1fr));"), true);
assert.equal(camerasCss.includes("gap: 21px;"), true);
assert.equal(camerasCss.includes("aspect-ratio: 260 / 148;"), true);
assert.equal(camerasCss.includes(".cameraTileStatus"), false);
assert.equal(camerasCss.includes("grid-template-columns: auto minmax(0, 1fr);"), true);

assert.equal(responsiveCss.includes("grid-template-columns: repeat(3, minmax(0, 1fr));"), true);
assert.equal(responsiveCss.includes("grid-template-columns: repeat(2, minmax(0, 1fr));"), true);
assert.equal(responsiveCss.includes("grid-template-columns: minmax(0, 1fr);"), true);
