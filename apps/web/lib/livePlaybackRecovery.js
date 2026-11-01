export const LIVE_RECOVERY_DELAYS_MS = [2000, 5000, 10000, 20000, 30000, 60000];

export function liveRecoveryDelay(attempt) {
  return LIVE_RECOVERY_DELAYS_MS[Math.min(Math.max(0, Number(attempt) || 0), LIVE_RECOVERY_DELAYS_MS.length - 1)];
}

export function liveFailureMessage(reason, wasPlaying = false) {
  if (reason === "auth_failed") return "Проверьте логин и пароль камеры. Повторим подключение автоматически.";
  if (reason === "camera_unreachable") return "Камера недоступна. Ждём связь и повторяем подключение.";
  if (reason === "resource_limit") return "Недостаточно ресурсов для онлайн-просмотра. Повторим подключение позже.";
  if (reason === "no_rtsp_url") return "Проверьте адрес потока в настройках камеры.";
  return wasPlaying ? "Просмотр прервался. Восстанавливаем подключение…" : "Подключаемся к камере. Повторяем попытку…";
}

export function notifyLiveRuntimeChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("kmvms:live-runtime-changed"));
}
