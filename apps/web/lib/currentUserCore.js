export const CURRENT_USER_IDLE = "idle";
export const CURRENT_USER_LOADING = "loading";
export const CURRENT_USER_READY = "ready";
export const CURRENT_USER_NO_TOKEN = "no_token";
export const CURRENT_USER_DENIED = "denied";
export const CURRENT_USER_UNAVAILABLE = "unavailable";

export function shouldFetchCurrentUser(token) {
  return Boolean(String(token || "").trim());
}

export function normalizeCurrentUser(user) {
  if (!user || typeof user !== "object" || Array.isArray(user)) return null;
  return {
    ...user,
    permissions: Array.isArray(user.permissions) ? user.permissions : [],
  };
}

export function isCurrentUserDeniedError(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("401") ||
    message.includes("403") ||
    message.includes("not authenticated") ||
    message.includes("invalid token") ||
    message.includes("forbidden") ||
    message.includes("permission") ||
    message.includes("недостат") ||
    message.includes("доступ")
  );
}
