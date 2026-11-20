import assert from "node:assert/strict";
import test from "node:test";
import { changeEventHistoryFilter, eventHistoryQuery, eventLabelKey, eventObjectLabelKeys, eventRuleOptionsQuery } from "../lib/cameraEvents.js";
import { filteredSelectionBody, snapshotHistoryQuery } from "../lib/cameraEventSelection.js";

test("AI condition is not mistaken for the original camera alarm", () => {
  const event = { kind: "intrusion", ai: { condition: "exit" }, object_kinds: ["person", "vehicle"] };
  assert.equal(eventLabelKey(event), "cameraEvents.ai.conditions.exit");
  assert.deepEqual(eventObjectLabelKeys(event), ["cameraEvents.kinds.person", "cameraEvents.kinds.vehicle"]);
  assert.equal(eventLabelKey({ kind:"person", ai:{ condition:"presence" } }), "cameraEvents.kinds.person");
  assert.equal(eventLabelKey({ kind:"intrusion" }), "cameraEvents.kinds.intrusion");
});

test("AI filters survive pagination and freeze into the same bulk selection", () => {
  const query = eventHistoryQuery({source:"ai", condition:"exit", ruleRef:"17:front-gate", days:30, unread:true, kind:"vehicle"});
  const paged = new URLSearchParams(snapshotHistoryQuery("", 25, { id:"test", query }));
  assert.equal(paged.get("rule_ref"), "17:front-gate");
  assert.equal(paged.get("source"), "ai");
  assert.equal(paged.get("condition"), "exit");
  const body = filteredSelectionBody(paged.toString(), "test-cursor", [{id:"visible"}]);
  assert.equal(body.source, "ai");
  assert.equal(body.rule_ref, "17:front-gate");
  assert.equal(body.condition, "exit");
  assert.equal(body.kind, "vehicle");
  assert.equal(body.unread, true);
  assert.deepEqual(body.visible_ids, ["visible"]);
});

test("leaving NAS AI clears only AI-specific conditions, while rule choices use the full period", () => {
  const input = { source: "ai", condition: "enter", ruleRef: "17:yard", camera: "17", kind: "person", days: 7, unread: true };
  const output = changeEventHistoryFilter(input, "source", "camera");
  assert.deepEqual(output, { ...input, source: "camera", condition: "", ruleRef: "" });
  assert.equal(changeEventHistoryFilter(input, "camera", "").ruleRef, "17:yard");
  assert.equal(changeEventHistoryFilter(input, "camera", "19").ruleRef, "");
  const scope = new URLSearchParams(eventRuleOptionsQuery(input));
  assert.deepEqual([...scope.keys()], ["camera_id", "since"]);
  assert.equal(scope.get("camera_id"), "17");
  const nonAiQuery = new URLSearchParams(eventHistoryQuery({ ...input, source: "" }));
  assert.equal(nonAiQuery.get("condition"), null);
  assert.equal(nonAiQuery.get("rule_ref"), null);
});
