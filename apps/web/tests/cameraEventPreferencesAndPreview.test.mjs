import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { mergeEventPreferenceDraft, toggleEventPreference, changeCameraNotificationTime, notificationCameraGroups } from "../lib/cameraEventPreferences.js";
import { cameraPreviewId, cameraPreviewLiveUrl } from "../lib/cameraPreviewLive.js";
import { enDictionary } from "../lib/i18n/en.js";
import { ruDictionary } from "../lib/i18n/ru.js";
import { zhCNDictionary } from "../lib/i18n/zhCN.js";
const read = path => readFileSync(new URL(path, import.meta.url), "utf8");

test("camera lists keep top-to-bottom groups of five in both schedule modes", () => {
  const cameras = Object.freeze(Array.from({ length: 16 }, (_, id) => Object.freeze({ id: id + 1, name: `Camera ${id + 1}` })));
  const groups = notificationCameraGroups(cameras);
  assert.deepEqual(groups.map(group => group.map(camera => camera.id)), [[1,2,3,4,5],[6,7,8,9,10],[11,12,13,14,15],[16]]);
  assert.deepEqual(notificationCameraGroups([]), []);
  assert.equal(notificationCameraGroups(cameras.slice(0, 5)).length, 1);
  assert.deepEqual(notificationCameraGroups(Array.from({ length: 31 }, (_, id) => ({ id }))).map(group => group.length), [5,5,5,5,5,5,1]);
  const form = read("../components/CameraEventPreferences.js"), css = read("../app/styles/35-camera-events.css");
  assert.match(form, /notificationCameraGroups\(cameras\)\.map/);
  assert.match(css, /\.cameraEventsCameraPreferences\s*\{[^}]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
  assert.doesNotMatch(css, /\.cameraEventsCameraPreferences\.hasSchedules\s*\{/);
  assert.match(css, /\.cameraEventsPreferenceIntro\s*\{[^}]*align-items: center;/);
  assert.match(form, /className="cameraEventsTimeSeparator" aria-hidden="true"/);
});

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

test("event history keeps personal preferences and cannot write the recording source", () => {
  const hub = read("../components/CameraEventsHub.js");
  assert.match(hub, /\["history", "notifications"\]/);
  assert.doesNotMatch(hub, /loadDetectors|\/detectors|\["detection"\]/);
  assert.doesNotMatch(hub, /events_enabled|CameraEventReceiving/);
  assert.match(hub, /camera-events\/preferences/);
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
