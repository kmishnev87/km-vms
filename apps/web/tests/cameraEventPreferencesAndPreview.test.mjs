import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { mergeEventPreferenceDraft, toggleEventPreference, changeCameraNotificationTime } from "../lib/cameraEventPreferences.js";
import { cameraPreviewId, cameraPreviewLiveUrl } from "../lib/cameraPreviewLive.js";
import { enDictionary } from "../lib/i18n/en.js";
import { ruDictionary } from "../lib/i18n/ru.js";
import { zhCNDictionary } from "../lib/i18n/zhCN.js";
const read = path => readFileSync(new URL(path, import.meta.url), "utf8");

test("a saved remote preference refreshes a pristine form, never erases a draft", () => {
  const previous = { notifications: true }, saved = { notifications: false };
  assert.equal(mergeEventPreferenceDraft(null, previous, saved), saved);
  assert.equal(mergeEventPreferenceDraft({ ...previous }, previous, saved), saved);
  const draft = { notifications: true, camera_ids: [42] };
  assert.equal(mergeEventPreferenceDraft(draft, previous, saved), draft);
  assert.equal(mergeEventPreferenceDraft(draft, previous, saved, true), saved);
});

test("individual camera hours preserve global defaults, another camera and list selection", () => {
  const previous = { notifications: true, camera_ids: null, start_minute: 1140, end_minute: 420,
    hours_enabled: false, camera_schedules: { "2": { start_minute: 0, end_minute: 360 } } };
  const next = changeCameraNotificationTime(previous, 1, "start_minute", 1380);
  assert.deepEqual(next.camera_schedules, { "1": { start_minute: 1380, end_minute: 420 }, "2": { start_minute: 0, end_minute: 360 } });
  assert.equal(next.hours_enabled, true);
  assert.equal(next.start_minute, previous.start_minute);
  assert.equal(next.camera_ids, null);
  assert.deepEqual(toggleEventPreference(next, "camera_ids", 1, [1, 2]).camera_ids, [2]);
  assert.deepEqual(previous.camera_schedules, { "2": { start_minute: 0, end_minute: 360 } });
});

test("event receiving is a permission-gated mirror, never a detector or personal setting", () => {
  const hub = read("../components/CameraEventsHub.js"), receiving = read("../components/CameraEventReceiving.js");
  assert.match(hub, /manage \? \["receiving"\] : \[\]/);
  assert.doesNotMatch(hub, /loadDetectors|\/detectors|\["detection"\]/);
  assert.match(hub, /apiFetch\(`\/cameras\/\$\{camera.id\}`,[\s\S]*?JSON.stringify\(\{ events_enabled \}\)/);
  assert.match(receiving, /checked=\{camera.events_enabled\}/);
  assert.match(receiving, /disabled=\{busy \|\| camera.protocol !== "onvif"\}/);
  assert.doesNotMatch(receiving, /camera.enabled|notifications|setInterval/);
});

test("preview URLs only contain a positive camera identity, never change a saved workspace", () => {
  assert.equal(cameraPreviewLiveUrl(42), "/live?camera_preview=42");
  assert.equal(cameraPreviewId("?camera_preview=42"), "42");
  for (const bad of ["", "0", "-1", "01", "1.2", "2147483648", "https://elsewhere.invalid"]) {
    assert.equal(cameraPreviewId(`?camera_preview=${bad}`), null);
    assert.equal(cameraPreviewLiveUrl(bad), null);
  }
  const page = read("../app/cameras/page.js"), live = read("../app/live/page.js");
  assert.equal((page.match(/onDoubleClick=\{\(\) => previewLive\(camera\)\}/g) || []).length, 2);
  assert.match(page, /cameraTilePreview cameraPreviewLiveTarget" onDoubleClick/);
  assert.match(page, /cameraPreviewField cameraPreviewLiveTarget"[\s\S]*?onDoubleClick/);
  assert.ok(/if \(cameraPreviewId\(window.location.search\)\) eventViewRef.current = true/.test(live), "preview mode must be temporary before workspace hydration");
  assert.match(live, /openSingleCamera\(cameraId, "preview"\)/);
});

test("all new controls and support failures have three real translations", () => {
  for (const dictionary of [enDictionary, ruDictionary, zhCNDictionary]) {
    for (const key of ["receiving", "receivingHelp", "receiveEvents", "scheduleAll", "schedulePerCamera", "scheduleMode", "previewLiveHelp"])
      assert.ok(dictionary.cameraEvents[key]?.length > 1);
    for (const key of ["event_reception_onvif_required", "event_reception_support_unconfirmed"])
      assert.ok(dictionary.cameraEvents.errors[key]?.length > 1);
  }
});
