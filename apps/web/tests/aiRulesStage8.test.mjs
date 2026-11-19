import test from "node:test";
import assert from "node:assert/strict";
import { defaultRule, polygonValid, ruleError, timeLabel, timeMinute } from "../lib/aiRules.js";
import { draftForCamera, recordingPolicyUpdate } from "../lib/recordingCenter.js";

test("rules validate geometry and schedules without requiring legacy action switches", () => {
  const rule = defaultRule("gate");
  const config = { rules:[rule], exclusions:[], reference:null };
  assert.equal(ruleError(config), "");
  rule.record = false; rule.notify = false;
  assert.equal(ruleError(config), "");
  rule.kind = "crossing"; rule.points = [[200,200], [210,210]];
  assert.equal(ruleError(config), "rule_capture_first");
  config.reference = "frame.fixture";
  assert.equal(ruleError(config), "rule_line_invalid");
  rule.points[1] = [400, 400];
  assert.equal(ruleError(config), "");
  rule.schedule = {days:[],start:1140,end:420};
  assert.equal(ruleError(config), "rule_days_required");
  assert.equal(timeMinute(timeLabel(1140)), 1140);
  assert(!polygonValid([[200,200],[800,800],[800,200],[200,800]]));
  assert(polygonValid([[200,200],[800,300],[800,800],[200,800]]));
});

test("rule config is part of the same recording policy revision and legacy drafts do not overwrite it", () => {
  const camera = {policy:{revision:9,mode:"continuous",source:"ai",profile:"none"}};
  const draft = draftForCamera(camera);
  assert.equal("ai_rules" in recordingPolicyUpdate(camera, draft).policy, false);
  draft.ai_rules = {rules:[defaultRule("new")],exclusions:[],reference:null};
  const payload = recordingPolicyUpdate(camera, draft);
  assert.equal(payload.expected_revision, 9);
  assert.equal(payload.policy.ai_rules.rules[0].id, "new");
  assert.equal(recordingPolicyUpdate(camera, {...draft, ai_rules:null}).policy.ai_rules, null);
});
