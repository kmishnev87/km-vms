import assert from "node:assert/strict";
import test from "node:test";
import {
  draftForCamera, hasPolicyChanges, inlinePolicyControls, recordingPolicyUpdate, updateMode, updateSource,
} from "../lib/recordingCenter.js";

function camera(overrides = {}) {
  return {
    camera_id: 1, protocol: "onvif", camera_events_available: true, alert_quiet_minutes: 1,
    policy: {revision: 2, mode: "continuous", source: "camera", profile: "none", event_kinds: ["motion"]},
    runtime: {ai_available: true}, ...overrides,
  };
}

for (const source of ["camera", "ai"]) {
  test(`continuous with ${source} uses the alert interval without a recording profile`, () => {
    const row = camera();
    const draft = updateSource(draftForCamera(row), source);
    const controls = inlinePolicyControls([row], row, draft);
    assert.equal(controls.alertProfile, true);
    assert.equal(controls.preRoll, false);
    assert.equal(controls.profiled, false);
    const chosen = {...draft, alert_quiet_minutes: 4};
    assert.equal(hasPolicyChanges(row, chosen), true);
    const payload = recordingPolicyUpdate(row, chosen);
    assert.equal(payload.alert_quiet_minutes, 4);
    assert.equal(payload.policy.profile, "none");
    assert.equal(payload.policy.prebuffer_seconds, 0);
  });
}

test("alert interval is independent from the event recording duration and absent on older NAS", () => {
  const row = camera({alert_quiet_minutes: 5});
  const draft = {...updateMode(draftForCamera(row), "events", row), postrecord_minutes: 2};
  assert.equal(inlinePolicyControls([row], row, draft).alertProfile, false);
  const payload = recordingPolicyUpdate(row, draft);
  assert.equal(payload.policy.postrecord_minutes, 2);
  assert.equal(Object.hasOwn(payload, "alert_quiet_minutes"), false);
  const legacy = camera({alert_quiet_minutes: undefined});
  assert.equal(inlinePolicyControls([legacy], legacy, draftForCamera(legacy)).alertProfile, false);
  const noAnalytics = updateSource(draftForCamera(row), "none");
  assert.equal(inlinePolicyControls([row], row, noAnalytics).alertProfile, false);
  assert.equal(Object.hasOwn(recordingPolicyUpdate(row, noAnalytics), "alert_quiet_minutes"), false);
});
