export const CAMERA_EVENT_KINDS = ["motion", "person", "vehicle", "animal", "tamper", "audio", "other"];
export const CAMERA_EVENT_REFRESH = "km-vms-camera-events-updated";
export const CAMERA_EVENT_OPEN = "km-vms-open-camera-event";
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

export function cameraEventId(search) {
  const value = new URLSearchParams(search || "").get("camera_event");
  return UUID.test(value || "") ? value : null;
}

export function eventErrorKey(error) {
  const code = error?.code || error?.detail?.code;
  return ["event_camera_not_available", "event_source_ambiguous", "event_archive_unavailable", "event_not_found",
    "event_access_denied", "detector_state_changed", "detector_change_unconfirmed", "detector_switch_not_exposed",
    "detector_read_failed", "analytics_unsupported", "analytics_profile_unavailable"].includes(code) ? code : "request_failed";
}

export function eventHistoryQuery(filters = {}, offset = 0) {
  const params = new URLSearchParams({ limit: "25", offset: String(Math.max(0, Number(offset) || 0)) });
  if (filters.camera) params.set("camera_id", filters.camera);
  if (CAMERA_EVENT_KINDS.includes(filters.kind)) params.set("kind", filters.kind);
  if (filters.unread) params.set("unread", "true");
  const days = [1, 7, 30].includes(Number(filters.days)) ? Number(filters.days) : 30;
  params.set("since", new Date(Date.now() - days * 86400000).toISOString());
  return params.toString();
}

// A rule describes what happened, not which class of object caused it.
// Keep the received person/vehicle classification when the camera supplied it;
// standard rule topics also give older unclassified events a useful label.
export function eventLabelKey(event) {
  const kind = CAMERA_EVENT_KINDS.includes(event?.kind) ? event.kind : "other";
  if (kind !== "other") return `cameraEvents.kinds.${kind}`;
  const topic = String(event?.topic || "").replace(/(^|\/)[A-Za-z_][\w.-]*:/g, "$1");
  for (const [suffix, label] of [
    ["RuleEngine/FieldDetector/ObjectsInside", "objectInZone"],
    ["RuleEngine/LineDetector/Crossed", "lineCrossing"],
    ["RuleEngine/LoiteringDetector/ObjectIsLoitering", "loitering"],
  ]) {
    if (topic === suffix) return `cameraEvents.rules.${label}`;
  }
  return "cameraEvents.kinds.other";
}

export function minuteValue(value) {
  const match = /^(\d{2}):(\d{2})$/.exec(String(value || ""));
  return match ? Math.min(1439, Number(match[1]) * 60 + Number(match[2])) : 0;
}

export function minuteLabel(value) {
  const minute = Math.min(1439, Math.max(0, Number(value) || 0));
  return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}
