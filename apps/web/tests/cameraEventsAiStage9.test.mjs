import assert from "node:assert/strict";
import test from "node:test";
import { eventHistoryQuery, eventLabelKey, eventObjectLabelKeys } from "../lib/cameraEvents.js";
import { filteredSelectionBody, snapshotHistoryQuery } from "../lib/cameraEventSelection.js";

test("AI condition is not mistaken for the original camera alarm", () => {
  const event = { kind: "intrusion", ai: { condition: "exit" }, object_kinds: ["person", "vehicle"] };
  assert.equal(eventLabelKey(event), "cameraEvents.ai.conditions.exit");
  assert.deepEqual(eventObjectLabelKeys(event), ["cameraEvents.kinds.person", "cameraEvents.kinds.vehicle"]);
  assert.equal(eventLabelKey({ kind:"person", ai:{ condition:"presence" } }), "cameraEvents.kinds.person");
  assert.equal(eventLabelKey({ kind:"intrusion" }), "cameraEvents.kinds.intrusion");
});

test("AI filters survive pagination and freeze into the same bulk selection", () => {
  const query = eventHistoryQuery({source:"ai", condition:"exit", rule:" Передние ворота ", days:30, unread:true, kind:"vehicle"});
  const paged = new URLSearchParams(snapshotHistoryQuery("", 25, { id:"test", query }));
  assert.equal(paged.get("rule"), "Передние ворота");
  assert.equal(paged.get("source"), "ai");
  assert.equal(paged.get("condition"), "exit");
  const body = filteredSelectionBody(paged.toString(), "test-cursor", [{id:"visible"}]);
  assert.equal(body.source, "ai");
  assert.equal(body.rule, "Передние ворота");
  assert.equal(body.condition, "exit");
  assert.equal(body.kind, "vehicle");
  assert.equal(body.unread, true);
  assert.deepEqual(body.visible_ids, ["visible"]);
});
