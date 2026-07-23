import { canUserAccessRoute } from "./routePermissions";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "/api";
const TOKEN_KEY = "token";
const TOKEN_EXPIRES_KEY = "token_expires_at";
const FORBIDDEN_RU = "Раздел недоступен. Права пользователя ограничены.";
const FORBIDDEN_EN = "Section unavailable. User permissions are limited.";
const FORBIDDEN_ZH_CN = "此部分不可用。用户权限受限。";
const LEGACY_FORBIDDEN_TEXT = ["Insufficient", "permissions"].join(" ");
const LEGACY_FORBIDDEN_RU = "Недостаточно прав пользователя";
const MAX_API_ERROR_MESSAGE_LENGTH = 240;
const UNSAFE_ERROR_TEXT = /<!doctype|<html|<body|traceback|stack trace|authorization:|bearer\s|rtsp:\/\/|\.env(?:\b|_)|\/(?:volume\d*|var|etc|home|tmp)\//i;

const API_ERROR_MESSAGES = {
  ru: {
    unauthorized: "Требуется повторный вход в систему.",
    network_unavailable: "Нет связи с сервером. Соединение будет проверено повторно.",
    temporarily_unavailable: "Сервис временно недоступен. Повторите попытку после восстановления соединения.",
    request_failed: "Не удалось выполнить запрос.",
  },
  en: {
    unauthorized: "Sign in again to continue.",
    network_unavailable: "The server connection is unavailable. The connection will be checked again.",
    temporarily_unavailable: "The service is temporarily unavailable. Try again after the connection is restored.",
    request_failed: "The request could not be completed.",
  },
  "zh-CN": {
    unauthorized: "请重新登录以继续。",
    network_unavailable: "暂时无法连接服务器，系统将再次检查连接。",
    temporarily_unavailable: "服务暂时不可用，请在连接恢复后重试。",
    request_failed: "无法完成请求。",
  },
};

export function forbiddenMessage(language = "ru") {
  if (language === "en") return FORBIDDEN_EN;
  if (language === "zh-CN") return FORBIDDEN_ZH_CN;
  return FORBIDDEN_RU;
}

function currentUiLanguage() {
  if (typeof window === "undefined") return "ru";
  const language = localStorage.getItem("km_vms_language");
  return ["ru", "en", "zh-CN"].includes(language) ? language : "ru";
}

export function canAccessPath(user, href) {
  return canUserAccessRoute(user, href);
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

function dispatchAuthChanged() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent("km-vms-auth-changed"));
}

export function saveAuthToken(token, { persistent = false, expiresAt = null } = {}) {
  if (typeof window === "undefined") return;
  clearAuthToken();
  if (persistent) {
    localStorage.setItem(TOKEN_KEY, token);
    if (expiresAt) localStorage.setItem(TOKEN_EXPIRES_KEY, expiresAt);
    dispatchAuthChanged();
    return;
  }
  sessionStorage.setItem(TOKEN_KEY, token);
  dispatchAuthChanged();
}

export function clearAuthToken() {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_EXPIRES_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(TOKEN_EXPIRES_KEY);
  dispatchAuthChanged();
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
  return detail || "Ошибка запроса";
}

function boundedErrorText(value) {
  const text = String(value || "").replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
  if (!text || UNSAFE_ERROR_TEXT.test(text)) return "";
  return text.slice(0, MAX_API_ERROR_MESSAGE_LENGTH);
}

function responseErrorCategory(response, isJson) {
  if (response.status === 401) return "unauthorized";
  if (response.status === 403) return "permission_denied";
  if ([502, 503, 504].includes(response.status)) return "temporarily_unavailable";
  return isJson ? "typed_backend_error" : "request_failed";
}

function localizedApiError(category, response = null) {
  if (category === "permission_denied") return forbiddenMessage(currentUiLanguage());
  const messages = API_ERROR_MESSAGES[currentUiLanguage()] || API_ERROR_MESSAGES.ru;
  const base = messages[category] || messages.request_failed;
  if (response?.status && category !== "unauthorized") return `${base} (HTTP ${response.status})`;
  return base;
}

function typedErrorCode(data) {
  const detail = data?.detail;
  const value = data?.code || detail?.code || detail?.reason || detail?.error;
  return boundedErrorText(value).slice(0, 120);
}

function objectErrorSummary(detail) {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return "";
  const direct = [detail.message, detail.error, detail.reason, detail.code, detail.status]
    .map(boundedErrorText)
    .find(Boolean);
  const blocker = Array.isArray(detail.blockers)
    ? detail.blockers.map((item) => boundedErrorText(item?.reason || item?.code || item?.message)).find(Boolean)
    : "";
  if (direct && blocker && direct !== blocker) return boundedErrorText(`${direct}: ${blocker}`);
  return direct || blocker || "";
}

function safeErrorMessage(response, data, category) {
  const detail = data?.detail;
  const candidate = typeof detail === "string"
    ? boundedErrorText(detail)
    : boundedErrorText(data?.message) || objectErrorSummary(detail) || boundedErrorText(data?.code);
  if (candidate) return boundedErrorText(normalizeErrorDetail(response, candidate));
  return localizedApiError(category, response);
}

function createApiError(response, data, isJson) {
  const category = responseErrorCategory(response, isJson);
  const error = new Error(safeErrorMessage(response, data, category));
  error.category = category;
  error.code = typedErrorCode(data);
  error.status = response.status;
  error.data = data;
  error.detail = data?.detail;
  error.response = {
    ok: response.ok,
    status: response.status,
    statusText: response.statusText,
  };
  return error;
}

function createNetworkError() {
  const error = new Error(localizedApiError("network_unavailable"));
  error.category = "network_unavailable";
  error.code = "";
  error.status = 0;
  error.data = null;
  error.detail = null;
  error.response = { ok: false, status: 0, statusText: "" };
  return error;
}

async function requestWithSafeNetworkError(url, options) {
  try {
    return await fetch(url, options);
  } catch {
    throw createNetworkError();
  }
}

export async function apiFetch(path, options = {}) {
  const url = buildUrl(path);
  const headers = makeHeaders(options.headers || {});
  const response = await requestWithSafeNetworkError(url, { ...options, headers });

  if (response.status === 204) return null;

  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");

  if (!response.ok) {
    let data = null;
    try {
      if (isJson) {
        data = await response.json();
      }
    } catch {}
    throw createApiError(response, data, isJson);
  }

  if (isJson) return response.json();
  return response.text();
}

export async function apiFetchBlob(path, options = {}) {
  const url = buildUrl(path);
  const headers = makeHeaders(options.headers || {});
  const response = await requestWithSafeNetworkError(url, { ...options, headers });

  if (!response.ok) {
    let data = null;
    const contentType = response.headers.get("content-type") || "";
    const isJson = contentType.includes("application/json");
    try {
      if (isJson) {
        data = await response.json();
      }
    } catch {}
    throw createApiError(response, data, isJson);
  }

  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/i);
  const filename = match ? match[1] : null;

  return { blob, filename };
}

export async function issueRecordingMediaToken(pathOrItem, action = "stream") {
  const data = await issueRecordingMediaTokenInfo(pathOrItem, action);
  return data.mediaToken;
}

export async function issueRecordingMediaTokenInfo(pathOrItem, action = "stream") {
  const payload = typeof pathOrItem === "object" && pathOrItem !== null
    ? {
        segment_id: pathOrItem.segment_id,
        archive_root_id: pathOrItem.archive_root_id,
        recording_ref: pathOrItem.recording_ref,
        path: pathOrItem.path,
        action,
      }
    : { path: pathOrItem, action };
  const data = await apiFetch("/recordings/media-token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return {
    mediaToken: data?.media_token || "",
    expiresAt: data?.expires_at || null,
    expiresIn: Number(data?.expires_in || 0),
  };
}

export async function issueChronologyMediaToken(cameraId, relPath, playback = null) {
  const data = await issueChronologyMediaTokenInfo(cameraId, relPath, playback);
  return data.mediaToken;
}

export async function issueChronologyMediaTokenInfo(cameraId, relPath, playback = null) {
  const params = new URLSearchParams();
  const segmentId = playback?.segment_id || playback?.segmentId;
  const archiveRootId = playback?.archive_root_id || playback?.archiveRootId;
  const playbackRef = playback?.playback_ref || playback?.playbackRef;
  if (segmentId) params.set("segment_id", String(segmentId));
  if (archiveRootId) params.set("archive_root_id", archiveRootId);
  if (playbackRef) params.set("playback_ref", playbackRef);
  if (!params.has("segment_id") && !params.has("playback_ref")) {
    params.set("camera_id", String(cameraId));
    params.set("rel_path", relPath);
  }
  const data = await apiFetch(`/chronology/media-token?${params.toString()}`, { method: "POST" });
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
