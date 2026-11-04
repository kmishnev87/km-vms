export const CURRENT_USER_IDLE = "idle";
export const CURRENT_USER_LOADING = "loading";
export const CURRENT_USER_READY = "ready";
export const CURRENT_USER_NO_TOKEN = "no_token";
export const CURRENT_USER_DENIED = "denied";
export const CURRENT_USER_UNAVAILABLE = "unavailable";

export function shouldFetchCurrentUser(token) {
  return Boolean(String(token || "").trim());
}

export function shouldRefreshCurrentUserState(token, status) {
  return (
    shouldFetchCurrentUser(token) &&
    [CURRENT_USER_NO_TOKEN, CURRENT_USER_DENIED, CURRENT_USER_UNAVAILABLE].includes(status)
  );
}

export function normalizeCurrentUser(user) {
  if (!user || typeof user !== "object" || Array.isArray(user)) return null;
  return {
    ...user,
    permissions: Array.isArray(user.permissions) ? user.permissions : [],
  };
}

export function isCurrentUserDeniedError(error) {
  const status = Number(error?.status);
  if (Number.isInteger(status) && status > 0) return status === 401;
  if (error?.category === "unauthorized") return true;
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("401") ||
    message.includes("not authenticated") ||
    message.includes("invalid token") ||
    message.includes("недействительный токен") ||
    message.includes("пользователь не найден")
  );
}
