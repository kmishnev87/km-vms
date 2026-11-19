import assert from "node:assert/strict";
import test from "node:test";
import {
  diagnosisKey, draftForCamera, linkCandidates, recordingPolicyUpdate, sourceWritesContinuously,
  statusView, updateMode, updateProfile, validateRecordingDraft,
} from "../lib/recordingCenter.js";

const build = (camera_id, policy = {}, overwrites = {}) => ({
  camera_id, recording_enabled: true, name: `Camera ${camera_id}`, protocol: "onvif", events_enabled: true,
  policy: { mode: "continuous", source: "camera", revision: 3, ...policy },
  runtime: { actual_status: "recorder_pending", legacy_recording_mode: "always" }, ...overwrites,
});

test("mode changes preserve the NAS revision and shape the buffers of all three profiles", () => {
  const row = build(1);
  const event = updateMode(draftForCamera(row), "events");
  assert.equal(event.profile, "economical");
  assert.equal(recordingPolicyUpdate(row, event).policy.prebuffer_seconds, 0);
  const balanced = updateProfile(event, "balanced");
  assert.equal(balanced.prebuffer_seconds, 30);
  const full = { ...updateProfile(balanced, "full_quality"), postrecord_minutes: 5, event_kinds: ["person"] };
  assert.equal(validateRecordingDraft(row, full, []), "");
  assert.deepEqual(recordingPolicyUpdate(row, full), { expected_revision: 3, policy: {
    mode: "events", source: "camera", profile: "full_quality", prebuffer_seconds: 30,
    postrecord_minutes: 5, event_kinds: ["person"], rule_ids: [], linked_camera_id: null,
  } });
  assert.equal(validateRecordingDraft(row, { ...full, prebuffer_seconds: 121 }, []), "prehistory");
  assert.equal(validateRecordingDraft(row, { ...full, event_kinds: [] }, []), "event_select_one");
  assert.equal(validateRecordingDraft({ ...row, events_enabled: false }, full, []), "event_requires");
  assert.equal(validateRecordingDraft({ ...row, protocol: "rtsp" }, full, []), "event_unsupported");
  assert.equal(validateRecordingDraft(row, { ...full, source: "ai" }, []), "ai_unavailable");
});

test("only viable sources are offered, including unrelated cameras and their valid chains", () => {
  const linked = (camera_id, linked_camera_id) => build(camera_id, { mode: "linked", linked_camera_id, source: "none" });
  const rows = [build(1), linked(2, 1), linked(3, 2), linked(4, 5), linked(5, 4), build(6), linked(7, 6), linked(8, 77)];
  assert.deepEqual(linkCandidates(rows, 1).map(row => row.camera_id), [6, 7]);
  assert.equal(sourceWritesContinuously(rows, 7, 1), true);
  const draft = { ...updateMode(draftForCamera(rows[0]), "linked"), linked_camera_id: 7 };
  assert.equal(validateRecordingDraft(rows[0], draft, linkCandidates(rows, 1)), "");
  assert.equal(recordingPolicyUpdate(rows[0], draft).policy.source, "none");
});

test("status badges use recorder evidence and never turn wait, pause or disk block into recording", () => {
  const view = (actual_status, emergency_recording = false, recording_enabled = true) => statusView(build(1, {}, {
    recording_enabled, runtime: { actual_status, emergency_recording, emergency_requested: true },
  }));
  assert.deepEqual(view("waiting"), { key: "status_waiting", tone: "quiet" });
  assert.equal(view("recording", true, false).key, "status_paused");
  assert.equal(view("storage_suspended", true).key, "status_storage_suspended");
  assert.equal(view("starting").key, "status_starting");
  assert.equal(view("recording", true).key, "emergency_active");
  assert.equal(view("recording").key, "status_recording");
  assert.equal(diagnosisKey({ runtime: { actual_status: "waiting" } }), null);
  assert.equal(diagnosisKey({ runtime: { incident: { reason: "timeout" } } }), "reason_timeout");
});
