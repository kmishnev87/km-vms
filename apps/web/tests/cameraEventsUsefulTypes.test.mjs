import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import { CAMERA_EVENT_KINDS, eventHistoryQuery, eventLabelKey, eventObjectLabelKeys } from "../lib/cameraEvents.js";
import { enDictionary } from "../lib/i18n/en.js";
import { ruDictionary } from "../lib/i18n/ru.js";
import { zhCNDictionary } from "../lib/i18n/zhCN.js";
import { changeCameraNotificationTime, toggleEventPreference } from "../lib/cameraEventPreferences.js";
const read = path => readFileSync(new URL(path, import.meta.url), "utf8");

test("eleven useful kinds match backend and have real labels in every language", () => {
  assert.deepEqual(CAMERA_EVENT_KINDS, ["motion", "person", "vehicle", "animal", "intrusion", "line_crossing", "loitering", "tamper", "audio", "wrong_direction", "fire"]);
  const backend = read("../../api/app/services/onvif_events.py").match(/EVENT_KINDS = \(([\s\S]*?)\)/)[1];
  assert.deepEqual([...backend.matchAll(/"([a-z_]+)"/g)].map(match => match[1]), CAMERA_EVENT_KINDS);
  for (const kind of CAMERA_EVENT_KINDS) {
    assert.equal(new URLSearchParams(eventHistoryQuery({ kind })).get("kind"), kind);
    assert.equal(eventLabelKey({ kind }), `cameraEvents.kinds.${kind}`);
    for (const dictionary of [enDictionary, ruDictionary, zhCNDictionary]) {
      assert.ok(dictionary.cameraEvents.kinds[kind]?.length > 1);
      assert.ok(dictionary.cameraEvents.notificationCameras);
      assert.ok(dictionary.cameraEvents.notificationTypes);
    }
  }
  for (const kind of ["other", "clock", "abandoned_object", "missing_object"]) {
    assert.equal(CAMERA_EVENT_KINDS.includes(kind), false);
    assert.equal(new URLSearchParams(eventHistoryQuery({ kind })).get("kind"), null);
  }
});

test("a rule retains all reported object classes without repeating its primary label", () => {
  assert.deepEqual(eventObjectLabelKeys({ kind: "line_crossing", object_kinds: ["person", "vehicle"] }), ["cameraEvents.kinds.person", "cameraEvents.kinds.vehicle"]);
  assert.deepEqual(eventObjectLabelKeys({ kind: "person", object_kind: "person" }), []);
  assert.deepEqual(eventObjectLabelKeys({ kind: "motion", object_kind: "animal" }), ["cameraEvents.kinds.animal"]);
  assert.deepEqual(eventObjectLabelKeys({ kind: "other" }), []);
});

test("preferences use aligned equal columns and a stationary save action, never label-dependent wrapping", () => {
  const hub = read("../components/CameraEventsHub.js"), form = read("../components/CameraEventPreferences.js"), css = read("../app/styles/35-camera-events.css");
  assert.match(css, /\.cameraEventsChoices\s*\{\s*display: grid;\s*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.cameraEventsPreferences fieldset\s*\{[^}]*padding: 0;[^}]*border: 0;/);
  assert.match(css, /\.cameraEventsPreferenceFooter\s*\{[^}]*flex-shrink: 0;/);
  assert.match(css, /\.cameraEventsPreferenceScroll\s*\{[^}]*overflow: auto;/);
  assert.match(css, /@media \(max-width: 480px\)[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
  const none = toggleEventPreference({ camera_ids: null }, "camera_ids", 1, [1]);
  assert.deepEqual(none.camera_ids, []);
  assert.match(form, /toggleEventPreference/);
  assert.match(hub, /eventObjectLabelKeys\(row\)/);
  assert.match(hub, /eventObjectLabelKeys\(eventToast\)/);
  assert.match(form, /fieldset disabled=\{busy\}/);
});

test("compact time controls stay editable while scheduling is off and selecting a time enables it", () => {
  const form = read("../components/CameraEventPreferences.js"), css = read("../app/styles/35-camera-events.css");
  assert.doesNotMatch(form, /disabled=\{busy \|\| !preference\.hours_enabled\}/);
  assert.match(css, /\.cameraEventsTimeRange > label\s*\{[^}]*display: inline-flex;[^}]*align-items: center;/);
  assert.match(css, /\.cameraEventsTimeInput\s*\{\s*width: 62px; min-width: 0; height: 30px;/);
  assert.match(css, /\.cameraEventsTimeInput::-webkit-calendar-picker-indicator\s*\{\s*display: none;/);
  assert.match(css, /\.cameraEventsSchedule\s*\{[^}]*justify-content: flex-start;/);
  let preference = { hours_enabled: false, start_minute: 0, end_minute: 0 };
  preference = changeCameraNotificationTime(preference, 1, "start_minute", 1335);
  preference = changeCameraNotificationTime(preference, 1, "end_minute", 390);
  assert.equal(preference.hours_enabled, true);
  assert.equal(preference.camera_schedules[1].start_minute, 1335);
  assert.equal(preference.camera_schedules[1].end_minute, 390);
  assert.match(form, /try \{ event.currentTarget.showPicker\?\.\(\); \} catch/);
  assert.match(form, /event.key === "Escape"\) event.stopPropagation\(\)/);
});
