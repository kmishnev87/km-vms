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

export function applyCameraFormPatch(prev, key, value, sensitiveFields = new Set()) {
  const next = {
    ...prev,
    [key]: value,
    ...(sensitiveFields.has(key)
      ? { preview_token: null, manual_confirm_unverified: false }
      : {}),
  };

  const protocol = key === "protocol" ? value : prev.protocol;
  const managementFields = new Set(["protocol", "host", "port", "username", "password", "onvif_path"]);
  const sharedStreamFields = new Set(["protocol", "rtsp_host", "rtsp_port", "rtsp_transport", "username", "password", "onvif_channel_id"]);
  if (protocol === "rtsp") {
    sharedStreamFields.add("host");
    sharedStreamFields.add("port");
  } else {
    managementFields.add("rtsp_host");
    managementFields.add("rtsp_port");
  }
  if (managementFields.has(key)) {
    next.onvif_probe_token = null;
  }
  if (sharedStreamFields.has(key)) {
    next.main_validation_token = null;
    next.sub_validation_token = null;
    next.validation_token = null;
  }
  if (key === "rtsp_main_url" || key === "onvif_profile_token") {
    next.main_validation_token = null;
    next.validation_token = null;
  }
  if (key === "rtsp_sub_url" || key === "onvif_sub_profile_token") {
    next.sub_validation_token = null;
  }
  if (key === "protocol" && value === "onvif") {
    next.rtsp_host = prev.rtsp_host || prev.host || "";
    next.rtsp_port = normalizeCameraPort(prev.rtsp_port) || 554;
  }

  return next;
}
