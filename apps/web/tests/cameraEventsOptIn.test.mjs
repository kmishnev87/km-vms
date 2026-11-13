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

test("new cameras opt out, saved choice survives an ordinary name change", () => {
  assert.equal(cameraEditorDraft().events_enabled, false);
  const baseline = cameraEditorDraft({ id: 42, protocol: "onvif", events_enabled: true });
  const draft = { ...baseline, name: "Renamed" };
  assert.equal(draft.events_enabled, true);
  assert.equal("events_enabled" in cameraSavePayload(draft, baseline, 42), false);
  assert.deepEqual(JSON.parse(JSON.stringify(cameraSavePayload({ ...baseline, events_enabled: false }, baseline, 42))), { events_enabled: false });
});

test("switching to RTSP cannot retain a hidden event receiving opt-in", () => {
  const draft = patchCameraEditor({ ...cameraEditorDraft(), events_enabled: true }, "protocol", "rtsp");
  assert.equal(draft.events_enabled, false);
});

test("capabilities are read once for the exact management identity with stale request cancellation", () => {
  const source = read("../components/cameras/useCameraEditor.js");
  assert.match(source, /\/cameras\/onvif\/events\/capabilities/);
  assert.match(source, /eventCapability\?\.key === eventManagementKey/);
  assert.match(source, /return \(\) => controller\.abort\(\)/);
  assert.doesNotMatch(source, /setInterval|Dahua|Hikvision|Tapo/);
});

test("unsupported devices are not offered a fake switch, existing choices can always be turned off", () => {
  const source = read("../components/cameras/CameraEditor.js");
  assert.match(source, /capability\?\.supported === true/);
  assert.match(source, /supported \|\| canDisable/);
  assert.match(source, /patch\("events_enabled", event.target.checked\)/);
  assert.match(source, /form.protocol !== "onvif"/);
  assert.match(source, /copy.editorReceiveEventsHelp/);
  const css = read("../app/styles/50-cameras-shared-modals.css");
  assert.match(css, /\.cameraFlowEventControl\s*\{[^}]*min-height: 34px/);
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
