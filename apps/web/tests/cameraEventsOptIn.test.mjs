import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const read = path => readFileSync(new URL(path, import.meta.url), "utf8");
const source = [read("../lib/cameraStage16.js"), read("../lib/cameraEditor.js")].join("\n")
  .replace(/^import .*;$/gm, "").replaceAll("export function ", "function ").replaceAll("export const ", "const ");
const context = { URL, Date, Set };
vm.runInNewContext(`${source}\nthis.model = { cameraEditorDraft, cameraSavePayload, patchCameraEditor };`, context);
const { cameraEditorDraft, cameraSavePayload, patchCameraEditor } = context.model;

test("camera editor does not carry the retired subscription checkbox", () => {
  assert.equal(cameraEditorDraft().events_enabled, undefined);
  const baseline = cameraEditorDraft({ id: 42, protocol: "onvif", events_enabled: true });
  const draft = { ...baseline, name: "Renamed" };
  assert.equal(draft.events_enabled, undefined);
  assert.equal("events_enabled" in cameraSavePayload(draft, baseline, 42), false);
  assert.deepEqual(JSON.parse(JSON.stringify(cameraSavePayload(draft, baseline, 42))), { name: "Renamed" });
});

test("switching to RTSP cannot retain a hidden event receiving opt-in", () => {
  const draft = patchCameraEditor(cameraEditorDraft(), "protocol", "rtsp");
  assert.equal(draft.events_enabled, undefined);
});

test("camera editor does not probe subscription capabilities unnecessarily", () => {
  const source = read("../components/cameras/useCameraEditor.js");
  assert.doesNotMatch(source, /\/cameras\/onvif\/events\/capabilities|eventCapability/);
  assert.doesNotMatch(source, /setInterval|Dahua|Hikvision|Tapo/);
});

test("only the recording center selects the events source", () => {
  const source = read("../components/cameras/CameraEditor.js");
  assert.doesNotMatch(source, /events_enabled|editorReceiveEvents/);
  assert.match(read("../lib/recordingCenter.js"), /camera_events_available/);
});

test("a rapid reopen uses the saved server choice before the background list refresh", async () => {
  const page = read("../app/cameras/page.js");
  const owner = page.slice(page.indexOf("  async function cameraSaved("), page.indexOf("  function openDeleteModal("));
  let cameras = [{ id: 42, name: "Front", events_enabled: true }], finishRefresh;
  const scope = {
    camerasRequestRef: { current: 3 },
    setCameras(update) { cameras = update(cameras); },
    setShowEditor(open) { assert.equal(open, false); assert.equal(cameras[0].events_enabled, false); },
    setEditorCamera() {}, invalidateCameraPtz() {}, notifyCameraRuntimeChanged() {},
    load() { return new Promise(resolve => { finishRefresh = resolve; }); }, loadSecondaryStatus: async () => {},
  };
  vm.runInNewContext(`${owner}\nthis.save = cameraSaved;`, scope);
  const saving = scope.save({ id: 42, name: "Front", events_enabled: false });
  assert.equal(cameras[0].events_enabled, false);
  assert.equal(scope.camerasRequestRef.current, 4);
  finishRefresh();
  await saving;
});
