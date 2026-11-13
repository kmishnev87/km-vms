import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../components/CameraEventsHub.js", import.meta.url), "utf8");
const requestBody = source.slice(source.indexOf("  function requestDelete() {"), source.indexOf("  async function deleteEvents()"));
const deleteBody = source.slice(source.indexOf("  async function deleteEvents()"), source.indexOf("  function changeFilter"));

test("one trash beside bulk acknowledgement, never a delete control in each row", () => {
  const header = source.slice(source.indexOf('<div className="cameraEventsSelection">'), source.indexOf('<div className="cameraEventsList"'));
  assert.match(header, /cameraEventsBulkActions/);
  assert.match(header, /action="read"[\s\S]*action="delete"/);
  assert.match(header, /manage \? <button/);
  assert.doesNotMatch(source.slice(source.indexOf("function rowActions"), source.indexOf("const historyContent")), /action="delete"/);
  assert.match(source, /presentation: "compact-confirmation"/);
  assert.match(source, /confirmTone: "danger", onConfirm: deleteEvents/);
});

test("confirmation freezes selection and uses the full snapshot, not the visible filtered count", () => {
  const selection = ["first", "second"];
  const state = { busy: false, loading: false, manage: true, selection, history: { total: 1, history_count: 40, read_cursor: "snapshot" } };
  let target;
  const context = vm.createContext({ ...state, setDeleteTarget: value => { target = value; } });
  vm.runInContext(requestBody+"requestDelete();", context);
  selection.push("later-selection");
  assert.deepEqual(Array.from(target.ids), ["first", "second"]);
  assert.equal(target.count, 2);
  context.selection = [];
  vm.runInContext("requestDelete();", context);
  assert.equal(target.through, "snapshot");
  assert.equal(target.count, 40);
  for (const field of ["busy", "loading"]) {
    target = null; context[field] = true;
    vm.runInContext("requestDelete();", context);
    assert.equal(target, null); context[field] = false;
  }
  target = null; context.manage = false;
  vm.runInContext("requestDelete();", context);
  assert.equal(target, null);
});

function runDelete({ target, current = true, fail = false }) {
  const changes = [], requests = [];
  const context = vm.createContext({ deleteTarget: target, busy: false, headers: { "Content-Type": "application/json" },
    captureAuthSession: () => () => current, act: action => action(() => current),
    apiFetch: async (path, options) => { requests.push({ path, body: JSON.parse(options.body) }); if (fail) throw Error("failed"); return { deleted: 2 }; },
    setDeleteTarget: value => changes.push(["target", value]), setSelection: value => changes.push(["selection", value]),
    setRefresh: () => changes.push(["refresh"]), setErrorToast: value => changes.push(["toast", value]),
    t: key => key, Date, CAMERA_EVENT_REFRESH: "refresh", CustomEvent: class { },
    window: { dispatchEvent: () => changes.push(["markers"]) } });
  return { result: vm.runInContext(deleteBody+"deleteEvents();", context), changes, requests };
}

test("deletion clears selection and refreshes markers only after confirmed server success", async () => {
  const run = runDelete({ target: { ids: ["first", "second"], count: 2 } });
  await run.result;
  assert.deepEqual(run.requests, [{ path: "/camera-events/delete", body: { ids: ["first", "second"] } }]);
  assert.deepEqual(run.changes.map(item => item[0]), ["target", "selection", "refresh", "markers", "toast"]);
  const all = runDelete({ target: { through: "snapshot", count: 40 } });
  await all.result;
  assert.deepEqual(all.requests, [{ path: "/camera-events/delete-all", body: { through: "snapshot" } }]);
});

test("failed response and an obsolete login never pretend that events were deleted", async () => {
  const failed = runDelete({ target: { ids: ["first"] }, fail: true });
  await assert.rejects(failed.result, /failed/);
  assert.equal(failed.changes.length, 0);
  const stale = runDelete({ target: { ids: ["first"] }, current: false });
  await stale.result;
  assert.equal(stale.changes.length, 0);
});

test("saved clips get a little more desktop height, while small-screen scrolling stays available", () => {
  const css = readFileSync(new URL("../app/styles/50-cameras-shared-modals.css", import.meta.url), "utf8");
  assert.match(css, /\.archiveExportModal\.archiveClipDialog\s*\{[^}]*height: min\(520px, calc\(100dvh - 32px\)\)/);
  assert.match(css, /\.archiveClipPanel\s*\{[^}]*overflow: auto/);
  assert.match(css, /@media \(max-width: 620px\)[\s\S]*height: min\(620px, calc\(100dvh - 32px\)\)/);
});
