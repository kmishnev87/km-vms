const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "/api";
const TOKEN_KEY = "token";
const TOKEN_EXPIRES_KEY = "token_expires_at";
const FORBIDDEN_RU = "???????????? ????????????????????. ???????????????????? ?????????? ????????????????????????.";
const FORBIDDEN_EN = "Section unavailable. User permissions are limited.";
const LEGACY_FORBIDDEN_TEXT = ["Insufficient", "permissions"].join(" ");
const LEGACY_FORBIDDEN_RU = "???????????????????? ?????????? ????????????????????????";

export function forbiddenMessage(language = "ru") {
  return language === "en" ? FORBIDDEN_EN : FORBIDDEN_RU;
}

function currentUiLanguage() {
  if (typeof window === "undefined") return "ru";
  return localStorage.getItem("km_vms_language") === "en" ? "en" : "ru";
}

export function canAccessPath(user, href) {
  const permissions = new Set(user?.permissions || []);
  if (href === "/live") return permissions.has("view_live");
  if (href === "/recordings") return permissions.has("view_recordings");
  if (href === "/chronology") return permissions.has("view_timeline");
  if (href === "/cameras") return permissions.has("manage_cameras");
  if (href === "/settings") return permissions.has("manage_settings");
  return false;
}

export function canDeleteRecordings(user) {
  return (user?.permissions || []).includes("delete_recordings");
}

function buildUrl(path) {
  if (!path) return API_BASE;
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  if (path.startsWith("/")) return `${API_BASE}${path}`;
  return `${API_BASE}/${path}`;
}

export function getAuthToken() {
  if (typeof window === "undefined") return "";
  const expiresAt = localStorage.getItem(TOKEN_EXPIRES_KEY);
  const persisted = localStorage.getItem(TOKEN_KEY) || "";
  if (persisted && !expiresAt) {
    localStorage.removeItem(TOKEN_KEY);
    return sessionStorage.getItem(TOKEN_KEY) || "";
  }
  if (persisted && expiresAt && Date.now() > Date.parse(expiresAt)) {
    clearAuthToken();
    return "";
  }
  return persisted || sessionStorage.getItem(TOKEN_KEY) || "";
}

export function saveAuthToken(token, { persistent = false, expiresAt = null } = {}) {
  if (typeof window === "undefined") return;
  clearAuthToken();
  if (persistent) {
    localStorage.setItem(TOKEN_KEY, token);
    if (expiresAt) localStorage.setItem(TOKEN_EXPIRES_KEY, expiresAt);
    return;
  }
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearAuthToken() {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_EXPIRES_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(TOKEN_EXPIRES_KEY);
}

function makeHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getAuthToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

function normalizeErrorDetail(response, detail) {
  const text = String(detail || "");
  if (
    response.status === 403 ||
    text.includes(LEGACY_FORBIDDEN_TEXT) ||
    text.includes(LEGACY_FORBIDDEN_RU)
  ) {
    return forbiddenMessage(currentUiLanguage());
  }
  return detail || "???????????? ??????????????";
}

export async function apiFetch(path, options = {}) {
  const url = buildUrl(path);
  const headers = makeHeaders(options.headers || {});
  const response = await fetch(url, { ...options, headers });

  if (response.status === 204) return null;

  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      if (isJson) {
        const data = await response.json();
        detail = typeof data?.detail === "string"
          ? data.detail
          : JSON.stringify(data);
      } else {
        detail = await response.text();
      }
    } catch {
      detail = `HTTP ${response.status}`;
    }
    throw new Error(normalizeErrorDetail(response, detail));
  }

  if (isJson) return response.json();
  return response.text();
}

export async function apiFetchBlob(path, options = {}) {
  const url = buildUrl(path);
  const headers = makeHeaders(options.headers || {});
  const response = await fetch(url, { ...options, headers });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        const data = await response.json();
        detail = typeof data?.detail === "string"
          ? data.detail
          : JSON.stringify(data);
      } else {
        detail = await response.text();
      }
    } catch {
      detail = `HTTP ${response.status}`;
    }
    throw new Error(normalizeErrorDetail(response, detail));
  }

  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/i);
  const filename = match ? match[1] : null;

  return { blob, filename };
}

export async function issueRecordingMediaToken(path, action = "stream") {
  const data = await issueRecordingMediaTokenInfo(path, action);
  return data.mediaToken;
}

export async function issueRecordingMediaTokenInfo(path, action = "stream") {
  const data = await apiFetch("/recordings/media-token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, action }),
  });
  return {
    mediaToken: data?.media_token || "",
    expiresAt: data?.expires_at || null,
    expiresIn: Number(data?.expires_in || 0),
  };
}

export async function issueChronologyMediaToken(cameraId, relPath) {
  const data = await issueChronologyMediaTokenInfo(cameraId, relPath);
  return data.mediaToken;
}

export async function issueChronologyMediaTokenInfo(cameraId, relPath) {
  const data = await apiFetch(
    `/chronology/media-token?camera_id=${encodeURIComponent(cameraId)}&rel_path=${encodeURIComponent(relPath)}`,
    { method: "POST" }
  );
  return {
    mediaToken: data?.media_token || "",
    expiresAt: data?.expires_at || null,
    expiresIn: Number(data?.expires_in || 0),
  };
}

export async function issueLiveMediaToken(cameraId, stream) {
  const data = await issueLiveMediaTokenInfo(cameraId, stream);
  return data.mediaToken;
}

export async function issueLiveMediaTokenInfo(cameraId, stream) {
  const data = await apiFetch("/live/media-token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ camera_id: Number(cameraId), stream }),
  });
  return {
    mediaToken: data?.media_token || "",
    expiresAt: data?.expires_at || null,
    expiresIn: Number(data?.expires_in || 0),
  };
}
