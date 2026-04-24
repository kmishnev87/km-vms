const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "/api";

function buildUrl(path) {
  if (!path) return API_BASE;
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  if (path.startsWith("/")) return `${API_BASE}${path}`;
  return `${API_BASE}/${path}`;
}

function getToken() {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("token") || "";
}

function makeHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
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
        detail = data?.detail || JSON.stringify(data);
      } else {
        detail = await response.text();
      }
    } catch {
      detail = `HTTP ${response.status}`;
    }
    throw new Error(detail || "Ошибка запроса");
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
        detail = data?.detail || JSON.stringify(data);
      } else {
        detail = await response.text();
      }
    } catch {
      detail = `HTTP ${response.status}`;
    }
    throw new Error(detail || "Ошибка запроса");
  }

  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/i);
  const filename = match ? match[1] : null;

  return { blob, filename };
}
