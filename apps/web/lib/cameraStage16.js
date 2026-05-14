export const CAMERAS_VIEW_MODE_STORAGE_PREFIX = "km_vms_cameras_view_mode";
export const VALID_CAMERA_VIEW_MODES = new Set(["list", "cards"]);

export function normalizeCamerasViewMode(value, fallback = "list") {
  return VALID_CAMERA_VIEW_MODES.has(value) ? value : fallback;
}

export function cameraViewModeStorageKey(currentUser) {
  const rawUser = currentUser?.id ?? currentUser?.username ?? currentUser?.login ?? "anonymous";
  const safeUser = String(rawUser || "anonymous").replace(/[^a-zA-Z0-9_.@-]/g, "_");
  return `${CAMERAS_VIEW_MODE_STORAGE_PREFIX}:${safeUser}`;
}

export function loadCamerasViewMode(storage, currentUser, fallback = "list") {
  if (!storage?.getItem) return fallback;
  try {
    return normalizeCamerasViewMode(storage.getItem(cameraViewModeStorageKey(currentUser)), fallback);
  } catch {
    return fallback;
  }
}

export function saveCamerasViewMode(storage, currentUser, value) {
  const normalized = normalizeCamerasViewMode(value, null);
  if (!normalized || !storage?.setItem) return false;
  try {
    storage.setItem(cameraViewModeStorageKey(currentUser), normalized);
    return true;
  } catch {
    return false;
  }
}

export function normalizeCameraPort(value) {
  if (value === undefined || value === null || value === "") return null;
  const text = String(value).trim();
  if (!text) return null;
  const parsed = Number(text);
  if (!Number.isInteger(parsed) || parsed <= 0) return null;
  return parsed;
}

export function isRtspPortManualOverrideForEdit(onvifPort, rtspPort) {
  const normalizedRtspPort = normalizeCameraPort(rtspPort);
  if (!normalizedRtspPort) return false;
  const normalizedOnvifPort = normalizeCameraPort(onvifPort);
  return Boolean(normalizedOnvifPort && normalizedRtspPort !== normalizedOnvifPort);
}

export function applyCameraFormPatch(prev, key, value, sensitiveFields = new Set()) {
  const next = {
    ...prev,
    [key]: value,
    ...(sensitiveFields.has(key)
      ? { preview_token: null, validation_token: null, onvif_probe_token: null, manual_confirm_unverified: false }
      : {}),
  };

  if (key === "rtsp_port") {
    next.rtsp_port_manually_set = true;
  }

  if (key === "protocol" && value === "onvif") {
    next.rtsp_host = prev.host || "";
    if (!prev.rtsp_port_manually_set) {
      next.rtsp_port = prev.rtsp_port || prev.port || 554;
    }
  }

  if (prev.protocol === "onvif" && key === "host" && (!prev.rtsp_host || prev.rtsp_host === prev.host)) {
    next.rtsp_host = value;
  }

  if ((prev.protocol === "onvif" || next.protocol === "onvif") && key === "port" && !prev.rtsp_port_manually_set) {
    next.rtsp_port = value || prev.rtsp_port || 554;
  }

  return next;
}

export function smartRtspPort(prev, fallback) {
  if (prev?.rtsp_port_manually_set) return prev.rtsp_port || 554;
  return fallback || prev?.rtsp_port || prev?.port || 554;
}
