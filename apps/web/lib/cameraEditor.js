import { applyCameraFormPatch } from "./cameraStage16";

// Drafts and proof fingerprints stay in memory; never persist camera credentials.
export const CAMERA_CONNECTION_FIELDS = new Set([
  "protocol", "host", "port", "username", "password", "rtsp_host", "rtsp_port",
  "rtsp_transport", "rtsp_main_url", "rtsp_sub_url", "onvif_path",
  "onvif_profile_token", "onvif_sub_profile_token", "onvif_channel_id",
]);
export const CAMERA_MANAGEMENT_FIELDS = new Set([
  "protocol", "host", "port", "username", "password", "rtsp_host", "rtsp_port", "onvif_path",
]);
export const CAMERA_CHECK_MAX_AGE_MS = 14 * 60 * 1000;
const NUMBER_FIELDS = new Set(["port", "rtsp_port", "segment_minutes", "retention_days", "storage_quota_gb"]);
const PROOF_FIELDS = ["preview_token", "validation_token", "main_validation_token", "sub_validation_token", "onvif_probe_token", "manual_confirm_unverified"];

export function cameraEditorDraft(camera = null) {
  const draft = {
    name: "", enabled: true, protocol: "onvif", host: "", port: 80,
    username: "", password: "", rtsp_main_url: "", rtsp_sub_url: "",
    rtsp_host: "", rtsp_port: 554, rtsp_transport: "tcp", onvif_path: "",
    onvif_profile_token: "", onvif_sub_profile_token: "", onvif_channel_id: "",
    recording_mode: "always", default_live_stream: "main", default_record_stream: "main",
    segment_minutes: 5, retention_days: 30, storage_quota_gb: 50,
    preview_token: null, validation_token: null, main_validation_token: null,
    sub_validation_token: null, onvif_probe_token: null, manual_confirm_unverified: false,
  };
  if (camera) {
    for (const key of Object.keys(draft)) {
      if (!PROOF_FIELDS.includes(key) && key !== "password" && camera[key] != null) draft[key] = camera[key];
    }
    if (draft.protocol === "onvif") {
      draft.rtsp_host = camera.rtsp_reachable_host || camera.rtsp_host || camera.host;
      draft.rtsp_port = camera.rtsp_reachable_port || camera.rtsp_port || 554;
    }
  }
  return normalizeEditorStreams(draft);
}

export function normalizeEditorStreams(draft) {
  const roles = cameraStreamRoles(draft);
  const fallback = roles[0] || "main";
  return {
    ...draft,
    default_record_stream: roles.includes(draft.default_record_stream) ? draft.default_record_stream : fallback,
    default_live_stream: roles.includes(draft.default_live_stream) ? draft.default_live_stream : fallback,
  };
}

export function patchCameraEditor(draft, key, value) {
  if (draft[key] === value) return draft;
  let next = applyCameraFormPatch(draft, key, value, CAMERA_CONNECTION_FIELDS);
  if (key === "protocol") {
    next = {
      ...next, port: value === "onvif" ? 80 : (draft.rtsp_port || 554),
      rtsp_host: value === "onvif" ? draft.host : "",
      rtsp_port: value === "onvif" ? (Number(draft.port) || 554) : 554,
      onvif_path: "", onvif_profile_token: "", onvif_sub_profile_token: "", onvif_channel_id: "",
    };
  }
  // Follow the address until the user has deliberately set a separate video endpoint.
  if (key === "host" && draft.protocol === "onvif" && (!draft.rtsp_host || draft.rtsp_host === draft.host)) {
    next = applyCameraFormPatch(next, "rtsp_host", value, CAMERA_CONNECTION_FIELDS);
  }
  return normalizeEditorStreams(next);
}

export function cameraStreamRoles(draft) {
  return ["main", "sub"].filter((role) => String(draft[`rtsp_${role}_url`] || "").trim());
}

export function cameraEditorPayload(draft, cameraId = null) {
  const result = { ...normalizeEditorStreams(draft), camera_id: cameraId };
  for (const key of NUMBER_FIELDS) if (key in result) result[key] = Number(result[key]);
  if (draft.protocol === "onvif") result.rtsp_host = draft.rtsp_host || draft.host;
  else { delete result.rtsp_host; delete result.rtsp_port; }
  return result;
}

function comparable(key, value) {
  if (NUMBER_FIELDS.has(key)) return Number(value);
  return value == null ? "" : value;
}

export function cameraEditorChanges(draft, baseline) {
  return Object.fromEntries(Object.keys(baseline).filter((key) => (
    !PROOF_FIELDS.includes(key) && !(key === "password" && !draft.password)
    && comparable(key, draft[key]) !== comparable(key, baseline[key])
  )).map((key) => [key, NUMBER_FIELDS.has(key) ? Number(draft[key]) : draft[key]]));
}

export function cameraConnectionChanged(draft, baseline, cameraId) {
  return !cameraId || Object.keys(cameraEditorChanges(draft, baseline)).some((key) => CAMERA_CONNECTION_FIELDS.has(key));
}

export function cameraSavePayload(draft, baseline, cameraId) {
  const payload = cameraId ? cameraEditorChanges(draft, baseline) : cameraEditorPayload(draft);
  delete payload.camera_id;
  if (!payload.password) delete payload.password;
  for (const key of PROOF_FIELDS) if (draft[key]) payload[key] = draft[key];
  return payload;
}

export function cameraManagementKey(draft) {
  const payload = cameraEditorPayload(draft);
  return JSON.stringify([...CAMERA_MANAGEMENT_FIELDS].map((key) => comparable(key, payload[key])));
}

export function cameraStreamKey(draft, role) {
  const payload = cameraEditorPayload(draft);
  return JSON.stringify([
    role, draft.protocol, draft.protocol === "onvif" ? payload.rtsp_host : draft.host,
    Number(draft.protocol === "onvif" ? payload.rtsp_port : draft.port), draft.username, draft.password,
    draft.rtsp_transport, draft[`rtsp_${role}_url`], draft.onvif_channel_id,
    draft[role === "main" ? "onvif_profile_token" : "onvif_sub_profile_token"],
  ]);
}

export function cameraCheckIsCurrent(check, key, now = Date.now()) {
  return Boolean(check?.token && check.key === key && now - check.at < CAMERA_CHECK_MAX_AGE_MS);
}

export function cameraChecksReady(draft, checks, now = Date.now()) {
  const roles = cameraStreamRoles(draft);
  return Boolean(roles.length && roles.every((role) => cameraCheckIsCurrent(checks[role], cameraStreamKey(draft, role), now))
    && (draft.protocol !== "onvif" || cameraCheckIsCurrent(checks.management, cameraManagementKey(draft), now)));
}

export function cameraProfilePath(profile) {
  if (profile?.stream_path) return profile.stream_path;
  try { const url = new URL(profile?.stream_uri); return `${url.pathname}${url.search}`; } catch { return ""; }
}

export function assignCameraProfile(draft, role, profile) {
  let result = patchCameraEditor(draft, role === "main" ? "onvif_profile_token" : "onvif_sub_profile_token", profile?.token || "");
  result = patchCameraEditor(result, `rtsp_${role}_url`, cameraProfilePath(profile));
  return result;
}

// Two user-facing purposes map onto the existing Main/Sub storage contract.
// Never overwrite the stream still assigned to the other purpose.
export function assignCameraPurposeProfile(draft, data, purpose, token) {
  const profile = (data?.profiles || []).find((item) => item.token === token);
  if (!profile) return draft;
  const field = purpose === "record" ? "default_record_stream" : "default_live_stream";
  const other = purpose === "record" ? "default_live_stream" : "default_record_stream";
  const existing = cameraStreamRoles(draft).find((role) => cameraAssignedProfile(draft, data, role)?.token === token);
  const role = existing || (draft[field] !== draft[other] ? draft[field] : draft[other] === "main" ? "sub" : "main");
  let next = { ...assignCameraProfile(draft, role, profile), [field]: role };
  // An unused role must not become a hidden prerequisite for saving one selected stream.
  for (const unused of ["main", "sub"].filter((item) => item !== next[field] && item !== next[other])) {
    next = assignCameraProfile(next, unused, null);
  }
  return normalizeEditorStreams(next);
}

export function cameraPreviewIdentity(draft, data) {
  return JSON.stringify([cameraManagementKey(draft), cameraSelectedSource(draft, data)?.id || draft.onvif_channel_id || "single"]);
}

export function cameraProfileHasMetadata(profile) {
  const current = profile?.current || profile?.settings;
  const video = profile?.rtsp_probe?.video || profile?.video;
  const fps = current?.fps || video?.fps;
  return Boolean((current?.codec || video?.codec)
    && (current?.resolution || (video?.width && video?.height))
    && fps && fps !== "0/0");
}

export function cameraDraftWithProfiles(draft, data) {
  if (!cameraStreamRoles(draft).length && data?.source_groups?.length) {
    return cameraDraftForSource(draft, data, data.source_groups[0].id);
  }
  const profiles = (data?.profiles || []).filter((profile) => cameraProfilePath(profile));
  const main = profiles.find((profile) => profile.token === data?.suggested_main_profile_token) || profiles[0];
  const sub = profiles.find((profile) => profile.token === data?.suggested_sub_profile_token && profile.token !== main?.token);
  let next = draft;
  // A camera can recreate ONVIF tokens without changing the actual stream path.
  // Reconcile by exact path only; never invent a saved profile or guess a new stream.
  for (const role of cameraStreamRoles(draft)) {
    const token = draft[role === "main" ? "onvif_profile_token" : "onvif_sub_profile_token"];
    if (profiles.some((profile) => profile.token === token)) continue;
    const matches = profiles.filter((profile) => cameraProfilePath(profile) === draft[`rtsp_${role}_url`]);
    if (matches.length === 1) next = assignCameraProfile(next, role, matches[0]);
  }
  // Recommendations fill empty roles only. Reading must never overwrite a saved/manual choice.
  if (!draft.rtsp_main_url && main) next = assignCameraProfile(next, "main", main);
  if (!cameraStreamRoles(draft).length && sub) {
    next = assignCameraProfile(next, "sub", sub);
    next = { ...next, default_live_stream: "sub" };
  }
  return next;
}

export function cameraProfileGroups(data) {
  return data?.source_groups || [];
}

export function cameraAssignedProfile(draft, data, role) {
  const token = draft[role === "main" ? "onvif_profile_token" : "onvif_sub_profile_token"];
  return (data?.profiles || []).find((profile) => token ? profile.token === token : cameraProfilePath(profile) === draft[`rtsp_${role}_url`]) || null;
}

export function cameraSelectedSource(draft, data) {
  const profile = cameraAssignedProfile(draft, data, "main") || cameraAssignedProfile(draft, data, "sub")
    || (data?.profiles || []).find((item) => cameraProfilePath(item) === draft.rtsp_main_url);
  return cameraProfileGroups(data).find((group) => group.profile_tokens.includes(profile?.token)) || null;
}

export function cameraDraftForSource(draft, data, sourceId) {
  const group = cameraProfileGroups(data).find((item) => item.id === sourceId);
  if (!group) return draft;
  const profiles = data.profiles || [];
  let next = assignCameraProfile(draft, "main", profiles.find((item) => item.token === group.main_profile_token));
  next = assignCameraProfile(next, "sub", profiles.find((item) => item.token === group.sub_profile_token));
  next = patchCameraEditor(next, "onvif_channel_id", group.channel == null ? "" : String(group.channel));
  return { ...next, default_record_stream: "main", default_live_stream: group.sub_profile_token ? "sub" : "main" };
}

export function cameraSourceConflict(draft, data) {
  const main = cameraAssignedProfile(draft, data, "main");
  const sub = cameraAssignedProfile(draft, data, "sub");
  return Boolean(main?.source_id && sub?.source_id && main.source_id !== sub.source_id);
}

export function cameraEditorProblem(draft, copy, section = "all") {
  if (["all", "connection"].includes(section)) {
    if (!draft.name.trim() || !draft.host.trim() || !Number.isInteger(Number(draft.port)) || Number(draft.port) < 1 || Number(draft.port) > 65535) return copy.editorRequired;
    if (draft.protocol === "onvif" && (!Number.isInteger(Number(draft.rtsp_port)) || Number(draft.rtsp_port) < 1 || Number(draft.rtsp_port) > 65535)) return copy.editorRequired;
    if (draft.protocol === "rtsp" && !cameraStreamRoles(draft).length) return copy.editorPathRequired;
  }
  if (["all", "recording"].includes(section)) {
    if (["segment_minutes", "retention_days", "storage_quota_gb"].some((key) => !Number.isInteger(Number(draft[key])) || Number(draft[key]) < 1)) return copy.editorRecordingInvalid;
  }
  return "";
}

export function cameraEditorError(error, copy) {
  if (error?.status === 401 || error?.status === 403) return error.message;
  const code = error?.code || error?.detail?.code;
  if (code === "wrong_credentials") return copy.authError;
  if (["timeout", "wrong_ip_or_unreachable", "wrong_port_or_service_unavailable"].includes(code)) return copy.editorUnreachable;
  if (code === "camera_validation_required") return copy.editorRecheck;
  if (error?.status === 422) return copy.editorRequired;
  if (String(error?.message).includes("таким именем")) return copy.editorDuplicate;
  if (error?.category === "network_unavailable") return error.message;
  return copy.cameraConnectFailed;
}
