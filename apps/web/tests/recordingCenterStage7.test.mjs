import assert from "node:assert/strict";
import test from "node:test";
import { aiPhaseKey, diagnosisKey, draftForCamera, inlinePolicyControls, profilesFor, recordingPolicyUpdate,
  updateMode, updateSource, validateRecordingDraft } from "../lib/recordingCenter.js";

const rtsp = { camera_id: 7, protocol: "rtsp", events_enabled: false, recording_enabled: true,
  policy: { revision: 2, mode: "continuous", source: "none" }, runtime: { ai_available: true } };

test("camera event types are independent of the scenario and its buffering controls", () => {
  const row = { ...rtsp, protocol: "onvif", camera_events_available: true,
    policy:{ revision:2, mode:"continuous", source:"camera", event_kinds:["person"] } };
  const continuous = draftForCamera(row);
  assert.equal(inlinePolicyControls([row], row, continuous).cameraConditions, true);
  assert.equal(inlinePolicyControls([row], row, continuous).profiled, false);
  const events = updateMode(continuous, "events", row);
  assert.equal(inlinePolicyControls([row], row, events).profiled, true);
  assert.deepEqual(updateMode(events, "continuous", row).event_kinds, ["person"]);
  assert.equal(validateRecordingDraft(row, {...continuous, event_kinds:[]}, []), "event_select_one");
});

test("AI is available without ONVIF, never defaults to scene change or economy, and keeps shared revisions", () => {
  const draft = updateMode(draftForCamera(rtsp), "events", rtsp);
  assert.equal(draft.source, "ai");
  assert.equal(draft.profile, "balanced");
  assert.deepEqual(profilesFor(draft), ["balanced", "full_quality"]);
  assert.deepEqual(draft.event_kinds, ["person", "vehicle", "animal"]);
  assert.equal(validateRecordingDraft(rtsp, draft, []), "");
  assert.equal(validateRecordingDraft(rtsp, { ...draft, profile: "economical" }, []), "profile");
  assert.equal(recordingPolicyUpdate(rtsp, draft).expected_revision, 2);
  assert.equal(recordingPolicyUpdate(rtsp, draft).policy.source, "ai");
  const continuous = updateSource(draftForCamera(rtsp), "ai");
  assert.equal(recordingPolicyUpdate(rtsp, continuous).policy.profile, "none");
  assert.equal(validateRecordingDraft(rtsp, { ...draft, rule_ids: ["future-zone"] }, []), "ai_rules_pending");
});

test("phase and diagnosis do not mistake an AI frame failure for a camera event channel failure", () => {
  assert.equal(aiPhaseKey({ runtime: { ai: { phase: "connecting" } } }), "status_analysis_starting");
  assert.equal(aiPhaseKey({ runtime: { ai: { phase: "healthy", backend: "cpu" } } }), "ai_running");
  assert.equal(diagnosisKey({ runtime: { ai: { reason: "no_usable_frames" }, actual_status: "source_fault" } }), "ai_no_frames");
});
