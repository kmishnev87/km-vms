import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { test } from "node:test";
import { eventLabelKey } from "../lib/cameraEvents.js";

const read = path => readFileSync(new URL(path, import.meta.url), "utf8");

test("standard IVS topics name the rule without inventing an object class", () => {
  for (const [topic, label] of [
    ["RuleEngine/FieldDetector/ObjectsInside", "objectInZone"],
    ["tns1:RuleEngine/tns1:LineDetector/Crossed", "lineCrossing"],
    ["RuleEngine/LoiteringDetector/ObjectIsLoitering", "loitering"],
  ]) assert.equal(eventLabelKey({ kind: "other", topic }), `cameraEvents.rules.${label}`);
  assert.equal(eventLabelKey({ kind: "person", topic: "RuleEngine/FieldDetector/ObjectsInside" }), "cameraEvents.kinds.person");
  assert.equal(eventLabelKey({ kind: "vehicle", topic: "RuleEngine/FieldDetector/ObjectsInside" }), "cameraEvents.kinds.vehicle");
  assert.equal(eventLabelKey({ kind: "other", topic: "Unfamiliar/ObjectsInside" }), "cameraEvents.kinds.other");
  assert.equal(eventLabelKey(null), "cameraEvents.kinds.other");
});

test("one bulk icon outside the list replaces disappearing per-row read actions", () => {
  const hub = read("../components/CameraEventsHub.js");
  const actions = hub.slice(hub.indexOf("function rowActions"), hub.indexOf("const historyContent"));
  assert.doesNotMatch(actions, /readSelection|markRead|row\.read/);
  assert.equal((actions.match(/className="cameraEventsIconButton"/g) || []).length, 2);
  assert.match(hub, /selection\.length \? readSelection\(selection\) : readAll\(\)/);
  assert.match(hub, /apiFetch\("\/camera-events\/read-all"/);
  assert.match(hub, /JSON\.stringify\(\{ through \}\)/);
  assert.match(hub, /\.indeterminate = selection\.length > 0 && !allSelected/);
  assert.match(hub, /t\(eventLabelKey\(row\)\)/);
  assert.match(hub, /t\(eventLabelKey\(eventToast\)\)/);
  assert.match(read("../components/ChronologyTimeline.js"), /t\(eventLabelKey\(event\)\)/);
});

test("unread state only changes a fixed-size marker, never row controls or geometry", () => {
  const css = read("../app/styles/35-camera-events.css");
  assert.match(css, /\.cameraEventsRow\.isUnread \.cameraEventsReadIndicator\s*\{\s*opacity:\s*1;/);
  assert.match(css, /\.cameraEventsReadIndicator\s*\{[^}]*width:\s*8px;[^}]*height:\s*8px;/);
  assert.doesNotMatch(css, /\.cameraEventsRow\.isUnread\s*\{/);
  for (const selector of ["cameraEventsSelection", "cameraEventsRow"])
    assert.match(css, new RegExp(`\\.${selector}\\s*\\{[^}]*padding:[^}]*var\\(--camera-events-row-padding\\)`));
});

test("Live event handoff fits just that camera and cannot overwrite the ordinary saved layout", () => {
  const source = read("../app/live/page.js");
  const body = source.match(/useCameraEventHandoff\(\{ ready:[\s\S]*?onTarget: target => \{([\s\S]*?)\n    \},\n  \}\);/)[1];
  let fitted = false, result, cancelled = false, invalidated = false;
  const eventViewRef = { current: false }, pendingLayoutSaveRef = { current: () => assert.fail("must not save") };
  vm.runInNewContext(`(target => {${body}})({ camera_id: 42 })`, {
    cameraMap: new Map([["42", { id: 42 }]]), defaultStream: () => "sub", nextZIndex: () => 3,
    tiles: [{ id: "ordinary", cameraId: "99" }], eventViewRef, pendingLayoutSaveRef,
    saveTimerRef: { current: 123 }, clearTimeout: () => { cancelled = true; },
    layoutWriterRef: { current: { invalidate: () => { invalidated = true; } } },
    setFullscreenTileId: () => {}, closePtz: () => {},
    setFrameLayoutActive: value => { fitted = value; }, setTiles: value => { result = value; },
  });
  assert.equal(eventViewRef.current, true);
  assert.equal(fitted, true);
  assert.equal(cancelled, true);
  assert.equal(invalidated, true);
  assert.equal(pendingLayoutSaveRef.current, null);
  assert.equal(result.length, 1);
  assert.equal(result[0].cameraId, "42");
  assert.equal(result[0].stream, "sub");
});
