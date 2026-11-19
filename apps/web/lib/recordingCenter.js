import { CAMERA_EVENT_KINDS } from "./cameraEvents.js";
import { ruleError } from "./aiRules.js";

export const RECORDING_PROFILES = ["economical", "balanced", "full_quality"];
export const AI_EVENT_KINDS = ["person", "vehicle", "animal", "motion"];
const DEFAULT_AI_EVENT_KINDS = AI_EVENT_KINDS.filter(kind => kind !== "motion");
const VISIBLE_STATES = [
  "paused", "waiting", "waiting_linked_source", "linked_source_unavailable",
  "linked_source_fault", "source_fault", "events_disabled", "recorder_pending",
  "starting", "restarting", "finalizing", "recording", "extended", "idle",
  "storage_suspended", "storage_waiting", "error", "analysis_starting",
];
const AI_SOURCE_FAULTS = new Set(["failed", "stalled", "recovering", "exhausted"]);
export const canUseCameraEvents = (row) => (row?.camera_events_available ?? row?.events_enabled) === true && row?.protocol === "onvif";
export const canUseAI = (row) => row.runtime?.ai_available === true;
export const eventKinds = (draft) => draft.source === "ai" ? AI_EVENT_KINDS : CAMERA_EVENT_KINDS;
export const profilesFor = (draft) => draft.source === "ai" ? RECORDING_PROFILES.slice(1) : RECORDING_PROFILES;

export function modeKey(policy) {
  return ["continuous", "events", "linked"].includes(policy?.mode) ? policy.mode : "continuous";
}

export function statusView(row) {
  const runtime = row.runtime || {};
  const state = !row.recording_enabled ? "paused" : runtime.actual_status;
  if (["recording", "extended"].includes(state) && runtime.emergency_recording === true) {
    return { key: "emergency_active", tone: "warn" };
  }
  if (["recording", "extended"].includes(state) && AI_SOURCE_FAULTS.has(runtime.ai?.phase)) {
    return { key: "status_recording_ai_fault", tone: "warn" };
  }
  if (state === "source_fault" && runtime.ai_managed) return { key: "status_ai_fault", tone: "alert" };
  if (state === "linked_source_unavailable" && runtime.source_status === "source_events_disabled") {
    return { key: "status_link_events_disabled", tone: "quiet" };
  }
  if (["waiting", "recording", "extended"].includes(state) && runtime.ai?.rules_view === "checking_profile") {
    return { key: "rule_view_profile_short", tone: "warn" };
  }
  if (state === "waiting" && ["view_unconfirmed", "invalid_rules"].includes(runtime.ai?.rules_view)) {
    return { key: "rule_view_short", tone: "warn" };
  }
  const known = VISIBLE_STATES.includes(state) ? state : "recorder_pending";
  return {
    key: "status_" + known,
    tone: ["recording", "extended"].includes(known) ? "recording"
      : ["source_fault", "linked_source_unavailable", "linked_source_fault", "error"].includes(known)
        ? "alert" : ["restarting", "storage_suspended", "storage_waiting"].includes(known) ? "warn" : "quiet",
  };
}

export function draftForCamera(row) {
  const policy = row.policy || {};
  return {
    mode: modeKey(policy), source: policy.source || "none",
    profile: policy.profile || "none",
    prebuffer_seconds: policy.prebuffer_seconds ?? 0,
    postrecord_minutes: policy.mode === "events" ? policy.postrecord_minutes ?? 1 : 1,
    alert_quiet_minutes: row.alert_quiet_minutes ?? null,
    linked_camera_id: policy.linked_camera_id ?? null,
    event_kinds: (policy.event_kinds || eventKinds(policy)).filter((kind) => eventKinds(policy).includes(kind)),
    rule_ids: [...(policy.rule_ids || [])],
    ...(policy.ai_rules ? { ai_rules: policy.ai_rules } : {}),
  };
}

export function updateMode(draft, mode, row) {
  if (draft.mode === mode) return draft;
  const selected = {
    ...draft, mode,
    source: mode === "events" ? "camera" : mode === "continuous" && draft.mode !== "linked" ? draft.source : "none",
    profile: mode === "continuous" ? "none" : RECORDING_PROFILES.includes(draft.profile) ? draft.profile : "economical",
    linked_camera_id: null, rule_ids: [], prebuffer_seconds: mode === "continuous" || draft.profile === "none" ? 0 : draft.prebuffer_seconds,
  };
  if (mode === "events" && (draft.source === "ai" || row && !canUseCameraEvents(row) && canUseAI(row))) {
    return updateSource(selected, "ai", draft.source === "ai");
  }
  return selected;
}

export function updateSource(draft, source, retainScene = draft.source === "ai") {
  const fallback = source === "ai" && draft.mode === "events" && !profilesFor({ source }).includes(draft.profile);
  const selected = { ...draft, source, rule_ids: [], profile: fallback ? "balanced" : draft.profile,
    prebuffer_seconds: fallback ? 30 : draft.prebuffer_seconds };
  const matches = draft.event_kinds.filter(kind => eventKinds(selected).includes(kind)
    && (source !== "ai" || retainScene || kind !== "motion"));
  return { ...selected, event_kinds: matches.length ? matches : [...(source === "ai" ? DEFAULT_AI_EVENT_KINDS : eventKinds(selected))] };
}

export function updateProfile(draft, profile) {
  return { ...draft, profile, prebuffer_seconds: profile === "economical" ? 0
    : draft.profile === "economical" ? 30 : draft.prebuffer_seconds };
}

function followUltimate(rows, candidate, dependentId) {
  const byId = new Map(rows.map((row) => [row.camera_id, row]));
  const seen = new Set([dependentId]);
  let current = candidate;
  while (current) {
    if (seen.has(current.camera_id)) return null;
    seen.add(current.camera_id);
    if (current.policy?.mode !== "linked") return current;
    current = byId.get(current.policy.linked_camera_id);
  }
  return null;
}

export function linkCandidates(rows, dependentId) {
  return rows.filter((row) => followUltimate(rows, row, dependentId) !== null);
}

export function sourceWritesContinuously(rows, sourceId, dependentId) {
  const root = followUltimate(rows, rows.find((row) => row.camera_id === sourceId), dependentId);
  return Boolean(root && root.policy?.mode !== "events" && !root.runtime?.event_recording_available
    && root.runtime?.legacy_recording_mode === "always");
}

export function inlinePolicyControls(rows, row, draft) {
  const linked = draft.mode === "linked";
  const events = draft.mode === "events";
  const candidates = linked ? linkCandidates(rows, row.camera_id) : [];
  const linkReady = linked && candidates.some(item => item.camera_id === draft.linked_camera_id);
  const constantSource = linkReady && sourceWritesContinuously(rows, draft.linked_camera_id, row.camera_id);
  const cameraConditions = draft.source === "camera" && canUseCameraEvents(row);
  const ownEventSource = events && (cameraConditions || draft.source === "ai" && canUseAI(row));
  const profiled = ownEventSource || linkReady && !constantSource;
  const alertProfile = !linked && !events && draft.alert_quiet_minutes != null
    && (cameraConditions || draft.source === "ai" && canUseAI(row));
  return {
    linked, events, candidates, constantSource, profiled, linkReady, ownEventSource, cameraConditions, alertProfile,
    preRoll: profiled && ["balanced", "full_quality"].includes(draft.profile),
    sources: [!events ? "none" : null, canUseCameraEvents(row) || draft.source === "camera" ? "camera" : null,
      canUseAI(row) || draft.source === "ai" ? "ai" : null].filter(Boolean),
    aiEditor: !linked && draft.source === "ai" && canUseAI(row),
  };
}

export function updateLinkedSource(rows, row, draft, linked_camera_id) {
  const next = { ...draft, linked_camera_id };
  return sourceWritesContinuously(rows, linked_camera_id, row.camera_id) ? updateProfile(next, "economical") : next;
}

export function hasPolicyChanges(row, draft) {
  const comparable = (value) => {
    const { ai_rules, ...plain } = recordingPolicyUpdate(row, value).policy;
    return JSON.stringify({...plain,ai_rules:ai_rules ?? null});
  };
  return comparable(draft) !== comparable(draftForCamera(row)) ||
    draft.source !== "none" && !draft.event_kinds.length;
}

export function stageRecordingDraft(entries, row, draft) {
  const current = entries[row.camera_id];
  if (!current && !hasPolicyChanges(row, draft)) return entries;
  if (current && current.revision === row.policy.revision && !hasPolicyChanges(row, draft)) {
    return dropRecordingDraft(entries, row.camera_id);
  }
  return { ...entries, [row.camera_id]: { revision: current?.revision ?? row.policy.revision, draft } };
}

export function dropRecordingDraft(entries, id) {
  if (!(id in entries)) return entries;
  const { [id]: removed, ...others } = entries;
  return others;
}

export function inlineSaveProblem(rows, row, entry) {
  if (entry.revision !== row.policy.revision) return "row_changed";
  return validateRecordingDraft(row, entry.draft, inlinePolicyControls(rows, row, entry.draft).candidates);
}

export function validateRecordingDraft(row, draft, candidates) {
  if (draft.source === "ai" && !canUseAI(row)) return "ai_unavailable";
  if (draft.source === "ai" && draft.rule_ids.length) return "ai_rules_pending";
  if (draft.source === "ai" && draft.ai_rules) {
    const issue = ruleError(draft.ai_rules);
    if (issue) return issue;
  }
  if (draft.mode === "events" && !["camera", "ai"].includes(draft.source)) return "source_choose";
  if (draft.source === "camera" && !canUseCameraEvents(row)) {
    return row.protocol === "onvif" ? "event_requires" : "event_unsupported";
  }
  if (draft.mode === "linked" && !candidates.some((source) => source.camera_id === draft.linked_camera_id)) {
    return draft.linked_camera_id ? "source_missing" : "source_choose";
  }
  if (draft.source !== "none" && !(draft.source === "ai" && draft.ai_rules) && !draft.event_kinds.length) return "event_select_one";
  if (draft.mode !== "continuous" && !profilesFor(draft).includes(draft.profile)) return "profile";
  if (draft.mode !== "continuous" && draft.profile !== "economical"
      && (!Number.isInteger(draft.prebuffer_seconds) || draft.prebuffer_seconds < 0 || draft.prebuffer_seconds > 120)) return "prehistory";
  return "";
}

export function recordingPolicyUpdate(camera, draft) {
  const ownEvents = draft.mode === "events";
  const linked = draft.mode === "linked";
  const { alert_quiet_minutes, ...policyDraft } = draft;
  const alertProfile = draft.mode === "continuous" && draft.source !== "none" && alert_quiet_minutes != null;
  return {
    expected_revision: camera.policy.revision,
    ...(alertProfile ? { alert_quiet_minutes } : {}),
    policy: {
      ...policyDraft,
      source: linked ? "none" : draft.source,
      linked_camera_id: linked ? draft.linked_camera_id : null,
      postrecord_minutes: ownEvents ? draft.postrecord_minutes : alertProfile ? alert_quiet_minutes : 1,
      profile: draft.mode === "continuous" ? "none" : draft.profile,
      prebuffer_seconds: draft.mode === "continuous" || draft.profile === "economical" ? 0 : draft.prebuffer_seconds,
      event_kinds: draft.event_kinds.length ? draft.event_kinds : draft.source === "ai" ? DEFAULT_AI_EVENT_KINDS : eventKinds(draft),
      rule_ids: ownEvents ? draft.rule_ids : [],
    },
  };
}

export function saveErrorKey(error) {
  switch (error?.code) {
    case "recording_policy_changed":
    case "emergency_setting_changed":
    case "recording_emergency_changed":
      return "error_changed";
    case "recording_link_source_unavailable":
      return "source_missing";
    case "recording_link_cycle":
      return "reason_cycle";
    case "camera_event_reception_required":
      return "event_requires";
    case "ai_frame_reference_required": return "rule_capture_first";
    case "ai_frame_reference_changed": return "rule_calibration_changed";
    case "ai_record_rule_required": return "rule_record_required";
    default:
      return "error_save";
  }
}

export function diagnosisKey(row) {
  const runtime = row.runtime || {};
  const reason = String(runtime.incident?.reason || "");
  if (runtime.source_status === "link_cycle" || runtime.source_status === "link_invalid") return "reason_cycle";
  if (runtime.source_status === "source_unavailable") return "reason_source";
  if (runtime.ai) {
    switch (runtime.ai.reason) {
      case "capacity_limited": return "ai_capacity";
      case "no_usable_frames": return "ai_no_frames";
      case "analysis_timeout": return "ai_timeout";
      case "resource_limit": return "ai_capacity";
      case "engine_failed": return "ai_failed";
      default: return null;
    }
  }
  if (reason.includes("timeout")) return "reason_timeout";
  if (reason.includes("auth") || reason.includes("forbidden")) return "reason_auth";
  if (reason.includes("unsupported")) return "reason_unsupported";
  if (reason || ["source_fault", "linked_source_fault"].includes(runtime.actual_status)) return "reason_connection";
  return null;
}

export function aiPhaseKey(row) {
  const phase = row.runtime?.ai?.phase;
  if (!phase) return null;
  if (phase === "healthy") return "ai_running";
  if (phase === "recovering") return "ai_recovering";
  if (["stalled", "failed", "exhausted"].includes(phase)) return "ai_waiting_retry";
  return "status_analysis_starting";
}
